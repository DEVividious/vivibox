"""The view's dialogs: confirmations, replies, the new task and new project forms, the
pickers, and the browser. The app in tui.py opens them and acts on what they return.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from rich.markup import escape
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from . import actions, context, ide, keys, panel, providers, ui
from . import init as project_init
from .config import ConfigError, load_config, load_project

NO_PROJECTS = """No projects yet, so your agents are sitting idle.

Press i to give them one: a repository you already have, or an empty folder to start a project
from scratch. Then n hands them a task."""


class Fields(VerticalScroll, can_focus=False, inherit_bindings=False):
    """A dialog's fields, scrolling when the terminal is short. No keys of its own: the arrows move
    between fields, as everywhere in a dialog, and scrolling follows the field you are on."""


class Dialog(ModalScreen):
    """Arrow keys move between fields and buttons wherever the focused field does not use them
    itself: left and right move the cursor in a text field, up and down open a list."""

    BINDINGS = [
        Binding("up", "app.focus_previous", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("right", "app.focus_next", show=False),
    ]


class Confirm(Dialog):
    """A yes or no. Red is for a yes that destroys or interrupts something; agreeing to go on is not
    a warning."""

    def __init__(self, question: str, yes: str = "Yes", destructive: bool = False):
        super().__init__()
        self.question, self.yes, self.destructive = question, yes, destructive

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="error" if self.destructive else "primary", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


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


def leave_at_edge(area: TextArea, event: events.Key) -> bool:
    """Up on the first line or down on the last moves on to the previous or next field."""
    row, _ = area.cursor_location
    if (event.key == "up" and row == 0) or (event.key == "down" and row == area.document.line_count - 1):
        event.stop()
        event.prevent_default()
        (area.screen.focus_previous if event.key == "up" else area.screen.focus_next)()
        return True
    return False


class EdgeTextArea(TextArea):
    async def _on_key(self, event: events.Key) -> None:
        if not leave_at_edge(self, event):
            await super()._on_key(event)


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
    test it is detected from its build files, with what its pipeline runs a pick away, or left to
    the first plan you accept."""

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
        self.candidates = project_init.candidates(root or where)
        self.verify, self.source, self.no_build = list(found.verify), found.source, False
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
        if self.no_build:
            how = actions.NO_BUILD
        elif self.verify:
            how = f"{escape(self.verify[0])}  [dim]from {escape(self.source)}[/]"
        else:
            how = "the first plan you accept decides"
        self.query_one("#verify", Label).update(how)

    def pick_verify(self, choice: dict) -> None:
        if not choice:
            return
        self.verify, self.no_build = choice["verify"], choice["no_build"]
        self.source = (
            dict((c, s) for c, s in self.candidates).get(self.verify[0], "you") if self.verify else ""
        )
        self.show_verify()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse":
            self.app.push_screen(Browse(FOLDER, "Pick the project's folder"), self.use_folder)
        elif event.button.id == "change":
            name = self.query_one("#name", Input).value.strip() or "this project"
            picker = ChooseVerify(name, self.verify, self.no_build, self.candidates, exists=False)
            self.app.push_screen(picker, self.pick_verify)
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


