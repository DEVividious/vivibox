"""What the agent chose (a file's name, a line of a file, what git said about them), as text you
read: a character with nothing to see of its own is shown as its code point between ‹ and ›
(ESC as ‹U+001B›), never sent to the terminal as itself, where it would move the cursor, erase a
line or turn the text around. ‹ and › are shown the same way, and so is a bracket drawn like
them around a code point (˂U+001B˃), so what a file says can never read like a character it does
not have, and a backslash, a regex or a Windows path stays as it is.

What is shown only: digests, approvals and comparisons are of the original bytes, never of this.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Iterable
from pathlib import Path

OPEN, CLOSE = "‹", "›"
# Unicode's categories of what has nothing to see: controls (C0, DEL, C1), format characters (bidi
# embeddings, overrides and isolates, zero-width characters, the BOM, tags), surrogates, private use,
# unassigned code points, the line and paragraph separators, and every space but the ASCII one.
INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})
# With nothing to see though their category says letter, mark or symbol: what Unicode calls
# default ignorable outside the categories above (the grapheme joiner, the Hangul fillers, the
# Khmer inherent vowels, the Mongolian and the general variation selectors) and the blank
# braille pattern.
UNSEEN = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u180f\u2800\u3164\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]"
)
# What the verification refuses in a line the work adds (gate.hidden_characters), kept beside
# what is shown escaped, so that the two are decided together and every character refused is
# one shown: the zero-width and bidi format characters and the tags, and of UNSEEN those that
# make a name read as another while no ordinary text has them: the Hangul fillers (letters, so
# a name of their own: the invisible variable) and the grapheme joiner. Not the variation
# selectors nor the blank braille pattern: an emoji in a project's own text carries U+FE0F and
# a spinner's frames U+2800, and work is not refused for those; they are shown, never refused.
HIDDEN = re.compile(
    "[\u034f\u115f\u1160\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u3164\ufeff\uffa0"
    "\U000e0000-\U000e007f]"
)
# Brackets many fonts draw like ‹ and ›: around a code point (˂U+001B˃) they would read as an
# escape that is not there, so there they are shown as the characters they are (name, content).
LIKE_OPEN = "\u02c2\u1438\u2329\u276e\u27e8\u3008\ufe64\uff1c"
LIKE_CLOSE = "\u02c3\u1433\u232a\u276f\u27e9\u3009\ufe65\uff1e"
_CODE, _CODE_BEFORE = re.compile(r"U\+[0-9A-F]{4,6}"), re.compile(r"U\+[0-9A-F]{4,6}\Z")
# The selectors of a character's text and emoji form, and what ends a keycap (1, U+FE0F, U+20E3).
EMOJI_SELECTORS, KEYCAP = "\ufe0e\ufe0f", "\u20e3"
NO_NEWLINE = "\\ No newline at end of file"
# Printable ASCII never needs a look; anything else (‹ and › among it) is looked up.
_LOOK = re.compile(r"[^\x20-\x7e]")


def code_point(c: str) -> str:
    return f"{OPEN}U+{ord(c):04X}{CLOSE}"


def _selects(text: str, at: int) -> bool:
    """Whether the selector at text[at] has something to see: after a symbol outside ASCII it
    picks that character's text or emoji form, and between a digit, # or * and U+20E3 it makes
    a keycap. After anything else, a second one in a row among it, it is drawn as nothing."""
    before = text[at - 1 : at]
    if before and before in "0123456789#*":
        return text[at + 1 : at + 2] == KEYCAP
    return bool(before) and ord(before) > 0x7F and unicodedata.category(before) in ("So", "Sm")


def _spells(c: str, m: re.Match) -> bool:
    """Whether c is a bracket like ‹ or › that stands where the notation's own would: before a
    code point, or after one."""
    if c in LIKE_OPEN:
        return bool(_CODE.match(m.string, m.end()))
    return c in LIKE_CLOSE and bool(_CODE_BEFORE.search(m.string, max(0, m.start() - 8), m.start()))


def _escape(text: str, keep: str = "", prose: bool = False) -> str:
    """prose: text put on the screen whole, where ‹ and › stay (what name or content escaped is
    not escaped twice) and so does the selector of an emoji; in a name or a line of a file
    every one of them is shown, as the name with it is another name."""

    def one(m: re.Match) -> str:
        c = m.group()
        if c in keep:
            return c
        if c in (OPEN, CLOSE):
            return c if prose else code_point(c)
        if prose and c in EMOJI_SELECTORS and _selects(m.string, m.start()):
            return c
        if not prose and _spells(c, m):
            return code_point(c)
        return code_point(c) if unicodedata.category(c) in INVISIBLE or UNSEEN.match(c) else c

    return _LOOK.sub(one, text)


def name(text: str) -> str:
    """A file's name or path, or a message that carries one: on one line, every invisible
    character, a tab and a line break among them, as its code point."""
    return _escape(text)


def said(text: str) -> str:
    """What a tool said of what it was asked (git's or Docker's words on stderr), for a message
    of vivibox's: each line as name shows it, whatever name the tool quoted in it, and every line
    after the first indented under it, so none reads as a line of vivibox's own. The one place
    such words are escaped: where the error is made."""
    found = [line.removesuffix("\r") for line in text.strip().split("\n")]
    return "\n    ".join(name(line) for line in found if line.strip())


def quoted(who: str, line: str) -> str:
    """A line someone else said (a build's output, which the agent's code prints), inside a
    sentence of vivibox's: said to be theirs, between « and », which the line cannot close (its
    own are shown as their code points), so nothing in it reads as what vivibox says after it.
    Nothing for no line."""
    return f"{who} said «{re.sub('[«»]', lambda m: code_point(m.group()), line)}»" if line else ""


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
    ‹ and › stay: what was escaped already (name, content) is not escaped twice. So does the
    selector of an emoji (U+FE0F after a symbol, or in a keycap), which has something to see."""
    return _escape(text, keep, prose=True)


