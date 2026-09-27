"""The u screen: how long each role and the verification took, one task a row, and what each
live task's pod uses now. Read in a thread of the screen's own, the times first and Docker's
figures when they come, and again every ten seconds while it is open: the list behind it never
waits for Docker, and nothing is measured once it is closed.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Label

from . import usage
from .widgets import Dialog

REFRESH_SECONDS = 10.0


class Usage(Dialog):
    """finished: the accepted and deleted tasks too, as the list shows them after h."""

    def __init__(self, finished: bool = False):
        super().__init__()
        self.finished = finished
        self.measured = False

    def compose(self) -> ComposeResult:
        self.frame_title = "Usage · how long each role and the verification took"
        with Vertical(classes="dialog usage"):
            table = DataTable(id="usage", cursor_type="row")
            table.add_columns(*usage.COLUMNS)
            yield table
            yield Label("", id="usage-problem", classes="files")
            yield Label("Reading the tasks' events…", id="usage-note", classes="files")

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()
        self.load()
        self.timer = self.set_interval(REFRESH_SECONDS, self.load)

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        if not self.measured:  # the first time: the times at once, without waiting for Docker
            self.app.call_from_thread(self.show, usage.gather(finished=self.finished), [])
        problems: list[str] = []
        rows = usage.gather(finished=self.finished, measure=True, problems=problems)
        self.measured = True
        if self.is_attached:
            self.app.call_from_thread(self.show, rows, problems)

    def show(self, rows: list[usage.Usage], problems: list[str]) -> None:
        if not self.is_attached:
            return
        table = self.query_one(DataTable)
        table.clear()
        for row in rows:
            table.add_row(*usage.cells(row), key=row.task)
        # The same tasks as the list: the finished ones only while it shows them; and why the
        # pods' figures are dashes, when they are: without it dashes read as pods doing nothing.
        which = "Live and finished tasks, as the list shows them" if self.finished else "Live tasks"
        self.query_one("#usage-note", Label).update(f"{which if rows else 'No tasks yet'}. Esc closes.")
        problem = self.query_one("#usage-problem", Label)
        problem.update(f"CPU, RAM and DISK not measured: {problems[0][:70]}" if problems else "")
        problem.display = bool(problems)

    def key_escape(self) -> None:
        self.timer.stop()  # nothing is measured once the screen is closed
        self.dismiss()