class ChooseVerify(ModalScreen[dict]):
    """How a project is verified: a command its build files or its pipeline name, no build, one of
    your own, or the file itself for the rest of it. Before the project exists (from i), the file
    is not offered, and leaving it to the first plan is. It opens on what is set now, marked, so
    Enter keeps it. Arrows pick, Enter takes, Escape leaves it as it is."""

    EDIT = "edit the project file in your editor, for pass_env and host services too…"
    PLAN_DECIDES = "leave it to the first plan you accept"
    NOW = "  ← now"

    def __init__(
        self,
        name: str,
        verify: list[str],
        no_build: bool,
        candidates: list[tuple[str, str]],
        exists: bool = True,
    ):
        super().__init__()
        self.project_name, self.verify, self.no_build = name, verify, no_build
        self.candidates, self.exists = candidates, exists

    def rows(self) -> list[tuple[str, dict]]:
        """Each choice as its label and what taking it means, the current one marked. A command of
        your own, from the file or typed in, is a row of its own; undecided is a row when it is
        what the project is at."""
        rows: list[tuple[str, dict]] = [
            (f"{escape(c)}  [dim]from {escape(source)}[/]", {"verify": [c], "no_build": False})
            for c, source in self.candidates
        ]
        if self.verify and self.verify[0] not in {c for c, _ in self.candidates}:
            source = "the project file" if self.exists else "you"
            rows.append(
                (
                    f"{escape(self.verify[0])}  [dim]from {source}[/]",
                    {"verify": self.verify, "no_build": False},
                )
            )
        rows.append((escape(actions.NO_BUILD), {"verify": [], "no_build": True}))
        undecided = not self.verify and not self.no_build
        if not self.exists or undecided:
            rows.append((self.PLAN_DECIDES, {"verify": [], "no_build": False}))
        if self.exists:
            rows.append((self.EDIT, {"edit": True}))
        return [(label + (self.NOW if choice == self.now() else ""), choice) for label, choice in rows]

    def now(self) -> dict:
        return {"verify": self.verify, "no_build": self.no_build}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"How {self.project_name} is verified:")
            yield OptionList(*(label for label, _ in self.rows()), id="choices")
            with Horizontal(classes="role"):
                yield Label("Other")
                yield Input(placeholder="a command of your own; Enter takes it", id="other")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = next(
            (i for i, (_, choice) in enumerate(self.rows()) if choice == self.now()), 0
        )
        options.focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.rows()[event.option_index][1])

    @on(Input.Submitted)
    def typed(self, event: Input.Submitted) -> None:
        if command := event.value.strip():
            self.dismiss({"verify": [command], "no_build": False})

    def key_escape(self) -> None:
        self.dismiss({})


class ChooseEditor(ModalScreen[str]):
    """What to open review copies with, asked once: arrows pick, Enter takes, Escape leaves it."""

    def __init__(self, found: list[ide.Editor]):
        super().__init__()
        self.found = found

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Open the review copy with:")
            options = OptionList(*[e.label for e in self.found], id="editors")
            yield options
            yield Label("Your choice is kept in config.toml; change it there, or per project.")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.found[event.option_index].command)

    def key_escape(self) -> None:
        self.dismiss("")


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

    def __init__(self, role: str, offered: list, configured, current):
        super().__init__()
        self.role, self.offered, self.configured, self.current = role, offered, configured, current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Run {self.role} on:")
            labels = [
                actions.choice_label(c, self.configured) + ("  ← now" if c == self.current else "")
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


def find_providers(catalog: list[tuple[str, str]], typed: str) -> list[tuple[str, str]]:
    """The providers whose name or id holds what you typed, as (id, label). What you typed is
    offered as a name of its own too, for a provider the list lacks or a list that could not be read."""
    text = typed.strip().casefold()
    found = [(pid, f"{escape(name)}  [dim]{escape(pid)}[/]") for pid, name in catalog
             if text in pid.casefold() or text in name.casefold()]  # fmt: skip
    if text and text not in {pid for pid, _ in found}:
        found.append((text, f"use “{escape(text)}” as the provider's name"))
    return found


STATUS = {
    "replaces": "  [yellow]differs from yours: tick to overwrite[/]",
    "same": "  [dim]same as yours[/]",
    "builtin": "  comes with vivibox; set its mode in Manage",
}


def import_label(f: providers.Found) -> str:
    """One line per provider or server, short enough that what it says of yours stays in view."""
    if f.status == "builtin":  # greyed whole: there is nothing here to choose
        return f"[dim]{escape(f.name)}  {escape(ui.shorten(f.what, 40))}{STATUS['builtin']}[/]"
    what, key = escape(ui.shorten(f.what, 48)), escape(ui.shorten(f.key, 30))
    return f"{escape(f.name)}  {what}, key {key}{STATUS.get(f.status, '')}"


class ChooseImport(Dialog):
    """What an opencode configuration brings, in two groups, providers and MCP servers. New ones are
    ticked; one that would replace yours is not, for you to decide; one you already have is shown
    and cannot be picked. Dismisses with those you keep ticked, or [] when you leave."""

    def __init__(self, source: Path, reading: providers.Reading):
        super().__init__()
        self.source, self.reading = source, reading

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"In {escape(shown_path(self.source))}; untick what to leave out.")
            for kind, heading in ((providers.PROVIDER, "Providers"), (providers.MCP, "MCP servers")):
                rows = [
                    Selection(
                        import_label(f),
                        i,
                        f.status == "new",
                        disabled=f.status in ("same", "builtin"),
                    )
                    for i, f in enumerate(self.reading.found)
                    if f.kind == kind
                ]
                if rows:
                    yield Label(heading, classes="group")
                    yield SelectionList[int](*rows, id=f"found-{kind}", classes="found")
            if self.reading.left:
                yield Label(f"Left in the file, not for vivibox: {', '.join(self.reading.left)}.")
            with Horizontal(classes="buttons"):
                yield Button("Import", variant="primary", id="import")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query(".found").first().focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        ticked = []
        if event.button.id == "import":
            for found in self.query(".found").results(SelectionList):
                ticked += found.selected
        self.dismiss([self.reading.found[i] for i in sorted(ticked)])

    def key_escape(self) -> None:
        self.dismiss([])


