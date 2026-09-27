"""The view's dialogs about tasks and projects: confirmations, replies, the new task and new
project forms, the pickers and the help. The app in tui.py opens them and acts on what they
return. The browser is in browse.py, the provider dialogs in providers_ui.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    Static,
    TextArea,
)

from . import actions, ide, panel, version
from .browse import FOLDER, Browse, shown_path
from .verify_ui import AskVerify
from .widgets import Dialog, EdgeTextArea

NO_PROJECTS = """No projects yet, so your agents are sitting idle.

Press i to give them one: a repository you already have, or an empty folder to start a project
from scratch. Then n hands them a task."""


class DeleteTask(Dialog):
    """Deleting a task, or a finished one's line in the history: what goes, what stays. Cancel has
    the focus, so an Enter pressed out of habit deletes nothing."""

    def __init__(self, title: str, about: str, goes: str, stays: str, warning: str = ""):
        super().__init__()
        self.title_text, self.about, self.goes, self.stays, self.warning = title, about, goes, stays, warning

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"[b]{escape(self.title_text)}[/]")
            yield Label(escape(self.about), classes="wrap")
            if self.warning:
                yield Label(f"[yellow]{escape(self.warning)}[/]", classes="wrap")
            yield Label(f"[red]Deleted:[/] {escape(self.goes)}", classes="wrap")
            yield Label(f"[green]Kept:[/] {escape(self.stays)}", classes="wrap")
            with Horizontal(classes="buttons"):
                yield Button("Delete", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#no").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


NOTHING_TO_SEND = "Write a comment first, or leave with Cancel."


class Reply(Dialog):
    def __init__(self, task_id: str, prompt: str = ""):
        super().__init__()
        self.task_id = task_id
        self.prompt = prompt or f"Your comment for the agent on {task_id}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.prompt} (ctrl+s sends):")
            yield EdgeTextArea(id="comment")
            with Horizontal(classes="buttons"):
                yield Button("Send", variant="primary", id="send")
                yield Button("Cancel", id="cancel")

    def send(self) -> None:
        """An empty comment is not sent, and the dialog says so. Closing as if it had been sent
        left you waiting for an agent nobody had told anything."""
        text = self.query_one(TextArea).text
        if text.strip():
            self.dismiss(text)
        else:
            self.notify(NOTHING_TO_SEND, severity="warning")

    def key_ctrl_s(self) -> None:
        self.send()

    def key_escape(self) -> None:
        self.dismiss("")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.send()
        else:
            self.dismiss("")


class ReplyWithCriteria(Dialog):
    """Sending finished or stuck work back: a comment, and criteria for what you found. A remark is
    something the agent may act on; a criterion is something the gate holds the work to."""

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Your comment for the agent on {self.task_id} (ctrl+s sends):")
            yield EdgeTextArea(id="comment")
            yield Label("New acceptance criteria, one per line (optional). The gate checks them like the")
            yield Label("ones you accepted, and the agent ticks them when they are met.")
            yield EdgeTextArea(id="criteria", classes="criteria")
            with Horizontal(classes="buttons"):
                yield Button("Send", variant="primary", id="send")
                yield Button("Cancel", id="cancel")

    def answer(self) -> dict:
        lines = self.query_one("#criteria", TextArea).text.splitlines()
        return {
            "comment": self.query_one("#comment", TextArea).text,
            "criteria": [c for c in lines if c.strip()],
        }

    def send(self) -> None:
        answer = self.answer()
        if answer["comment"].strip() or answer["criteria"]:
            self.dismiss(answer)
        else:
            self.notify(NOTHING_TO_SEND, severity="warning")

    def key_ctrl_s(self) -> None:
        self.send()

    def key_escape(self) -> None:
        self.dismiss({})

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.send()
        else:
            self.dismiss({})


MENTION_AT_CURSOR = re.compile(r"(?:^|\s)@(\S*)$")


class NewProject(Dialog):
    """A repository vivibox does not know yet, or a folder where one should start. How to build and
    test it is yours to type, or left to the first task's writer, who proposes the command it ran;
    what the build files name is only a note."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Folder of the project: where you started vivibox, or another you browse to.")
            with Horizontal(classes="role"):
                yield Label("", id="path", classes="wrap")
                yield Button("Browse…", id="browse")
            yield Label("Name")
            yield Input(id="name")
            yield Label("Verification")
            with Horizontal(classes="role"):
                yield Label("", id="verify", classes="wrap")
                yield Button("Change…", id="change")
            yield Label("Prepare: what a new task's clone runs first, before the writer")
            with Horizontal(classes="role"):
                yield Label("", id="prepare", classes="wrap")
                yield Button("Change…", id="change-prepare")
            yield Label("", id="notes")
            with Horizontal(classes="buttons"):
                yield Button("Set up", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.look(Path.cwd())
        self.query_one("#browse", Button).focus()

    def use_folder(self, where: Path | None) -> None:
        if where is not None:
            self.look(where)
            self.query_one("#create", Button).focus()

    def look(self, where: Path) -> None:
        """Following the folder you point at: its name, and what setting it up would mean."""
        self.where = where
        self.query_one("#path", Label).update(f"[b]{escape(shown_path(where))}[/]")
        found = actions.propose_project(where)
        root = actions.git_root(where)
        self.taken = actions.project_at(root) if root else ""
        self.verify, self.no_build = list(found.verify), False
        self.prepare = list(found.prepare)
        name = self.query_one("#name", Input)
        name.value = self.taken or found.name
        name.disabled = bool(self.taken)
        if self.taken:
            notes = [f"already a project: {self.taken}; Set up opens a task for it instead"]
        elif root:
            notes = [f"repository {root}", *found.notes]
        else:
            notes = [f"a new repository starts in {where}"]
        self.query_one("#notes", Label).update(" · ".join(notes))
        self.query_one("#change", Button).display = not self.taken
        self.query_one("#change-prepare", Button).display = not self.taken
        self.show_verify()
        self.show_prepare()
        self.query_one("#create", Button).label = "Open a task" if self.taken else "Set up"

    def show_verify(self) -> None:
        how = escape(" && ".join(self.verify)) if self.verify else actions.WRITER_PROPOSES
        self.query_one("#verify", Label).update(how)

    def pick_verify(self, choice: dict) -> None:
        if choice:
            self.verify, self.no_build = choice["verify"], choice["no_build"]
            self.show_verify()

    def show_prepare(self) -> None:
        how = escape(" && ".join(self.prepare)) if self.prepare else NOTHING_TO_PREPARE
        self.query_one("#prepare", Label).update(how)

    def typed_prepare(self, value: str | None) -> None:
        if value is not None:
            self.prepare = [value.strip()] if value.strip() else []
            self.show_prepare()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse":
            self.app.push_screen(Browse(FOLDER, "Pick the project's folder"), self.use_folder)
        elif event.button.id == "change":
            name = self.query_one("#name", Input).value.strip() or "this project"
            self.app.push_screen(AskVerify(name, self.verify, self.no_build), self.pick_verify)
        elif event.button.id == "change-prepare":
            from .settings import Ask  # settings builds on this module

            self.app.push_screen(
                Ask(PREPARE_QUESTION, " && ".join(self.prepare), PREPARE_HINT), self.typed_prepare
            )
        elif event.button.id != "create":
            self.dismiss({})
        elif self.taken:
            self.dismiss({"use": self.taken})
        else:
            name = self.query_one("#name", Input).value.strip()
            self.dismiss({
                "path": str(self.where), "name": name, "verify": self.verify, "no_build": self.no_build,
                "prepare": self.prepare,
            })  # fmt: skip

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})


