"""Find a task's starting branch among thousands, without switching the source checkout."""

from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.widgets import Button, Input, Label, OptionList

from . import repo
from .widgets import Dialog


def matches(choices: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    """Words may occur anywhere in a branch; a partial path also matches as a subsequence."""
    words = query.casefold().split()
    if not words:
        return choices
    ranked = []
    for index, choice in enumerate(choices):
        name = choice[0].casefold()
        if all(word in name for word in words):
            ranked.append((0 if name.startswith(words[0]) else 1, index, choice))
        else:
            letters = iter(name)
            if all(any(letter == candidate for candidate in letters) for letter in "".join(words)):
                ranked.append((2, index, choice))
    return [choice for _, _, choice in sorted(ranked)]


class BranchPicker(Dialog):
    def __init__(self, source: Path, selected: str = "HEAD"):
        super().__init__()
        self.source, self.selected = source, selected
        self.choices: list[tuple[str, str]] = []
        self.offered: list[tuple[str, str]] = []
        self.ready = False

    def compose(self) -> ComposeResult:
        self.frame_title = "Branch to start from"
        with Vertical(classes="dialog"):
            yield Input(placeholder="Type a branch name, path or ticket number", id="branch-query")
            yield OptionList(id="branch-options", classes="catalog")
            yield Label("Loading branches…", id="branch-count", classes="note")
            with Horizontal(classes="buttons"):
                yield Button("Use branch", variant="primary", id="choose", disabled=True)
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            choices = repo.branches(self.source)
        except repo.RepoError as error:
            self.app.call_from_thread(self.failed, str(error))
            return
        self.app.call_from_thread(self.loaded, choices)

    def failed(self, error: str) -> None:
        if self.is_mounted:
            self.query_one("#branch-count", Label).update(
                f"Could not list branches: {error}. Escape returns to the task."
            )

    def loaded(self, choices: list[tuple[str, str]]) -> None:
        if not self.is_mounted:
            return
        self.choices = choices
        self.ready = True
        self.filter()

    @on(Input.Changed, "#branch-query")
    def filter(self) -> None:
        if not self.ready:
            return
        found = matches(self.choices, self.query_one(Input).value)
        self.offered = found[:100]
        options = self.query_one(OptionList)
        options.clear_options().add_options(Text(label) for label, _ in self.offered)
        options.highlighted = (
            next((i for i, (_, ref) in enumerate(self.offered) if ref == self.selected), 0)
            if self.offered
            else None
        )
        self.query_one("#choose", Button).disabled = not self.offered
        count = f"{len(found)} matches"
        if len(found) > len(self.offered):
            count += "; showing the first 100 — type more to narrow the list"
        self.query_one("#branch-count", Label).update(count if found else "No matching branches")

    @on(Input.Submitted)
    @on(OptionList.OptionSelected)
    def choose(self) -> None:
        index = self.query_one(OptionList).highlighted
        if index is not None and self.offered:
            self.dismiss(self.offered[index])

    def on_key(self, event: Key) -> None:
        if isinstance(self.focused, Input) and event.key in ("down", "up"):
            options = self.query_one(OptionList)
            options.action_cursor_down() if event.key == "down" else options.action_cursor_up()
            event.stop()
            event.prevent_default()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "choose":
            self.choose()
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)