def shown_path(path: Path) -> str:
    home = str(Path.home())
    return str(path).replace(home, "~", 1) if str(path).startswith(home + "/") else str(path)


def judge(path: Path) -> tuple[bool, str]:
    """Whether a file is an opencode configuration vivibox can bring something over from, said."""
    try:
        reading = providers.read_opencode(path)
    except ConfigError as e:
        text = e.args[0]
        if "is not JSON" in text:
            return False, "broken: not JSON" + (f" ({text.split(': ', 1)[1]})" if ": " in text else "")
        return False, "JSON, but not an opencode configuration with providers or MCP servers"
    parts = []
    for kind, what in ((providers.PROVIDER, "provider"), (providers.MCP, "MCP server")):
        if n := sum(f.kind == kind for f in reading.found):
            parts.append(f"{n} {what}{'s' * (n != 1)}")
    return True, "opencode configuration: " + ", ".join(parts)


# Folders nobody picks anything from, and which would bury what they do pick.
SKIPPED = {".git", "node_modules", "__pycache__", ".venv", ".cache", ".npm", ".m2", ".gradle"}
JSON, FOLDER, ANY = "json", "folder", "any"


def is_repository(path: Path) -> bool:
    return (path / ".git").exists()


def browse_start() -> Path:
    """Where browsing starts: home, where your projects and downloads are."""
    return Path.home()


class PathTree(DirectoryTree):
    """Your folders, and in them only what can be picked: JSON files, folders, or anything. What can
    be picked is marked; hidden folders are dimmed."""

    def __init__(self, path: Path, mode: str, **kwargs):
        self.mode = mode
        super().__init__(path, **kwargs)

    def filter_paths(self, paths):
        return [p for p in paths if shows(self.mode, p)]

    async def _on_click(self, event: events.Click) -> None:
        """A click marks, and opens or closes a folder; it picks nothing. Picking is Enter, Select,
        or a double click: a single one picked whatever the mouse passed over."""
        event.prevent_default()  # the tree's own handler would select, and so pick, on every click
        meta = event.style.meta
        if "line" not in meta:
            return
        self.cursor_line = meta["line"]
        node = self.cursor_node
        if event.chain >= 2:
            self.action_select_cursor()
        elif node is not None and node.allow_expand:
            node.toggle()

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        path = node.data.path if node.data else None
        if path is None:
            return label
        if (self.mode == JSON and path.is_file()) or (self.mode == FOLDER and is_repository(path)):
            label.stylize("bold green")
        elif path.name.startswith("."):
            label.stylize("dim")
        return label


def shows(mode: str, path: Path) -> bool:
    if path.is_dir():
        return path.name not in SKIPPED
    if mode == JSON:
        return path.suffix in (".json", ".jsonc")
    return mode == ANY


def folder_verdict(path: Path) -> tuple[bool, str]:
    """What setting up a project here would mean, in a few words."""
    root = actions.git_root(path)
    if root and (taken := actions.project_at(root)):
        return True, f"already the project {taken}"
    if root:
        return True, "a git repository" if root == path else f"inside the repository {shown_path(root)}"
    if not any(path.iterdir()):
        return True, "an empty folder: a new project starts here"
    return True, "a folder without git: a repository starts here"


class NameFolder(Dialog):
    """The name of a new folder, in the one the browser is at. Dismisses with it, or ""."""

    def __init__(self, parent: Path):
        super().__init__()
        self.folder = parent

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"New folder in {escape(shown_path(self.folder))}:")
            yield Input(placeholder="name", id="name")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#name").focus()

    def answer(self) -> str:
        name = self.query_one("#name", Input).value.strip()
        return name if name and "/" not in name and name not in (".", "..") else ""

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.answer() if event.button.id == "create" else "")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.dismiss(self.answer())

    def key_escape(self) -> None:
        self.dismiss("")


