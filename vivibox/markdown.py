"""The details panel's text, read as markdown. Much of it is not vivibox's: a plan, a question, a
review's notes, a criterion, a step of the writer's list. It keeps its headings, lists and
emphasis, and makes no link: nothing in it opens an address on your machine, hides one behind
other words, or is left out of what you read (but the name of a code block's language, which
the block's colours stand for; a word there that is no language is drawn).

Which links are vivibox's is settled by the function that wrote them, never by where one leads:
own_link signs a link with a key made when vivibox starts and kept nowhere, and the parser's last
rule (as_text) turns every other link into text, whatever syntax made it. Text that did not pass
through own_link has no way to the key, so no way to a link.

The widgets that draw what this module reads are in blocks.py.

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
from collections.abc import Collection, Iterable

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_block.table import escapedSplit, getLine, table
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

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


def pieces(text: str) -> frozenset[str]:
    """Every piece an agent wrote in the panel's text, as it stands between its marks."""
    return frozenset(_PIECE.findall(text))


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


def _frame(nesting: int, source: str = "") -> Token:
    """One end of the frame around a piece: a quote's token, which the widget knows how to nest,
    marked as vivibox's (PROSE), which a quote written as text never is; the piece's source
    with it, for where its widgets cannot be made (Panel)."""
    token = Token("blockquote_open" if nesting > 0 else "blockquote_close", "blockquote", nesting)
    token.block, token.meta = True, {PROSE: True, "source": source}
    return token


def source(text: str, room: int) -> Token:
    """A piece, or a table, as its source: a code block, the mark of a folded row in it as its
    code point, as in every block; with the columns it has in a terminal of 80 (room)."""
    block = _block("fence", text.replace(MARK, _MARKED) + "\n")
    block.meta = {ROOM_AT_80: room}
    return block


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


# What a piece's text has in a terminal of 80 columns: the terminal's width less the panel's
# padding (2), its text's (4), a scrollbar (2) and the frame (2). And the fewest columns a block
# of a piece is drawn in; a table asks for more by its columns, and settles that itself (Table).
AT_80 = 70
# On a code block's or a table's token: the columns it has in a terminal of 80 (_narrowest).
# One of vivibox's own, outside every piece, has no such word and the frame's two columns more.
ROOM_AT_80 = "room"
ROOM = 10
_BLOCKS = ("paragraph_open", "heading_open", "fence", "code_block", "table_open", "hr", RULE)


def _narrowest(tokens: list[Token]) -> int:
    """The columns the narrowest block among a piece's tokens has in a terminal of 80, by what it
    stands in, as the widgets draw each: a quote takes three columns (its bar, its padding), a
    list's item the two of its bullet, a numbered list's item the digits of the list's last
    number and three more (123456789. takes twelve, a level)."""
    taken: list[int] = []  # by each quote and list item a block stands in
    numbers: list[int] = []  # the width of the numbers of each numbered list it stands in
    least = AT_80
    for at, token in enumerate(tokens):
        if token.type == "ordered_list_open":
            items = [
                t for t in _inside(tokens, at) if t.type == "list_item_open" and t.level == token.level + 1
            ]
            first = int(items[0].info) if items and items[0].info.isdigit() else 1
            numbers.append(len(str(first + len(items) - 1)) + 3)
        elif token.type == "ordered_list_close":
            numbers.pop()
        elif token.type in ("blockquote_open", "list_item_open"):
            taken.append(3 if token.type == "blockquote_open" else numbers[-1] if token.info else 2)
        elif token.type in ("blockquote_close", "list_item_close"):
            taken.pop()
        elif token.type in _BLOCKS:
            room = AT_80 - sum(taken)
            least = min(least, room)
            if token.type in ("fence", "code_block", "table_open"):  # what a code block reckons by
                token.meta = {**(token.meta or {}), ROOM_AT_80: room}
    return least


def _inside(tokens: list[Token], at: int) -> list[Token]:
    """The tokens between the one that opens at at and the one that closes it."""
    end = next(
        i for i in range(at + 1, len(tokens)) if tokens[i].level == tokens[at].level and tokens[i].nesting < 0
    )
    return tokens[at + 1 : end]


def _whole(tokens: list[Token], deepest: int) -> bool:
    """Whether a piece is on the screen whole when drawn as markdown, in a terminal of 80 columns
    or wider. Not when the parser left some of it out: it reads so many levels deep (its
    maxNesting) and drops what a quote or a list item past that holds, with everything after it
    to the end, without a word. And not when a block of it has fewer than ROOM columns: six
    numbered lists one in another leave the last none, and its words were drawn nowhere."""
    dropped = any(t.type in ("blockquote_open", "list_item_open") and t.level >= deepest for t in tokens)
    return not dropped and _narrowest(tokens) >= ROOM


def _piece(text: str, as_source: bool = False) -> list[Token]:
    """What an agent wrote, parsed as a document of its own, its headings bold lines; and what
    cannot be drawn whole (_whole), or was found to make the widgets raise (as_source), as its
    source, in a code block, where nothing of it is left out and nothing is read as markdown."""
    rules = _rules()
    tokens = [] if as_source else rules.parse(text, {PROSE: True})
    if as_source or not _whole(tokens, rules.options["maxNesting"] - 1):
        return [source(text, AT_80)]
    return _demoted(tokens)


def parser(as_source: Collection[str] = ()) -> MarkdownIt:
    """The panel's text as it is read: by _rules, vivibox's own lines apart from every piece an
    agent wrote (_apart), each put where it stood, in a frame. as_source: the pieces to draw as
    their source, whatever they hold (Panel.gives_up)."""

    def spliced(state: StateCore) -> None:
        out: list[Token] = []
        for token in state.tokens:
            piece = token.type == PROSE and _piece(token.content, token.content in as_source)
            out += [_frame(1, token.content), *piece, _frame(-1)] if token.type == PROSE else [token]
        state.tokens = out

    found = _rules()
    found.core.ruler.at("block", _apart)
    found.core.ruler.push("spliced", spliced)
    return found
