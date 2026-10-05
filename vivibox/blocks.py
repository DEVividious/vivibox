"""The widgets the details panel's text is drawn with, for what markdown.py reads: the panel
itself, the frame around what an agent wrote, a code block that folds its long lines, a table
that is a grid only where it has room, and what keeps an error of theirs from ending the view.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from contextlib import suppress
from functools import lru_cache
from itertools import accumulate

from markdown_it.token import Token
from rich.cells import cell_len, chop_cells
from rich.segment import Segment
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.content import Content, Span
from textual.css.query import NoMatches
from textual.geometry import Offset, Size
from textual.highlight import ANSIDarkHighlightTheme, ANSILightHighlightTheme, HighlightTheme, highlight
from textual.rlock import RLock
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Label, Markdown
from textual.widgets._markdown import (
    MarkdownBlock,
    MarkdownBlockQuote,
    MarkdownFence,
    MarkdownHorizontalRule,
    MarkdownTable,
    MarkdownTableContent,
)

from .markdown import AT_80, MARK, PROSE, ROOM_AT_80, RULE, parser, pieces, source

COLUMN = 4
# What a cut puts into a code block's text: the end of the row, and the column a further row
# stands in.
GONE_ON = "\n "
# What you are told when a piece is drawn as its source because its widgets raised.
COULD_NOT = "What an agent wrote could not be drawn as markdown ({}): it is shown as its source."


class Cells(MarkdownTableContent):
    """A table's cells as the panel draws them: each whole, on as many rows as it takes. Textual
    draws a cell on one row, cut with an ellipsis, and gives it its text as a tooltip to read the
    rest by. A tooltip of a string is markup, and a cell's text is an agent's: none is made here,
    and none is needed, as nothing is cut. An address in a cell is on the screen whole."""

    DEFAULT_CSS = """
    Cells {
        grid-columns: 1fr;
    }
    Cells > .cell, Cells > .header {
        text-overflow: fold;
    }
    """

    def __init__(self, headers: list[Content], rows: list[list[Content]]) -> None:
        super().__init__(headers, rows)
        # Never laid out narrower than a grid with room is (Table): in fewer columns than it has
        # lines between its cells, Textual's grid divides by zero, and the view ends.
        self.styles.min_width = len(headers) * (COLUMN + 3) + 1

    def compose(self) -> ComposeResult:
        for cell in super().compose():
            cell.tooltip = None  # before it is on the screen: nothing of it is ever a tooltip
            yield cell


class Table(MarkdownTable):
    """A table of the panel: Textual's, its cells drawn by Cells, while every column has room
    for COLUMN characters of text a row. Columns share the width, and with many of them a cell
    is a character wide, or has no width at all: thirty columns at 80 were an empty grid. On a
    panel too narrow for its columns the table is drawn as its source, in a code block, where
    every cell is on the screen whatever the width (Fence). Decided when its width is first
    known, before anything of it is mounted, and again whenever the width changes: a grid is
    never mounted where it has no room."""

    COLUMN = COLUMN
    DEFAULT_CSS = """
    Table > Fence {
        margin: 0;
    }
    """

    def __init__(self, markdown: Markdown, token: Token, *args, **kwargs) -> None:
        super().__init__(markdown, token, *args, **kwargs)
        self._as_source: bool | None = None  # neither yet

    def compose(self) -> ComposeResult:
        self._headers, self._rows = self._get_headers_and_rows()
        return ()

    def room(self, width: int) -> int:
        """The room a cell's text has at that width: a column's share of it, less the line
        between two columns and a cell's padding."""
        columns = len(self._headers) or 1
        return (width - columns - 1) // columns - 2

    async def on_resize(self, event: events.Resize) -> None:
        """Reckoned with the panel's scrollbar shown, whether it is or not: a grid and the
        source differ in height, and a table that had room only without the scrollbar, and
        needed it only as a grid, would be drawn as one and the other in turn for ever. So what
        a table is drawn as depends on the terminal's width alone."""
        panel = self._markdown.parent
        spare = 0 if panel.show_vertical_scrollbar else panel.styles.scrollbar_size_vertical
        narrow = self.room(event.size.width - spare) < self.COLUMN
        if narrow != self._as_source:
            self._as_source = narrow
            meta = self._token.meta or {}
            written = source(meta.get("source", ""), meta.get(ROOM_AT_80, AT_80 + 2))
            await self.remove_children()
            shown = Fence(self._markdown, written, written.content.rstrip()) if narrow else None
            await self.mount(shown or Cells(self._headers, self._rows))


