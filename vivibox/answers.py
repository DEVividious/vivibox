"""An answer an agent's CLI brings in (a plan, a review): from a file named, from standard input
with "-" or when something is piped in, else None for the task's own answer file."""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path


def _socket(stream) -> bool:
    """A socket for stdin, as Claude Code's shell gives a command, stays open with nothing on it:
    read, it never ends. What is piped or redirected is a pipe or a file."""
    try:
        return stat.S_ISSOCK(os.fstat(stream.fileno()).st_mode)
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        return False


def read(given: str | None) -> str | None:
    """given: the command's argument, None or "" when it named no file."""
    if given == "-" or (not given and not sys.stdin.isatty() and not _socket(sys.stdin)):
        # An agent's shell has no terminal and nothing on stdin: the answer file, not an empty answer.
        return sys.stdin.read() or None
    return Path(given).expanduser().read_text() if given else None