def own_folder() -> Path:
    """A folder of yours alone under the temporary one, for what vivibox writes only to show it."""
    folder = Path(tempfile.gettempdir()) / f"vivibox-{os.getuid()}"
    folder.mkdir(mode=0o700, exist_ok=True)
    found = folder.lstat()
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() or found.st_mode & 0o077:
        raise OSError(f"{folder} is not a folder of yours alone")
    return folder


def terminal_copy(path: Path) -> Path:
    """path's text escaped (terminal), for a pager that passes every escape sequence on (cat,
    more): in a folder of yours alone under the temporary one, named by the path's digest, so the
    next copy of the same file takes its place. A file vivibox cannot read is said so there."""
    folder = own_folder()
    try:  # never through a link, never waiting on a FIFO
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise OSError(f"{path} is not a plain file")
            text = terminal(file.read().decode("utf-8", "surrogateescape"))
    except OSError as e:
        text = f"vivibox could not read {name(str(path))}: {name(e.strerror or str(e))}\n"
    copy = folder / f"{hashlib.sha256(str(path).encode('utf-8', 'surrogateescape')).hexdigest()}.txt"
    copy.write_text(text, encoding="utf-8")
    return copy


def fenced(text: str, info: str = "") -> str:
    """text as a markdown code block, read as it is: its fence longer than any run of backticks in
    it, so no line of it closes the block and what follows is never read as markdown. Nothing for
    no text: an empty block says nothing."""
    if not text.strip():
        return ""
    ticks = "`" * max(3, 1 + max((len(run) for run in re.findall("`+", text)), default=0))
    return f"{ticks}{info}\n{text}\n{ticks}"


def code(text: str) -> str:
    """text as inline markdown code, read as it is, whatever backticks it has."""
    ticks = "`" * (1 + max((len(run) for run in re.findall("`+", text)), default=0))
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"