class Browse(Dialog):
    """Walks your folders from home to pick a file or a folder, so no path is typed from memory.
    Arrows move, right opens a folder, left closes it, Enter picks, Backspace goes up a folder.
    Dismisses with the path, or None."""

    BINDINGS = [
        Binding("backspace", "up", show=False),
        Binding("right", "open", show=False),
        Binding("left", "close", show=False),
    ]

    def __init__(self, mode: str, title: str, start: Path | None = None):
        super().__init__()
        self.mode, self.title_text = mode, title
        self.start = start or browse_start()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"[b]{escape(self.title_text)}[/]")
            yield Label(
                "→ opens a folder · ← closes it · Enter or Select picks · Backspace goes up", classes="files"
            )
            tree = PathTree(self.start, self.mode, id="tree", classes="tree")
            tree.auto_expand = False  # Enter picks; the arrows open and close
            yield tree
            yield Label("", id="verdict")
            with Horizontal(classes="buttons"):
                yield Button("Select", variant="primary", id="select")
                if self.mode == FOLDER:  # a project from scratch starts in a folder not made yet
                    yield Button("New folder…", id="new-folder")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#tree").focus()

    def here(self) -> Path:
        """The folder the cursor is in, or on."""
        node = self.query_one("#tree", PathTree).cursor_node
        path = (
            node.data.path if node is not None and node.data else Path(self.query_one("#tree", PathTree).path)
        )
        return path if path.is_dir() else path.parent

    @on(Button.Pressed, "#select")
    def select_marked(self) -> None:
        node = self.query_one("#tree", PathTree).cursor_node
        if node is None or not node.data:
            return
        path = node.data.path
        ok, said = self.judged(path)
        if ok:
            self.dismiss(path)
        else:
            self.notify(said or "Pick one of the marked entries.", severity="warning")

    @on(Button.Pressed, "#new-folder")
    def new_folder(self) -> None:
        parent = self.here()

        def named(name: str) -> None:
            if not name:
                return
            made = parent / name
            try:
                made.mkdir()
            except OSError as e:
                self.notify(f"Cannot make {shown_path(made)}: {e.strerror}", severity="error")
                return
            self.dismiss(made)

        self.app.push_screen(NameFolder(parent), named)

    def judged(self, path: Path) -> tuple[bool, str]:
        if self.mode == JSON:
            return judge(path) if path.is_file() else (False, shown_path(path))
        if self.mode == FOLDER:
            return folder_verdict(path) if path.is_dir() else (False, "")
        return True, shown_path(path)

    @on(DirectoryTree.NodeHighlighted)
    def looked_at(self, event) -> None:
        path = event.node.data.path if event.node.data else None
        verdict = self.query_one("#verdict", Label)
        if path is None:
            verdict.update("")
            return
        ok, said = self.judged(path)
        colour = "green" if ok else "red" if path.is_file() else "dim"
        verdict.update(f"[{colour}]{escape(said)}[/]")

    @on(DirectoryTree.FileSelected)
    def file_picked(self, event: DirectoryTree.FileSelected) -> None:
        self.pick(event.path)

    @on(DirectoryTree.DirectorySelected)
    def folder_picked(self, event: DirectoryTree.DirectorySelected) -> None:
        if self.mode == JSON:  # a folder is no answer here: Enter opens it instead
            event.node.toggle()
        else:
            self.pick(event.path)

    def pick(self, path: Path) -> None:
        if self.judged(path)[0]:
            self.dismiss(path)

    def action_open(self) -> None:
        node = self.query_one("#tree", PathTree).cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_close(self) -> None:
        tree = self.query_one("#tree", PathTree)
        node = tree.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None:
            tree.move_cursor(node.parent)

    def action_up(self) -> None:
        tree = self.query_one("#tree", PathTree)
        tree.path = Path(tree.path).parent

    @on(Button.Pressed, "#cancel")
    def cancelled(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ImportSource(Dialog):
    """Which opencode configuration to bring over from: those found where opencode keeps one, and a
    file just downloaded, or one you find by browsing. Dismisses with its path, or None."""

    def __init__(self, found: list[tuple[Path, int]]):
        super().__init__()
        self.found = found

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            if self.found:
                yield Label("Import from an opencode configuration:")
            else:
                yield Label("No opencode configuration where opencode keeps one, nor one just downloaded.")
            rows = [f"{escape(shown_path(p))}  [dim]{n} to bring over[/]" for p, n in self.found]
            yield OptionList(*rows, "Browse…", id="sources")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#sources").focus()

    @on(OptionList.OptionSelected, "#sources")
    def chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option_index < len(self.found):
            self.dismiss(self.found[event.option_index][0])
        else:
            self.app.push_screen(Browse(JSON, "Find the opencode configuration"), self.browsed)

    def browsed(self, path: Path | None) -> None:
        # A statement, not a lambda returning dismiss(): Textual awaits what a callback returns,
        # and awaiting a screen's dismiss() from its own message handler is an error.
        if path:
            self.dismiss(path)

    @on(Button.Pressed, "#cancel")
    def cancelled(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


def provider_rows() -> list[tuple[str, str, str, bool]]:
    """Your providers and MCP servers, and vivibox's own: (kind, name, what it is, whether it is on)."""
    stored, defined = keys.list_keys(), providers.load()
    rows = []
    for name in sorted({n for n in stored if not providers.is_mcp_secret(n)} | set(defined)):
        key = (
            f"key {stored[name]}"
            if name in stored
            else "no key needed"
            if providers.keyless(name)
            else "no key"
        )
        said = [key]
        if name in defined:
            n = len(defined[name].get("models", {}))
            said.append(f"your endpoint, {n} model{'s' * (n != 1)}")
        rows.append((providers.PROVIDER, name, ", ".join(said), providers.enabled(providers.PROVIDER, name)))
    servers = {**providers.load_mcp(), **providers.BUILTIN_MCP}
    for name, entry in sorted(servers.items()):
        where = (
            entry.get("url", "") if entry.get("type") == "remote" else " ".join(entry.get("command", [])[:1])
        )
        if name == providers.SERENA:
            said = f"MCP server, comes with vivibox, {SERENA_MODE_SAID[providers.serena_mode()]}"
        else:
            said = f"MCP server, {entry.get('type', 'local')} {where}"
        rows.append((providers.MCP, name, said, providers.enabled(providers.MCP, name)))
    return rows


SERENA_MODE_SAID = {
    "auto": f"auto: on for a project with {providers.SERENA_MIN_FILES}+ source files",
    "on": "on for every task",
    "off": "off",
}


def row_label(name: str, said: str, on: bool) -> str:
    state = "" if on else "  [yellow]off[/]"
    return f"{escape(name)}  [dim]{escape(ui.shorten(said, 70))}[/]{state}"


class ManageProviders(Dialog):
    """Your providers and MCP servers: add a provider, import an opencode.json, or manage what is on."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Providers & MCP")
            yield OptionList(id="providers", classes="catalog")
            with Horizontal(classes="buttons"):
                yield Button("Add provider…", variant="primary", id="add")
                yield Button("Import opencode.json…", id="import")
                yield Button("Manage…", id="manage")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.fill()
        self.query_one("#add").focus()

    def fill(self) -> None:
        self.rows = provider_rows()
        options = self.query_one("#providers", OptionList)
        options.clear_options()
        options.add_options([row_label(n, said, on) for _, n, said, on in self.rows])
        mine = [r for r in self.rows if r[1] not in providers.BUILTIN_MCP]
        if not mine:
            options.add_option(Option("no providers yet: add one, or import an opencode.json", disabled=True))

    def changed(self, names) -> None:
        if names:
            self.fill()
            self.app.refresh_models(names)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self.app.push_screen(AddProvider(self.app.catalog, importing=False), self.changed)
        elif event.button.id == "import":
            self.app.import_opencode(self.changed)
        elif event.button.id == "manage":
            self.app.push_screen(ManageItems(), self.changed)
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ManageItems(Dialog):
    """What is on: a ticked provider's models are offered for a task, a ticked MCP server is given to
    every task. Unticking keeps it, and its key, for later; Remove takes it away. Save applies."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Tick what is on; Remove takes the highlighted one away with its secrets.")
            yield Label("A change reaches a task the next time it starts.", classes="files")
            self.rows = [r for r in provider_rows() if r[1] != providers.SERENA]
            yield SelectionList[int](
                *(Selection(row_label(n, said, True), i, on) for i, (_, n, said, on) in enumerate(self.rows)),
                id="items",
                classes="catalog",
            )
            with Horizontal(classes="role"):
                yield Label("Serena", classes="role-name")
                yield Select(
                    [(said, mode) for mode, said in SERENA_MODE_SAID.items()],
                    value=providers.serena_mode(),
                    allow_blank=False,
                    id="serena-mode",
                )
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Remove", variant="error", id="remove")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#items").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        items = self.query_one("#items", SelectionList)
        if event.button.id == "save":
            ticked = set(items.selected)
            changed = []
            for i, (kind, name, _, on) in enumerate(self.rows):
                if (i in ticked) != on:
                    providers.set_enabled(kind, name, i in ticked)
                    changed.append(name)
            mode = str(self.query_one("#serena-mode", Select).value)
            if mode != providers.serena_mode():
                providers.set_serena_mode(mode)
                changed.append(providers.SERENA)
            self.dismiss(changed)
        elif event.button.id == "remove":
            at = items.highlighted
            if at is None or at >= len(self.rows):
                return
            kind, name, _, _ = self.rows[at]
            what = "MCP server" if kind == providers.MCP else "provider"

            def answered(yes: bool) -> None:
                if yes and providers.forget(name, kind):
                    self.notify(f"Removed {name}.")
                    self.dismiss([name])

            self.app.push_screen(
                Confirm(
                    f"Remove the {what} {name} and its secrets from vivibox?", "Remove", destructive=True
                ),
                answered,
            )
        else:
            self.dismiss([])

    def key_escape(self) -> None:
        self.dismiss([])


