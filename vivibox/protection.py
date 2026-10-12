"""What git on the host follows from the task's clone, recorded when the clone is made and compared
before vivibox runs git there. The agent writes the clone's .git, and git reads more of it than
its config and hooks: commondir names another git dir (and so its config and its hooks), gitdir and
a submodule's .git file name a git dir, alternates name other object stores, config.worktree is
more config, HEAD may be a link, and the index lists the submodules git goes into, each with a .git
of its own whose config git reads. git on the host would run what any of them leads to.

Everything is looked at from the clone's root, one name at a time and never through a link: a link
is recorded as its target, never followed, and no name is . or ... A file is opened without
following a link and without waiting, checked to be a plain file through its descriptor, and read
up to LIMIT. A FIFO, a socket or a device is never opened, and counts as a change, as does a file
past LIMIT. The index is read here, not by git: vivibox runs no git before the check.

Left out, because none of them can name a program, another config or another object store: refs,
packed-refs, logs, ORIG_HEAD, FETCH_HEAD and the other heads, the index's entries but its
submodules, objects and packs, the commit-graph, shallow but for being a plain file or none
(git reads it to serve a fetch from the clone), info/ (exclude; attributes, which name
drivers only a config defines; grafts; sparse-checkout), description, rr-cache, the fsmonitor
daemon's socket (used only when a config turns it on), and rebase and sequencer state, which only
commands vivibox never runs on the host act on. remotes/ and branches/ name stores, but only for a
command that names a remote, and vivibox runs none in the clone.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import struct
from collections.abc import Callable, Iterator
from pathlib import Path

from . import display
from .handoff import FLAGS

RECORD = "git-protection.json"
FORMAT = 2
LIMIT = 1 << 20
# An index is about 80 to 140 bytes a file: 100,000 files are 8 MB, and 128 MiB is a million files
# or more, read in about half a second (two million entries of the least size, the agent's worst,
# in 1.4 s, once). Past it vivibox reads none of it, and refuses the clone: it cannot tell which
# submodules git goes into.
INDEX_LIMIT = 128 << 20
# A name in the index: past it no system checks the file out (PATH_MAX is 4096 on Linux).
MAX_NAME = 4096
# Bytes of a name's prefix length in a version-4 index: 3 hold MAX_NAME.
VARINT = 10
# Submodules in one index: far above any repository's; each costs a name to copy.
MAX_GITLINKS = 10_000
# Extensions of one index: git writes a handful (TREE, REUC, UNTR, FSMN, EOIE, IEOT, link, sdir).
MAX_EXTENSIONS = 64
# Entries of .git/modules and of hooks looked at in one pass, whatever the agent put there.
MAX_ENTRIES = 10_000
# Folders .git/modules may hold one in another: submodules of submodules, names with a / in them.
MAX_DEPTH = 64
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

ABSENT, FOLDER, FILE, NOT_LINK, EMPTY = "absent", "folder", "file", "not a link", "empty"
SPECIAL = {
    stat.S_IFIFO: "a named pipe (FIFO)", stat.S_IFSOCK: "a socket", stat.S_IFCHR: "a device",
    stat.S_IFBLK: "a device",
}  # fmt: skip
TOO_LARGE = f"larger than {LIMIT >> 20} MiB"
TOO_MANY = f"more than {MAX_ENTRIES} entries"
# What is a change wherever it is, recorded or not: git would wait on it, or vivibox cannot read it.
ALWAYS = (*SPECIAL.values(), TOO_LARGE, TOO_MANY, "unreadable")
# A folder of .git/modules with one of these is a git dir to git.
MARKS = ("HEAD", "config", "commondir")
ALTERNATES = ("objects/info/alternates", "objects/info/http-alternates")


class _TooMany(Exception):
    pass


# What a task adds in the course of ordinary work and can take back: a repository of its own in
# a folder the index lists (git init there, then git add), a worktree (git worktree add).
REPOSITORY, WORKTREE = "a repository of its own", "a worktree"


class Added(str):
    """A line of what changed that is such an addition, and which (what). git on the host goes
    into it no more than into any other change; it is told apart only for what is said."""

    what = ""


def _added(line: str, what: str) -> Added:
    found = Added(line)
    found.what = what
    return found


@contextlib.contextmanager
def _folder(root: Path, rel: str) -> Iterator[int | str]:
    """A descriptor of the folder at rel under root, reached without a link; else what is there."""
    fd, found, passed = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC), "", []
    try:
        for name in (p for p in rel.split("/") if p):
            passed.append(name)
            found = "unreadable (a name of . or ..)" if name in (".", "..") else _look(fd, name, read=False)
            if found != FOLDER:
                break
            try:
                inner = os.open(name, DIR_FLAGS, dir_fd=fd)
            except OSError as e:  # a link since the look
                found = f"unreadable ({e.strerror})"
                break
            os.close(fd)
            fd, found = inner, ""
        yield fd if not found else found if found == ABSENT else f"{found} at {'/'.join(passed)}"
    finally:
        os.close(fd)


def _look(fd: int, name: str, read: bool = True, mode: bool = False) -> str:
    try:
        info = os.lstat(name, dir_fd=fd)
    except FileNotFoundError:
        return ABSENT
    except OSError as e:
        return f"unreadable ({e.strerror})"
    kind = stat.S_IFMT(info.st_mode)
    if kind == stat.S_IFLNK:
        try:
            return f"link -> {os.readlink(name, dir_fd=fd)}"
        except OSError as e:
            return f"unreadable ({e.strerror})"
    if kind in SPECIAL:
        return SPECIAL[kind]
    if kind == stat.S_IFDIR:
        return FOLDER
    if not read:
        return FILE
    data = _content(fd, name, LIMIT)
    if isinstance(data, str):
        return data
    content, opened = data
    perm = f"{stat.S_IMODE(opened.st_mode):04o} " if mode else ""
    return f"{FILE} {perm}{hashlib.sha256(content).hexdigest()}"


def _content(fd: int, name: str, limit: int) -> tuple[bytes, os.stat_result] | str:
    """The plain file name in the folder fd, opened without a link and without waiting, up to
    limit; what it is instead (TOO_LARGE past limit)."""
    try:
        f = os.open(name, os.O_RDONLY | FLAGS, dir_fd=fd)
    except FileNotFoundError:
        return ABSENT
    except OSError as e:  # a link or a FIFO since the look
        return f"unreadable ({e.strerror})"
    try:
        opened = os.fstat(f)
        if not stat.S_ISREG(opened.st_mode):
            return SPECIAL.get(
                stat.S_IFMT(opened.st_mode), FOLDER if stat.S_ISDIR(opened.st_mode) else "unreadable"
            )
        if opened.st_size > limit:
            return TOO_LARGE
        data = b""
        while len(data) <= limit and (chunk := os.read(f, limit + 1 - len(data))):
            data += chunk
    finally:
        os.close(f)
    return TOO_LARGE if len(data) > limit else (data, opened)


def _read(root: Path, rel: str, limit: int) -> bytes | str:
    """The content of the plain file at rel, reached without a link; what is there instead."""
    parent, _, name = rel.rpartition("/")
    with _folder(root, parent) as fd:
        if isinstance(fd, str):
            return fd
        found = _look(fd, name, read=False)
        if found != FILE:
            return found
        data = _content(fd, name, limit)
        return data if isinstance(data, str) else data[0]


def look(root: Path, rel: str, read: bool = True, mode: bool = False) -> str:
    """What is at rel under root: ABSENT, FOLDER, "link -> <target>", "file <sha256>" (with its
    mode when asked), or what makes it nothing to read (a FIFO, a socket, a device, too large).
    "<what> at <name>" when a folder on the way is no folder."""
    parent, _, name = rel.rpartition("/")
    with _folder(root, parent) as fd:
        return fd if isinstance(fd, str) else _look(fd, name, read, mode)


def listing(root: Path, rel: str) -> list[str] | str:
    """The names in the folder at rel, sorted; what is there instead when it is no folder."""
    with _folder(root, rel) as fd:
        if isinstance(fd, str):
            return fd
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_ENTRIES:
                    raise _TooMany
        return sorted(names)


def git_dirs(root: Path) -> tuple[list[str], dict[str, str]]:
    """The clone's git dir and its submodules' (those under .git/modules), as paths from root; and
    what in .git/modules git could reach that is no folder: a link."""
    dirs, odd, left = [".git"], {}, [MAX_ENTRIES]

    def walk(rel: str) -> None:
        if rel.count("/") > MAX_DEPTH:
            raise _TooMany
        names = listing(root, rel)
        if isinstance(names, str):
            if names != ABSENT and not names.startswith(FILE):
                odd[rel] = names.removesuffix(f" at {rel}")
            return
        for name in names:
            left[0] -= 1
            if left[0] < 0:
                raise _TooMany
            sub = f"{rel}/{name}"
            found = look(root, sub, read=False)
            if found == FOLDER:
                inside = listing(root, sub)
                if isinstance(inside, list) and set(inside) & set(MARKS):
                    dirs.append(sub)
                    walk(f"{sub}/modules")
                else:
                    walk(sub)  # a folder of a submodule's name with a / in it
            elif found != FILE:
                odd[sub] = found

    walk(".git/modules")
    return dirs, odd


def _git_dir(root: Path, d: str) -> dict[str, str]:
    found = {}
    for name in ("config", "config.worktree", "commondir", "gitdir"):
        found[f"{d}/{name}"] = look(root, f"{d}/{name}")
    for name in ALTERNATES:  # the empty file prepare makes to mount over is none
        value = look(root, f"{d}/{name}")
        found[f"{d}/{name}"] = EMPTY if value in (ABSENT, f"{FILE} {hashlib.sha256().hexdigest()}") else value
    value = look(root, f"{d}/HEAD", read=False)  # it changes with every switch; a link or a FIFO must not
    found[f"{d}/HEAD"] = NOT_LINK if value in (ABSENT, FILE, FOLDER) else value
    # git reads it to serve a fetch from the clone (a reader's copy, history.ensure): it comes
    # and goes with a shallow clone's work, and a link or a FIFO must not.
    value = look(root, f"{d}/shallow", read=False)
    found[f"{d}/shallow"] = NOT_LINK if value in (ABSENT, FILE) else value
    for name in ("objects", "objects/info"):
        found[f"{d}/{name}"] = look(root, f"{d}/{name}", read=False)
    for name in ("hooks", "worktrees"):
        found[f"{d}/{name}"] = look(root, f"{d}/{name}", read=False)
    hooks = listing(root, f"{d}/hooks")
    for name in hooks if isinstance(hooks, list) else []:
        found[f"{d}/hooks/{name}"] = look(root, f"{d}/hooks/{name}", mode=True)
    worktrees = listing(root, f"{d}/worktrees")
    for name in worktrees if isinstance(worktrees, list) else []:
        w = f"{d}/worktrees/{name}"
        found[w] = look(root, w, read=False)
        for inside in ("commondir", "gitdir", "config.worktree"):
            found[f"{w}/{inside}"] = look(root, f"{w}/{inside}")
    return found


def fingerprint(root: Path, gitfiles: list[str]) -> dict[str, str]:
    """What git follows from the clone's git dirs, and the submodules' .git files at gitfiles."""
    try:
        dirs, found = git_dirs(root)
        for d in dirs:
            found.update(_git_dir(root, d))
        for path in gitfiles:
            found[path] = look(root, path)
    except _TooMany:
        return {".git": TOO_MANY}
    return found


def _hash_length(root: Path, git_dir: str) -> int:
    """The length of an object name in the git dir's objects: its config's extensions.objectformat."""
    data = _read(root, f"{git_dir}/config", LIMIT)
    section = ""
    for line in (data if isinstance(data, bytes) else b"").decode(errors="replace").splitlines():
        text = line.strip()
        if text.startswith("["):
            section = text.strip("[]").strip().lower()
            continue
        key, _, value = text.partition("=")
        if section == "extensions" and key.strip().lower() == "objectformat":
            return 32 if value.strip().strip('"').lower() == "sha256" else 20
    return 20


# What an index lists, or why vivibox cannot read it, by what its file was when it was read: the
# inode and the change time say a file was written since, and the agent cannot set a change time.
# An index refused is not read again until it changes.
_LISTED: dict[tuple, list[str] | str] = {}


def _gitlinks(root: Path, git_dir: str) -> list[str] | str:
    """The submodules the git dir's index lists, as paths from its work tree; what is wrong when
    vivibox cannot read it. No index, or one git would wait on, lists none git goes into."""
    index = f"{git_dir}/index"
    kind = look(root, index, read=False)
    if kind != FILE:
        return f"{display.name(index)} is {display.name(kind)}" if kind.startswith("link") else []
    hash_length = _hash_length(root, git_dir)
    parent, _, name = index.rpartition("/")
    with _folder(root, parent) as fd:
        if isinstance(fd, str):
            return []
        try:
            f = os.open(name, os.O_RDONLY | FLAGS, dir_fd=fd)
        except OSError as e:
            return f"{display.name(index)} is unreadable ({e.strerror})"
        try:
            info = os.fstat(f)
            if not stat.S_ISREG(info.st_mode):
                return []
            key = (str(root), git_dir, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                   info.st_ctime_ns, hash_length)  # fmt: skip
            if key not in _LISTED:
                if len(_LISTED) > 64:
                    _LISTED.clear()
                _LISTED[key] = _listed(f, info.st_size, hash_length, display.name(index))
            return _LISTED[key]
        finally:
            os.close(f)


def index_gitlinks(root: Path) -> list[str]:
    """The folders the clone's own index lists as submodules, as paths from root, read from the
    file (no git); none for an index vivibox cannot read, which the check before it refused."""
    listed = _gitlinks(root, ".git")
    return listed if isinstance(listed, list) else []


def _listed(f: int, size: int, hash_length: int, index: str) -> list[str] | str:
    """The submodules the index open at f lists; why not. Its first megabyte is read, and the rest
    only when the entries go on past it, never past INDEX_LIMIT; its extensions are walked by
    their headers alone, which costs nothing whatever their sizes."""
    if size > INDEX_LIMIT:
        return f"{index} is larger than {INDEX_LIMIT >> 20} MiB"
    header = lambda at: os.pread(f, 8, at)  # noqa: E731
    try:
        try:
            return gitlinks(_pread(f, min(size, 1 << 20)), hash_length, size, header)
        except _Short:
            return gitlinks(_pread(f, size), hash_length, size, header)
    except (ValueError, _Short) as e:
        if str(e).startswith("more than "):
            return f"{index} holds {e}"
        return f"{index} is an index vivibox cannot read"


def _pread(f: int, size: int) -> bytes:
    """The first size bytes of the file open at f; fewer when it shrank since."""
    chunks, read = [], 0
    while read < size and (chunk := os.pread(f, min(1 << 24, size - read), read)):
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


def gitlinks(
    data: bytes | bytearray, hash_length: int = 20, size: int | None = None,
    header: Callable[[int], bytes] | None = None,
) -> list[str]:  # fmt: skip
    """The paths of the submodules (mode 160000) in an index file's content. ValueError for one
    vivibox cannot read, whatever is wrong with it: the agent writes every byte. A split index or
    a sparse one keeps entries elsewhere, and is one too. size: the whole index's, when data is
    only its start (_Short when the entries go on past it), and header reads its extensions'."""
    size = len(data) if size is None else size
    found, pos = _entries(data, hash_length, complete=len(data) >= size)
    _extensions(header or (lambda at: bytes(data[at : at + 8])), pos, size - hash_length)
    return found


