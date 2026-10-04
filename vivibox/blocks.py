"""The widgets the details panel's text is drawn with, for what markdown.py reads: the panel
itself, the frame around what an agent wrote, a code block that folds its long lines, a table
that is a grid only where it has room, and what keeps an error of theirs from ending the view.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress

from markdown_it.token import Token
from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Label, Markdown
from textual.widgets._markdown import (
    MarkdownBlock,
    MarkdownBlockQuote,
    MarkdownFence,
    MarkdownHorizontalRule,
    MarkdownTable,
    MarkdownTableContent,
)

from .markdown import MARK, PROSE, RULE, parser, pieces, source

COLUMN = 4
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
            written = source((self._token.meta or {}).get("source", ""))
            await self.remove_children()
            shown = Fence(self._markdown, written, written.content.rstrip()) if narrow else None
            await self.mount(shown or Cells(self._headers, self._rows))


def _rows(line: Content, width: int) -> list[Content]:
    """line on as many rows as it takes at width cells: cut between two characters, by the
    cells each takes on the screen and not by their count. A character two cells wide that no
    longer fits goes to the next row whole, and one that takes no cell (a combining mark) stays
    with the one before it. A row holds a character at least, however narrow it is. One row
    while the width is not known yet (0)."""
    if not width or line.cell_length <= width:
        return [line]
    cuts, used = [], 0
    for at, character in enumerate(line.plain):
        cells = cell_len(character)
        if used and used + cells > width:
            cuts.append(at)
            used = 0
        used += cells
    return line.divide(cuts)


class Fence(MarkdownFence):
    """A code block of the panel: a diff, a file, a log, a block in a plan. Every line of it is
    on the screen whole. Textual draws a line as one row and leaves what is past the block's
    width to a scroll sideways that nothing announces: of a line of a risky file with 200 spaces
    in it, the harmless start alone was shown where you approve the file "as shown". Here a line
    longer than the block is folded onto further rows, each with MARK in the first of two
    columns that are the block's own: no text of the block is drawn there, so no line of it
    can be drawn as the rest of the line above, or hide that it is. Folded again whenever the
    block's width changes; nothing scrolls sideways."""

    DEFAULT_CSS = """
    Fence {
        overflow: hidden hidden;
        & > Label {
            width: 1fr;
            padding: 1 2 1 0;
        }
    }
    Fence:ansi > Label {
        padding: 1 0;
    }
    """

    def __init__(self, markdown: Markdown, token: Token, code: str) -> None:
        super().__init__(markdown, token, code)
        self._width = 0

    @property
    def allow_horizontal_scroll(self) -> bool:
        return False

    def folded(self) -> Content:
        """The block's text for the width it has: its lines, tabs as the columns they take,
        each on the rows it needs, the two columns before each row blank or the mark."""
        mark = Content.styled(f"{MARK} ", "$text-muted")
        rows = [
            Content.assemble(mark if more else "  ", row)
            for line in self._highlighted_code.expand_tabs(8).split("\n", allow_blank=True)
            for more, row in enumerate(_rows(line, self._width))
        ]
        return Content("\n").join(rows)

    def on_resize(self, event: events.Resize) -> None:
        """The width its text has: the block's, less the two columns of the mark and the
        label's own padding."""
        try:
            label = self.query_one("#code-content", Label)
        except NoMatches:
            return
        width = max(1, event.size.width - 2 - label.styles.gutter.width)
        if width != self._width:
            self._width = width
            label.update(self.folded())

    def set_content(self, content: Content) -> None:
        self._content = content
        with suppress(NoMatches):
            self.query_one("#code-content", Label).update(self.folded())

    def compose(self) -> ComposeResult:
        yield Label(self.folded(), id="code-content", expand=True, markup=False)


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
                    made = list(super()._parse_markdown([piece[0], source(piece[0].meta["source"]), token]))
                yield from made
                own, piece = [], []
        yield from super()._parse_markdown(own + piece)

    def gives_up(self, error: Exception) -> bool:
        """After an error while this text was drawn: every piece an agent wrote in it is drawn
        again as its source, which no widget reads as anything, and stays so while the panel
        shows it; you are told once. The widgets that raised are taken out at once, and what
        else is raised until they are gone is the same error (a layout that fails does so
        wherever a size is asked for). False when there is no piece to draw so, or they were so
        drawn already: the error is not theirs."""
        found = pieces(self.source)
        if self._redrawing or found <= self._as_source:
            return self._redrawing
        self._as_source, self._redrawing = found, True
        self.call_later(self._redraw, type(error).__name__)
        return True

    async def _redraw(self, error: str) -> None:
        try:
            await self.remove_children()
        finally:
            self._redrawing = False  # what is raised from here on is not theirs
        await self.update(self.source)
        self.notify(COULD_NOT.format(error), severity="warning", markup=False)


class Kept:
    """For the app the panel is in: an error raised while the panel draws what an agent wrote
    does not end the view. Textual ends the app on any error of a widget, in its layout too,
    which no widget of the panel can catch for the others; here the panel draws the agent's
    pieces as their source instead (Panel.gives_up), once for a text, and says so. An error
    that is still raised after that ends the view as before."""

    def _handle_exception(self, error: Exception) -> None:
        try:
            given_up = any(panel.gives_up(error) for panel in self.screen_stack[0].query(Panel))
        except Exception:  # no screen left to ask, or the panel itself is what fails
            given_up = False
        if not given_up:
            super()._handle_exception(error)
