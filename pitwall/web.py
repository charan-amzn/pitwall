"""Pitwall web UI — a pit-wall-styled front-end over the same Agent.

This is an *additive* front-end: it wraps the exact same ``pitwall.agent.Agent``
the CLI uses. One MicroVM is launched lazily for the server (shared across the
session), and each question streams the agent's activity to the browser over
Server-Sent Events (SSE):

  event types pushed to the client:
    vm          — microVM lifecycle: {state: launching|running|ready|error, ...}
    user        — echo of the question
    text        — assistant narration (between tool calls)
    tool_use    — {name, input}  — a Bash/Write/Edit/Read call about to run
    tool_result — {content, is_error}
    artifact    — {name, data_uri}  — an image the agent produced
    answer      — Claude's final text readout
    done        — {cost_usd, turns}
    error       — something went wrong

Run with:  pitwall-web   (or: python -m pitwall.web)
Requires the `web` extra:  pip install -e ".[web]"
"""

from __future__ import annotations

import atexit
import base64
import json
import mimetypes
import os
import queue
import signal
import threading
from pathlib import Path

from pitwall.agent import Agent, TurnEvents, build_system_prompt
from pitwall.config import load_dotenv_if_present
from pitwall.data import dataset_summary
from pitwall.sandbox import get_microvm_sandbox

_FRONTEND = Path(__file__).resolve().parent / "static" / "index.html"


def _artifact_to_data_uri(path: str) -> dict | None:
    """Read one image artifact off disk and inline it as a data URI."""
    p = Path(path)
    mime, _ = mimetypes.guess_type(p.name)
    if not (mime and mime.startswith("image/")):
        return None
    try:
        b64 = base64.b64encode(p.read_bytes()).decode()
    except OSError:
        return None
    return {"name": p.name, "data_uri": f"data:{mime};base64,{b64}"}


class PitwallSession:
    """Owns one MicroVM and runs turns for the web UI."""

    def __init__(self) -> None:
        load_dotenv_if_present()
        self._sandbox = None
        self._agent: Agent | None = None
        self._lock = threading.Lock()

    @property
    def model_label(self) -> str:
        # The VM chooses the model; we surface whatever the user configured.
        return f"bedrock:{os.environ.get('PITWALL_MODEL', 'us.anthropic.claude-opus-4-8')}"

    def ensure_agent(self, emit) -> Agent:
        """Lazily launch the MicroVM + build the Agent (thread-safe)."""
        with self._lock:
            if self._agent is not None:
                emit("vm", {"state": "ready", "driver": self._sandbox.driver_kind})
                return self._agent
            emit("vm", {"state": "launching", "message": "Launching MicroVM…"})
            sandbox = get_microvm_sandbox()()
            self._sandbox = sandbox
            self._agent = Agent(
                sandbox=sandbox,
                system_prompt=build_system_prompt(dataset_summary()),
            )
            emit(
                "vm",
                {
                    "state": "running",
                    "driver": sandbox.driver_kind,
                    "endpoint": getattr(sandbox, "_endpoint", None),
                    "microvm_id": getattr(sandbox, "_microvm_id", None),
                },
            )
            return self._agent

    def run_turn(self, question: str, emit) -> None:
        """Run one question, pushing SSE events via ``emit(event, data)``."""
        try:
            agent = self.ensure_agent(emit)
        except BaseException as exc:  # noqa: BLE001 — incl. SystemExit from config
            emit("vm", {"state": "error"})
            emit("error", {"message": f"Could not start the MicroVM: {exc}".strip()})
            emit("done", {})
            return

        emit("user", {"text": question})

        events = TurnEvents(
            on_text=lambda t: emit("text", {"text": t}),
            on_tool_use=lambda name, inp: emit("tool_use", {"name": name, "input": inp}),
            on_tool_result=lambda content, is_error: emit(
                "tool_result", {"content": content, "is_error": is_error}
            ),
            on_artifact=lambda path: _emit_artifact(emit, path),
        )
        try:
            answer = agent.ask(question, events)
            emit("answer", {"text": answer})
        except BaseException as exc:  # noqa: BLE001
            emit("error", {"message": str(exc) or exc.__class__.__name__})
        finally:
            emit("done", {})

    def close(self) -> None:
        with self._lock:
            if self._sandbox is not None:
                self._sandbox.close()  # terminates the MicroVM
                self._sandbox = None
                self._agent = None


def _emit_artifact(emit, path: str) -> None:
    """Convert an on-disk artifact to a data URI and push it to the client."""
    payload = _artifact_to_data_uri(path)
    if payload:
        emit("artifact", payload)


def create_app():
    """Build the FastAPI app. Imported lazily so the web extra is optional."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, StreamingResponse
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The web UI needs the 'web' extra: pip install -e \".[web]\""
        ) from exc

    app = FastAPI(title="Pitwall")
    session = PitwallSession()

    # Terminating the per-session MicroVM is important (it bills while running).
    # Belt and suspenders: atexit + signal handlers, all idempotent.
    atexit.register(session.close)

    def _graceful(signum, _frame):
        session.close()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _graceful)
        except ValueError:
            pass  # not on the main thread (e.g. under some test runners)

    @app.get("/")
    def index():
        return FileResponse(_FRONTEND)

    @app.get("/api/meta")
    def meta():
        return {"model": session.model_label}

    @app.get("/api/ask")
    def ask(q: str):
        # Bridge the synchronous, callback-driven agent loop to SSE: run the
        # turn in a worker thread, funnel events through a thread-safe queue.
        events_q: "queue.Queue[tuple[str, dict] | None]" = queue.Queue()

        def emit(event: str, data: dict) -> None:
            events_q.put((event, data))

        def worker() -> None:
            try:
                session.run_turn(q, emit)
            finally:
                events_q.put(None)  # sentinel: stream complete

        threading.Thread(target=worker, daemon=True).start()

        def event_stream():
            while True:
                item = events_q.get()
                if item is None:
                    break
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.on_event("shutdown")
    def _shutdown():
        session.close()

    return app


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit('The web UI needs the "web" extra: pip install -e ".[web]"')
    host = os.environ.get("PITWALL_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("PITWALL_WEB_PORT", "8000"))
    print(f"🏁 Pitwall web UI on http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
