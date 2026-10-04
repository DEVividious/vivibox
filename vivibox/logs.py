"""What l opens: the task's timeline, each role's conversation, its verification logs newest
first, and the supervisor's log last, as diagnostics. One entry opens at once; while a
verification runs its log is the entry the cursor is on, followed as it is written."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from . import display, handoff, look, prepare, reviewing, timeline, transcript
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
# What l says of a file the agent left in handoff/ that vivibox does not read: the dialog's line is
# too short for the whole path in front of it.
NOT_READ = "not read: a link or not a plain file; delete it"


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
    folder = task.meta / "handoff"
    reviews = [(m.group(1), p) for p in folder.glob("review-*.md") if (m := reviewing.NUMBERED.match(p.name))]
    for n, path in sorted(reviews, key=lambda r: int(r[0]), reverse=True):
        try:
            review = reviewing.parse_review(handoff.read_text(path))
        except handoff.HandoffError:  # said, and no pager on it: a FIFO would hold it
            found.append(Entry(path.name, NOT_READ, []))
            continue
        said = f"review {n} · {len(review.blocking)} blocking · {len(review.not_blocking)} not blocking"
        if problem := reviewing.unreadable(task, int(n)):  # no counts: they would read as a clean review
            # A file's name, not its whole path, fits the line.
            said = f"review {n} · unreadable: {problem.rsplit('/', 1)[-1] if problem[:1] == '/' else problem}"
        found.append(Entry(path.name, said, pager_command(path)))
    prepared = folder / prepare.LOG
    preparing = prepare.underway(task)
    try:
        lines = handoff.read_file(prepared, errors="replace").count("\n")
    except FileNotFoundError:
        lines = None
    except handoff.HandoffError:
        found.append(Entry(prepare.LOG, NOT_READ, []))
        lines = None
    if lines is not None:
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
    unread = unread_reviews(kept)
    for n, path in sorted(reviews, key=lambda r: int(r[0]), reverse=True):
        text = path.read_text(errors="replace")
        review = reviewing.parse_review(text)
        said = f"review {n} · {len(review.blocking)} blocking · {len(review.not_blocking)} not blocking"
        # No counts for what is no review: they would read as a clean one. Why, as the supervisor
        # recorded it; where the archive has no such record, the counted section the text lacks
        # (what a round keeps when the reviewer's file was not read has none).
        headings = {line.strip().lower() for line in text.splitlines()}
        counted = (reviewing.BLOCKING, reviewing.NOT_BLOCKING)
        if problem := unread.get(int(n), ""):
            said = f"review {n} · unreadable: {problem.rsplit('/', 1)[-1] if problem[:1] == '/' else problem}"
        elif int(n) not in unread and (missing := [h for h in counted if h.lower() not in headings]):
            said = f"review {n} · not a review: the section {missing[0]} is missing"
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


def unread_reviews(kept: Path) -> dict[int, str]:
    """Each round's review as the archive's events record it: why the supervisor could not read
    it, "" when it could. No entry for a round the archive has no record of, or cannot read."""
    found: dict[int, str] = {}
    try:
        for line in (kept / "events.jsonl").read_text().splitlines():
            event = json.loads(line) if line else {}
            if event.get("type") == "review":
                found[int(event["data"]["round"])] = str(event["data"].get("problem", ""))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
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
        if len(text) > shown:  # the build's output, as text: no escape sequence of it reaches the terminal
            out.write(display.terminal(text[shown:]))
            out.flush()
            shown = len(text)

    while task.read_state().state is State.VERIFY:
        more()
        sleep(1)
    more()
    print(f"\n{VERIFICATION_OVER}", file=out, flush=True)
