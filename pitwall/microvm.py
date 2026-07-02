"""The Pitwall sandbox: one AWS Lambda MicroVM per session.

The entire coding agent (Claude Code CLI) runs *inside* the MicroVM; this
module is the thin host-side client. The VM's execution role gives it Bedrock
InvokeModel, so no keys ever cross the boundary.

Lifecycle (per the AWS docs, verified field/command names):
  start    run-microvm        -> {microvmId, endpoint, state}
           get-microvm        -> poll until state == RUNNING
           create-microvm-auth-token -> {authToken: {"X-aws-proxy-auth": ...}}
  ask      POST https://<endpoint>/ask with X-aws-proxy-auth header; stream
           NDJSON events back from the in-VM server (microvm_image/server.py).
  close    terminate-microvm

Lambda's idle policy handles suspend/resume automatically: no traffic for
``maxIdleDurationSeconds`` suspends the VM (state preserved, compute billing
stops); the next /ask request auto-resumes it.

DRIVERS — the same API is reachable two ways, and Pitwall supports both:
  * boto3   (``boto3.client("lambda-microvms")``)
  * aws CLI (``aws lambda-microvms <cmd> --output json``)
The newer of the two is often available before the other on a given machine, so
``PITWALL_MICROVM_DRIVER=auto`` (default) picks whichever is present:
boto3 if its SDK carries the service model, else the CLI if it has the
subcommand, else a clear error. Force one with ``boto3`` / ``cli``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from pitwall.config import MicroVMConfig
from pitwall.sandbox import AskResult, EventCallback, Sandbox

_SERVICE = "lambda-microvms"
_AUTH_HEADER = "X-aws-proxy-auth"
_PORT_HEADER = "X-aws-proxy-port"
_RUNNING = "RUNNING"
_TERMINAL_BAD = {"FAILED", "TERMINATED", "TERMINATING"}


class MicroVMUnavailableError(RuntimeError):
    """No usable driver (neither boto3 service model nor CLI subcommand)."""


def _error_result(message: str) -> AskResult:
    """Build a failed ``AskResult`` carrying a single ``error`` event."""
    return AskResult(
        answer="",
        events=[{"type": "error", "message": message}],
        is_error=True,
    )


def _save_artifact(name: str, b64: str) -> str | None:
    """Decode a base64 artifact from the VM into ``pitwall_artifacts/``.

    Returns the local path or ``None`` on decode/write failure. The ``vm_``
    prefix keeps VM-sourced files distinct from any host-side artifacts.
    """
    import base64
    from pathlib import Path

    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
    except (ValueError, TypeError):
        return None
    out_dir = Path("pitwall_artifacts")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = os.path.basename(name) or "artifact"
    dest = out_dir / f"vm_{safe}"
    try:
        dest.write_bytes(data)
    except OSError:
        return None
    return str(dest)


# --------------------------------------------------------------------------
# Drivers — each maps the four operations the sandbox needs onto a transport.
# --------------------------------------------------------------------------
class _Boto3Driver:
    kind = "boto3"

    def __init__(self, config: MicroVMConfig) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # silence boto3's Py3.9-EOL warning
            import boto3
        self._client = boto3.client(_SERVICE, region_name=config.region)

    @staticmethod
    def usable(config: MicroVMConfig) -> bool:
        try:
            import boto3
        except ImportError:
            return False
        return _SERVICE in boto3.session.Session().get_available_services()

    def list_images(self) -> list[dict]:
        return self._client.list_microvm_images().get("items", [])

    def run_microvm(self, kwargs: dict) -> dict:
        return self._client.run_microvm(**kwargs)

    def get_state(self, microvm_id: str) -> str:
        return self._client.get_microvm(microvmIdentifier=microvm_id).get("state", "")

    def create_token(self, microvm_id: str, minutes: int, allowed_ports: list) -> dict:
        return self._client.create_microvm_auth_token(
            microvmIdentifier=microvm_id,
            expirationInMinutes=minutes,
            allowedPorts=allowed_ports,
        )

    def terminate(self, microvm_id: str) -> None:
        self._client.terminate_microvm(microvmIdentifier=microvm_id)


class _CliDriver:
    """Drives microVMs through the ``aws lambda-microvms`` CLI subcommands.

    The CLI emits the same camelCase member names as the API model
    (``microvmId``, ``endpoint``, ``authToken``), so output parsing matches the
    boto3 shapes. List params are passed space-separated; ``--idle-policy`` and
    ``--allowed-ports`` are JSON strings, exactly as the docs show.
    """

    kind = "cli"

    def __init__(self, config: MicroVMConfig) -> None:
        self._aws = config.cli_path
        self._region = config.region

    @staticmethod
    def usable(config: MicroVMConfig) -> bool:
        aws = shutil.which(config.cli_path)
        if not aws:
            return False
        try:
            # `help` exits 0 only if the subcommand exists. AWS_PAGER='' stops
            # it blocking on a pager.
            proc = subprocess.run(
                [aws, "lambda-microvms", "help"],
                env={**os.environ, "AWS_PAGER": ""},
                capture_output=True,
                timeout=20,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _run(self, args: list[str]) -> dict:
        cmd = [self._aws, "lambda-microvms", *args, "--region", self._region, "--output", "json"]
        proc = subprocess.run(
            cmd,
            env={**os.environ, "AWS_PAGER": ""},
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`aws lambda-microvms {args[0]}` failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()[:500]}"
            )
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    def list_images(self) -> list[dict]:
        return self._run(["list-microvm-images"]).get("items", [])

    def run_microvm(self, kwargs: dict) -> dict:
        args = ["run-microvm", "--image-identifier", kwargs["imageIdentifier"]]
        if kwargs.get("ingressNetworkConnectors"):
            args += ["--ingress-network-connectors", *kwargs["ingressNetworkConnectors"]]
        if kwargs.get("egressNetworkConnectors"):
            args += ["--egress-network-connectors", *kwargs["egressNetworkConnectors"]]
        if kwargs.get("idlePolicy"):
            args += ["--idle-policy", json.dumps(kwargs["idlePolicy"])]
        if kwargs.get("maximumDurationInSeconds"):
            args += ["--maximum-duration-in-seconds", str(kwargs["maximumDurationInSeconds"])]
        if kwargs.get("executionRoleArn"):
            args += ["--execution-role-arn", kwargs["executionRoleArn"]]
        return self._run(args)

    def get_state(self, microvm_id: str) -> str:
        return self._run(["get-microvm", "--microvm-identifier", microvm_id]).get("state", "")

    def create_token(self, microvm_id: str, minutes: int, allowed_ports: list) -> dict:
        return self._run(
            [
                "create-microvm-auth-token",
                "--microvm-identifier",
                microvm_id,
                "--expiration-in-minutes",
                str(minutes),
                "--allowed-ports",
                json.dumps(allowed_ports),
            ]
        )

    def terminate(self, microvm_id: str) -> None:
        self._run(["terminate-microvm", "--microvm-identifier", microvm_id])


def _make_driver(config: MicroVMConfig):
    pref = config.driver
    if pref == "boto3":
        if not _Boto3Driver.usable(config):
            raise MicroVMUnavailableError(_boto3_help())
        return _Boto3Driver(config)
    if pref == "cli":
        if not _CliDriver.usable(config):
            raise MicroVMUnavailableError(_cli_help(config))
        return _CliDriver(config)

    # auto: prefer boto3, fall back to the CLI.
    if _Boto3Driver.usable(config):
        return _Boto3Driver(config)
    if _CliDriver.usable(config):
        return _CliDriver(config)
    raise MicroVMUnavailableError(
        "No usable Lambda MicroVMs driver found.\n"
        + _boto3_help()
        + "\n"
        + _cli_help(config)
        + "\nSet PITWALL_MICROVM_DRIVER=boto3|cli to force one."
    )


def _boto3_help() -> str:
    return (
        "  • boto3: the installed SDK does not expose the "
        f"'{_SERVICE}' service. Upgrade boto3 to a build that includes it, or "
        "point AWS_DATA_PATH at a directory containing its service model."
    )


def _cli_help(config: MicroVMConfig) -> str:
    return (
        f"  • aws CLI: `{config.cli_path} lambda-microvms` is not available. "
        "Install/upgrade the AWS CLI to a version that includes it "
        f"(set PITWALL_AWS_CLI if your binary isn't '{config.cli_path}')."
    )


# --------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------
class MicroVMSandbox(Sandbox):
    name = "microvm"

    def __init__(self, config: MicroVMConfig | None = None) -> None:
        self.config = config or MicroVMConfig.from_env()
        self._driver = _make_driver(self.config)
        self._microvm_id: str | None = None
        self._endpoint: str | None = None
        self._token: str | None = None
        self._start()

    @property
    def driver_kind(self) -> str:
        return self._driver.kind

    # -- lifecycle ----------------------------------------------------------
    def _resolve_image_arn(self) -> str:
        """Use the explicit ARN if given, else discover it by image name."""
        cfg = self.config
        if cfg.image_arn:
            return cfg.image_arn
        images = self._driver.list_images()
        match = next((i for i in images if i.get("name") == cfg.image_name), None)
        if match and match.get("imageArn"):
            return match["imageArn"]
        names = ", ".join(sorted(i.get("name", "?") for i in images)) or "(none)"
        raise RuntimeError(
            f"No MicroVM image named '{cfg.image_name}' found in {cfg.region}. "
            f"Existing images: {names}.\n"
            "Build it first:  ./microvm_image/build_image.sh\n"
            "(or set PITWALL_MICROVM_IMAGE_ARN / PITWALL_MICROVM_IMAGE_NAME)."
        )

    def _start(self) -> None:
        cfg = self.config
        kwargs = {
            "imageIdentifier": self._resolve_image_arn(),
            "ingressNetworkConnectors": cfg.ingress_connectors,
            "egressNetworkConnectors": cfg.egress_connectors,
            "idlePolicy": cfg.idle_policy,
            "maximumDurationInSeconds": cfg.max_duration_seconds,
        }
        if cfg.execution_role_arn:
            kwargs["executionRoleArn"] = cfg.execution_role_arn

        resp = self._driver.run_microvm(kwargs)
        self._microvm_id = resp["microvmId"]
        self._endpoint = resp["endpoint"]
        self._wait_until_running()
        self._token = self._create_token()

    def _wait_until_running(self) -> None:
        deadline = time.monotonic() + self.config.ready_timeout_seconds
        delay = 1.0
        while time.monotonic() < deadline:
            state = self._driver.get_state(self._microvm_id)
            if state == _RUNNING:
                return
            if state in _TERMINAL_BAD:
                raise RuntimeError(
                    f"MicroVM {self._microvm_id} entered state {state} before RUNNING."
                )
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
        raise TimeoutError(
            f"MicroVM {self._microvm_id} not RUNNING within "
            f"{self.config.ready_timeout_seconds}s."
        )

    def _create_token(self) -> str:
        resp = self._driver.create_token(
            self._microvm_id, self.config.auth_token_minutes, [{"allPorts": {}}]
        )
        token = resp["authToken"]
        # Doc shows authToken as a dict carrying the header value; tolerate a
        # plain-string form too.
        if isinstance(token, dict):
            return token.get(_AUTH_HEADER) or next(iter(token.values()))
        return token

    # -- ask ----------------------------------------------------------------
    def ask(
        self,
        question: str,
        system_prompt: str,
        on_event: EventCallback | None = None,
    ) -> AskResult:
        """POST /ask and stream events back, live, into ``on_event``.

        Every event is also collected into ``AskResult.events``. Artifacts
        (base64 files, typically charts) are decoded to ``pitwall_artifacts/``
        as they arrive so the CLI/web can render them incrementally.
        """
        if not self._endpoint or not self._token:
            return AskResult(answer="", is_error=True, events=[
                {"type": "error", "message": "MicroVM session not started."}
            ])

        body = json.dumps({"question": question, "system_prompt": system_prompt}).encode()
        try:
            return self._stream_ask(body, on_event)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:  # token expired/invalid -> refresh once, retry
                self._token = self._create_token()
                try:
                    return self._stream_ask(body, on_event)
                except (urllib.error.HTTPError, urllib.error.URLError) as exc2:
                    return _error_result(f"MicroVM endpoint error after token refresh: {exc2}")
            detail = exc.read().decode(errors="replace")[:500]
            return _error_result(f"MicroVM endpoint HTTP {exc.code}: {detail}")
        except urllib.error.URLError as exc:
            return _error_result(f"MicroVM endpoint unreachable: {exc.reason}")

    def _stream_ask(self, body: bytes, on_event: EventCallback | None) -> AskResult:
        req = urllib.request.Request(
            f"https://{self._endpoint}/ask",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                _AUTH_HEADER: self._token,
                _PORT_HEADER: str(self.config.target_port),
            },
        )
        events: list[dict] = []
        artifacts: list[str] = []
        answer = ""
        cost = None
        turns = None
        is_error = False

        with urllib.request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
            for raw in resp:
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                # Artifacts are saved to disk here so callers get a plain path
                # to a real file, not a base64 blob to re-decode themselves.
                if event.get("type") == "artifact":
                    saved = _save_artifact(event.get("name", "artifact"), event.get("b64", ""))
                    if saved:
                        artifacts.append(saved)
                        event = {**event, "path": saved}
                        # Drop the base64 from the fanned-out event: it's now
                        # on disk, and keeping it inflates the events list.
                        event.pop("b64", None)

                if event.get("type") == "answer":
                    answer = event.get("text", "") or answer
                if event.get("type") == "done":
                    cost = event.get("cost_usd")
                    turns = event.get("turns")
                    is_error = is_error or bool(event.get("is_error"))
                if event.get("type") == "error":
                    is_error = True

                events.append(event)
                if on_event:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001 — callback failures must not kill the stream
                        pass

        return AskResult(
            answer=answer,
            events=events,
            artifacts=artifacts,
            is_error=is_error,
            cost_usd=cost,
            turns=turns,
        )

    def close(self) -> None:
        if self._driver and self._microvm_id:
            try:
                self._driver.terminate(self._microvm_id)
            except Exception:  # noqa: BLE001 - cleanup must never raise
                pass
            self._microvm_id = None
