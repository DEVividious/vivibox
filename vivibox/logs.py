"""What l opens: the task's timeline, its verification logs newest first, and the supervisor's
log last, as diagnostics. One entry opens at once; while a verification runs its log is the
entry the cursor is on, followed as it is written."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList

from . import prepare, reviewing, timeline
from .panel import pager_command
from .states import State
from .task import Task, TaskState

COMMAND = re.compile(r"^\$ (.+)$", re.MULTILINE)
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


def entries(task: Task, st: TaskState, running: bool) -> tuple[list[Entry], int]:
    """What l offers, and which entry the cursor starts on: the log being written during a
    verification, else the timeline."""
    found = [
        Entry("timeline", "what happened, oldest first", pager_command(timeline.write(task), at_end=True))
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
    return found, 1 if verifying and logs else 0


class ChooseLog(ModalScreen["list[str] | None"]):
    """Which log to read. Arrows pick, Enter opens it in your pager, Escape leaves."""

    def __init__(self, found: list[Entry], start: int = 0):
        super().__init__()
        self.found, self.start = found, start

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Read:")
            yield OptionList(*[f"{e.label}  [dim]{e.said}[/]" for e in self.found], id="logs")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = self.start
        options.focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.found[event.option_index].command)

    def key_escape(self) -> None:
        self.dismiss(None)