class _Short(Exception):
    """The entries go on past what was read of the index."""


_MODE, _FLAGS, _SIZES = struct.Struct(">I"), struct.Struct(">H"), struct.Struct(">4sI")


def _entries(data: bytes | bytearray, hash_length: int, complete: bool) -> tuple[list[str], int]:
    """The submodules data's entries list, and where the entries end, in one pass over data, each
    byte looked at a bounded number of times: a name, a prefix's length and the submodules are
    bounded too, whatever the agent wrote. _Short when the entries go on past data and it is not
    the whole index; ValueError for one vivibox cannot read."""
    if data[:4] != b"DIRC":
        raise ValueError("no index")
    pos = 12
    try:
        version, count = struct.unpack_from(">II", data, 4)
        if version not in (2, 3, 4):
            raise ValueError(f"index version {version}")
        fixed, previous, found = 40 + hash_length, bytearray(), []
        mode_of, flags_of, find, end_of = _MODE.unpack_from, _FLAGS.unpack_from, data.find, len(data)
        for _ in range(count):
            mode = mode_of(data, pos + 24)[0]
            head = fixed + (4 if flags_of(data, pos + fixed)[0] & 0x4000 else 2)
            start = pos + head
            if version == 4:
                strip, start = _varint(data, start)
                if strip > len(previous):
                    raise ValueError("a name that strips more than the one before")
                end = find(b"\0", start, start + MAX_NAME + 1)
                if end < 0:
                    raise IndexError("a name past its limit, or not ended")
                del previous[len(previous) - strip :]
                previous += data[start:end]
                if len(previous) > MAX_NAME:
                    raise ValueError("a name past its limit")
                path, next_pos = previous, end + 1
            else:
                end = find(b"\0", start, start + MAX_NAME + 1)
                if end < 0:
                    raise IndexError("a name past its limit, or not ended")
                path, next_pos = data[start:end], pos + ((head + end - start + 8) & ~7)
            if next_pos > end_of:
                raise IndexError("an entry past the end")
            pos = next_pos
            if mode == 0o160000:
                if len(found) >= MAX_GITLINKS:
                    raise ValueError(f"more than {MAX_GITLINKS:,} submodules")
                found.append(os.fsdecode(bytes(path)))
        return found, pos
    except ValueError:
        raise
    except Exception as e:  # a name or a size past the end (IndexError, struct.error), or anything else
        # Within an entry's largest size of the end, the rest of the index may hold what is missing.
        if not complete and pos + 64 + VARINT + MAX_NAME >= len(data):
            raise _Short from None
        raise ValueError(f"an index vivibox cannot read: {e!r}") from None


