"""Widgets the dialogs share: the modal base, framed with its title and its keys, whose arrows
move between fields; a scrolling field list; a text area the arrows leave at its edges; a list
to pick one from; and a yes-or-no.
"""

from __future__ import annotations

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Select, TextArea
from textual.widgets.option_list import Option

from . import look


class Fields(VerticalScroll, can_focus=False, inherit_bindings=False):
    """A dialog's fields, scrolling when the terminal is short. No keys of its own: the arrows move
    between fields, as everywhere in a dialog, and scrolling follows the field you are on."""


class Dialog(ModalScreen):
    """Every dialog: a frame with its title in the top edge and the keys that close it in the
    bottom edge, the same for all of them (the first .dialog in it). Arrow keys move between
    fields and buttons wherever the focused field does not use them itself: left and right move
    the cursor in a text field, up and down open a list."""

    # The frame's title; "" for none, where the first line of the dialog is a question.
    frame_title = ""
    # The keys named in the frame's bottom edge, as (key, what it does).
    hint_keys: tuple[tuple[str, str], ...] = (look.ESC_CLOSES,)
    # What a field is for, by its id: the line under the fields (#about) says it while the field
    # has focus, so the form itself stays one word per label.
    field_help: dict[str, str] = {}

    BINDINGS = [
        Binding("up", "app.focus_previous", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    @on(events.Mount)
    def framed(self) -> None:
        self.reframe()

    @on(events.DescendantFocus)
    def mark_row(self, event: events.DescendantFocus) -> None:
        """Focus is one signal in a form: the label of the row that has the keys, in the accent
        (.row.-focused). Kept here, not in CSS's :focus-within, which a row kept after focus left."""
        for row in self.query(".row.-focused"):
            row.remove_class("-focused")
        for node in event.widget.ancestors_with_self:
            if node.has_class("row"):
                node.add_class("-focused")
                break
        if self.field_help:
            for about in self.query("#about").results(Label):
                about.update(self.field_help.get(event.widget.id or "", ""))

    def reframe(self) -> None:
        """The frame's edges from frame_title and hint_keys, after either changed."""
        for frame in self.query(".dialog").results():
            frame.border_title = self.frame_title or None
            frame.border_subtitle = look.hints(*self.hint_keys) if self.hint_keys else None
            break

    async def handle_key(self, event: events.Key) -> bool:
        """Esc on an open list closes the list: the dialog's own Esc would run first and throw away
        everything typed in it."""
        if event.key == "escape":
            for select in self.query(Select):
                if select.expanded:
                    select.expanded = False
                    select.focus()
                    return True
        return await super().handle_key(event)


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


class Choose(Dialog):
    """A list to pick one from: a name and what it is, in two columns. Arrows move, Enter takes the
    highlighted row, Esc leaves. Dismisses with picked(index), or with `none` on Esc."""

    hint_keys = (("enter", "choose"), look.ESC_CLOSES)
    # The widest the name column gets; a longer name pushes its own description on.
    NAME_WIDTH = 28

    def __init__(self, title: str, rows: list[tuple[str, str]], start: int = 0, note: str = "", none=None):
        super().__init__()
        self.frame_title, self.entries, self.start, self.note, self.none = title, rows, start, note, none

    def options(self) -> list[Option]:
        width = min(max((len(name) for name, _ in self.entries), default=0), self.NAME_WIDTH)
        return [
            Option(f"{escape(name.ljust(width))}  {look.muted(said)}" if said else escape(name))
            for name, said in self.entries
        ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield OptionList(*self.options(), classes="catalog")
            if self.note:
                yield Label(self.note, classes="files wrap")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.highlighted = self.start
        options.focus()

    def picked(self, index: int):
        return index

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.picked(event.option_index))

    def key_escape(self) -> None:
        self.dismiss(self.none)


class Confirm(Dialog):
    """A yes or no. Red is for a yes that destroys or interrupts something; agreeing to go on is not
    a warning."""

    hint_keys = (("enter", "choose"), look.ESC_CANCELS)

    def __init__(self, question: str, yes: str = "Yes", destructive: bool = False, title: str = ""):
        super().__init__()
        self.question, self.yes, self.destructive = question, yes, destructive
        self.frame_title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog narrow"):
            yield Label(self.question, classes="wrap")
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="error" if self.destructive else "primary", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)
