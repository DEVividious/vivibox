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
submodules, objects and packs, the commit-graph, shallow, info/ (exclude; attributes, which name
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
# An index of a few million files; past it vivibox cannot tell which submodules git goes into.
INDEX_LIMIT = 1 << 30
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


# The submodules an index lists, by what its file was when it was read: the inode and the change
# time say a file was written since, and the agent cannot set a change time.
_LISTED: dict[tuple, list[str]] = {}


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
                data = b""
                while len(data) <= INDEX_LIMIT and (chunk := os.read(f, INDEX_LIMIT + 1 - len(data))):
                    data += chunk
                if len(data) > INDEX_LIMIT or info.st_size > INDEX_LIMIT:
                    return f"{display.name(index)} is larger than {INDEX_LIMIT >> 20} MiB"
                try:
                    listed = gitlinks(data, hash_length)
                except (ValueError, struct.error):
                    return f"{display.name(index)} is an index vivibox cannot read"
                if len(_LISTED) > 64:
                    _LISTED.clear()
                _LISTED[key] = listed
            return _LISTED[key]
        finally:
            os.close(f)


def gitlinks(data: bytes, hash_length: int = 20) -> list[str]:
    """The paths of the submodules (mode 160000) in an index file's content. ValueError for one
    vivibox cannot read: a split index or a sparse one keeps entries elsewhere."""
    if data[:4] != b"DIRC":
        raise ValueError("no index")
    version, count = struct.unpack_from(">II", data, 4)
    if version not in (2, 3, 4):
        raise ValueError(f"index version {version}")
    fixed, pos, previous, found = 40 + hash_length, 12, b"", []
    for _ in range(count):
        mode = struct.unpack_from(">I", data, pos + 24)[0]
        flags = struct.unpack_from(">H", data, pos + fixed)[0]
        head = fixed + (4 if flags & 0x4000 else 2)
        start = pos + head
        if version == 4:
            strip, start = _varint(data, start)
            if strip > len(previous):
                raise ValueError("a name that strips more than the one before")
            end = data.index(b"\0", start)
            path = previous[: len(previous) - strip] + data[start:end]
            pos = end + 1
        else:
            end = data.index(b"\0", start)
            path = data[start:end]
            pos += (head + len(path) + 8) & ~7
        previous = path
        if mode == 0o160000:
            found.append(os.fsdecode(path))
    while pos + 8 <= len(data) - hash_length:
        signature, size = data[pos : pos + 4], struct.unpack_from(">I", data, pos + 4)[0]
        if signature in (b"link", b"sdir"):
            raise ValueError("a split or sparse index")
        pos += 8 + size
    return found


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    """git's offset varint at pos, and where what follows it starts."""
    c = data[pos]
    value, pos = c & 0x7F, pos + 1
    while c & 0x80:
        c = data[pos]
        value = ((value + 1) << 7) | (c & 0x7F)
        pos += 1
    return value, pos


def _home(root: Path, rel: str, dirs: list[str]) -> str | None:
    """The git dir among dirs that the .git file at rel names, as git reads it; None for another
    place, or for a .git that is no such file."""
    data = _read(root, rel, LIMIT)
    if isinstance(data, str) or not data.startswith(b"gitdir: "):
        return None
    target = os.fsdecode(data.removeprefix(b"gitdir: ").rstrip(b"\r\n"))
    if not target or "\n" in target:
        return None
    path = os.path.normpath(os.path.join(root, os.path.dirname(rel), target))
    home = os.path.relpath(path, root)
    return home if home in dirs else None


def gitfiles(root: Path, dirs: list[str], allowed: Callable[[str], bool]) -> tuple[list[str], list[str]]:
    """The .git of every submodule git goes into, from the clone's index down, as paths from root;
    and what is wrong: a submodule whose .git allowed does not allow, or that leads to no git dir
    among dirs, which vivibox checked. Each index is read only once its git dir was found as
    recorded; vivibox runs no git for it."""
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
            if look(root, rel, read=False) == ABSENT:
                continue  # not checked out: git does not go into it
            home = _home(root, rel, dirs)
            if home is None or not allowed(rel):
                problems.append(f"{display.name(rel)} was added, a repository git would go into")
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


def differences(recorded: dict[str, str], now: dict[str, str]) -> list[str]:
    """What changed, a line each, as text to read."""
    found = []
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
            found.append(f"{name} was added")
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
    found = differences(files, fingerprint(root, recorded["gitfiles"]))
    if found:
        return found
    known = set(recorded["gitfiles"])
    return gitfiles(root, recorded["git_dirs"], lambda rel: rel in known)[1]


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
    found = differences(want, fingerprint(root, [])) or gitfiles(root, dirs, lambda rel: True)[1]
    if found:
        return [*found[:-1], f"{found[-1]}; {EARLIER}"]
    files = gitfiles(root, dirs, lambda rel: True)[0]
    write(meta, _recorded(dirs, files, fingerprint(root, files)))
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
