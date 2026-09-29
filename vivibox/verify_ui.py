"""How a project is verified, asked the same way everywhere: on the project's screen (e), for a
new project (i), and once you have accepted the work of a task whose writer proposed a command."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Checkbox, Input, Label

from . import init, look, proposal
from .config import by_modules
from .widgets import Dialog

WRITER = "let the writer find the command and propose it after the work"
MODULES = "build only the modules a task changes ({count} modules)"


class AskVerify(Dialog):
    """One command, Enter takes it. Or the box, taken the moment it is ticked: the next task's
    writer finds one and proposes it once its work is done. Escape leaves it as it is."""

    frame_title = "Verification"
    hint_keys = (("enter", "save"), look.ESC_CANCELS)

    def __init__(
        self,
        name: str,
        verify: list[str],
        no_build: bool = False,
        heading: str = "",
        writer_box: bool = True,
        modules: list[str] | tuple = (),
        repo: Path | None = None,
    ):
        super().__init__()
        self.project_name, self.verify, self.no_build = name, verify, no_build
        # From two modules the command can build only the ones a task changes (ADR-0033).
        self.modules = list(modules) if len(modules) >= 2 else []
        # Whose workspaces' scripts say how npm and pnpm narrow a command.
        self.repo = repo
        # At the command checkpoint the writer has just had its say: the box is not a choice.
        self.writer_box = writer_box
        self.heading = heading or (
            f"How {name} is verified: the command that builds it and runs its tests. Enter saves."
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="wrap")
            yield Input(" && ".join(self.verify), id="command", compact=True)
            if self.modules:
                command = " && ".join(self.verify)
                yield Checkbox(
                    MODULES.format(count=len(self.modules)), value=by_modules(command), id="modules"
                )
                yield Label("", id="modules-preview", classes="files wrap")
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
        self.preview()

    def preview(self) -> None:
        """What a task changing the first module would run: the command by modules, made plain."""
        if not self.modules:
            return
        command = self.query_one(Input).value.strip()
        shown = self.query_one("#modules-preview", Label)
        if by_modules(command):
            example = proposal.for_modules([command], self.modules[:1])[0]
            shown.update(f"A task changing {self.modules[0]} runs: {example}")
        else:
            shown.update("Every task builds the whole project.")

    @on(Input.Changed)
    def typed(self, event: Input.Changed) -> None:
        if event.value.strip() and self.writer_box:
            self.query_one("#writer", Checkbox).value = False
        self.preview()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if command := event.value.strip():
            self.dismiss({"verify": [command], "no_build": False})
        elif self.no_build and self.writer_box and not self.query_one("#writer", Checkbox).value:
            self.dismiss({})  # nothing typed and the box left alone: the file stays as it says
        else:
            self.dismiss({"verify": [], "no_build": False})

    @on(Checkbox.Changed, "#modules")
    def by_modules(self, event: Checkbox.Changed) -> None:
        field = self.query_one(Input)
        command = field.value.strip()
        if event.value:
            field.value = init.scoped(command, self.repo) or command
        else:
            field.value = proposal.whole(command)
        self.preview()

    @on(Checkbox.Changed, "#writer")
    def ticked(self, event: Checkbox.Changed) -> None:
        if event.value:
            self.dismiss({"verify": [], "no_build": False})

    def key_escape(self) -> None:
        self.dismiss({})
