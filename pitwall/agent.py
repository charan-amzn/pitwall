"""The Pitwall host-side proxy to the in-VM coding agent.

The agent brain lives *inside* the MicroVM — the Claude Code CLI, pointed at
Amazon Bedrock via the VM's execution role, writes Python and runs it in place.
This module is the host-side thin proxy: it forwards the user's question and
the system prompt to the VM's ``/ask`` endpoint, then fans the event stream out
to CLI/web renderers via the ``TurnEvents`` callbacks.

That keeps the two demo constraints intact:
  * VM-level isolation of the model AND its generated code (both live in the
    MicroVM — the host holds no keys and executes no user code).
  * Live visibility of every tool call so the CLI can render it as it happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from pitwall.sandbox import AskResult, Sandbox


def build_system_prompt(dataset_summary: str) -> str:
    return (
        "You are Pitwall, an expert Formula 1 race-strategy engineer and data "
        "analyst. You answer questions about F1 by writing and running Python "
        "against a dataset of race results, lap times, and pit stops.\n\n"
        "How you work:\n"
        "- You are running INSIDE a per-session sandbox. Use the Bash and Write "
        "tools to create Python files in the current working directory and run "
        "them with `python3`. pandas, numpy, and matplotlib are pre-installed. "
        "Do not attempt to install packages or reach the network.\n"
        "- The F1 dataset lives at $PITWALL_DATA (also available as the env "
        "var PITWALL_DATA inside Bash). Load the CSVs with pandas.\n"
        "- Ground answers in computation over the data; do not estimate from "
        "memory. Inspect the data first if you are unsure of its shape.\n"
        "- When a chart helps, use matplotlib with the 'Agg' backend and "
        "savefig() to a file in the current directory. Every file you create "
        "in the working directory is collected and shown to the user.\n"
        "- After the analysis, give a concise, race-engineer-style readout: "
        "lead with the answer, then the supporting numbers. Use driver codes "
        "and team names. Note when a result is limited by the data (e.g. a "
        "race not yet run, or a field that's blank in this dataset).\n\n"
        f"{dataset_summary}"
    )


@dataclass
class TurnEvents:
    """Hooks so the CLI / web UI can render the agent's activity live.

    Every hook is optional. Callbacks receive already-typed data — the raw
    event stream is preserved on the returned ``AskResult`` for post-analysis.
    """

    # A block of assistant text (narration in between tool calls).
    on_text: Optional[Callable[[str], None]] = None
    # The agent invoked a tool. ``name`` is Claude Code's tool name (Bash,
    # Write, Edit, Read); ``inputs`` is the tool's argument object.
    on_tool_use: Optional[Callable[[str, dict], None]] = None
    # A tool returned. ``content`` is the stdout/summary Claude sees;
    # ``is_error`` mirrors Claude Code's flag.
    on_tool_result: Optional[Callable[[str, bool], None]] = None
    # An artifact file was produced by a tool. Path is the local (host) file.
    on_artifact: Optional[Callable[[str], None]] = None
    # Anything else (session banner, done, error) — receives the raw event.
    on_event: Optional[Callable[[dict], None]] = None


@dataclass
class Agent:
    sandbox: Sandbox
    system_prompt: str

    def ask(self, question: str, events: Optional[TurnEvents] = None) -> str:
        """Run one user turn end-to-end; return the final answer text."""
        events = events or TurnEvents()

        def _dispatch(event: dict) -> None:
            kind = event.get("type")
            if kind == "text" and events.on_text:
                events.on_text(event.get("text", ""))
            elif kind == "tool_use" and events.on_tool_use:
                events.on_tool_use(event.get("name", ""), event.get("input", {}) or {})
            elif kind == "tool_result" and events.on_tool_result:
                events.on_tool_result(event.get("content", ""), bool(event.get("is_error")))
            elif kind == "artifact" and events.on_artifact:
                path = event.get("path")
                if path:
                    events.on_artifact(path)
            if events.on_event:
                events.on_event(event)

        result: AskResult = self.sandbox.ask(question, self.system_prompt, _dispatch)
        if result.answer:
            return result.answer
        # No final answer: surface the first error we saw, else a canned line.
        for ev in result.events:
            if ev.get("type") == "error":
                return f"(agent error: {ev.get('message', 'unknown error')})"
        return "(no answer produced)"
