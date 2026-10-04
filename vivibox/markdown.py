"""The details panel's text, read as markdown. Much of it is not vivibox's: a plan, a question, a
review's notes, a criterion, a step of the writer's list. It keeps its headings, lists and
emphasis, and makes no link: nothing in it opens an address on your machine, hides one behind
other words, or is left out of what you read (but the name of a code block's language, which
the block's colours stand for; a word there that is no language is drawn).

Which links are vivibox's is settled by the function that wrote them, never by where one leads:
own_link signs a link with a key made when vivibox starts and kept nowhere, and the parser's last
rule (as_text) turns every other link into text, whatever syntax made it. Text that did not pass
through own_link has no way to the key, so no way to a link.

What an agent wrote is also read apart from vivibox's own lines. prose puts a piece of it
between two marks made of the same key, and the parser reads what stands between them on its
own, as a document of its own, and puts the result in a frame (a rule down its left, Quote):
no block the piece opens goes on into the lines after it, no heading in it is a heading (a bold
line instead, whatever its level and however it was written), and no line outside a frame is an
agent's.

What is shown only: the text's source, what is stored and what is hashed stay as they were.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from contextlib import suppress

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_block.table import escapedSplit, getLine, table
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
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

from . import display

# In the memory of this process alone: no file, no log and no screen holds it.
_KEY = secrets.token_hex(16)


# The marks around what an agent wrote, and what stands between two of them; a mark with no end
# (text cut by whatever carried it) leaves the rest the agent's, never the other way round.
_OPEN = f"<agent {_KEY}>"
_CLOSE = f"</agent {_KEY}>"
_PIECE = re.compile(f"{re.escape(_OPEN)}\n(.*?)(?:\n{re.escape(_CLOSE)}|\\Z)", re.DOTALL)
# What says of a token that it is the frame vivibox put around a piece: no text sets it.
PROSE = "prose"
# A horizontal rule's token, by a name the widget leaves to Panel.unhandled_token.
RULE = "rule"
# What says of a row of a code block that it goes on from the row above (Fence), in a column of
# the block's own; the same character in a block's text is shown as its code point.
MARK = "\u21b3"
_MARKED = display.code_point(MARK)


def prose(text: str) -> str:
    """What an agent wrote (a plan, a question, a review's notes, its list of steps), for the
    panel's text: the one way such words go into it. They are read as markdown on their own,
    apart from the lines before and after, with their headings as bold lines, and drawn behind
    a rule that vivibox's own lines never have. Nothing for no text."""
    text = text.replace(_OPEN, "").replace(_CLOSE, "")  # a piece within a piece is one piece
    return f"{_OPEN}\n{text}\n{_CLOSE}" if text.strip() else ""


def listed(items: Iterable[str]) -> str:
    """A list whose items are an agent's words (criteria, the steps of its working list), as one
    piece: each item on a line of its own, whatever line breaks it held, so an item is one item
    of the list and never a block after it."""
    return prose("\n".join(f"- {' '.join(item.split())}" for item in items))


def own_link(label: str, url: str) -> str:
    """A link vivibox itself puts in the panel (an address of what the task is running): the only
    kind a click opens. label and url are vivibox's own, never text an agent wrote."""
    return f'[{label}]({url} "{_KEY}")'


def link_text(label: str, target: str, title: str = "") -> str:
    """A link that is not vivibox's, as the text it is shown as: its label, then the address it
    leads to, whole, in parentheses, and after the address the link's title in quotes, when it has
    one; the address alone where the label is it or says nothing. The one place that decides the
    form."""
    where = f'{target} "{title}"' if title else target
    return where if label in ("", target) else f"{label} ({where})"


def _text(content: str) -> Token:
    token = Token("text", "", 0)
    token.content = display.terminal(content)
    return token


def _plain(children: list[Token], agents: bool = False) -> list[Token]:
    """A line's tokens with every link that is not vivibox's as text: the label keeps its own
    emphasis and code, the address and the title follow it. An image is a link to a picture, and
    reads the same. What an entity spelled (&#x202e;) is escaped here: the source was escaped
    before it was parsed, and the parser made the character after. agents: the line is of a
    piece an agent wrote, where no link is vivibox's, whatever its title."""
    out: list[Token] = []
    # The link that is open: where its label starts, where it leads, its title. A picture in its
    # label has an address and a title of its own, and never touches these.
    start, target, title = None, "", ""
    for child in children:
        if child.type == "link_open" and (agents or child.attrs.get("title") != _KEY):
            start, target = len(out), str(child.attrs.get("href", ""))
            title = str(child.attrs.get("title") or "")
        elif child.type == "link_close" and start is not None:
            label = "".join(token.content for token in out[start:])
            if after := link_text(label, target, title).removeprefix(label):
                out.append(_text(after))
            start = None
        elif child.type == "image":  # its words may hold a link of their own: that address too
            words = "".join(token.content for token in _plain(child.children or [], agents))
            picture, named = str(child.attrs.get("src", "")), str(child.attrs.get("title") or "")
            out.append(_text(link_text(words, picture, named)))
        else:
            if child.type in ("text", "code_inline"):
                child.content = display.terminal(child.content)
            out.append(child)
    return out


def _block(kind: str, content: str = "", children: list[Token] | None = None) -> Token:
    token = Token(kind, "", 0)
    token.block, token.content, token.children = True, content, children
    return token


def _cells(line: str) -> int:
    """How many cells a row of a table has, counted as the parser counts them."""
    found = escapedSplit(line.strip())
    return len(found) - (found[:1] == [""]) - (len(found) > 1 and found[-1:] == [""])


def _table(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    """The parser's rule for a table, which also keeps the table's source on it, and whether one
    of its rows has more cells than the table has columns (cut): markdown drops those cells
    without a word. The lines are the table's own, as the parser reads them inside a list or a
    quote; _whole_tables and Table draw them where the table cannot be drawn whole."""
    first = len(state.tokens)
    if silent:
        return table(state, start, end, silent)
    if not table(state, start, end, silent):
        return False
    lines = [getLine(state, n) for n in range(start, state.line)]
    cut = any(_cells(line) > _cells(lines[1]) for line in lines[2:])
    state.tokens[first].meta = {"source": "\n".join(lines), "cut": cut}
    return True


def _whole_tables(tokens: list[Token]) -> list[Token]:
    """The tokens with every table that would be drawn without some of its cells (_table) as
    its source, in a code block: nothing of it is left out, and nothing in it is markdown."""
    out: list[Token] = []
    skip = False
    for token in tokens:
        if token.type == "table_open" and (token.meta or {}).get("cut"):
            skip = True
            out.append(_block("fence", display.terminal(token.meta["source"]) + "\n"))
        if not skip:
            out.append(token)
        skip = skip and token.type != "table_close"
    return out


def _language(word: str) -> bool:
    """Whether the highlighter that colours a block knows word for a language: asked of pygments
    the way Textual asks it for the block (textual.highlight), by a lexer's name or alias,
    whatever the case of the letters."""
    try:
        get_lexer_by_name(word)
    except ClassNotFound:
        return False
    return True


def _fence_words(tokens: list[Token]) -> list[Token]:
    """The tokens with the first line of a fenced block, when it says more than a language, in a
    line of its own above the block, as code: the widget takes that line for the language's name
    and draws none of it. The block keeps the first word, its language. One word alone is drawn
    as the block's colours when the highlighter knows it for a language (_language), and as
    words when it does not: IGNORE_THE_PLAN is no language."""
    out: list[Token] = []
    for token in tokens:
        words = token.info.split(None, 1) if token.type == "fence" else []
        if len(words) > 1 or (words and not _language(words[0])):
            said = _block("code_inline", display.terminal(token.info.strip(), ""))
            said.block = False
            out += [_block("paragraph_open"), _block("inline", children=[said]), _block("paragraph_close")]
            token.info = words[0]
        out.append(token)
    return out


def as_text(state: StateCore) -> None:
    """The last rule of _rules: after it, the only links left are those own_link wrote, and what
    markdown or the widget would leave out (a link's title, the words after a block's language,
    a row's cells past the table's columns) is in what is drawn."""
    state.tokens = _fence_words(_whole_tables(state.tokens))
    for token in state.tokens:
        if token.type == "inline" and token.children:
            token.children = _plain(token.children, bool(state.env.get(PROSE)))
        elif token.type == "hr":  # the widget draws an hr above whatever it stands in: Panel
            token.type = RULE
        elif token.type in ("fence", "code_block"):  # the mark of a folded line is Fence's alone
            token.content = token.content.replace(MARK, _MARKED)


def _rules() -> MarkdownIt:
    """Markdown as the panel reads it, vivibox's own lines and an agent's piece alike. A bare
    address is the text it is, never made a link (linkify). HTML is text too: the widget draws no
    tag, so one would be read out of sight, and what it wraps would lose its tags without a word
    (html). A link's definition on a line of its own stays on the screen, where the parser would
    take it out of the text (reference); a link that names one is then the text it was written
    as."""
    found = MarkdownIt("gfm-like", {"linkify": False, "html": False}).disable("reference")
    found.block.ruler.at("table", _table, {"alt": ["paragraph", "reference"]})
    found.core.ruler.push("as_text", as_text)
    return found


def _frame(nesting: int) -> Token:
    """One end of the frame around a piece: a quote's token, which the widget knows how to nest,
    marked as vivibox's (PROSE), which a quote written as text never is."""
    token = Token("blockquote_open" if nesting > 0 else "blockquote_close", "blockquote", nesting)
    token.block, token.meta = True, {PROSE: True}
    return token


def _demoted(tokens: list[Token]) -> list[Token]:
    """A piece's tokens with every heading as a bold line: a paragraph, whatever the heading's
    level, whether it was written with # or with a line under it, and wherever it stands (in a
    list, in a quote). Decided on what the parser found, never on how a line begins: the panel's
    headings are vivibox's sections, and its own alone."""
    for at, token in enumerate(tokens):
        if token.type in ("heading_open", "heading_close"):
            token.type, token.tag, token.markup = token.type.replace("heading", "paragraph"), "p", ""
            if token.nesting > 0 and (line := tokens[at + 1]).type == "inline":
                line.children = [Token("strong_open", "strong", 1), *(line.children or [])]
                line.children.append(Token("strong_close", "strong", -1))
    return tokens


def _apart(state: StateCore) -> None:
    """In the place of the parser's rule for blocks: vivibox's own lines are read as blocks, run by
    run, and each piece an agent wrote (prose) is left out of that reading whole, kept for
    _spliced. Whatever a piece opens (a fence, a list, a quote, a table) has nothing after it to
    go on into, and the lines around it are read as if nothing stood between them and it."""
    for at, part in enumerate(_PIECE.split(state.src)):
        if at % 2:
            state.tokens.append(_block(PROSE, part))
        else:
            state.md.block.parse(part, state.md, state.env, state.tokens)


def _spliced(state: StateCore) -> None:
    """The parser's last rule: each piece an agent wrote is parsed as a document of its own, by
    the same rules, and its tokens put where it stood, in a frame, its headings bold lines."""
    out: list[Token] = []
    for token in state.tokens:
        if token.type == PROSE:
            out += [_frame(1), *_demoted(_rules().parse(token.content, {PROSE: True})), _frame(-1)]
        else:
            out.append(token)
    state.tokens = out


def parser() -> MarkdownIt:
    """The panel's text as it is read: by _rules, vivibox's own lines apart from every piece an
    agent wrote (_apart, _spliced)."""
    found = _rules()
    found.core.ruler.at("block", _apart)
    found.core.ruler.push("spliced", _spliced)
    return found


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

    def compose(self) -> ComposeResult:
        for cell in super().compose():
            cell.tooltip = None  # before it is on the screen: nothing of it is ever a tooltip
            yield cell


class Table(MarkdownTable):
    """A table of the panel: Textual's, its cells drawn by Cells, while every column has room
    for COLUMN characters of text a row. Columns share the width, and with many of them a cell
    is a character wide, or has no width at all: thirty columns at 80 were an empty grid. On a
    panel too narrow for its columns the table is drawn as its source, in a code block, where
    every cell is on the screen whatever the width (Fence). Decided again whenever the width
    changes."""

    COLUMN = 4
    DEFAULT_CSS = """
    Table > Fence {
        margin: 0;
    }
    """

    def __init__(self, markdown: Markdown, token: Token, *args, **kwargs) -> None:
        super().__init__(markdown, token, *args, **kwargs)
        self._as_source = False

    def compose(self) -> ComposeResult:
        for made in super().compose():
            yield Cells(made.headers, made.rows)

    def room(self, width: int) -> int:
        """The room a cell's text has at that width: a column's share of it, less the line
        between two columns and a cell's padding."""
        columns = len(self._headers) or 1
        return (width - columns - 1) // columns - 2

    async def on_resize(self, event: events.Resize) -> None:
        """A table drawn as its source is a grid again only at a width with room to spare for
        the panel's scrollbar: the two differ in height, and one that needs the scrollbar while
        the other does not would be drawn in turn for ever."""
        spare = self._markdown.parent.styles.scrollbar_size_vertical if self._as_source else 0
        narrow = self.room(event.size.width - spare) < self.COLUMN
        if narrow != self._as_source:
            self._as_source = narrow
            source = display.terminal((self._token.meta or {}).get("source", "")).replace(MARK, _MARKED)
            await self.remove_children()
            shown = Fence(self._markdown, _block("fence", source), source) if narrow else None
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
        super().__init__("", parser_factory=parser, **kwargs)
