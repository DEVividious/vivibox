"""What the supervisor asks of a coding agent's tool, and what a turn of it reports.

A tool overrides what it has: opencode makes a session ahead of the turn and streams its cost,
Claude Code reports when the turn is over, the manual planner runs no turn at all. The supervisor
reads the same attributes and calls the same methods on each, so nothing asks a tool what it can do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


class HarnessError(Exception):
    pass


# The running cost, tokens and step count, at every step of a turn.
OnStep = Callable[[float, int, int], None]


@dataclass
class Turn:
    session: str
    ok: bool
    cost: float
    tokens: int
    text: str
    error: str = ""


class Harness:
    # The tool's name, for the event log; conversations are kept one per role, not per harness.
    name = ""
    # False when a turn's reported cost is a list price rather than money spent, as it is on a
    # subscription. A total that added the two would be neither.
    metered = True
    # True for the planner that is you: the supervisor writes a prompt for your chat and reads
    # your answer, and turn() is never called.
    manual = False

    def start_session(self, title: str) -> str:
        """A conversation made before the turn that fills it, so it can be watched from the
        start; "" from a tool that only reports the session when the turn is over."""
        return ""

    def turn(self, prompt: str, session: str = "", title: str = "", on_step: OnStep | None = None) -> Turn:
        """One turn of the agent, in the session given or a new one. on_step, when given, gets the
        running cost, tokens and step count as they come in."""
        raise NotImplementedError
