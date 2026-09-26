"""The view's dialogs about tasks and projects: confirmations, replies, the new task and new
project forms, the pickers and the help. The app in tui.py opens them and acts on what they
return. The browser is in browse.py, the provider dialogs in providers_ui.py.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from rich.markup import escape
from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    TextArea,
)

from . import actions, context, ide, panel, repo, version
from .browse import ANY, FOLDER, Browse, shown_path
from .config import ConfigError, load_config, load_project
from .verify_ui import AskVerify
from .widgets import Dialog, EdgeTextArea, Fields, leave_at_edge

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


class DescriptionArea(TextArea):
    """A text area that suggests paths after '@', like Claude Code: a folder's entries, or any path in
    the project that contains what you typed; arrows pick, Tab or Enter take one, Escape closes the
    list."""

    def __init__(self, suggestions: OptionList, cwd: Path, **kwargs):
        super().__init__(**kwargs)
        self.suggestions, self.cwd = suggestions, cwd
        self._repo: Path | None = None
        self.paths: list[str] = []

    @property
    def repo(self) -> Path | None:
        """The selected project's repository: its files are suggested too, and they are the ones
        the agent has in its clone. Its tree is listed once here, not on every keystroke."""
        return self._repo

    @repo.setter
    def repo(self, path: Path | None) -> None:
        self._repo = path
        self.paths = context.project_paths(path) if path else []

    def mention(self) -> str | None:
        row, col = self.cursor_location
        m = MENTION_AT_CURSOR.search(self.document.get_line(row)[:col])
        return m.group(1) if m else None

    def suggest(self) -> None:
        partial = self.mention()
        found = (
            context.complete(partial, self.cwd, repo=self.repo, paths=self.paths)
            if partial is not None
            else []
        )
        self.suggestions.set_options(found)
        self.suggestions.display = bool(found)
        if found:
            self.suggestions.highlighted = 0

    def take(self, choice: str) -> None:
        partial = self.mention() or ""
        row, col = self.cursor_location
        self.replace(choice, (row, col - len(partial)), (row, col))
        if not choice.endswith("/"):
            self.insert(" ")
        self.suggest()  # a directory opens its contents

    async def _on_key(self, event: events.Key) -> None:
        if not self.suggestions.display and leave_at_edge(self, event):
            return
        if self.suggestions.display and event.key in ("up", "down", "tab", "enter", "escape"):
            event.stop()
            event.prevent_default()
            options = self.suggestions
            if event.key == "escape":
                options.display = False
            elif event.key == "up":
                options.action_cursor_up()
            elif event.key == "down":
                options.action_cursor_down()
            elif options.highlighted is not None:
                self.take(str(options.get_option_at_index(options.highlighted).prompt))
            return
        await super()._on_key(event)

    @on(TextArea.Changed)
    def changed(self) -> None:
        self.suggest()


NEW_PROJECT = "+ set up another project…"


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
        self.show_verify()
        self.query_one("#create", Button).label = "Open a task" if self.taken else "Set up"

    def show_verify(self) -> None:
        how = escape(" && ".join(self.verify)) if self.verify else actions.WRITER_PROPOSES
        self.query_one("#verify", Label).update(how)

    def pick_verify(self, choice: dict) -> None:
        if choice:
            self.verify, self.no_build = choice["verify"], choice["no_build"]
            self.show_verify()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse":
            self.app.push_screen(Browse(FOLDER, "Pick the project's folder"), self.use_folder)
        elif event.button.id == "change":
            name = self.query_one("#name", Input).value.strip() or "this project"
            self.app.push_screen(AskVerify(name, self.verify, self.no_build), self.pick_verify)
        elif event.button.id != "create":
            self.dismiss({})
        elif self.taken:
            self.dismiss({"use": self.taken})
        else:
            name = self.query_one("#name", Input).value.strip()
            self.dismiss(
                {"path": str(self.where), "name": name, "verify": self.verify, "no_build": self.no_build}
            )

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


class NewTask(Dialog):
    """A form: labels on the left, one field per row, the description and the closing buttons
    the only boxes. Every field is on the screen at once; a short terminal shrinks the
    description before anything scrolls."""

    def __init__(self, preselect: str = "", available: dict[str, list[str]] | None = None):
        super().__init__()
        self.preselect = preselect
        # The models of the providers you have keys for; None while the view is still asking.
        self.available = available
        self.base_ref = "HEAD"

    def compose(self) -> ComposeResult:
        names = panel.projects()
        chosen = self.preselect if self.preselect in names else names[0]
        with Vertical(classes="dialog form"):
            with Fields(classes="fields"):
                with Vertical(id="task", classes="section"):
                    yield Label("Task", classes="title")
                    # A list even with one project in it: the dialog looks the same however many
                    # you have. What is not on it is set up from it, like a provider from a model list.
                    with Horizontal(classes="row"):
                        yield Label("Project", classes="key")
                        yield Select(
                            [*((n, n) for n in names), (NEW_PROJECT, NEW_PROJECT)],
                            value=chosen, allow_blank=False, compact=True, id="project",
                        )  # fmt: skip
                    with Horizontal(classes="row gap", id="kind-row"):
                        yield Label("Kind", classes="key")
                        yield Select(
                            [("Feature: new behaviour", "feature"), ("Bug: something works wrong", "bug"),
                             ("Other: refactoring, tests, upkeep", "other")],
                            value="feature", allow_blank=False, compact=True, id="kind",
                        )  # fmt: skip
                    with Horizontal(classes="row"):
                        yield Label("Branch", classes="key")
                        yield Button("Current…", compact=True, id="base-ref")
                    with Horizontal(classes="row", id="build-row"):
                        yield Label("Build", classes="key")
                        yield Checkbox(
                            "Nothing to build or test: research, a ticket analysis",
                            compact=True, id="no-build",
                        )  # fmt: skip
                    suggestions = OptionList(id="suggestions")
                    suggestions.display = False
                    with Horizontal(classes="row", id="task-row"):
                        yield Label("Goal", classes="key")
                        yield DescriptionArea(
                            suggestions, Path.cwd(), id="goal", classes="description",
                            placeholder="What should the agent do? A line, or a whole ticket with its"
                            " context and constraints.",
                        )  # fmt: skip
                    yield suggestions
                    # Attach fills the description, so it stands under it, not among the lists.
                    with Horizontal(classes="row"):
                        yield Label("", classes="key")
                        yield Button("Attach…", compact=True, id="attach")
                        yield Label("or @path, for a copy of a file or folder.", classes="hint")
                with Vertical(id="planning", classes="section"):
                    yield Label("Agents", classes="title")
                    # One question, not two boxes that could both be ticked.
                    with Horizontal(classes="row"):
                        yield Label("Plan", classes="key")
                        yield Select(
                            [("Stop for my review of the plan", "review"),
                             ("Accept the agent's plan without stopping (--auto)", "auto"),
                             ("Only create the task, to write the plan myself (--draft)", "draft")],
                            value="review", allow_blank=False, compact=True, id="plan",
                        )  # fmt: skip
                    # Each role on config.toml's choice unless you pick another; m changes it later.
                    # In the order they work: the planner, the writer, then the reviewer.
                    config = load_config()
                    order = {"planner": 0, "writer": 1, "reviewer": 2}
                    for name in sorted(config.roles, key=lambda n: (order.get(n, 9), n)):
                        offered = actions.choices(name, config, self.available)
                        configured = actions.configured_choice(config, name)
                        with Horizontal(classes="row gap"):
                            yield Label(name.capitalize(), classes="key")
                            options = [(actions.choice_label(c, configured), c) for c in offered]
                            yield Select(
                                options, value=configured, allow_blank=False, compact=True,
                                id=f"role-{name}", classes="model",
                            )  # fmt: skip
                    if "reviewer" in config.roles:
                        with Horizontal(classes="row gap"):
                            yield Label("Review", classes="key")
                            yield Select(
                                [("Blocking notes go back to the writer by themselves (loop)", "loop"),
                                 ("Every note comes to me (supervised)", "supervised"),
                                 ("No reviewer for this task", "none")],
                                value=config.review_mode, allow_blank=False, compact=True, id="review",
                            )  # fmt: skip
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")
                yield Label("ctrl+s creates the task", classes="hint keys")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "base-ref":
            from .branches import BranchPicker

            source = load_project(str(self.query_one("#project", Select).value)).repo
            self.app.push_screen(BranchPicker(source, self.base_ref), self.branch_chosen)
            return
        if event.button.id == "attach":
            self.app.push_screen(Browse(ANY, "Attach a file or a folder for the agent"), self.attach)
            return
        if event.button.id != "create":
            self.dismiss({})
            return
        self.dismiss(
            {
                "project": self.query_one("#project", Select).value,
                "goal": self.query_one("#goal", TextArea).text.strip(),
                "kind": self.query_one("#kind", Select).value,
                "auto": self.query_one("#plan", Select).value == "auto",
                "draft": self.query_one("#plan", Select).value == "draft",
                "roles": {
                    s.id.removeprefix("role-"): s.value
                    for s in self.query(".model").results(Select)
                    if s.parent.display  # a task without a review names no reviewer
                },
                "review": self.query_one("#review", Select).value if self.query("#review") else "",
                "no_build": self.query_one("#no-build", Checkbox).value,
                "base_ref": self.base_ref,
            }
        )

    # The dialog's frame, padding and buttons: what the fields leave room for.
    CHROME = 9

    def on_mount(self) -> None:
        self.for_project(str(self.query_one("#project", Select).value))
        self.reviewer_follows_review()
        self.call_after_refresh(self.fit)

    @on(Select.Changed, "#review")
    def review_changed(self, event: Select.Changed) -> None:
        self.reviewer_follows_review()
        self.call_after_refresh(self.fit)

    def reviewer_follows_review(self) -> None:
        """No reviewer for this task: its model row goes, and nothing runs for it."""
        if self.query("#review"):
            reviewing = self.query_one("#review", Select).value != "none"
            self.query_one("#role-reviewer", Select).parent.display = reviewing

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        """The description as tall as the screen leaves after the other rows, a line at least, so
        the whole form stays in view and the description scrolls inside itself. Before the
        description would shrink below three lines, the groups' headings go, then the blank rows
        between the lists of a group."""
        fields = self.query_one(Fields)
        goal = self.query_one("#goal", TextArea)
        dialog = self.query_one(".dialog")
        room = int(self.size.height * 0.9) - self.CHROME
        fields.styles.max_height = max(5, room)
        # Counted, not measured: a measure is the last layout's, whatever class the dialog has
        # been given since. A row is a line; the second group stands a blank row below the first.
        rows = [row for row in self.query(".row") if row.display and row.id != "task-row"]
        others = len(rows) + 1
        gaps = len([row for row in rows if row.has_class("gap")])
        titles = 2 * len(self.query(".title"))  # a heading and the blank row under it
        spare = room - others - 3
        plain, tight = spare < gaps + titles, spare < gaps
        dialog.set_class(plain, "plain")
        dialog.set_class(tight, "tight")
        taken = others + (0 if tight else gaps) + (0 if plain else titles)
        goal.styles.height = max(3, min(12, room - taken))
        goal.focus()

    @on(Select.Changed, "#project")
    def switched(self, event: Select.Changed) -> None:
        if event.value == NEW_PROJECT:
            self.dismiss({"project": NEW_PROJECT})
            return
        self.for_project(str(event.value))

    def for_project(self, name: str) -> None:
        """An empty project has nothing that could work wrong, so what kind of task this is is not asked."""
        self.query_one("#kind-row").display = not actions.empty_project(name)
        self.base_ref = "HEAD"
        self.query_one("#base-ref", Button).label = "Current…"
        with contextlib.suppress(ConfigError):
            self.query_one("#goal", DescriptionArea).repo = load_project(name).repo
            self.load_current_branch(name, load_project(name).repo)

    @work(thread=True, exclusive=True, group="current-branch")
    def load_current_branch(self, project: str, source: Path) -> None:
        try:
            label = actions.current_branch(source)
        except repo.RepoError:
            return  # opening the picker shows the repository error and how to return
        self.app.call_from_thread(self.current_branch_loaded, project, label)

    def current_branch_loaded(self, project: str, label: str) -> None:
        if (
            self.is_mounted
            and self.base_ref == "HEAD"
            and self.query_one("#project", Select).value == project
        ):
            self.query_one("#base-ref", Button).label = f"Current ({label})…"

    def branch_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice:
            label, self.base_ref = choice
            self.query_one("#base-ref", Button).label = label + "…"

    def attach(self, path: Path | None) -> None:
        """The picked file or folder as an @mention, where the cursor is in the description."""
        goal = self.query_one("#goal", DescriptionArea)
        if path is not None:
            goal.insert(f"@{shown_path(path)} ")
        goal.focus()

    def key_ctrl_s(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})


HELP = """[b]Your decisions[/b], on the selected task
  a     accept the plan, or the finished work
  r     reply: reject, ask for changes, or answer the agent
  p     approve changes to risky files
  g     verify a blocked task again, without the agent

[b]The selected task[/b]
  d, Enter  show or hide its details
  e     edit the plan; with a manual planner, paste your chat's plan
  c, C  copy the planning prompt for a chat, or (Shift+c) for a CLI
  o     open the review copy in your IDE
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
  e     its settings: verification, how to run it, java, pass_env, editor
  o     open its repository in your IDE
  x     forget it, once it has no tasks

[b]Anywhere[/b]
  i     set up a project
  k     settings: providers & MCP, roles, editor for o, notifications, limits
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
