"""Widgets the dialogs share: the modal base whose arrows move between fields, a scrolling
field list, a text area the arrows leave at its edges, and a yes-or-no.
"""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, TextArea


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
