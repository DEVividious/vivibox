"""Writing one key of config.toml or a project file, in place: the line the key has, commented out
or not, is replaced, and everything else stays as it was. The comments in those files are their
manual, so a change from the view must not throw them away."""

from __future__ import annotations

import json
import re
from pathlib import Path

HEADER = re.compile(r"^\[([^\]\n]+)\][ \t]*$", re.MULTILINE)


def render(value: object) -> str:
    """A value as TOML writes it: strings quoted, lists on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(v) for v in value) + "]"
    raise TypeError(f"cannot write {value!r} to a TOML file")


def _span(text: str, table: str) -> tuple[int, int] | None:
    """Where the table's body is: after its header, up to the next header. None when the file
    has no such table. "" is the top level, before the first header."""
    headers = list(HEADER.finditer(text))
    if not table:
        return 0, headers[0].start() if headers else len(text)
    for i, found in enumerate(headers):
        if found.group(1).strip() == table:
            end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            return found.end(), end
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
