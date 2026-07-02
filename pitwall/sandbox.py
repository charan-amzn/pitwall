"""Agent-sandbox interface for Pitwall.

The whole coding agent lives *inside* the MicroVM — not on the host. The host
does not write or run Python. It sends the user's question to the in-VM Claude
Code CLI (via ``Sandbox.ask``) and consumes the newline-delimited event stream
the VM emits: assistant text, tool calls (Bash/Write/Edit), tool results,
artifacts (charts) as base64, then the final answer.

This module defines the interface. The concrete backend (``MicroVMSandbox``,
in ``pitwall.microvm``) is imported lazily so this module stays dep-light.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


@dataclass
class AskResult:
    """Outcome of one user turn inside the sandbox.

    ``events`` is the raw list of events streamed by the VM (already handed to
    ``on_event`` live); it's preserved so callers can post-process a turn.
    ``answer`` is the final assistant text (Claude Code's ``result.result``).
    """

    answer: str
    events: list[dict] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # local paths of saved files
    is_error: bool = False
    cost_usd: Optional[float] = None
    turns: Optional[int] = None


EventCallback = Callable[[dict], None]


class Sandbox(ABC):
    """A microVM that hosts a Claude Code agent + a baked dataset."""

    name: str = "sandbox"

    @abstractmethod
    def ask(
        self,
        question: str,
        system_prompt: str,
        on_event: Optional[EventCallback] = None,
    ) -> AskResult:
        """Ask the in-VM agent a question and stream events back live.

        The callback is invoked once per event (see server.py for the vocabulary).
        Must not raise on user-level errors — surface those in ``AskResult``.
        """

    def close(self) -> None:  # pragma: no cover - default no-op
        """Release any resources (the microVM, tokens, ...)."""

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def get_microvm_sandbox():
    """Return the ``MicroVMSandbox`` class. Imported on demand (pulls in boto3)."""
    from pitwall.microvm import MicroVMSandbox

    return MicroVMSandbox
