"""The host's reads of a task's clone, which the agent's pod mounts read-write: the agent names
every file in it, and can change it while the host reads. The walk to a file starts at the clone's
root and opens one name at a time from the folder it holds, never through a link: a link is read
and its target walked the same way, only while it stays inside the root (no absolute target, no
.. above the root, at most MAX_LINKS of them). A FIFO, a socket or a device is refused before it is
opened; the file is then opened without following a link and without waiting, and checked again to
be a plain file through its descriptor. A name swapped for a link after the look fails the open;
a link swapped after it was read is not consulted again. Your checkout is read the same way, so a
project is detected alike in both.

What is read is bounded too: a file costs the agent nothing on the disk when it is sparse, and
reading it whole could take the host's memory. A file past its limit is refused, by the size its
descriptor says and by what was read, never more than the limit and one byte."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .handoff import FLAGS, NOT_PLAIN

MAX_LINKS = 40
# What detection and the configuration it gives read (a package.json, a pipeline): far above any
# real build file. What the checks read (a risky file's digest, an untracked file for invisible
# characters): far above a lockfile; past it a check counts the file against the work.
CONFIG_LIMIT = 1 << 20
CHECK_LIMIT = 64 << 20
CHUNK = 1 << 20
KINDS = {
    stat.S_IFIFO: "a FIFO", stat.S_IFSOCK: "a socket", stat.S_IFCHR: "a device", stat.S_IFBLK: "a device",
}  # fmt: skip
OUT = "a link that leads out of the repository"
LOOP = f"a loop of links, or more than {MAX_LINKS} links in a row"
# The end of a finding for a file a check did not read (gate.hidden_characters).
NOT_CHECKED = "so it was not checked"


class Refused(OSError):
    """A path of the repository vivibox does not read: a link that leads out of it, a file that
    is not a plain one (a FIFO, a socket, a device), or one past its limit. kind says which, the
    same for the same thing; links, the targets of the links followed on the way out."""

    def __init__(self, path: str, kind: str, links: list[str] | None = None):
        super().__init__(f"{path} is {kind}, and vivibox does not read it")
        self.kind, self.links = kind, links or []


def too_large(limit: int) -> str:
    return f"larger than {limit >> 20} MiB"


def not_checked(path: str, kind: str) -> str:
    return f"{path}: vivibox does not read it ({kind}), {NOT_CHECKED}"


def split_checked(found: list[str]) -> tuple[list[str], list[str]]:
    """A check's findings, and the files among them it did not read (not_checked)."""
    return [f for f in found if not f.endswith(NOT_CHECKED)], [f for f in found if f.endswith(NOT_CHECKED)]


def _reach(root: Path, path: Path | str, follow: bool = True) -> int | str:
    """The descriptor of the plain file at path, a path under root as any other (absolute, or
    from the current folder); without follow, the target of a link that is its last name instead.
    FileNotFoundError when there is none, IsADirectoryError for a folder, Refused for the rest."""
    try:  # lexically, never through a link: what the walk follows, it follows itself
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
    except ValueError:
        raise Refused(str(path), OUT) from None
    parts, links = list(rel.parts), []
    folders = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        while parts:
            name = parts.pop(0)
            if name in ("", "."):
                continue
            if name == "..":
                if len(folders) == 1:
                    raise Refused(str(rel), OUT, links)
                os.close(folders.pop())
                continue
            info = os.stat(name, dir_fd=folders[-1], follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=folders[-1])
                if not follow and not parts:
                    return target
                links.append(target)
                if len(links) > MAX_LINKS:
                    raise Refused(str(rel), LOOP)
                if target.startswith("/"):
                    raise Refused(str(rel), OUT, links)
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


def _chunks(root: Path, path: Path | str, limit: int, follow: bool = True):
    """The file's content a chunk at a time, refused past limit: by the size its descriptor says
    before anything is read, and by what was read, for a file that grows meanwhile. Without
    follow, a link is refused."""
    fd = _reach(root, path, follow)
    if isinstance(fd, str):
        raise Refused(str(path), "a link")
    with os.fdopen(fd, "rb") as file:
        if os.fstat(file.fileno()).st_size > limit:
            raise Refused(str(path), too_large(limit))
        left = limit + 1
        while chunk := file.read(min(CHUNK, left)):
            left -= len(chunk)
            if left == 0:
                raise Refused(str(path), too_large(limit))
            yield chunk


def read_bytes(root: Path, path: Path | str, limit: int = CHECK_LIMIT) -> bytes:
    """The file's content; Refused, or another OSError, when vivibox does not read it."""
    return b"".join(_chunks(root, path, limit))


def sha256(root: Path, path: Path | str, limit: int = CHECK_LIMIT) -> str:
    """The file's digest, read a chunk at a time; refused as read_bytes refuses."""
    digest = hashlib.sha256()
    for chunk in _chunks(root, path, limit):
        digest.update(chunk)
    return digest.hexdigest()


def read_text(root: Path, path: Path | str, limit: int = CONFIG_LIMIT) -> str:
    """The file's text, for detection: "" when it is not there, or is a folder, a link out of the
    repository, not a plain file or past limit, all of which detection takes as missing."""
    try:
        return read_bytes(root, path, limit).decode("utf-8", errors="replace")
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


def copy(root: Path, path: Path | str, target: Path, limit: int = CHECK_LIMIT) -> None:
    """A plain file of the repository, copied to a place of the host's own; never a link's file,
    nor one past limit."""
    chunks = _chunks(root, path, limit, follow=False)
    first = next(chunks, b"")  # refused before anything is written
    try:
        with open(target, "wb") as file:
            file.write(first)
            for chunk in chunks:
                file.write(chunk)
    except Refused:  # grew past limit while it was copied
        target.unlink(missing_ok=True)
        raise