def _extensions(header: Callable[[int], bytes], pos: int, end: int) -> None:
    """Walks the extensions from pos to end by their headers (header(at): the 8 bytes at at).
    ValueError for a split or sparse index, or more than MAX_EXTENSIONS: real ones have a few."""
    for _ in range(MAX_EXTENSIONS + 1):
        if pos + 8 > end:
            return
        found = header(pos)
        if len(found) < 8:
            raise ValueError("an extension past the end")
        signature, size = _SIZES.unpack(found)
        if signature in (b"link", b"sdir"):
            raise ValueError("a split or sparse index")
        pos += 8 + size
    raise ValueError(f"more than {MAX_EXTENSIONS} extensions")


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """git's offset varint at pos, and where what follows it starts; at most VARINT bytes, so that
    a run of continuation bytes costs nothing (a value past MAX_NAME strips more than any name)."""
    c = data[pos]
    value, pos = c & 0x7F, pos + 1
    for _ in range(VARINT):
        if not c & 0x80:
            return value, pos
        c = data[pos]
        value = ((value + 1) << 7) | (c & 0x7F)
        pos += 1
    raise ValueError("a prefix length of more bytes than any name needs")


def _named(data: bytes | str, root: Path, rel: str) -> list[str]:
    """Where the .git file at rel, of content data, sends git, as a path from root: the place its
    one line names (gitdir: …), taken by the names alone. Two answers where root is reached
    through a link (git in the pod writes the path it is at); none for a .git that is no such
    file."""
    if isinstance(data, str) or not data.startswith(b"gitdir: "):
        return []
    target = os.fsdecode(data.removeprefix(b"gitdir: ").rstrip(b"\r\n"))
    if not target or "\n" in target:
        return []
    bases = dict.fromkeys((str(root), os.path.realpath(root)))
    return [
        os.path.relpath(os.path.normpath(os.path.join(base, os.path.dirname(rel), target)), base)
        for base in bases
    ]


