"""The host's reads of a task's clone, which the agent's pod mounts read-write: the agent names
every file in it, and can change it while the host reads. The walk to a file starts at the clone's
root and opens one name at a time from the folder it holds, never through a link: a link is read
and its target walked the same way, only while it stays inside the root (no absolute target, no
.. above the root, at most MAX_LINKS of them). A FIFO, a socket or a device is refused before it is
opened; the file is then opened without following a link and without waiting, and checked again to
be a plain file through its descriptor. A name swapped for a link after the look fails the open;
a link swapped after it was read is not consulted again. Your checkout is read the same way, so a
project is detected alike in both."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .handoff import FLAGS, NOT_PLAIN

MAX_LINKS = 40
KINDS = {
    stat.S_IFIFO: "a FIFO", stat.S_IFSOCK: "a socket", stat.S_IFCHR: "a device", stat.S_IFBLK: "a device",
}  # fmt: skip
OUT = "a link that leads out of the repository"


class Refused(OSError):
    """A path of the repository vivibox does not read: a link that leads out of it, or a file that
    is not a plain one (a FIFO, a socket, a device). kind says which, the same for the same thing."""

    def __init__(self, path: str, kind: str):
        super().__init__(f"{path} is {kind}, and vivibox does not read it")
        self.kind = kind


def _reach(root: Path, path: Path | str, follow: bool = True) -> int | str:
    """The descriptor of the plain file at path (under root, or relative to it); without follow,
    the target of a link that is its last name instead. FileNotFoundError when there is none,
    IsADirectoryError for a folder, Refused for the rest."""
    try:
        rel = Path(path).relative_to(root) if Path(path).is_absolute() else Path(path)
    except ValueError:
        raise Refused(str(path), OUT) from None
    parts, links = list(rel.parts), 0
    folders = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        while parts:
            name = parts.pop(0)
            if name in ("", "."):
                continue
            if name == "..":
                if len(folders) == 1:
                    raise Refused(str(rel), OUT)
                os.close(folders.pop())
                continue
            info = os.stat(name, dir_fd=folders[-1], follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=folders[-1])
                if not follow and not parts:
                    return target
                links += 1
                if links > MAX_LINKS or target.startswith("/"):
                    raise Refused(str(rel), OUT)
                parts[:0] = target.split("/")
            elif parts and stat.S_ISDIR(info.st_mode):
                folders.append(_open(name, os.O_DIRECTORY, folders[-1], rel))
            elif parts:
                raise NotADirectoryError(f"{rel}: {name} is not a folder")
            elif stat.S_ISDIR(info.st_mode):
                raise IsADirectoryError(f"{rel} is a folder")
            elif not stat.S_ISREG(info.st_mode):
                raise Refused(str(rel), KINDS.get(stat.S_IFMT(info.st_mode), "not a plain file"))
            else:
                fd = _open(name, 0, folders[-1], rel)
                if not stat.S_ISREG(os.fstat(fd).st_mode):  # swapped in after the look
                    os.close(fd)
                    raise Refused(str(rel), "not a plain file")
                return fd
        raise IsADirectoryError(f"{rel} is the repository itself")
    finally:
        for fd in folders:
            os.close(fd)


def _open(name: str, flags: int, folder: int, rel: Path) -> int:
    try:
        return os.open(name, os.O_RDONLY | flags | FLAGS, dir_fd=folder)
    except OSError as e:
        if e.errno in NOT_PLAIN:  # swapped for a link or a special file after the look
            raise Refused(str(rel), "not a plain file") from e
        raise


def read_bytes(root: Path, path: Path | str) -> bytes:
    """The file's content; Refused, or another OSError, when vivibox does not read it."""
    fd = _reach(root, path)
    with os.fdopen(fd, "rb") as file:
        return file.read()


def read_text(root: Path, path: Path | str) -> str:
    """The file's text, for detection: "" when it is not there, or is a folder, a link out of the
    repository or not a plain file, all of which detection takes as missing."""
    try:
        return read_bytes(root, path).decode("utf-8", errors="replace")
    except OSError:
        return ""


def is_file(root: Path, path: Path | str) -> bool:
    """Whether path is a plain file vivibox reads, reached as read_bytes reaches it."""
    try:
        os.close(_reach(root, path))
    except OSError:
        return False
    return True


def link(root: Path, path: Path | str) -> str | None:
    """The target of the link path is, as git stores it; None when path is not a link."""
    try:
        reached = _reach(root, path, follow=False)
    except OSError:
        return None
    if isinstance(reached, int):
        os.close(reached)
        return None
    return reached


def as_committed(root: Path, path: Path | str) -> str:
    """What git would commit of an untracked path, as text: a link's target, a plain file's
    content. Refused for anything else; UnicodeDecodeError for a binary file."""
    target = link(root, path)
    return target if target is not None else read_bytes(root, path).decode("utf-8")


def copy(root: Path, path: Path | str, target: Path) -> None:
    """A plain file of the repository, copied to a place of the host's own; never a link's file."""
    fd = _reach(root, path, follow=False)
    if isinstance(fd, str):
        raise Refused(str(path), "a link")
    with os.fdopen(fd, "rb") as source, open(target, "wb") as file:
        shutil.copyfileobj(source, file)
