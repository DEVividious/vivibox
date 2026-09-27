"""What l opens: the task's timeline, each role's conversation, its verification logs newest
first, and the supervisor's log last, as diagnostics. One entry opens at once; while a
verification runs its log is the entry the cursor is on, followed as it is written."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import look, prepare, reviewing, timeline, transcript
from .panel import pager_command
from .states import State
from .task import Task, TaskState
from .widgets import Choose

COMMAND = re.compile(r"^\$ (.+)$", re.MULTILINE)
VERIFICATION_OVER = (
    "The verification finished: nothing runs in it any more. Its log and the earlier ones stay"
    " under l; Ctrl-q leaves."
)
EXIT = re.compile(r"^\[exit (-?\d+)( after ([^\]]+))?\]$", re.MULTILINE)
ITERATION = re.compile(r"^verify-(\d+)-")


@dataclass(frozen=True)
class Entry:
    label: str
    said: str
    command: list[str]


def describe(path: Path) -> str:
    """A verification log in a line: its attempt, what ran, how it went, how long, how much."""
    text = path.read_text(errors="replace")
    commands = COMMAND.findall(text)
    exits = EXIT.findall(text)
    said = []
    if m := ITERATION.match(path.name):
        said.append(f"attempt {m.group(1)}")
    if commands:
        said.append(", ".join(f"`{c}`" for c in commands[:2]) + (", …" if len(commands) > 2 else ""))
    if exits:
        failed = [code for code, _, _ in exits if code != "0"]
        said.append("failed" if failed else "passed")
        took = [after for _, _, after in exits if after]
        if took:
            said.append(took[-1] if len(took) == 1 else f"{len(took)} commands, last {took[-1]}")
    elif commands:
        said.append("running")
    said.append(f"{text.count(chr(10))} lines")
    return " · ".join(said)


def conversations(folder: Path) -> list[Entry]:
    """Each role's transcript, in the order the roles work, opened at its newest turn."""
    return [
        Entry(log.name, transcript.describe(log, role), pager_command(log, at_end=True))
        for role in transcript.ROLES
        if (log := folder / f"{role}.log").exists()
    ]


def entries(task: Task, st: TaskState, running: bool) -> tuple[list[Entry], int]:
    """What l offers, and which entry the cursor starts on: the log being written during a
    verification, else the timeline."""
    found = [
        Entry("timeline", "what happened, oldest first", pager_command(timeline.write(task), at_end=True)),
        *conversations(task.meta / "log"),
    ]
    logs = sorted((task.meta / "log").glob("verify-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    verifying = st.state is State.VERIFY and running
    for i, log in enumerate(logs):
        follow = verifying and i == 0
        found.append(Entry(log.name, describe(log), pager_command(log, follow=follow, at_end=not follow)))
    handoff = task.meta / "handoff"
    reviews = [
        (m.group(1), p) for p in handoff.glob("review-*.md") if (m := reviewing.NUMBERED.match(p.name))
    ]
    for n, path in sorted(reviews, key=lambda r: int(r[0]), reverse=True):
        review = reviewing.parse_review(path.read_text())
        said = f"review {n} · {len(review.blocking)} blocking · {len(review.not_blocking)} not blocking"
        found.append(Entry(path.name, said, pager_command(path)))
    prepared = handoff / prepare.LOG
    preparing = prepare.underway(task)
    if prepared.exists():
        code = prepare.ending(task)
        how = (
            "running"
            if preparing
            else "cut short"
            if code is None
            else "passed"
            if code == 0
            else f"failed, exit {code}"
        )
        lines = prepared.read_text(errors="replace").count("\n")
        said = f"the project's preparation · {how} · {lines} lines"
        found.append(
            Entry(prepare.LOG, said, pager_command(prepared, follow=preparing, at_end=not preparing))
        )
    supervisor = task.meta / "log" / "supervisor.log"
    if supervisor.exists():
        found.append(
            Entry(
                "supervisor.log",
                "diagnostics: what the supervisor did",
                pager_command(supervisor, at_end=True),
            )
        )
    if preparing and st.state is State.IMPLEMENT and prepared.exists():
        return found, next(i for i, e in enumerate(found) if e.label == prepare.LOG)
    return found, next(i for i, e in enumerate(found) if e.label == logs[0].name) if verifying and logs else 0


def archived_entries(kept: Path) -> list[Entry]:
    """What l offers on a finished task: what its archive kept, the timeline first, the
    verification logs newest first, the reviews, the supervisor's log. Nothing for a task that
    finished before the logs were kept."""
    found = []
    if (kept / "timeline.txt").exists():
        found.append(Entry("timeline", "what happened, oldest first", pager_command(kept / "timeline.txt")))
    found += conversations(kept / "log")
    logs = sorted((kept / "log").glob("verify-*.log"), key=lambda p: p.name, reverse=True)
    for log in logs:
        found.append(Entry(log.name, describe(log), pager_command(log, at_end=True)))
    reviews = [(m.group(1), p) for p in kept.glob("review-*.md") if (m := reviewing.NUMBERED.match(p.name))]
    for n, path in sorted(reviews, key=lambda r: int(r[0]), reverse=True):
        review = reviewing.parse_review(path.read_text())
        said = f"review {n} · {len(review.blocking)} blocking · {len(review.not_blocking)} not blocking"
        found.append(Entry(path.name, said, pager_command(path)))
    supervisor = kept / "log" / "supervisor.log"
    if supervisor.exists():
        found.append(
            Entry(
                "supervisor.log",
                "diagnostics: what the supervisor did",
                pager_command(supervisor, at_end=True),
            )
        )
    return found


class ChooseLog(Choose):
    """Which log to read. Enter opens it in your pager."""

    hint_keys = (("enter", "open"), look.ESC_CLOSES)

    def __init__(self, found: list[Entry], start: int = 0):
        super().__init__("Logs", [(e.label, e.said) for e in found], start=start)
        self.found = found

    def picked(self, index: int) -> list[str]:
        return self.found[index].command


def follow_verification(
    task: Task, log: Path, out: IO[str], sleep: Callable[[float], None] = time.sleep
) -> None:
    """The verification's log as it is written, in the window w opens, and a word once the task
    has left verifying: a window on a log nobody writes to looked like a verification that
    hangs. The summary is written before the state changes, so it is shown before the word."""
    shown = 0

    def more() -> None:
        nonlocal shown
        text = log.read_text()
        if len(text) > shown:
            out.write(text[shown:])
            out.flush()
            shown = len(text)

    while task.read_state().state is State.VERIFY:
        more()
        sleep(1)
    more()
    print(f"\n{VERIFICATION_OVER}", file=out, flush=True)