class AddProvider(Dialog):
    """A provider opencode knows, picked from its list, with your key; or every provider of an
    opencode.json you already use, such as your employer's endpoint. Dismisses with the providers
    added, or [] when you leave."""

    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, catalog: list[tuple[str, str]] | None = None, importing: bool = True):
        super().__init__()
        # None while the list is still being read; [] when it cannot be, and you type the name.
        self.catalog = catalog
        # Opened from the providers screen, which has its own import button, it does without one.
        self.importing = importing
        self.shown: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Add a provider: type to search the ones opencode knows, arrows to pick")
            yield Input(placeholder="search, e.g. deep", id="search")
            yield OptionList(id="catalog", classes="catalog")
            yield Input(
                placeholder="API key for the provider picked above (not shown)", password=True, id="key"
            )
            yield Label("", id="problem")
            with Horizontal(classes="buttons"):
                yield Button("Add", variant="primary", id="add")
                yield Button("Import opencode.json…", id="import")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#search", Input).focus()
        self.query_one("#import").display = self.importing
        if self.catalog is None:
            self.query_one("#catalog", OptionList).add_option(
                Option("reading the providers opencode knows…", disabled=True)
            )
            self.read_catalog()
        else:
            self.show()

    @work(thread=True)
    def read_catalog(self) -> None:
        found = actions.provider_catalog()
        self.app.call_from_thread(self.loaded, found)

    def loaded(self, found: list[tuple[str, str]]) -> None:
        self.catalog = self.app.catalog = found
        self.show()

    @on(Input.Changed, "#search")
    def show(self) -> None:
        if self.catalog is None:
            return
        self.shown = find_providers(self.catalog, self.query_one("#search", Input).value)
        options = self.query_one("#catalog", OptionList)
        options.clear_options()
        options.add_options([label for _, label in self.shown])
        if self.shown:
            options.highlighted = 0

    def picked(self) -> str:
        at = self.query_one("#catalog", OptionList).highlighted
        return self.shown[at][0] if at is not None and at < len(self.shown) else ""

    def action_move(self, step: int) -> None:
        """In the search field the arrows walk the list; elsewhere they move between fields."""
        if self.focused is self.query_one("#search"):
            options = self.query_one("#catalog", OptionList)
            options.action_cursor_down() if step > 0 else options.action_cursor_up()
        else:
            self.app.action_focus_next() if step > 0 else self.app.action_focus_previous()

    @on(OptionList.OptionSelected, "#catalog")
    def chosen(self) -> None:
        self.query_one("#key", Input).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        try:
            if event.button.id == "add":
                name = self.picked()
                if not name:
                    raise ConfigError("pick a provider from the list, or type its name")
                keys.set_key(name, self.query_one("#key", Input).value)
                self.dismiss([name])
            elif event.button.id == "import":
                self.app.import_opencode(self.imported)
            else:
                self.dismiss([])
        except (keys.KeyStoreError, ConfigError) as e:
            self.query_one("#problem", Label).update(f"[red]{e.args[0]}[/]")

    def imported(self, names: list[str]) -> None:
        if names:  # nothing imported: back to this dialog, to add a provider by name instead
            self.dismiss(names)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#key", Input).focus()
        else:
            self.query_one("#add", Button).press()

    def key_escape(self) -> None:
        self.dismiss([])


