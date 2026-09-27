"""A role's conversation, turn by turn, in log/<role>.log: what it was asked in short, what it
said, the tools it called, what the turn cost and how long it took. The conversation itself is in
opencode in the pod and goes with it; this stays with the task, and in its archive once it is done.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .harness import Turn
from .task import Task

ROLES = ("planner", "writer", "reviewer")
# How much of the prompt a turn's heading keeps: the brief and the plan are the same every turn.
PROMPT_LINES = 6
PROMPT_CHARACTERS = 600
FOOTER = re.compile(r"^--- \S+ · (ok|failed)(?:: [^·]*)? · \$([\d.]+)", re.MULTILINE)


def path(task: Task, role: str) -> Path:
    return task.meta / "log" / f"{role}.log"


def shortened(prompt: str) -> str:
    lines = [line for line in prompt.strip().splitlines() if line.strip()]
    shown = "\n".join(lines[:PROMPT_LINES])
    if len(shown) > PROMPT_CHARACTERS:
        shown = shown[: PROMPT_CHARACTERS - 1] + "…"
    elif len(lines) > PROMPT_LINES:
        shown += f"\n… {len(lines) - PROMPT_LINES} more lines"
    return shown


def write(
    task: Task, role: str, agent: str, state: str, kind: str, prompt: str, turn: Turn, seconds: float
) -> None:
    """One turn, appended to the role's log: its heading, what was said and done, how it ended."""
    started = time.strftime("%H:%M:%S", time.localtime(time.time() - seconds))
    played = f" (as {agent})" if agent and agent != role else ""
    said = turn.transcript or ([turn.text.strip()] if turn.text.strip() else [])
    ended = "ok" if turn.ok else f"failed: {' '.join(turn.error.split())[:200]}"
    spent = f"${turn.cost:.4f} · {turn.tokens} tokens · {round(seconds)} s"
    lines = [
        f"=== {started} {role}{played} · {state}{f' · {kind}' if kind else ''} ===",
        "Prompt: " + shortened(prompt).replace("\n", "\n        "),
        "",
        *said,
        f"--- {time.strftime('%H:%M:%S')} · {ended} · {spent}",
        "",
    ]
    target = path(task, role)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as f:
        f.write("\n".join(lines) + "\n")


def describe(log: Path, role: str) -> str:
    """A role's log in a line, for l: whose, how many turns, what they cost."""
    ends = FOOTER.findall(log.read_text(errors="replace"))
    failed = sum(1 for ok, _ in ends if ok == "failed")
    said = [f"the {role}'s conversation", f"{len(ends)} turn{'s' if len(ends) != 1 else ''}"]
    if failed:
        said.append(f"{failed} failed")
    said.append(f"${sum(float(cost) for _, cost in ends):.2f}")
    return " · ".join(said)
