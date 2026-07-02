"""Pitwall CLI — chat with the F1 race-engineer data lab.

Each session runs the *entire coding agent* (Claude Code, pointed at Bedrock)
inside its own AWS Lambda MicroVM. The host process here is just a thin proxy:
it launches the VM, forwards your question, and renders the tool trace the VM
streams back — tool calls (Bash/Write/Edit), tool results, chart artifacts,
and finally the assistant's readout.

Usage:
    pitwall                       # interactive REPL
    pitwall "your question"       # one-shot question, then exit

Environment:
    PITWALL_MODEL             Bedrock inference-profile id used INSIDE the VM
                              (default: us.anthropic.claude-opus-4-8). Forwarded
                              to the VM at ask time so a rebuild isn't needed
                              to swap models.
    PITWALL_MICROVM_IMAGE_ARN Optional — pin the MicroVM image (auto-discovered
                              by name otherwise).
    AWS_REGION                Required — the region for both microVMs and the
                              Bedrock endpoint the VM calls.
"""

from __future__ import annotations

import argparse
import os
import sys

from pitwall import __version__
from pitwall.agent import Agent, TurnEvents, build_system_prompt
from pitwall.config import load_dotenv_if_present
from pitwall.data import dataset_summary
from pitwall.sandbox import get_microvm_sandbox

# --- tiny ANSI helpers (color only when attached to a TTY) -----------------
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _dim(t: str) -> str:
    return _c("2", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _cyan(t: str) -> str:
    return _c("36", t)


def _green(t: str) -> str:
    return _c("32", t)


def _yellow(t: str) -> str:
    return _c("33", t)


def _red(t: str) -> str:
    return _c("31", t)


def _fmt_tool_call(name: str, inputs: dict) -> str:
    """One-line summary of a tool call — the argument that matters, truncated.

    Prefixed with ``Claude Code ·`` so the trace makes clear that the *agent
    itself* (Claude Code, running inside the MicroVM) is what's invoking each
    tool — not the host process.
    """
    prefix = "Claude Code · "
    if name == "Bash":
        cmd = str(inputs.get("command", "")).strip().replace("\n", " ")
        return f"{prefix}{name}  ▸ {cmd[:200]}"
    if name in ("Write", "Edit"):
        path = str(inputs.get("file_path", ""))
        return f"{prefix}{name}  ▸ {path}"
    if name == "Read":
        return f"{prefix}{name}  ▸ {inputs.get('file_path', '')}"
    return f"{prefix}{name}"


def _make_events(show_code: bool) -> TurnEvents:
    def on_text(text: str) -> None:
        # Narration between tool calls; keep it dim so the final answer stands out.
        for line in text.strip().splitlines():
            print(_dim("  · ") + line)

    def on_tool_use(name: str, inputs: dict) -> None:
        if not show_code:
            print(_dim("  · running analysis…"))
            return
        summary = _fmt_tool_call(name, inputs)
        print(_dim(f"\n  ┌─ {summary}"))
        # For Write, echo the actual file body so the demo shows the Python.
        if name in ("Write", "Edit"):
            body = inputs.get("content") or inputs.get("new_string") or ""
            if body:
                for line in body.rstrip().splitlines()[:40]:
                    print(_dim("  │ ") + line)
                if len(body.splitlines()) > 40:
                    print(_dim("  │ …"))
        print(_dim("  └────────────────────────────"))

    def on_tool_result(content: str, is_error: bool) -> None:
        tag = _red("err") if is_error else _green("ok")
        preview = content.strip().splitlines()[:8]
        print(_dim(f"  result [{tag}]"))
        for line in preview:
            print(_dim("  │ ") + line)
        if content and len(content.splitlines()) > 8:
            print(_dim("  │ …"))

    def on_artifact(path: str) -> None:
        print(_dim("  → saved ") + _cyan(path))

    return TurnEvents(
        on_text=on_text,
        on_tool_use=on_tool_use,
        on_tool_result=on_tool_result,
        on_artifact=on_artifact,
    )


def _print_answer(text: str) -> None:
    print(f"\n{_bold(_cyan('Pitwall'))}: {text}\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitwall", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", nargs="*", help="A question to ask, then exit. Omit for a REPL.")
    p.add_argument("--model", help="Bedrock model id used inside the VM (overrides PITWALL_MODEL).")
    p.add_argument("--hide-code", action="store_true",
                   help="Don't print the generated code/commands, only results.")
    p.add_argument("--version", action="version", version=f"pitwall {__version__}")
    return p


def _model_label() -> str:
    return os.environ.get("PITWALL_MODEL", "us.anthropic.claude-opus-4-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    load_dotenv_if_present()

    if args.model:
        os.environ["PITWALL_MODEL"] = args.model

    # Launch the per-session MicroVM. Everything else — model auth, code
    # execution, tool loop — happens INSIDE it.
    print(_dim("Launching MicroVM…"))
    try:
        sandbox = get_microvm_sandbox()()  # reads config from env
    except SystemExit as exc:  # missing/invalid MicroVM config
        print(_red(str(exc)), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — MicroVMUnavailableError, AWS errors, etc.
        print(_red(f"Could not start the MicroVM sandbox:\n{exc}"), file=sys.stderr)
        return 2

    agent = Agent(sandbox=sandbox, system_prompt=build_system_prompt(dataset_summary()))
    events = _make_events(show_code=not args.hide_code)

    banner = (
        f"{_bold('🏁 Pitwall')} — F1 race-engineer data lab  "
        f"{_dim(f'[agent: Claude Code in the MicroVM · model: bedrock:{_model_label()} · sandbox: {sandbox.name}]')}"
    )

    try:
        if args.question:
            print(banner)
            question = " ".join(args.question)
            print(f"\n{_bold('You')}: {question}")
            _print_answer(agent.ask(question, events))
            return 0

        print(banner)
        print(_dim("Ask about races, strategy, pace, pit stops. Ctrl-D or 'exit' to quit.\n"))
        while True:
            try:
                question = input(_bold("You") + ": ").strip()
            except EOFError:
                print()
                break
            if question.lower() in {"exit", "quit", ":q"}:
                break
            if not question:
                continue
            try:
                _print_answer(agent.ask(question, events))
            except KeyboardInterrupt:
                print(_dim("\n  (interrupted)\n"))
        return 0
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        sandbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