class NewTask(Dialog):
    """A form: labels on the left, one field per row, the description and the closing buttons
    the only boxes. Every field is on the screen at once; a short terminal shrinks the
    description before anything scrolls."""

    def __init__(self, preselect: str = "", available: dict[str, list[str]] | None = None):
        super().__init__()
        self.preselect = preselect
        # The models of the providers you have keys for; None while the view is still asking.
        self.available = available

    def compose(self) -> ComposeResult:
        names = panel.projects()
        chosen = self.preselect if self.preselect in names else names[0]
        with Vertical(classes="dialog form"):
            with Fields(classes="fields"):
                with Vertical(id="task", classes="section"):
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
                    suggestions = OptionList(id="suggestions")
                    suggestions.display = False
                    with Horizontal(classes="row", id="task-row"):
                        yield Label("Task", classes="key")
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
                    config = load_config()
                    for name in sorted(config.roles):
                        offered = actions.choices(name, config, self.available)
                        configured = actions.configured_choice(config, name)
                        with Horizontal(classes="row gap"):
                            yield Label(name.capitalize(), classes="key")
                            options = [(actions.choice_label(c, configured), c) for c in offered]
                            yield Select(
                                options, value=configured, allow_blank=False, compact=True,
                                id=f"role-{name}", classes="model",
                            )  # fmt: skip
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")
                yield Label("ctrl+s creates the task", classes="hint keys")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
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
                "roles": {s.id.removeprefix("role-"): s.value for s in self.query(".model").results(Select)},
            }
        )

    # The dialog's frame, padding and buttons: what the fields leave room for.
    CHROME = 9

    def on_mount(self) -> None:
        self.for_project(str(self.query_one("#project", Select).value))
        self.call_after_refresh(self.fit)

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        """The description as tall as the screen leaves after the other rows, a line at least, so
        the whole form stays in view and the description scrolls inside itself. The blank rows
        between the lists of a group go first, before the description would shrink below three
        lines."""
        fields = self.query_one(Fields)
        goal = self.query_one("#goal", TextArea)
        dialog = self.query_one(".dialog")
        room = int(self.size.height * 0.9) - self.CHROME
        fields.styles.max_height = max(5, room)
        others = fields.virtual_size.height - self.query_one("#task-row").outer_size.height
        gaps = len([row for row in self.query(".row.gap") if row.display])
        if not dialog.has_class("tight"):
            others -= gaps  # the rows without their gaps, whichever way they are drawn now
        tight = room - others - gaps < 3
        dialog.set_class(tight, "tight")
        goal.styles.height = max(3, min(12, room - others - (0 if tight else gaps)))
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
        with contextlib.suppress(ConfigError):
            self.query_one("#goal", DescriptionArea).repo = load_project(name).repo

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
  c, C  copy the planning prompt for a chat, or for a CLI
  o     open the review copy in your IDE
  v     run the app in its pod, or stop it
  w     watch or talk to the agent; while verifying, its log (Ctrl-q leaves);
        with two conversations, asks which; in a box, a shell in it
  l     the newest log in your pager: followed while the verification runs,
        else opened at its end
  s     stop the task, or start it again
  m     what each role runs on, for this task
  x     delete the task; on a finished one, its line in the history

[b]The selected project[/b] (Enter folds or unfolds its tasks)
  n     new task in it
  b     open a box: its pod for you to work in by hand, opencode included
  e     edit its file
  o     open its repository in your IDE
  x     forget it, once it has no tasks

[b]Anywhere[/b]
  i     set up a project
  k     providers & MCP
  h     show or hide finished tasks
  q     quit
"""


class Help(ModalScreen):
    """Every key, and when it does something. The footer shows only what applies right now."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog help"):
            yield Static(HELP, id="help")
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


# --- the view -----------------------------------------------------------------------------------
