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
from textual.widgets import Button, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from . import look


class ContextHelp(Label):
    """The help under a dialog's fields, in the same place whatever has focus: what the focused
    field is for, or what the highlighted option is (a look.Explained), set apart by a rule.

    Its size never follows what it says: `lines` lines of text under the rule, set by the dialog
    from the terminal (`reserve`), and what does not fit is cut. Focus changes the words, never
    the dialog's rectangle. `full` draws an Explained whole, else in a title line and one more."""

    def __init__(self, lines: int = 3, **kwargs):
        super().__init__("", **kwargs)
        self.said: str | look.Explained = ""
        self.full = True
        self.reserve(lines)

    def reserve(self, lines: int, full: bool | None = None) -> None:
        """The lines of text it has, whatever it says; the rule above them is one more."""
        self.styles.height = lines + 1
        self.set_full(lines >= 4 if full is None else full)

    def explain(self, said: str | look.Explained) -> None:
        self.said = said
        self.draw()

    def set_full(self, full: bool) -> None:
        if full != self.full:
            self.full = full
            self.draw()

    def draw(self) -> None:
        """Facts stand a line each, cut at the edge rather than wrapped under their names; a
        sentence of help wraps."""
        said = self.said
        lines = isinstance(said, look.Explained) and bool(said.facts or not self.full)
        self.set_class(lines, "-lines")
        self.update(said.lines(self.full) if isinstance(said, look.Explained) else said)


def follow_highlight(screen, select: Select, shown) -> None:
    """Calls shown(index) as the highlight moves in select's open list, and shown(None) when the
    list closes, so a help can say what the highlighted option is while you compare. Textual's
    list stops its own highlight messages inside the select."""
    from textual.widgets._select import SelectOverlay

    overlay = select.query_one(SelectOverlay)

    def moved(index: int | None) -> None:
        if select.expanded and index is not None:
            shown(index)

    screen.watch(overlay, "highlighted", moved, init=False)
    screen.watch(select, "expanded", lambda opened: opened or shown(None), init=False)


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
        said = self.help_for(event.widget)
        if said is not None:
            for about in self.query("#about").results(Label):
                if isinstance(about, ContextHelp):
                    about.explain(said)
                else:
                    about.update(said if isinstance(said, str) else said.title)

    def help_for(self, widget) -> str | look.Explained | None:
        """What the line under the fields says while this widget has focus; None leaves it."""
        if not self.field_help:
            return None
        return self.field_help.get(widget.id or "", "")

    def reframe(self) -> None:
        """The title in the frame's top edge; the keys at the right of the dialog's last row, the
        closing buttons' row, or a row of their own: inside the dialog, a row above its frame,
        not printed on the frame."""
        frame = next(iter(self.query(".dialog").results()), None)
        if frame is None:
            return
        frame.border_title = self.frame_title or None
        said = look.hints(*self.hint_keys) if self.hint_keys else ""
        if found := frame.query(".keys"):
            found.first(Static).update(said)
            return
        keys = Static(said, classes="keys")
        rows = [child for child in frame.children if child.has_class("buttons")]
        if rows:
            rows[-1].mount(keys)
        else:
            frame.mount(Horizontal(keys, classes="buttons footer"))

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
