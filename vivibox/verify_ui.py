"""How a project is verified, asked the same way everywhere: on the project's screen (e), for a
new project (i), and once you have accepted the work of a task whose writer proposed a command."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Checkbox, Input, Label

from . import look
from .widgets import Dialog

WRITER = "let the writer find the command and propose it after the work"


class AskVerify(Dialog):
    """One command, Enter takes it. Or the box, taken the moment it is ticked: the next task's
    writer finds one and proposes it once its work is done. Escape leaves it as it is."""

    frame_title = "Verification"
    hint_keys = (("enter", "save"), look.ESC_CANCELS)

    def __init__(
        self, name: str, verify: list[str], no_build: bool = False, heading: str = "", writer_box: bool = True
    ):
        super().__init__()
        self.project_name, self.verify, self.no_build = name, verify, no_build
        # At the command checkpoint the writer has just had its say: the box is not a choice.
        self.writer_box = writer_box
        self.heading = heading or (
            f"How {name} is verified: the command that builds it and runs its tests. Enter saves."
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="wrap")
            yield Input(" && ".join(self.verify), id="command", compact=True)
            if self.writer_box:
                yield Checkbox(WRITER, value=not self.verify and not self.no_build, id="writer")
            if self.no_build:
                yield Label(
                    "The project file says there is nothing to build (verify = false); a command, "
                    "or the box, replaces that.",
                    classes="files wrap",
                )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Changed)
    def typed(self, event: Input.Changed) -> None:
        if event.value.strip() and self.writer_box:
            self.query_one(Checkbox).value = False

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if command := event.value.strip():
            self.dismiss({"verify": [command], "no_build": False})
        elif self.no_build and self.writer_box and not self.query_one(Checkbox).value:
            self.dismiss({})  # nothing typed and the box left alone: the file stays as it says
        else:
            self.dismiss({"verify": [], "no_build": False})

    @on(Checkbox.Changed)
    def ticked(self, event: Checkbox.Changed) -> None:
        if event.value:
            self.dismiss({"verify": [], "no_build": False})

    def key_escape(self) -> None:
        self.dismiss({})