TAB = 8
# The columns a block's text has in a terminal of 80, and the most rows folding may add to a
# block there before it is left unfolded (unfolded).
MOST = 2000
# What is said above a block left unfolded, and where its files are approved: how many lines,
# of what, and the task whose risky files a terminal prints whole.
NOT_FOLDED = (
    "{} lines of {} are longer than the panel and are not folded: it is too large to fold. Their ends "
    "are off the screen until you scroll sideways (Tab to the block, then → or End), or read a risky "
    "file whole in a terminal: vivibox risky {}"
)
# The longest line a code block is drawn with before its width is known (Fence.folded).
LIKELY = 60
# The width each of the last few code blocks was drawn at, by its text.
_DRAWN_AT: dict[int, int] = {}


def _cuts(line: str, width: int) -> list[int]:
    """Where in a line longer than width cells each further row begins: the first row takes
    width cells, a further one a cell fewer, as its text stands a column in. Cells are counted
    as what draws the row counts them (rich.cells): two for a wide character and for a symbol
    with its emoji selector, none for a combining mark, which stays with its letter; a row is
    cut between two characters, never within one."""
    step = max(2, width - 1)
    if line.isascii():
        return list(range(width, len(line), step))
    cuts, at = [], len(chop_cells(line, width)[0])
    for row in chop_cells(line[at:], step):
        cuts += [at] * bool(row)
        at += len(row)
    return cuts


@lru_cache(maxsize=8)
def _read(
    code: str, language: str, ansi: bool, dark: bool
) -> tuple[Content, tuple[int, ...], tuple[int, ...]]:
    """A block's text, read once however often it is drawn: coloured as Textual colours it, its
    tabs as the columns they take; where each of its lines begins; the cells each takes."""
    theme = (ANSIDarkHighlightTheme if dark else ANSILightHighlightTheme) if ansi else HighlightTheme
    content = highlight(code, language=language or None, theme=theme).expand_tabs(TAB)
    starts, cells, at = [], [], 0
    for line in content.plain.split("\n"):
        starts.append(at)
        cells.append(len(line) if line.isascii() else cell_len(line))
        at += len(line) + 1
    return content, tuple(starts), tuple(cells)


@lru_cache(maxsize=8)
def _folded(
    code: str, language: str, ansi: bool, dark: bool, width: int
) -> tuple[Content, str, tuple[int, ...]]:
    """A block's text for a width: (the text as drawn, the marks beside its rows, where it was
    cut). Only a line longer than the width is touched; with none, the text is the one that
    was read, itself, and there are no marks. A cut puts a line break and a column of space
    into the text, and the colours on either side stay where they were."""
    content, starts, cells = _read(code, language, ansi, dark)
    plain = content.plain
    long = [at for at, taken in enumerate(cells) if taken > width]
    if not long:
        return content, "", ()
    cuts: list[int] = []
    marks = [""] * len(cells)
    for at in long:
        end = starts[at + 1] - 1 if at + 1 < len(starts) else len(plain)
        found = _cuts(plain[starts[at] : end], width)
        marks[at] = f"\n{MARK}" * len(found)
        cuts += [starts[at] + cut for cut in found]
    parts, before = [], 0
    for cut in cuts:
        parts += [plain[before:cut], GONE_ON]
        before = cut
    by = len(GONE_ON)
    spans = [
        Span(a + by * bisect_right(cuts, a), b + by * bisect_left(cuts, b), style)
        for a, b, style in content.spans
    ]
    return Content("".join([*parts, plain[before:]]), spans), "\n".join(marks), tuple(cuts)


class Marks(Widget):
    """The column of a code block that says which rows go on from the row above: the mark
    beside each of them, drawn as the block's text is. A row of it is made when it is drawn
    (a block of forty thousand rows has a few dozen on the screen). Nothing of it is copied."""

    def __init__(self, rows: str, **how) -> None:
        super().__init__(**how)
        self.rows = rows.split("\n")

    def mark(self, rows: str) -> None:
        self.rows = rows.split("\n")
        self.refresh(layout=True)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return 2

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.rows)

    def render_line(self, y: int) -> Strip:
        return Strip([Segment((self.rows[y] if y < len(self.rows) else "").ljust(2), self.rich_style)], 2)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return None


class Code(Label):
    """The text of a code block. What is copied from it is what was written (Fence.copied)."""

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return self.query_ancestor(Fence).copied(selection), "\n"


