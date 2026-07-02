"""In-MicroVM Claude Code agent server (runs *inside* the Lambda MicroVM).

The MicroVM hosts the *entire coding agent* — not just a Python runner. The
host asks a question over HTTPS and this server invokes the ``claude`` CLI in
headless mode, pointed at Amazon Bedrock, with permissions to write files and
run Bash *inside this VM only*. Every tool call, tool result, and the final
answer stream back to the host as newline-delimited JSON events.

Endpoints:

  POST /ask     body {"question","system_prompt","cwd"?} — SSE-ish NDJSON stream:
                  {"type":"text","text":...}
                  {"type":"tool_use","name":...,"input":...}
                  {"type":"tool_result","content":...,"is_error":bool}
                  {"type":"artifact","name":...,"b64":...}
                  {"type":"answer","text":...}
                  {"type":"done","cost_usd":...,"turns":...}
                  {"type":"error","message":...}
  the Lambda MicroVMs lifecycle hooks under /aws/lambda-microvms/runtime/v1/*
  GET  /healthz -> liveness

Isolation: this process IS the isolation boundary's payload — the whole point
is that Claude Code (and everything it Bash/Write's) runs in a per-session
Firecracker MicroVM. Bedrock creds come from the VM's execution role via the
standard AWS SDK credential chain — no static keys.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PITWALL_VM_PORT", "8080"))
DATA_DIR = os.environ.get("PITWALL_DATA", "/opt/pitwall/data")
CLAUDE_BIN = os.environ.get("PITWALL_CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get(
    "PITWALL_CLAUDE_MODEL", "us.anthropic.claude-opus-4-8"
)
ASK_TIMEOUT = int(os.environ.get("PITWALL_VM_ASK_TIMEOUT", "600"))
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"

# Tools Claude Code is allowed to use inside the VM. Bash + Write + Edit + Read
# cover "write a Python script and run it"; we don't need WebFetch/Task/etc.
ALLOWED_TOOLS = "Bash,Write,Edit,Read"


def _emit(wfile, event: dict) -> None:
    """Write one NDJSON event and flush immediately (server-side push)."""
    wfile.write((json.dumps(event) + "\n").encode())
    wfile.flush()


def _collect_artifacts(workdir: str) -> list[dict]:
    """Grab any files Claude created in ``workdir`` and base64-encode them."""
    artifacts: list[dict] = []
    if not os.path.isdir(workdir):
        return artifacts
    for name in sorted(os.listdir(workdir)):
        if name.startswith(".") or name == "__pycache__":
            continue
        full = os.path.join(workdir, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                artifacts.append({"name": name, "b64": base64.b64encode(fh.read()).decode()})
        except OSError:
            continue
    return artifacts


def _seen_artifact_names(previously: set[str], workdir: str) -> list[dict]:
    """Return only artifacts new since the last check; update ``previously``."""
    fresh: list[dict] = []
    if not os.path.isdir(workdir):
        return fresh
    for name in sorted(os.listdir(workdir)):
        if name in previously or name.startswith(".") or name == "__pycache__":
            continue
        full = os.path.join(workdir, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                fresh.append({"name": name, "b64": base64.b64encode(fh.read()).decode()})
        except OSError:
            continue
        previously.add(name)
    return fresh


def _run_claude(question: str, system_prompt: str, workdir: str, model: str, wfile) -> None:
    """Invoke ``claude`` headless in ``workdir`` and forward events to ``wfile``.

    The CLI writes newline-delimited JSON envelopes to stdout — one per
    ``system|assistant|user|result`` message. We normalize those into the
    smaller event vocabulary the host renders (text/tool_use/tool_result/…),
    which keeps the wire format independent of Claude Code's internal schema.
    """
    cmd = [
        CLAUDE_BIN,
        "-p",
        question,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--model",
        model,
        "--append-system-prompt",
        system_prompt,
    ]

    env = {
        **os.environ,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "PITWALL_DATA": DATA_DIR,
        "MPLBACKEND": "Agg",
    }
    if "AWS_REGION" not in env and "AWS_DEFAULT_REGION" in env:
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"]

    seen: set[str] = set()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        _emit(wfile, {"type": "error", "message": f"claude CLI not found at {CLAUDE_BIN!r}"})
        return

    # Drain stderr on a background thread so a chatty stderr can't deadlock us.
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    threading.Thread(target=_drain_stderr, daemon=True).start()

    watchdog = threading.Timer(ASK_TIMEOUT, proc.kill)
    watchdog.daemon = True
    watchdog.start()

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for evt in _normalize(obj):
                _emit(wfile, evt)
            for art in _seen_artifact_names(seen, workdir):
                _emit(wfile, {"type": "artifact", **art})
    finally:
        watchdog.cancel()
        rc = proc.wait()

    # Flush anything created right at the end (e.g. a final chart from the
    # last tool call, whose result line might have already been consumed).
    for art in _seen_artifact_names(seen, workdir):
        _emit(wfile, {"type": "artifact", **art})

    if rc != 0:
        tail = "".join(stderr_chunks)[-800:]
        _emit(
            wfile,
            {
                "type": "error",
                "message": f"claude exited with code {rc}",
                "stderr": tail,
            },
        )


def _normalize(obj: dict):
    """Translate one Claude Code stream-json envelope into 0+ Pitwall events."""
    kind = obj.get("type")

    if kind == "assistant":
        for block in obj.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    yield {"type": "text", "text": text}
            elif btype == "tool_use":
                yield {
                    "type": "tool_use",
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                    "id": block.get("id", ""),
                }
        return

    if kind == "user":
        content = obj.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    payload = block.get("content", "")
                    text = payload if isinstance(payload, str) else json.dumps(payload)
                    yield {
                        "type": "tool_result",
                        "content": text,
                        "is_error": bool(block.get("is_error")),
                        "id": block.get("tool_use_id", ""),
                    }
        return

    if kind == "result":
        yield {
            "type": "answer",
            "text": obj.get("result", "") or "",
        }
        yield {
            "type": "done",
            "cost_usd": obj.get("total_cost_usd"),
            "turns": obj.get("num_turns"),
            "is_error": bool(obj.get("is_error")),
        }
        return

    if kind == "system":
        # Only forward the init envelope (which carries the model banner);
        # Claude Code emits additional `system` events for each internal
        # sub-agent init, and rendering them all would be noise.
        if obj.get("subtype") == "init":
            yield {
                "type": "session",
                "model": obj.get("model", ""),
                "session_id": obj.get("session_id", ""),
            }
        return


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", ""):
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        path = self.path.rstrip("/")

        if path.startswith(HOOK_PREFIX):
            self._send(200, {"status": "ok"})
            return

        if path == "/ask":
            try:
                body = json.loads(raw or b"{}")
            except (ValueError, AttributeError):
                self._send(400, {"error": "invalid JSON body"})
                return

            question = body.get("question", "")
            system_prompt = body.get("system_prompt", "")
            model = body.get("model") or DEFAULT_MODEL
            if not isinstance(question, str) or not question.strip():
                self._send(400, {"error": "missing 'question'"})
                return

            # Stream NDJSON events. Each `_emit` flushes so the host renders live.
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            workdir = tempfile.mkdtemp(prefix="pitwall_ask_")
            try:
                _run_claude(question, system_prompt, workdir, model, self.wfile)
            except Exception as exc:  # noqa: BLE001
                try:
                    _emit(self.wfile, {"type": "error", "message": str(exc)})
                except Exception:  # noqa: BLE001
                    pass
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            return

        self._send(404, {"error": "not found"})

    def log_message(self, *args):  # silence default access logging
        pass


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"pitwall in-VM agent server listening on :{PORT} "
        f"(data={DATA_DIR}, model={DEFAULT_MODEL})",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
