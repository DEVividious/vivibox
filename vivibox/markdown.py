"""The details panel's text, read as markdown. Much of it is not vivibox's: a plan, a question, a
review's notes, a criterion, a step of the writer's list. It keeps its headings, lists and
emphasis, and makes no link: nothing in it opens an address on your machine, hides one behind
other words, or is left out of what you read (but the name of a code block's language, which
the block's colours stand for).

Which links are vivibox's is settled by the function that wrote them, never by where one leads:
own_link signs a link with a key made when vivibox starts and kept nowhere, and the parser's last
rule (as_text) turns every other link into text, whatever syntax made it. Text that did not pass
through own_link has no way to the key, so no way to a link.

What is shown only: the text's source, what is stored and what is hashed stay as they were.
"""

from __future__ import annotations

import secrets

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_block.table import escapedSplit, getLine, table
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from textual.app import ComposeResult
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownBlock, MarkdownTable, MarkdownTableContent

from . import display

# In the memory of this process alone: no file, no log and no screen holds it.
_KEY = secrets.token_hex(16)


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


def _plain(children: list[Token]) -> list[Token]:
    """A line's tokens with every link that is not vivibox's as text: the label keeps its own
    emphasis and code, the address and the title follow it. An image is a link to a picture, and
    reads the same. What an entity spelled (&#x202e;) is escaped here: the source was escaped
    before it was parsed, and the parser made the character after."""
    out: list[Token] = []
    start, target, title = None, "", ""
    for child in children:
        if child.type == "link_open" and child.attrs.get("title") != _KEY:
            start, target = len(out), str(child.attrs.get("href", ""))
            title = str(child.attrs.get("title") or "")
        elif child.type == "link_close" and start is not None:
            label = "".join(token.content for token in out[start:])
            if after := link_text(label, target, title).removeprefix(label):
                out.append(_text(after))
            start = None
        elif child.type == "image":  # its words may hold a link of their own: that address too
            label = "".join(token.content for token in _plain(child.children or []))
            where, title = str(child.attrs.get("src", "")), str(child.attrs.get("title") or "")
            out.append(_text(link_text(label, where, title)))
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
    """The parser's rule for a table, which also says of a table whether one of its rows has
    more cells than the table has columns: markdown drops those cells without a word. The lines
    are the table's own, as the parser reads them inside a list or a quote, kept on the table
    for _whole_tables."""
    first = len(state.tokens)
    if silent:
        return table(state, start, end, silent)
    if not table(state, start, end, silent):
        return False
    lines = [getLine(state, n) for n in range(start, state.line)]
    if any(_cells(line) > _cells(lines[1]) for line in lines[2:]):
        state.tokens[first].meta = {"source": "\n".join(lines)}
    return True


def _whole_tables(tokens: list[Token]) -> list[Token]:
    """The tokens with every table that would be drawn without some of its cells (_table) as
    its source, in a code block: nothing of it is left out, and nothing in it is markdown."""
    out: list[Token] = []
    skip = False
    for token in tokens:
        if token.type == "table_open" and (source := (token.meta or {}).get("source")):
            skip = True
            out.append(_block("fence", display.terminal(source) + "\n"))
        if not skip:
            out.append(token)
        skip = skip and token.type != "table_close"
    return out


def _fence_words(tokens: list[Token]) -> list[Token]:
    """The tokens with the first line of a fenced block, when it says more than one word, in a
    line of its own above the block, as code: the widget takes that line for the language's name
    and draws none of it. The block keeps the first word, its language. One word alone is not
    drawn as text: it is the language, whose colours the block has, or has not."""
    out: list[Token] = []
    for token in tokens:
        words = token.info.split(None, 1) if token.type == "fence" else []
        if len(words) > 1:
            said = _block("code_inline", display.terminal(token.info.strip(), ""))
            said.block = False
            out += [_block("paragraph_open"), _block("inline", children=[said]), _block("paragraph_close")]
            token.info = words[0]
        out.append(token)
    return out


def as_text(state: StateCore) -> None:
    """The parser's last rule: after it, the only links left are those own_link wrote, and what
    markdown or the widget would leave out (a link's title, the words after a block's language,
    a row's cells past the table's columns) is in what is drawn."""
    state.tokens = _fence_words(_whole_tables(state.tokens))
    for token in state.tokens:
        if token.type == "inline" and token.children:
            token.children = _plain(token.children)


def parser() -> MarkdownIt:
    """Markdown as the panel reads it. A bare address is the text it is, never made a link
    (linkify). HTML is text too: the widget draws no tag, so one would be read out of sight, and
    what it wraps would lose its tags without a word (html). A link's definition on a line of its
    own stays on the screen, where the parser would take it out of the text (reference); a link
    that names one is then the text it was written as."""
    found = MarkdownIt("gfm-like", {"linkify": False, "html": False}).disable("reference")
    found.block.ruler.at("table", _table, {"alt": ["paragraph", "reference"]})
    found.core.ruler.push("as_text", as_text)
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
    """A table of the panel: Textual's, its cells drawn by Cells."""

    def compose(self) -> ComposeResult:
        for made in super().compose():
            yield Cells(made.headers, made.rows)


class Panel(Markdown):
    """The details panel's text. A click opens an address in your browser, and the only
    addresses a click finds are vivibox's own (own_link)."""

    BLOCKS: dict[str, type[MarkdownBlock]] = {**Markdown.BLOCKS, "table_open": Table}

    def __init__(self, **kwargs) -> None:
        super().__init__("", parser_factory=parser, **kwargs)