class ChooseEditor(ModalScreen["str | None"]):
    """What to open review copies with, from the editors found here: arrows pick, Enter takes,
    Escape (None) leaves it as it is. first: a row before them, with the command it stands for
    ("" for config.toml's own)."""

    def __init__(self, found: list[ide.Editor], first: tuple[str, str] | None = None):
        super().__init__()
        self.found = found
        self.commands = [e.command for e in found]
        self.labels = [e.label for e in found]
        if first:
            self.labels.insert(0, first[0])
            self.commands.insert(0, first[1])

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Open the review copy with:")
            yield OptionList(*self.labels, id="editors")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.commands[event.option_index])

    def key_escape(self) -> None:
        self.dismiss(None)


class ChooseSession(ModalScreen[str]):
    """Which conversation w opens when the task has two. Arrows pick, Enter takes, Escape leaves."""

    def __init__(self, rows: list[tuple[str, str]]):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Look at:")
            yield OptionList(*[f"{role}  {doing}" for role, doing in self.rows], id="sessions")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.rows[event.option_index][0])

    def key_escape(self) -> None:
        self.dismiss("")


class ChooseRole(ModalScreen[str]):
    """Which role to put on another model for this task. Arrows pick, Enter takes, Escape leaves."""

    def __init__(self, rows: list[tuple[str, str, bool]]):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Run this task's roles on:")
            labels = [f"{name}  {model}" + ("  (this task)" if own else "") for name, model, own in self.rows]
            yield OptionList(*labels, id="roles")
            yield Label("A change applies the next time the task starts.")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.rows[event.option_index][0])

    def key_escape(self) -> None:
        self.dismiss("")


