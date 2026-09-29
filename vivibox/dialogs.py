"""The view's dialogs about tasks and projects: confirmations, replies, the new task and new
project forms, the pickers and the help. The app in tui.py opens them and acts on what they
return. The browser is in browse.py, the provider dialogs in providers_ui.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.console import Group
from rich.markup import escape
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from . import actions, ide, look, version
from .browse import FOLDER, Browse, shown_path
from .verify_ui import AskVerify
from .widgets import Choose, ContextHelp, Dialog, EdgeTextArea

NO_PROJECTS = """No projects yet, so your agents are sitting idle.

Press i to give them one: a repository you already have, or an empty folder to start a project
from scratch. Then n hands them a task."""


class DeleteTask(Dialog):
    """Deleting a task, or a finished one's line in the history: what goes, what stays. Cancel has
    the focus, so an Enter pressed out of habit deletes nothing."""

    def __init__(self, title: str, about: str, goes: str, stays: str, warning: str = ""):
        super().__init__()
        self.title_text, self.about, self.goes, self.stays, self.warning = title, about, goes, stays, warning

    hint_keys = (("enter", "choose"), look.ESC_CANCELS)

    def compose(self) -> ComposeResult:
        self.frame_title = self.title_text
        with Vertical(classes="dialog narrow"):
            yield Label(escape(self.about), classes="wrap")
            if self.warning:
                yield Label(look.colored(self.warning, look.WAITING), classes="wrap spaced")
            # What goes is read, not metadata: in the foreground, a row apart.
            yield Label(f"[b {look.ERROR}]Deleted[/]  {escape(self.goes)}", classes="wrap spaced")
            yield Label(f"[b {look.SUCCESS}]Kept[/]     {escape(self.stays)}", classes="wrap")
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
    hint_keys = (("ctrl+s", "send"), look.ESC_CANCELS)

    def __init__(self, task_id: str, prompt: str = ""):
        super().__init__()
        self.task_id = task_id
        self.frame_title = f"Reply · {task_id}"
        self.prompt = prompt or f"Your comment for the agent on {task_id}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.prompt}:", classes="wrap")
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

    hint_keys = (("ctrl+s", "send"), look.ESC_CANCELS)

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id
        self.frame_title = f"Reply · {task_id}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Your comment for the agent on {self.task_id}:", classes="wrap")
            yield EdgeTextArea(id="comment")
            yield Label("New acceptance criteria, one per line (optional)", classes="wrap files")
            yield Label(
                "The verification checks them like the ones you accepted, and the agent ticks them when "
                "they are met.",
                classes="wrap note",
            )
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

    frame_title = "New project"
    hint_keys = (("enter", "set up"), look.ESC_CANCELS)
    field_help = {
        "browse": "The project's folder: a repository you already have, or an empty folder to start "
        "one in. It starts on the folder vivibox was started in.",
        "name": "What the list, n and the command line call the project.",
        "change": "The command that proves a task's work: it runs on a fresh clone after every turn "
        "of the writer. Left empty, the first task's writer proposes one for you to accept.",
        "change-prepare": "What a new task's clone runs once, before the writer's first turn: "
        "usually a build without tests.",
        "create": "",
        "cancel": "",
    }

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog form"):
            with Vertical(classes="section first"):
                yield Label("PROJECT", classes="title")
                with Horizontal(classes="row"):
                    yield Label("Folder", classes="key")
                    yield Label("", id="path", classes="value")
                    yield Button("Browse…", compact=True, id="browse", classes="inline")
                with Horizontal(classes="row"):
                    yield Label("Name", classes="key")
                    yield Input(id="name", compact=True)
            with Vertical(classes="section"):
                yield Label("FOR ITS TASKS", classes="title")
                with Horizontal(classes="row"):
                    yield Label("Verification", classes="key")
                    yield Label("", id="verify", classes="value")
                    yield Button("Change…", compact=True, id="change", classes="inline")
                with Horizontal(classes="row"):
                    yield Label("Preparation", classes="key")
                    yield Label("", id="prepare", classes="value")
                    yield Button("Change…", compact=True, id="change-prepare", classes="inline")
            yield Label("", id="notes", classes="files wrap")
            yield ContextHelp(3, id="about")
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
        self.query_one("#path", Label).update(escape(shown_path(where)))
        found = actions.propose_project(where)
        root = actions.git_root(where)
        self.taken = actions.project_at(root) if root else ""
        self.verify, self.no_build = list(found.verify), False
        self.modules = list(found.modules)
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
        if any("{modules}" in c for c in self.verify):
            how += " · only the modules a task changes"
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
            self.app.push_screen(
                AskVerify(name, self.verify, self.no_build, modules=self.modules), self.pick_verify
            )
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


class ChooseEditor(Choose):
    """What to open review copies with, from the editors found here. Dismisses with its command;
    None leaves it as it is. first: a row before them, with the command it stands for ("" for
    config.toml's own)."""

    def __init__(self, found: list[ide.Editor], first: tuple[str, str] | None = None):
        rows = [(e.label, "") for e in found]
        self.commands = [e.command for e in found]
        if first:
            rows.insert(0, (first[0], ""))
            self.commands.insert(0, first[1])
        super().__init__("Open the review copy with", rows)

    def picked(self, index: int) -> str:
        return self.commands[index]


class ChooseSession(Choose):
    """Which conversation w opens when the task has two."""

    def __init__(self, rows: list[tuple[str, str]]):
        super().__init__("Look at", rows, none="")
        self.rows = rows

    def picked(self, index: int) -> str:
        return self.rows[index][0]


class ChooseRole(Choose):
    """Which role to put on another model for this task."""

    def __init__(self, rows: list[tuple[str, str, bool]]):
        shown = [(name, model + ("  (this task)" if own else "")) for name, model, own in rows]
        super().__init__(
            "Run this task's roles on", shown, note="A change applies the next time the task starts.", none=""
        )
        self.rows = rows

    def picked(self, index: int) -> str:
        return self.rows[index][0]


class ChooseModel(Choose):
    """What one role runs on, from what you can run: planning yourself, or a model of a provider
    you have a key for. None leaves it alone; config.toml's own choice gives the role back to it."""

    def __init__(self, role: str, offered: list, configured, current, available: dict | None = None):
        rows = [
            (actions.choice_label(c, configured, available), "now" if c == current else "") for c in offered
        ]
        start = offered.index(current) if current in offered else 0
        super().__init__(f"Run the {role} on", rows, start=start)
        self.offered = offered

    def picked(self, index: int):
        return self.offered[index]


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
  u     usage: time and cost of each role and the verification, per task
  h     show or hide the tasks you accepted
  H     Shift+h: show or hide the tasks you deleted (hidden to start with)
  q     quit

[b]Costs[/b] (in the list, the details, u and stats)
  $0.41      spent on your API keys
  $0.41 sub  what your agent CLI used on the task on your subscription, priced
        at API list prices; never added to money spent; ≥ when a model had
        no price
"""


def help_sections(note: str = "") -> list[tuple[str, list[tuple[str, str]]]]:
    """HELP as its sections: a heading and its (keys, what they do), a line that goes on
    joined to the one before it. note: what o opens with here, "o …", a section of its own."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for line in HELP.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):
            sections.append((line, []))
        elif m := re.match(r"^  (\S(?:[^ ]| (?=\S))*)\s{2,}(.*)$", line):
            sections[-1][1].append((m.group(1), m.group(2)))
        else:
            keys, said = sections[-1][1][-1]
            sections[-1][1][-1] = (keys, f"{said} {line.strip()}")
    if note:
        key, _, said = note.partition(" ")
        sections.append(("[b]On this machine[/b]", [(key, said)]))
    return sections


def help_text(note: str = "") -> Group:
    """The keys in the accent colour in a column of their own, what they do beside them and
    wrapped under themselves, the headings muted, as keys and headings are everywhere else."""
    parts = []
    for i, (heading, rows) in enumerate(help_sections(note)):
        heading = heading.replace("[b]", f"[b {look.MUTED}]").replace("[/b]", "[/]")
        parts.append(Text.from_markup(("\n" if i else "") + heading))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True, min_width=5)
        grid.add_column()
        for keys, said in rows:
            # The costs' legend is figures, drawn as the list draws them; the rest are keys.
            style = (
                look.SUBSCRIPTION
                if keys.endswith(" sub")
                else look.SECONDARY
                if keys.startswith("$")
                else f"bold {look.ACCENT}"
            )
            grid.add_row(Text(keys, style=style), Text(said))
        parts.append(Padding(grid, (0, 0, 0, 2)))
    return Group(*parts)