def _home(data: bytes | str, root: Path, rel: str, dirs: list[str]) -> str | None:
    """The git dir among dirs that the .git file at rel names, as git reads it; None for another
    place, or for a .git that is no such file."""
    return next((home for home in _named(data, root, rel)[:1] if home in dirs), None)


def _worktrees_own(data: bytes | str, root: Path, rel: str, dirs: list[str], seen: dict[str, str]) -> bool:
    """Whether the .git at rel is the file git wrote in the folder of a worktree it added to one
    of dirs, and nothing else: it names <git dir>/worktrees/<name>, an entry that seen (the look
    at the git dirs that the changes were told from) has as a folder under a folder, with no
    settings of its own, whose commondir is git's own (../..) and whose gitdir leads back to
    this very file. The entry's two files are read here and held to what seen hashed, and the
    .git file is read once more: whatever differs between two reads is not what was looked at,
    and is not taken for a worktree's. Files only; no git."""
    for entry in _named(data, root, rel):
        git_dir, _, name = entry.rpartition("/worktrees/")
        if git_dir not in dirs or not name or "/" in name or name in (".", ".."):
            continue
        folders = (f"{git_dir}/worktrees", entry)
        if any(seen.get(key) != FOLDER for key in folders) or seen.get(f"{entry}/config.worktree") != ABSENT:
            continue
        read = {name: _read(root, f"{entry}/{name}", LIMIT) for name in ("commondir", "gitdir")}
        if any(
            isinstance(content, str)
            or seen.get(f"{entry}/{name}") != f"{FILE} {hashlib.sha256(content).hexdigest()}"
            for name, content in read.items()
        ):
            continue
        if read["commondir"].strip() != b"../..":
            continue
        back = os.fsdecode(read["gitdir"].rstrip(b"\r\n"))
        leads = {
            os.path.normpath(os.path.join(base, entry, back)) == os.path.normpath(os.path.join(base, rel))
            for base in (str(root), os.path.realpath(root))
        }
        if "\n" not in back and True in leads and _read(root, rel, LIMIT) == data:
            return True
    return False