class ChooseModel(ModalScreen["actions.Choice | None"]):
    """What one role runs on, from what you can run: planning yourself, or a model of a provider
    you have a key for. None leaves it alone; config.toml's own choice gives the role back to it."""

    def __init__(self, role: str, offered: list, configured, current, available: dict | None = None):
        super().__init__()
        self.role, self.offered, self.configured, self.current = role, offered, configured, current
        self.available = available

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Run {self.role} on:")
            labels = [
                actions.choice_label(c, self.configured, self.available)
                + ("  ← now" if c == self.current else "")
                for c in self.offered
            ]
            yield OptionList(*labels, id="models")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.offered[event.option_index])

    def key_escape(self) -> None:
        self.dismiss(None)


NEW_PROJECT = "+ set up another project…"


# The words for prepare, the same under i and under e on the project.
NOTHING_TO_PREPARE = "nothing; the writer builds what it needs"
PREPARE_QUESTION = (
    "What to run once in a new task's clone while the plan is made; the writer's first turn waits for it:"
)
PREPARE_HINT = "A build without tests, so the writer builds one module at a time. Empty: nothing."


HELP = """[b]Your decisions[/b], on the selected task
  a     accept the plan, or the finished work
  r     reply: reject, ask for changes, or answer the agent
  p     approve changes to risky files
  g     verify a blocked task again, without the agent

[b]The selected task[/b]
  d, Enter  show or hide its details
  e     edit the plan; with a manual planner, paste your chat's plan
  c, C  copy the planning prompt for a chat, or (Shift+c) for a CLI
  o     open the review copy in your IDE / text editor
  v     run the app in its pod, or stop it
  w     watch or talk to the agent; while verifying, its log (Ctrl-q leaves);
        with two conversations, asks which; in a box, a shell in it
  l     the newest log in your pager: followed while the verification runs,
        else opened at its end
  s     stop the task, or start it again
  S     Shift+s: stop it by force when s hangs; the turn under way is lost
  m     what each role runs on, for this task
  x     delete the task; on a finished one, its line in the history

[b]The selected project[/b] (Enter folds or unfolds its tasks)
  n     new task in it
  b     open a box: its pod for you to work in by hand, opencode included
  e     its settings: verification, run, java, pass_env, IDE / text editor
  o     open its repository in your IDE / text editor
  x     forget it, once it has no tasks

[b]Anywhere[/b]
  i     set up a project
  k     settings: providers & MCP, roles, orchestration, editor, ntfy, limits
  u     usage: how long each role and the verification took, per task
  h     show or hide the tasks you accepted
  H     Shift+h: show or hide the tasks you deleted (hidden to start with)
  q     quit
"""


class Help(ModalScreen):
    """Every key, and when it does something. The footer shows only what applies right now.
    note: a line about this machine, such as what o opens with."""

    def __init__(self, note: str = ""):
        super().__init__()
        self.note = note

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog help"):
            # The build first, where it is seen without scrolling: a bug report starts with it.
            yield Label(f"vivibox {version.current()}", classes="files")
            yield Static(HELP, id="help")
            if self.note:
                yield Label(self.note, id="note")
            yield Label("Esc closes.", classes="files")

    def key_escape(self) -> None:
        self.dismiss()

    def key_question_mark(self) -> None:
        self.dismiss()


class CommitWork(Dialog):
    """After accepting: the work is staged in your checkout; commit it now, or leave it for your IDE."""

    def __init__(self, done: actions.Finished):
        super().__init__()
        self.done = done
        self.branch = actions.current_branch(done.source)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.done.task_id} is done. Its work is uncommitted in {self.done.source}:")
            yield Label(self.done.status.rstrip() or "(no changes)", classes="files")
            warn = " (your main branch)" if self.branch in panel.PROTECTED_BRANCHES else ""
            yield Label(f"Commit to {self.branch}{warn} with this message? (ctrl+s commits)")
            yield EdgeTextArea(self.done.message, id="message")
            with Horizontal(classes="buttons"):
                # On a main branch the safe choice comes first.
                commit = Button("Commit", variant="primary", id="commit")
                later = Button("Leave uncommitted", id="later")
                yield from ((later, commit) if warn else (commit, later))

    def on_mount(self) -> None:
        self.query_one("#later" if self.branch in panel.PROTECTED_BRANCHES else "#message").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(TextArea).text if event.button.id == "commit" else "")

    def key_ctrl_s(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def key_escape(self) -> None:
        self.dismiss("")
