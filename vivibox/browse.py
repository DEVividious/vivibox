"""The file and folder browser: walks your folders from home to pick a repository, a folder for a
new project, an opencode configuration or a file to attach, saying what each pick would mean.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    Label,
)

from . import actions, providers
from .config import ConfigError
from .widgets import Dialog


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
