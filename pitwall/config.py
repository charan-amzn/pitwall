"""Central configuration — everything is environment-variable driven.

Anyone can clone the repo, copy ``.env.example`` to ``.env``, fill in their
AWS / Bedrock (or Anthropic) credentials and MicroVM image ARN, and run. If
``python-dotenv`` is installed, a ``.env`` file in the working directory or repo
root is loaded automatically; otherwise plain environment variables are used.

Nothing here hardcodes an account, region, model, or ARN — see ``.env.example``
for the full list of knobs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv_if_present() -> None:
    """Best-effort: load a .env file so env vars don't have to be exported.

    No-op (and never raises) if python-dotenv isn't installed or no .env exists.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _region() -> str | None:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


@dataclass
class MicroVMConfig:
    """Settings for the ``MicroVMSandbox`` (all from env vars)."""

    image_arn: str | None  # explicit ARN; if None, discovered by image_name
    image_name: str        # used to auto-discover the ARN when image_arn is None
    region: str
    ingress_connectors: list[str]
    egress_connectors: list[str]
    execution_role_arn: str | None
    max_idle_seconds: int
    suspended_seconds: int
    max_duration_seconds: int
    auth_token_minutes: int
    target_port: int
    ready_timeout_seconds: int
    request_timeout_seconds: int
    driver: str  # auto | boto3 | cli
    cli_path: str

    @classmethod
    def from_env(cls) -> "MicroVMConfig":
        region = _region()
        if not region:
            raise SystemExit(
                "AWS_REGION is not set. Export AWS_REGION (e.g. us-west-2) or set "
                "it in your .env.\nSee .env.example and README -> Setup."
            )
        # PITWALL_MICROVM_IMAGE_ARN is optional: if unset, the sandbox discovers
        # the image by name (PITWALL_MICROVM_IMAGE_NAME, default 'pitwall-lab').
        image_arn = os.environ.get("PITWALL_MICROVM_IMAGE_ARN") or None

        # Lambda-managed default connectors, parameterized by region so they're
        # not hardcoded to one account/region.
        default_ingress = f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:ALL_INGRESS"
        default_egress = f"arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"

        # Exec role: the MicroVM assumes it at run time so Claude Code inside
        # the VM can call Bedrock without static credentials. Auto-derived from
        # the account (via STS) unless the user set it explicitly. STS lookup
        # is deferred so we don't force AWS auth for --help / --version.
        exec_role_arn = os.environ.get("PITWALL_MICROVM_EXEC_ROLE_ARN") or None
        exec_role_name = os.environ.get("PITWALL_EXEC_ROLE_NAME", "PitwallMicrovmExecRole")
        if not exec_role_arn:
            exec_role_arn = _derive_role_arn(exec_role_name)

        return cls(
            image_arn=image_arn,
            image_name=os.environ.get("PITWALL_MICROVM_IMAGE_NAME", "pitwall-lab"),
            region=region,
            ingress_connectors=_csv_env("PITWALL_MICROVM_INGRESS", default_ingress),
            egress_connectors=_csv_env("PITWALL_MICROVM_EGRESS", default_egress),
            execution_role_arn=exec_role_arn,
            max_idle_seconds=_int_env("PITWALL_MICROVM_MAX_IDLE_S", 900),
            suspended_seconds=_int_env("PITWALL_MICROVM_SUSPENDED_S", 1800),
            max_duration_seconds=_int_env("PITWALL_MICROVM_MAX_DURATION_S", 14400),
            auth_token_minutes=_int_env("PITWALL_MICROVM_TOKEN_MINUTES", 30),
            target_port=_int_env("PITWALL_MICROVM_PORT", 8080),
            ready_timeout_seconds=_int_env("PITWALL_MICROVM_READY_TIMEOUT_S", 180),
            request_timeout_seconds=_int_env("PITWALL_MICROVM_REQUEST_TIMEOUT_S", 600),
            driver=_choice_env("PITWALL_MICROVM_DRIVER", "auto", {"auto", "boto3", "cli"}),
            cli_path=os.environ.get("PITWALL_AWS_CLI", "aws"),
        )

    @property
    def idle_policy(self) -> dict:
        return {
            "autoResumeEnabled": True,
            "maxIdleDurationSeconds": self.max_idle_seconds,
            "suspendedDurationSeconds": self.suspended_seconds,
        }


def _derive_role_arn(role_name: str) -> str | None:
    """Best-effort: ask STS for the account id and build the role ARN.

    Returns ``None`` if boto3/STS aren't available so the sandbox falls back to
    launching without an execution role (the MicroVM will run, but Bedrock
    calls from inside the VM will 403). Explicit ``PITWALL_MICROVM_EXEC_ROLE_ARN``
    always wins over this derivation.
    """
    try:
        import boto3  # type: ignore
    except ImportError:
        return None
    try:
        account = boto3.client("sts").get_caller_identity()["Account"]
    except Exception:  # noqa: BLE001 — no creds, network issue, etc.
        return None
    return f"arn:aws:iam::{account}:role/{role_name}"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}.") from exc


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _choice_env(name: str, default: str, allowed: set[str]) -> str:
    val = os.environ.get(name, default).strip().lower() or default
    if val not in allowed:
        raise SystemExit(f"{name} must be one of {sorted(allowed)}, got {val!r}.")
    return val