def unfolded(cells: Sequence[int], width: int = 68) -> int:
    """How many lines of a block are left unfolded, by the cells its lines take and the width
    its text has in a terminal of 80 columns: all that are longer than that, when folding them
    there would add more than MOST rows (a first row of width cells, further ones a cell fewer:
    _cuts); else none, and every long line is folded. The width is the block's own (Fence: 68
    cells outside a piece, 66 behind the rule, as few as 6 in lists), and that of 80 columns
    whatever the terminal's is, so a block is the one or the other at every width. Textual lays
    out and draws every row of a block each time the panel is drawn: twenty thousand lines
    folded in two stood the view still for seconds."""
    long = [taken for taken in cells if taken > width]
    added = sum(-(-(taken - width) // max(2, width - 1)) for taken in long)
    return len(long) if added > MOST else 0


class Sideways(ScrollableContainer):
    """What a block too large to fold is read in: a row a line, scrolled sideways, its
    scrollbar always shown. Tab reaches it; the arrows move a screen's width, Home and End to
    a line's start and to the end of the longest."""

    BINDINGS = [
        Binding("right", "sideways(1)", "Right", show=False),
        Binding("left", "sideways(-1)", "Left", show=False),
        Binding("end", "sideways(0, True)", "End", show=False),
        Binding("home", "sideways(0)", "Start", show=False),
    ]

    def action_sideways(self, pages: int, end: bool = False) -> None:
        to = self.max_scroll_x if end else (self.scroll_x + pages * max(1, self.size.width - 4)) * abs(pages)
        self.scroll_to(x=to, animate=False)


class Fence(MarkdownFence):
    """A code block of the panel: a diff, a file, a log, a block in a plan. Every line of it is
    on the screen whole. Textual draws a line as one row and leaves what is past the block's
    width to a scroll sideways that nothing announces: of a line of a risky file with 200 spaces
    in it, the harmless start alone was shown where you approve the file "as shown". Here a line
    longer than the block is folded onto further rows. Beside each of them, in the first of the
    two columns before the text, stands MARK, drawn as the text is: those columns are a widget
    of their own (Marks), so no text of the block is ever drawn there. And a further row stands
    a column in, so that in a diff nothing of it is in the column of + and -: the rest of an
    added line that begins with - does not read as a line taken out. Folded again when the
    block's width changes, by the work of its long lines alone (_folded, which remembers the
    last few); nothing scrolls sideways. What is copied from it is what was written: a folded
    line is one line, with no mark and no column of ours."""

    DEFAULT_CSS = """
    Fence {
        layout: horizontal;
        overflow: hidden hidden;
        & > #code-marks {
            width: 2;
            height: auto;
            padding: 1 0;
            color: auto;
            text-style: bold;
        }
        & > #code-content {
            width: 1fr;
            padding: 1 2 1 0;
            text-wrap: nowrap;
            text-overflow: fold;
        }
    }
    Fence:ansi > #code-content {
        padding: 1 0;
    }
    Fence.-unfolded {
        layout: vertical;
        & > #code-unfolded {
            width: 1fr;
            height: auto;
            padding: 1 2 0 2;
            text-style: bold;
        }
        & > #code-sideways {
            height: auto;
            overflow: scroll hidden;
            scrollbar-size-horizontal: 1;
            & > #code-content {
                width: auto;
                padding: 1 2;
                text-overflow: clip;
            }
        }
    }
    """

    def __init__(self, markdown: Markdown, token: Token, code: str) -> None:
        super().__init__(markdown, token, code)
        # The width the same text was last drawn at: the panel makes every block anew when
        # anything in its text changes, and this one is then drawn once, as it was.
        self._width = _DRAWN_AT.get(hash((code, self.lexer)), 0)
        # How many of its lines are left unfolded: none, or every long one (unfolded).
        # at the width its text has at 80 columns: its room there, less the marks and the padding.
        room = (token.meta or {}).get(ROOM_AT_80, AT_80 + 2)
        read = _read(code, self.lexer, self.app.native_ansi_color, self.app.current_theme.dark)
        self.unfolded = unfolded(read[2], max(3, room - 4))
        self.set_class(bool(self.unfolded), "-unfolded")

    @property
    def allow_horizontal_scroll(self) -> bool:
        return False

    @classmethod
    def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
        return _read(code, language, ansi, dark)[0]

    def folded(self) -> tuple[Content, str, tuple[int, ...]]:
        """The block's text for the width it has. While that is not known: as it was read, when
        no line of it is long (then that is most likely what is drawn, and it is drawn once);
        nothing yet, when one is: Textual would cut every row of it for a width it does not
        have, and all of it would be drawn a second time."""
        how = (self.code, self.lexer, self.app.native_ansi_color, self.app.current_theme.dark)
        if self.unfolded:
            return _read(*how)[0], "", ()
        if self._width:
            return _folded(*how, self._width)
        content, _, cells = _read(*how)
        return (content if max(cells, default=0) <= LIKELY else Content(""), "", ())

    def draw(self) -> None:
        """Its text and its marks as they are for its width now; what is drawn already stays."""
        text, marks, _ = self.folded()
        while len(_DRAWN_AT) > 32:
            del _DRAWN_AT[next(iter(_DRAWN_AT))]
        _DRAWN_AT[hash((self.code, self.lexer))] = self._width
        with suppress(NoMatches):
            label = self.query_one("#code-content", Code)
            if label.content is not text:
                label.update(text)
                self.query_one("#code-marks", Marks).mark(marks)

    def on_resize(self, event: events.Resize) -> None:
        """The width its text has: the block's, less the two columns of the marks and the
        text's own padding. Three cells at least: a character of two, and a column to stand in."""
        with suppress(NoMatches):
            width = max(3, event.size.width - 2 - self.query_one("#code-content", Code).styles.gutter.width)
            if width != self._width:
                self._width = width
                self.draw()

    def set_content(self, content: Content) -> None:
        self._content = content
        self.draw()

    def compose(self) -> ComposeResult:
        text, marks, _ = self.folded()
        if self.unfolded:
            task = getattr(self.app, "selected_id", lambda: None)() or "<task>"
            yield Label(
                NOT_FOLDED.format(self.unfolded, "this block", task), id="code-unfolded", markup=False
            )
            yield Sideways(Code(text, id="code-content", markup=False), id="code-sideways")
        else:
            yield Marks(marks, id="code-marks")
            yield Code(text, id="code-content", expand=True, markup=False)

    def copied(self, selection: Selection) -> str:
        """What a selection of the rows as drawn holds of the block as it was written: from the
        place in the text its first row and column stand for to the one its last do. A column a
        further row stands in is no place of the text, and a tab is the one character it is."""
        text, _, cuts = self.folded()
        rows = text.plain.split("\n")
        begins = list(accumulate((len(row) + 1 for row in rows), initial=0))
        drawn_cuts = [cut + len(GONE_ON) * at for at, cut in enumerate(cuts)]

        def read(place: Offset | None, otherwise: int) -> int:
            """The place in the text as it was read (its tabs as columns) of a column of a row."""
            if place is None:
                return otherwise
            row = min(max(place.y, 0), len(rows) - 1)
            drawn = begins[row] + min(max(place.x, 0), len(rows[row]))
            before = bisect_right(drawn_cuts, drawn)
            return max(cuts[before - 1], drawn - len(GONE_ON) * before) if before else drawn

        expanded, starts, _ = _read(
            self.code, self.lexer, self.app.native_ansi_color, self.app.current_theme.dark
        )
        written = self.code.split("\n")

        def wrote(at: int) -> int:
            """The place in the code as it was written of a place in the text as it was read."""
            if "\t" not in self.code or len(written) != len(starts):
                return at
            line = bisect_right(starts, at) - 1
            column, character = 0, 0
            for character, char in enumerate(written[line]):  # noqa: B007
                if column >= at - starts[line]:
                    break
                column += TAB - column % TAB if char == "\t" else cell_len(char)
            else:
                character = len(written[line])
            return sum(len(row) + 1 for row in written[:line]) + character

        return self.code[wrote(read(selection.start, 0)) : wrote(read(selection.end, len(expanded.plain)))]


class Quote(MarkdownBlockQuote):
    """A quote of the panel. One an agent wrote is drawn as Textual draws a quote. The frame
    around a piece an agent wrote (PROSE) is a rule down its left and nothing else (its look is
    in vivibox.tcss): every row of the piece has it, blank ones too, nothing in the piece ends
    it, and a line of vivibox's own never has one. A quote in the piece is a quote inside the
    rule."""

    DEFAULT_CSS = """
    Quote.prose {
        background: transparent;
        border-left: heavy $foreground 30%;
        margin: 0 0 1 0;
        padding: 0 0 0 1;
    }
    Quote.prose > MarkdownParagraph {
        margin: 0 0 1 0;
    }
    Quote.prose > MarkdownBlock:last-child {
        margin-bottom: 0;
    }
    """

    def __init__(self, markdown: Markdown, token: Token, *args, **kwargs) -> None:
        super().__init__(markdown, token, *args, **kwargs)
        self.set_class(bool((token.meta or {}).get(PROSE)), PROSE)


class Panel(Markdown):
    """The details panel's text. A click opens an address in your browser, and the only
    addresses a click finds are vivibox's own (own_link)."""

    BLOCKS: dict[str, type[MarkdownBlock]] = {
        **Markdown.BLOCKS,
        "table_open": Table,
        "blockquote_open": Quote,
        "fence": Fence,
        "code_block": Fence,
    }

    def unhandled_token(self, token: Token) -> MarkdownBlock | None:
        """A horizontal rule, where it was written: Textual draws one at the top level, above
        the list, the quote or the frame it stands in, where an agent's would be a line of
        vivibox's own."""
        return MarkdownHorizontalRule(self, token) if token.type == RULE else None

    def __init__(self, **kwargs) -> None:
        self._as_source: frozenset[str] = frozenset()
        self._redrawing = False
        self._gone: set[int] = set()  # the widgets taken out for a redraw, until it is made
        super().__init__("", parser_factory=lambda: parser(self._as_source), **kwargs)

    def _parse_markdown(self, tokens: Iterable[Token]) -> Iterable[MarkdownBlock]:
        """Textual's widgets for the tokens, those of each piece an agent wrote made apart: a
        piece whose widgets cannot be made is drawn as its source, in its frame, and the rest
        of the panel as it is."""
        own: list[Token] = []
        piece: list[Token] = []
        for token in tokens:
            framing = token.type.startswith("blockquote_") and (token.meta or {}).get(PROSE)
            (piece if piece or framing else own).append(token)
            if framing and token.nesting < 0:
                yield from super()._parse_markdown(own)
                try:
                    made = list(super()._parse_markdown(piece))
                except Exception as error:
                    self.notify(COULD_NOT.format(type(error).__name__), severity="warning", markup=False)
                    made = list(
                        super()._parse_markdown([piece[0], source(piece[0].meta["source"], AT_80), token])
                    )
                yield from made
                own, piece = [], []
        yield from super()._parse_markdown(own + piece)

    def drew(self, error: Exception) -> bool:
        """Whether error was raised while a piece an agent wrote was drawn: by a widget inside a
        piece's frame, or in the layout of one. Read from the error's own traceback: a call in
        it is a method of such a widget, or lays one out (Textual's layout names the widget it
        arranges parent or widget). The widgets taken out for a redraw count while they are on
        their way out. An error with no such call is none of a piece's: a key's action, what
        follows a dialog's answer, a dialog's own handler, a worker."""
        at = error.__traceback__
        while at is not None:
            for name in ("self", "parent", "widget"):
                node = at.tb_frame.f_locals.get(name)
                if id(node) in self._gone:
                    return True
                while isinstance(node, Widget) and node is not self:
                    if isinstance(node, Quote) and node.has_class(PROSE) and self in node.ancestors:
                        return True
                    node = node.parent
            at = at.tb_next
        return False

    def gives_up(self, error: Exception) -> bool:
        """After an error raised while a piece of this text was drawn (drew): every piece an
        agent wrote in it is drawn again as its source, which no widget reads as anything, and
        stays so while the panel shows it; you are told once. The widgets that raised are taken
        out at once, and what else they raise until they are gone is the same error (a layout
        that fails does so wherever a size is asked for). False for any other error, and when
        the pieces were drawn as their source already: the error is not cured by that."""
        if not self.drew(error):
            return False
        found = pieces(self.source)
        if self._redrawing or found <= self._as_source:
            return self._redrawing
        self._as_source, self._redrawing = found, True
        self._gone = {id(node) for node in self.walk_children()}
        self.remove_children()  # out of the panel now; a widget that raised may never say it is gone
        self.app.call_later(self._redraw, type(error).__name__)
        return True

    def _redraw(self, error: str) -> None:
        self._redrawing, self._gone = False, set()  # what is raised from here on is not theirs
        # The drawing that failed may wait for ever on a widget that raised, the panel's lock held.
        self.lock = RLock()
        self.update(self.source)
        self.notify(COULD_NOT.format(error), severity="warning", markup=False)


class Kept:
    """For the app the panel is in: an error raised while the panel draws what an agent wrote
    does not end the view. Textual ends the app on any error of a widget, in its layout too,
    which no widget of the panel can catch for the others; here the panel draws the agent's
    pieces as their source instead (Panel.gives_up), once for a text, and says so. That error
    alone: every other one, and one that is still raised after that, is Textual's to handle as
    it always did, with its traceback and its return code."""

    def _handle_exception(self, error: Exception) -> None:
        try:
            given_up = any(panel.gives_up(error) for panel in self.screen_stack[0].query(Panel))
        except Exception:  # no screen left to ask, or the panel itself is what fails
            given_up = False
        if not given_up:
            super()._handle_exception(error)
