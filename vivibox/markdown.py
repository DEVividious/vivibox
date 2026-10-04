"""The details panel's text, read as markdown. Much of it is not vivibox's: a plan, a question, a
review's notes, a criterion, a step of the writer's list. It keeps its headings, lists and
emphasis, and makes no link: nothing in it opens an address on your machine, hides one behind
other words, or is left out of what you read.

Which links are vivibox's is settled by the function that wrote them, never by where one leads:
own_link signs a link with a key made when vivibox starts and kept nowhere, and the parser's last
rule (as_text) turns every other link into text, whatever syntax made it. Text that did not pass
through own_link has no way to the key, so no way to a link.

What is shown only: the text's source, what is stored and what is hashed stay as they were.
"""

from __future__ import annotations

import secrets

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from textual.widgets import Markdown

from . import display

# In the memory of this process alone: no file, no log and no screen holds it.
_KEY = secrets.token_hex(16)


def own_link(label: str, url: str) -> str:
    """A link vivibox itself puts in the panel (an address of what the task is running): the only
    kind a click opens. label and url are vivibox's own, never text an agent wrote."""
    return f'[{label}]({url} "{_KEY}")'


def link_text(label: str, target: str) -> str:
    """A link that is not vivibox's, as the text it is shown as: its label, then the address it
    leads to, whole, in parentheses; the address alone where the label is it or says nothing. The
    one place that decides the form."""
    return target if label in ("", target) else f"{label} ({target})"


def _text(content: str) -> Token:
    token = Token("text", "", 0)
    token.content = display.terminal(content)
    return token


def _plain(children: list[Token]) -> list[Token]:
    """A line's tokens with every link that is not vivibox's as text: the label keeps its own
    emphasis and code, the address follows it. An image is a link to a picture, and reads the
    same. What an entity spelled (&#x1b;) is escaped here: the source was escaped before it was
    parsed, and the parser made the character after."""
    out: list[Token] = []
    start, target = None, ""
    for child in children:
        if child.type == "link_open" and child.attrs.get("title") != _KEY:
            start, target = len(out), str(child.attrs.get("href", ""))
        elif child.type == "link_close" and start is not None:
            label = "".join(token.content for token in out[start:])
            if after := link_text(label, target).removeprefix(label):
                out.append(_text(after))
            start = None
        elif child.type == "image":
            label = "".join(token.content for token in child.children or [])
            out.append(_text(link_text(label, str(child.attrs.get("src", "")))))
        else:
            if child.type in ("text", "code_inline"):
                child.content = display.terminal(child.content)
            out.append(child)
    return out


def as_text(state: StateCore) -> None:
    """The parser's last rule: after it, the only links left are those own_link wrote."""
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
    found.core.ruler.push("as_text", as_text)
    return found


class Panel(Markdown):
    """The details panel's text. A click opens an address in your browser, and the only
    addresses a click finds are vivibox's own (own_link)."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", parser_factory=parser, **kwargs)
