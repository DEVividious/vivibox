"""Writing one key of config.toml or a project file, in place: the line the key has, commented out
or not, is replaced, and everything else stays as it was. The comments in those files are their
manual, so a change from the view must not throw them away."""

from __future__ import annotations

import json
import re
from pathlib import Path

# A table's header: its name, keys bare or quoted and joined by dots, in brackets, indented or not,
# with a note after it or not; an array of tables' [[name]] ends a table too, and is never one, its
# name keeping the brackets.
_KEY = r"""(?:[A-Za-z0-9_-]+|"(?:\\.|[^"\\\n])*"|'[^'\n]*')"""
_NAME = rf"[ \t]*{_KEY}(?:[ \t]*\.[ \t]*{_KEY})*[ \t]*"
HEADER = re.compile(rf"^[ \t]*\[(\[{_NAME}\]|{_NAME})\][ \t]*(?:#[^\n]*)?$", re.MULTILINE)
# What opens or closes a value that goes on over lines: a bracket, a brace, the quotes of a
# multi-line string; a string on one line and a note are passed over whole.
_TOKEN = re.compile(r"\"\"\"|'''|\"(?:\\.|[^\"\\\n])*\"|'[^'\n]*'|#.*|[\[\]{}]")
_CLOSES = {'"""': re.compile(r'(?:\\.|[^\\])*?"""(?!")', re.S), "'''": re.compile(r".*?'''(?!')", re.S)}


def _name(header: str) -> str:
    """A header's table as set_value names it: "[ roles . writer ]" is "roles.writer"."""
    return re.sub(r"[ \t]*\.[ \t]*", ".", header.strip())


def _scan(line: str, depth: int, closing: str | None) -> tuple[int, str | None]:
    """The brackets and braces still open after the line, and the quotes that end the multi-line
    string it leaves open, if any."""
    i = 0
    while True:
        if closing:
            end = _CLOSES[closing].match(line, i)
            if end is None:
                return depth, closing
            i, closing = end.end(), None
        token = _TOKEN.search(line, i)
        if token is None:
            return depth, None
        i, mark = token.end(), token.group()
        if mark in _CLOSES:
            closing = mark
        elif mark in ("[", "{"):
            depth += 1
        elif mark in ("]", "}"):
            depth = max(depth - 1, 0)


def _headers(text: str) -> list[tuple[int, int, str]]:
    """Where each header starts and ends, and its name. A line of an array, an inline table or a
    multi-line string that looks like a header is none."""
    headers: list[tuple[int, int, str]] = []
    depth, closing, pos = 0, None, 0
    for line in text.split("\n"):
        found = HEADER.match(line) if depth == 0 and closing is None else None
        if found:
            headers.append((pos + found.start(), pos + found.end(), found.group(1)))
        else:
            depth, closing = _scan(line, depth, closing)
        pos += len(line) + 1
    return headers


def render(value: object) -> str:
    """A value as TOML writes it: strings quoted, lists on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(v) for v in value) + "]"
    raise TypeError(f"cannot write {value!r} to a TOML file")


def _span(text: str, table: str) -> tuple[int, int] | None:
    """Where the table's body is: after its header, up to the next header. None when the file
    has no such table. "" is the top level, before the first header."""
    headers = _headers(text)
    if not table:
        return 0, headers[0][0] if headers else len(text)
    for i, (_, found_end, name) in enumerate(headers):
        if _name(name) == table:
            end = headers[i + 1][0] if i + 1 < len(headers) else len(text)
            return found_end, end
    return None


def set_value(path: Path, key: str, value: object, table: str = "") -> None:
    """Writes key = value into the table ("" for the top level). The key's own line, live or
    commented out, is replaced, a note after the value included; a key the table lacks goes right
    under its header; a table the file lacks is added at the end."""
    text = path.read_text() if path.exists() else ""
    line = f"{key} = {render(value)}"
    found = _span(text, table)
    if found is None:
        path.write_text(
            text.rstrip("\n") + f"\n\n[{table}]\n{line}\n" if text.strip() else f"[{table}]\n{line}\n"
        )
        return
    start, end = found
    body = text[start:end]
    # The key's line: a list, a quoted string, or anything else, then a note after it, if any.
    value_part = r"(\[[^\]]*\]|\"(?:[^\"\\\\]|\\\\.)*\"|[^\n]*?)"
    own = re.compile(
        rf"^[ \t]*#?[ \t]*{re.escape(key)}[ \t]*=[ \t]*{value_part}[ \t]*(#[^\n]*)?$", re.MULTILINE
    )
    body, n = own.subn(line, body, count=1)
    if n == 0:
        body = ("\n" if table else "") + line + ("\n" if not body.startswith("\n") else "") + body
    path.write_text(text[:start] + body + text[end:])
