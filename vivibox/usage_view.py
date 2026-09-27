"""The u screen: how long each role and the verification took, one task a row, and what each
live task's pod uses now. Read in a thread of the screen's own, the times first and Docker's
figures when they come, and again every ten seconds while it is open: the list behind it never
waits for Docker, and nothing is measured once it is closed.
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label

from . import usage

REFRESH_SECONDS = 10.0


class Usage(ModalScreen):
    """finished: the accepted and deleted tasks too, as the list shows them after h."""

    def __init__(self, finished: bool = False):
        super().__init__()
        self.finished = finished
        self.measured = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog usage"):
            yield Label("Usage: how long each role and the verification took", classes="title")
            table = DataTable(id="usage", cursor_type="row", zebra_stripes=True)
            table.add_columns(*usage.COLUMNS)
            yield table
            yield Label("Reading the tasks' events…", id="usage-note", classes="files")

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()
        self.load()
        self.set_interval(REFRESH_SECONDS, self.load)

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        if not self.measured:  # the first time: the times at once, without waiting for Docker
            self.app.call_from_thread(self.show, usage.gather(finished=self.finished))
        rows = usage.gather(finished=self.finished, measure=True)
        self.measured = True
        if self.is_attached:
            self.app.call_from_thread(self.show, rows)

    def show(self, rows: list[usage.Usage]) -> None:
        if not self.is_attached:
            return
        table = self.query_one(DataTable)
        table.clear()
        for row in rows:
            table.add_row(*usage.cells(row), key=row.task)
        # The same tasks as the list: the finished ones only while it shows them.
        which = "Live and finished tasks, as the list shows them" if self.finished else "Live tasks"
        self.query_one("#usage-note", Label).update(f"{which if rows else 'No tasks yet'}. Esc closes.")

    def key_escape(self) -> None:
        self.dismiss()
