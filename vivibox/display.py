"""What the agent chose (a file's name, a line of a file, what git said about them), as text you
read: a character with nothing to see of its own is shown as its code point between ‹ and ›
(ESC as ‹U+001B›), never sent to the terminal as itself, where it would move the cursor, erase a
line or turn the text around. ‹ and › are shown the same way, so what a file says can never read
like a character it does not have, and a backslash, a regex or a Windows path stays as it is.

What is shown only: digests, approvals and comparisons are of the original bytes, never of this.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

OPEN, CLOSE = "‹", "›"
# Unicode's categories of what has nothing to see: controls (C0, DEL, C1), format characters (bidi
# embeddings, overrides and isolates, zero-width characters, the BOM, tags), surrogates, private use,
# unassigned code points, the line and paragraph separators, and every space but the ASCII one.
INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})
NO_NEWLINE = "\\ No newline at end of file"
# Printable ASCII never needs a look; anything else (‹ and › among it) is looked up.
_LOOK = re.compile(r"[^\x20-\x7e]")


def code_point(c: str) -> str:
    return f"{OPEN}U+{ord(c):04X}{CLOSE}"


def _escape(text: str, keep: str = "", markers: bool = True) -> str:
    def one(m: re.Match) -> str:
        c = m.group()
        if c in keep:
            return c
        if c in (OPEN, CLOSE):
            return code_point(c) if markers else c
        return code_point(c) if unicodedata.category(c) in INVISIBLE else c

    return _LOOK.sub(one, text)


def name(text: str) -> str:
    """A file's name or path, or a message that carries one: on one line, every invisible
    character, a tab and a line break among them, as its code point."""
    return _escape(text)


def lines(text: str) -> list[str]:
    """text's lines, each with its \\n: a line ends there alone. What another tool may take for a
    line break (\\r for bash; FF, VT, NEL or U+2028 for Groovy, which takes them for spaces) stays
    in the line it is in, where content shows it: a line of its own here would be a line you read
    and no tool runs, or the other way round."""
    found = text.split("\n")
    return [line + "\n" for line in found[:-1]] + ([found[-1]] if found[-1] else [])


def content(line: str) -> str:
    """A line of a file (lines) as you read it: a tab stays, every other invisible character is
    shown, \\r too, and it ends in its \\n. A last line with no \\n says so, as git does."""
    if line.endswith("\n"):
        return _escape(line[:-1], "\t") + "\n"
    return _escape(line, "\t") + f"\n{NO_NEWLINE}\n"


def diff(found: Iterable[str]) -> str:
    """A unified diff of raw lines (lines), as you read it: computed on the files' own lines and
    escaped only here. Its first two lines carry names escaped already (name); a hunk's head
    starts with @, and every other line with its mark, the line of the file after it."""
    return "".join(
        line if i < 2 or line.startswith("@") else line[0] + content(line[1:]) for i, line in enumerate(found)
    )


def terminal(text: str, keep: str = "\n\t") -> str:
    """Text vivibox puts on the screen whole (the details panel's markdown, what the agent wrote
    in the list or a notification): every invisible character but those kept (a line break and a
    tab; none in a row of a list) as its code point, so none reaches the terminal as itself.
    ‹ and › stay: what was escaped already (name, content) is not escaped twice."""
    return _escape(text, keep, markers=False)


def fenced(text: str, info: str = "") -> str:
    """text as a markdown code block, read as it is: its fence longer than any run of backticks in
    it, so no line of it closes the block and what follows is never read as markdown."""
    ticks = "`" * max(3, 1 + max((len(run) for run in re.findall("`+", text)), default=0))
    return f"{ticks}{info}\n{text}\n{ticks}"


def code(text: str) -> str:
    """text as inline markdown code, read as it is, whatever backticks it has."""
    ticks = "`" * (1 + max((len(run) for run in re.findall("`+", text)), default=0))
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"
