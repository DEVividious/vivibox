"""The host's reads and writes of files in a task's handoff/. The agent's pod mounts that folder
read-write, so the agent names every file in it: a link there could point the host at any file of
yours, and a FIFO could hold it on open forever. Each file is opened without following a link and
without waiting, checked to be a plain file of one name, and only then emptied, written or read
through the descriptor that was checked. progress.mirror keeps the same rules for criteria.md."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# What open says when the name is a link (ELOOP; EMLINK on BSD), a FIFO nobody reads (ENXIO) or a
# folder (EISDIR): none of them is a plain file.
NOT_PLAIN = {errno.ELOOP, errno.EMLINK, errno.ENXIO, errno.EISDIR}


class HandoffError(OSError):
    """A file in handoff/ that vivibox did not write or read, and why, in a sentence for you."""


def _refused(path: Path, verb: str) -> HandoffError:
    return HandoffError(
        f"handoff/{path.name} is a link or not a plain file, and vivibox does not {verb} through"
        " one; delete it and try again"
    )


@contextmanager
def _opened(path: Path, flags: int, mode: str, empty: bool = False) -> Iterator[IO]:
    """The file, checked before anything is emptied: emptying on open would empty a file a hard
    link shares before the check could refuse it."""
    verb = "read" if "r" in mode else "write"
    try:
        fd = os.open(path, flags | FLAGS, 0o644)
    except FileNotFoundError:
        if flags & os.O_CREAT:
            raise HandoffError(f"vivibox could not write handoff/{path.name}: its folder is gone") from None
        raise
    except OSError as e:
        if e.errno in NOT_PLAIN:
            raise _refused(path, verb) from e
        raise HandoffError(f"vivibox could not {verb} handoff/{path.name}: {e.strerror}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _refused(path, verb)
        if empty:
            os.ftruncate(fd, 0)
        file = os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8"}))
    except BaseException:
        os.close(fd)
        raise
    with file:
        yield file


def write_text(path: Path, text: str) -> None:
    """The whole file, replaced."""
    with _opened(path, os.O_WRONLY | os.O_CREAT, "w", empty=True) as file:
        file.write(text)


def append_text(path: Path, text: str) -> None:
    with _opened(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, "a") as file:
        file.write(text)


def copy_file(src: Path, path: Path) -> None:
    """src is the host's own (the gate's log); only the copy lands where the agent names files."""
    with open(src, "rb") as source, _opened(path, os.O_WRONLY | os.O_CREAT, "wb", empty=True) as file:
        shutil.copyfileobj(source, file)


def read_text(path: Path) -> str:
    """The file's text, or "" when there is none."""
    try:
        with _opened(path, os.O_RDONLY, "r") as file:
            return file.read()
    except FileNotFoundError:
        return ""
