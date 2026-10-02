"""The host's reads and writes of files in a task's handoff/, and its reads of the other folders an
agent or the reviewer writes. The agent's pod mounts that folder read-write, so the agent names
every file in it: a link there could point the host at any file of yours, and a FIFO could hold it
on open forever. Each file is looked at first, and a link, a FIFO, a device or a folder is refused
without being opened; then it is opened without following a link and without waiting, checked
again to be a plain file of one name (it may have been swapped in between), and only then
emptied, written or read through the descriptor that was checked. Where the agent could put a
link in place of a folder on the way (the reviewer's out folder), the walk starts at a root the
agent cannot replace and opens each folder below it the same way. progress.mirror keeps the same
rules for criteria.md."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO

FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
FOLDER = os.O_RDONLY | os.O_DIRECTORY | FLAGS
# What open says when the name is a link (ELOOP; EMLINK on BSD), a FIFO nobody reads or a device
# without a driver (ENXIO), a folder (EISDIR) or, on the way to it, not a folder (ENOTDIR): none
# of them is a plain file.
NOT_PLAIN = {errno.ELOOP, errno.EMLINK, errno.ENXIO, errno.EISDIR, errno.ENOTDIR}


class HandoffError(OSError):
    """A file the agent names (in handoff/, or the reviewer's out folder) that vivibox did not write
    or read. why says what happened and why, for a message that adds its own thing to do; the
    error's text adds the one that fits a refused step."""

    def __init__(self, why: str, then: str = ""):
        super().__init__(f"{why}; {then}" if then else why)
        self.why = why


def _refused(path: Path, verb: str, kind: str = "a plain file", inside: str = "") -> HandoffError:
    """inside: the file under path, when path is the folder refused on the way to it."""
    # The whole path: you delete the file on the host, where handoff/ is the task's folder.
    object_ = f"{inside} " if inside else ""
    return HandoffError(
        f"{path} is a link or not {kind}, and vivibox does not {verb} {object_}through one",
        "delete it and try again",
    )


def _plain(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _folder(path: Path, root: Path | None, verb: str) -> tuple[int | None, str]:
    """Where path's file is opened from: a descriptor of its folder and its name. With root, each
    folder below root is opened a name at a time, none through a link, and a refusal names the
    folder, the thing to fix; without, the path as it is, in a folder the agent cannot replace
    (handoff/ is a mount point in its pod)."""
    if root is None:
        return None, str(path)
    *below, name = path.relative_to(root).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, part in enumerate(below):
            try:
                fd, above = os.open(part, FOLDER, dir_fd=fd), fd
            except OSError as e:
                if e.errno in NOT_PLAIN:
                    raise _refused(root.joinpath(*below[: i + 1]), verb, "a folder", name) from e
                raise
            os.close(above)
    except BaseException:
        os.close(fd)
        raise
    return fd, name


@contextmanager
def _opened(
    path: Path, flags: int, mode: str, empty: bool = False, root: Path | None = None, errors: str = "strict"
) -> Iterator[IO]:
    """The file, checked before anything is emptied: emptying on open would empty a file a hard
    link shares before the check could refuse it."""
    verb = "read" if "r" in mode else "write"
    folder = None
    try:
        folder, name = _folder(path, root, verb)
        try:  # before any open: a FIFO or a device is never opened at all
            if not _plain(os.stat(name, dir_fd=folder, follow_symlinks=False)):
                raise _refused(path, verb)
        except FileNotFoundError:
            pass  # created below, or not there for open to say so
        fd = os.open(name, flags | FLAGS, 0o644, dir_fd=folder)
    except HandoffError:
        raise
    except FileNotFoundError:
        if flags & os.O_CREAT:
            raise HandoffError(f"vivibox could not write {path}: its folder is gone") from None
        raise
    except OSError as e:
        if e.errno in NOT_PLAIN:
            raise _refused(path, verb) from e
        raise HandoffError(f"vivibox could not {verb} {path}: {e.strerror}") from e
    finally:
        if folder is not None:
            os.close(folder)
    try:
        if not _plain(os.fstat(fd)):  # swapped in after the look
            raise _refused(path, verb)
        if empty:
            os.ftruncate(fd, 0)
        file = os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8", "errors": errors}))
    except BaseException:
        os.close(fd)
        raise
    with file:
        yield file


def write_text(path: Path, text: str) -> None:
    """The whole file, replaced."""
    with _opened(path, os.O_WRONLY | os.O_CREAT, "w", empty=True) as file:
        file.write(text)


def appending(path: Path) -> AbstractContextManager[IO]:
    """The file open for appending, made when missing, or refused before anything is written: for
    a step that must not happen in half, such as criteria added for a comment that then could not
    be written, run while it is open."""
    return _opened(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, "a")


def append_text(path: Path, text: str) -> None:
    with appending(path) as file:
        file.write(text)


def placeholder(path: Path) -> None:
    """path a plain empty file, for the host to mount a file of its own over, in a folder the agent
    writes. A link there is removed, never followed: Docker would follow it in the container and
    mount the file where it points, leaving the link for the agent to swap for a file of its own.
    A file there may hold the agent's words, or share another file's by a hard link: its name is
    removed, and nothing in it is opened. A folder or a special file is refused: a folder's files
    are not deleted for the agent, and vivibox makes neither. A plain empty file stays as it is:
    a container may have it mounted, and a new file in its place would take that mount away."""
    try:
        folder = os.open(path.parent, FOLDER)
    except OSError as e:
        raise HandoffError(f"vivibox could not open {path.parent}: {e.strerror}") from e
    try:
        try:
            found = os.stat(path.name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            found = None
        if found and _plain(found) and found.st_size == 0:
            return
        if found and not (stat.S_ISLNK(found.st_mode) or stat.S_ISREG(found.st_mode)):
            raise HandoffError(
                f"{path} is a folder or a special file, where vivibox mounts a file of its own",
                "delete it and try again",
            )
        if found:
            os.unlink(path.name, dir_fd=folder)
        try:  # a file of its own, or nothing: whatever came in between is not taken for one
            os.close(os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FLAGS, 0o644, dir_fd=folder))
        except OSError as e:
            raise HandoffError(f"vivibox could not make {path}: {e.strerror}", "try again") from e
    finally:
        os.close(folder)


def copy_file(src: Path, path: Path) -> None:
    """src is the host's own (the gate's log); only the copy lands where the agent names files."""
    with open(src, "rb") as source, _opened(path, os.O_WRONLY | os.O_CREAT, "wb", empty=True) as file:
        shutil.copyfileobj(source, file)


def read_file(path: Path, root: Path | None = None, errors: str = "strict") -> str:
    """The file's text; FileNotFoundError when there is none. root: where the walk to it starts,
    when the agent could put a link in place of a folder below it."""
    with _opened(path, os.O_RDONLY, "r", root=root, errors=errors) as file:
        return file.read()


def read_text(path: Path, root: Path | None = None, errors: str = "strict") -> str:
    """The file's text, or "" when there is none."""
    try:
        return read_file(path, root, errors)
    except FileNotFoundError:
        return ""


def read_or_nothing(path: Path) -> str:
    """The file's text, or "" when there is none or it is a link or a special file: for a check
    that counts it as missing then, which no link can make more favourable."""
    try:
        return read_text(path)
    except HandoffError:
        return ""


def for_you(path: Path) -> str:
    """The file's text for you to read, stripped; why vivibox did not read it, when it is a link or
    a special file; "" when there is none."""
    try:
        return read_text(path).strip()
    except HandoffError as e:
        return str(e)
    except OSError:
        return ""


def refused(folder: Path, *names: str) -> str:
    """Why vivibox does not read these files of folder, for each one it would refuse; "" when none
    is there or each is a plain file. It only looks: nothing is opened."""
    said = []
    for name in names:
        try:
            if not _plain(os.lstat(folder / name)):
                said.append(_refused(folder / name, "read").why)
        except FileNotFoundError:
            continue
    return "; ".join(said)


def copy_out(path: Path, target: Path) -> None:
    """A file the agent wrote, kept in a place of the host's own (the history): read as read_text
    reads it, written as the host writes its own files."""
    with _opened(path, os.O_RDONLY, "rb") as source, open(target, "wb") as file:
        shutil.copyfileobj(source, file)