class Help(Dialog):
    """Every key, and when it does something. The footer shows only what applies right now.
    note: a line about this machine, such as what o opens with."""

    frame_title = "Keys"

    def __init__(self, note: str = ""):
        super().__init__()
        self.note = note

    def compose(self) -> ComposeResult:
        # The keys scroll inside the dialog; the row of keys that closes it stays under them.
        with Vertical(classes="dialog help"), VerticalScroll(classes="help-body"):
            # The build first, where it is seen without scrolling: a bug report starts with it.
            yield Label(f"vivibox {version.current()}", classes="note")
            # What o opens with is a section of the help, wrapped: a long command on a line of
            # its own was cut at the dialog's edge.
            yield Static(help_text(self.note), id="help")

    def key_escape(self) -> None:
        self.dismiss()

    def key_question_mark(self) -> None:
        self.dismiss()


class CommitWork(Dialog):
    """After accepting: the work is staged in your checkout; commit it now, on the branch the task
    started on or on a new one, or leave it for your IDE. Dismisses with the message, the branch
    and whether to create it; {} leaves it uncommitted."""

    hint_keys = (("ctrl+s", "commit"), ("esc", "leave uncommitted"))

    def __init__(self, done: actions.Finished):
        super().__init__()
        self.done = done

    def compose(self) -> ComposeResult:
        choices = actions.branch_choices(self.done)
        self.frame_title = f"Commit · {self.done.task_id}"
        with Vertical(classes="dialog form plain"):
            yield Label(
                f"{self.done.task_id} is done. Its work is uncommitted in {self.done.source}:", classes="wrap"
            )
            yield Label(self.done.status.rstrip() or "(no changes)", classes="files")
            # A row apart from the files above it: the form starts here.
            with Horizontal(classes="row spaced"):
                yield Label("Branch", classes="key")
                yield Select(
                    choices, value=actions.default_branch(self.done) if choices else Select.BLANK,
                    allow_blank=not choices, compact=True, id="branch",
                )  # fmt: skip
            with Horizontal(classes="row spaced text-row"):
                yield Label("Message", classes="key")
                yield EdgeTextArea(self.done.message, id="message")
            with Horizontal(classes="buttons"):
                yield Button("Commit", variant="primary", id="commit")
                yield Button("Leave uncommitted", id="later")

    def on_mount(self) -> None:
        self.query_one("#branch").focus()

    def answer(self) -> dict:
        chosen = self.query_one("#branch", Select).value
        branch = {"start": self.done.start_branch, "new": self.done.new_branch}.get(chosen, "")
        return {"message": self.query_one(TextArea).text, "branch": branch, "create": chosen == "new"}

    def commit(self) -> None:
        answer = self.answer()
        if not answer["message"].strip():  # stays open: nothing is lost by saying so
            self.notify("A commit needs a message.", severity="error")
            return
        self.dismiss(answer)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "commit":
            self.commit()
        else:
            self.dismiss({})

    def key_ctrl_s(self) -> None:
        self.commit()

    def key_escape(self) -> None:
        self.dismiss({})
