"""The dialogs about providers and MCP servers: adding a provider and its key, importing an
opencode configuration, and managing what is on.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from . import actions, keys, providers, ui
from .browse import JSON, Browse, shown_path
from .config import ConfigError
from .widgets import Confirm, Dialog


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
    for name in sorted({n for n in stored if providers.is_provider_key(n)} | set(defined)):
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