def gitfiles(
    root: Path, dirs: list[str], allowed: Callable[[str], bool], seen: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """The .git of every submodule git goes into, from the clone's index down, as paths from root;
    and what is wrong: a submodule whose .git allowed does not allow, or that leads to no git dir
    among dirs, which vivibox checked. Each index is read only once its git dir was found as
    recorded; vivibox runs no git for it. Each .git is read once, and what it is is told from
    that read. seen: the look at the git dirs (fingerprint) that goes with this one, for
    telling the folder of a worktree git added (_worktrees_own); without it, none is."""
    found, problems, todo, read = [], [], [("", ".git")], set()
    while todo:
        worktree, git_dir = todo.pop()
        if git_dir in read:  # two submodules with one git dir: git's own never are
            problems.append(f"{display.name(worktree)}/.git leads to a git dir another submodule has")
            continue
        read.add(git_dir)
        listed = _gitlinks(root, git_dir)
        if isinstance(listed, str):
            problems.append(listed)
            continue
        for path in listed:
            rel = f"{worktree}/{path}/.git".lstrip("/")
            data = _read(root, rel, LIMIT)
            if data == ABSENT:
                continue  # not checked out: git does not go into it
            home = _home(data, root, rel, dirs)
            if home is None or not allowed(rel):
                said = f"{display.name(rel)} was added, a repository git would go into"
                if set(rel.split("/")) & {"", ".", ".."}:  # no name git itself writes
                    problems.append(said)
                elif data == FOLDER:  # as git init and git add leave one
                    problems.append(_added(said, REPOSITORY))
                elif seen is not None and _worktrees_own(data, root, rel, dirs, seen):
                    listed = f"{display.name(rel)} was added, the folder of a worktree, which the index lists"
                    problems.append(_added(listed, WORKTREE))
                else:
                    problems.append(said)
                continue
            found.append(rel)
            todo.append((rel.removesuffix("/.git"), home))
    return sorted(found), problems


def record(root: Path) -> dict:
    """What prepare leaves, for check to compare with."""
    dirs = git_dirs(root)[0]
    files, problems = gitfiles(root, dirs, lambda rel: True)
    if problems:
        raise OSError(f"the clone's submodules could not be read: {problems[0]}")
    return _recorded(dirs, files, fingerprint(root, files))


# The record is what was found, a path each, beside these.
ABOUT = ("format", "git_dirs", "gitfiles")


def _recorded(dirs: list[str], files: list[str], found: dict[str, str]) -> dict:
    return {"format": FORMAT, "git_dirs": dirs, "gitfiles": files, **found}


def write(meta: Path, recorded: dict) -> None:
    (meta / RECORD).write_text(json.dumps(recorded, indent=2) + "\n")


def differences(recorded: dict[str, str], now: dict[str, str], dirs: list[str] | tuple = ()) -> list[str]:
    """What changed, a line each, as text to read. dirs: the git dirs, whose worktrees, when one
    was added as a folder and its plain files, are told apart as that (Added)."""
    found = []
    worktrees = tuple(f"{d}/worktrees" for d in dirs)
    for key in sorted(set(recorded) | set(now)):
        was, value = recorded.get(key, ABSENT), now.get(key, ABSENT)
        name = display.name(key)
        if value.startswith(ALWAYS):
            found.append(f"{name} is {display.name(value)}")
        elif was == value:
            continue
        elif value == ABSENT:
            found.append(f"{name} was deleted")
        elif was == ABSENT:
            of_worktree = key in worktrees or key.startswith(tuple(f"{w}/" for w in worktrees))
            plain = value in (FOLDER, FILE) or value.startswith(f"{FILE} ")  # no link: one leads elsewhere
            said = f"{name} was added"
            found.append(_added(said, WORKTREE) if of_worktree and plain else said)
        elif value.startswith("link -> ") and " at " not in value:
            found.append(f"{name} is now a link to {display.name(value.removeprefix('link -> '))}")
        else:
            found.append(f"{name} changed")
    return found


def check(root: Path, meta: Path) -> list[str]:
    """What changed since prepare recorded the clone; nothing when it is as recorded.
    FileNotFoundError when there is no record."""
    recorded = json.loads((meta / RECORD).read_text())
    if "format" not in recorded:
        return _upgrade(root, meta, recorded)
    files = {k: v for k, v in recorded.items() if k not in ABOUT}
    for d in recorded["git_dirs"]:  # a record from before the shallow file was looked at
        files.setdefault(f"{d}/shallow", NOT_LINK)
    now = fingerprint(root, recorded["gitfiles"])
    found = differences(files, now, recorded["git_dirs"])
    if any(not isinstance(change, Added) for change in found):
        return found
    # The git dirs as recorded, or with a worktree added and nothing else: the index is read
    # too, and what it has is said first, so that what the task can take out is never said
    # before what it cannot. A worktree's own folder, which git lists there once it is added,
    # is told by the look the changes above came from (now), and is the worktree, no more.
    known = set(recorded["gitfiles"])
    return [*gitfiles(root, recorded["git_dirs"], lambda rel: rel in known, now)[1], *found]


# --- A clone recorded by an earlier vivibox, which recorded each git dir's config and hooks' names

EARLIER = "it was recorded by an earlier vivibox, which did not record these"


def _as_prepared(d: str, old: dict[str, str]) -> dict[str, str]:
    """What prepare leaves in git dir d, for what the earlier record did not hold."""
    hooks = old.get(f"{d}/hooks")
    found = {
        f"{d}/config": f"{FILE} {old.get(f'{d}/config')}",
        f"{d}/hooks": FOLDER if hooks == "" else f"hooks {hooks!r}",
        **dict.fromkeys(
            (f"{d}/{n}" for n in ("config.worktree", "commondir", "gitdir", "worktrees")), ABSENT
        ),
        **dict.fromkeys((f"{d}/{n}" for n in ALTERNATES), EMPTY),
        f"{d}/HEAD": NOT_LINK,
        f"{d}/shallow": NOT_LINK,
        **dict.fromkeys((f"{d}/{n}" for n in ("objects", "objects/info")), FOLDER),
    }
    return found


def _upgrade(root: Path, meta: Path, old: dict[str, str]) -> list[str]:
    """Recorded again when every git dir is as the old record says and as prepare leaves the rest,
    and every submodule's .git leads to one of them; else what is not, and nothing recorded."""
    dirs = sorted(k.removesuffix("/config") for k in old if k.endswith("/config"))
    want: dict[str, str] = {}
    for d in dirs:
        want.update(_as_prepared(d, old))
    files, found = gitfiles(root, dirs, lambda rel: True)  # each leads to one of dirs (_home)
    now = fingerprint(root, files)
    found = differences(want, {k: v for k, v in now.items() if k not in files}) or found
    if found:
        return [*found[:-1], f"{found[-1]}; {EARLIER}"]
    # What was compared is what is recorded, never a later read; the check after it reads again.
    write(meta, _recorded(dirs, files, now))
    return check(root, meta)


# --- What the agent's pod mounts over the clone


def mount_points(root: Path, meta: Path) -> list[tuple[str, bool]]:
    """What the pod mounts over the clone's git dirs, as (path from root, read-only): config, hooks
    and alternates read-only, each git dir's objects and objects/info on themselves, and a
    submodule's git dir and the folders above it on themselves, so that no folder holding a
    read-only mount can be renamed away with it. Only the git dirs prepare recorded, and only what
    is there as prepare left it: Docker mounts what a link leads to, which is the host's."""
    try:
        recorded = json.loads((meta / RECORD).read_text())
    except FileNotFoundError:
        recorded = {}
    if "git_dirs" in recorded:
        dirs = recorded["git_dirs"]
    elif recorded:
        dirs = sorted(k.removesuffix("/config") for k in recorded if k.endswith("/config"))
    else:
        dirs = git_dirs(root)[0]
    points: dict[str, bool] = {}
    for d in dirs:
        parts = d.split("/")
        for i in range(2, len(parts) + 1):
            points.setdefault("/".join(parts[:i]), False)
        for name in ("objects", "objects/info"):
            points[f"{d}/{name}"] = False
        for name in ALTERNATES:
            make_empty(root, f"{d}/{name}")
        points.update({f"{d}/config": True, f"{d}/hooks": True, **{f"{d}/{n}": True for n in ALTERNATES}})
    result = []
    for rel, read_only in sorted(points.items()):
        expect = FILE if rel.endswith(("/config", *ALTERNATES)) else FOLDER
        if look(root, rel, read=False) == expect:
            result.append((rel, read_only))
    return result


def make_empty(root: Path, rel: str) -> None:
    """An empty file at rel when nothing is there, in a folder reached without a link."""
    parent, _, name = rel.rpartition("/")
    with _folder(root, parent) as fd:
        if isinstance(fd, int):
            with contextlib.suppress(FileExistsError):
                os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FLAGS, 0o644, dir_fd=fd))
