"""Files you point a task at with @path in its description.

A file of the project itself is the same file in the agent's clone, the one it edits, so the
description points there and nothing is copied. Anything else is copied into the task, read-only
for the agent, under /task/context/: the agent never sees your disk. Files that look like
credentials are refused, so a stray @~/.aws does not hand keys to a model.

The clone is of a commit. A project file with uncommitted changes is still the clone's, and the
task says so; one that is not committed at all is not in the clone, so it is copied like an
outside file, and the task says that too.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MOUNT = "/task/context"
LIMIT_BYTES = 20 * 1024 * 1024
# @ at the start of a word (not an e-mail address), then a path: ~/, /, ./ or ../ make it explicit;
# notes/x.md or x.md count only when such a file exists, since tickets also @mention people.
MENTION = re.compile(r"(?:(?<=\s)|^)@((?:~|\.{1,2})?/\S+|[\w.-]+/\S*|[\w-]+\.\w+)", re.MULTILINE)
EXPLICIT = ("~/", "/", "./", "../")
TRAILING = ".,;:!?)]}'\""
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.*", "settings.xml", "*.kdbx",
)  # fmt: skip
SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".docker", ".kube", "vivibox")


class ContextError(Exception):
    pass


def mentions(text: str) -> list[str]:
    return [m.group(1).rstrip(TRAILING) for m in MENTION.finditer(text)]


def _secret(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, p) for p in SECRET_NAMES) or any(
        part in SECRET_DIRS for part in path.parts
    )


def _files(path: Path) -> list[Path]:
    return [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]


@dataclass
class Mentions:
    """What the @paths of a description point at: files to copy, files of the project (by their
    path in it), and what to tell the person about them."""

    copies: dict[str, Path] = field(default_factory=dict)
    in_project: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _locate(mention: str, cwd: Path, repo: Path | None) -> Path | None:
    """Where a mention points: from where you are, else from the project's root, so @src/Main.java
    works from any folder you started vivibox in."""
    here = (cwd / Path(mention).expanduser()).resolve()
    if here.exists():
        return here
    if repo and not mention.startswith(EXPLICIT) and (there := (repo / mention).resolve()).exists():
        return there
    return None


def _git_status(repo: Path, path: str) -> str:
    """ " " unchanged, "??" untracked, else modified; "" when git cannot say."""
    p = subprocess.run(
        ["git", "status", "--porcelain", "--", path], cwd=repo, capture_output=True, text=True, check=False
    )
    return p.stdout[:2] if p.returncode == 0 and p.stdout else ("" if p.returncode else "  ")


def resolve(description: str, cwd: Path, repo: Path | None = None) -> Mentions:
    """Checks every @path before anything is created. repo: the project's repository, whose files
    the agent has in its clone."""
    found, total = Mentions(), 0
    repo = repo.resolve() if repo else None
    for mention in mentions(description):
        path = _locate(mention, cwd, repo)
        if path is None:
            if mention.startswith(EXPLICIT):
                raise ContextError(f"@{mention}: no such file or directory")
            continue
        if repo and path.is_relative_to(repo) and path != repo:
            inside = str(path.relative_to(repo))
            status = _git_status(repo, inside)
            if status != "??":
                found.in_project[mention] = inside
                if status.strip():
                    found.notes.append(
                        f"{inside} has uncommitted changes; the agent sees the committed version"
                    )
                continue
            found.notes.append(f"{inside} is not committed, so the agent gets a copy under {MOUNT}")
        for f in _files(path):
            if _secret(f):
                raise ContextError(f"@{mention}: {f} looks like a credential; not giving it to the agent")
            total += f.stat().st_size
        found.copies[mention] = path
    if total > LIMIT_BYTES:
        raise ContextError(f"the files you mention take {total // 1024 // 1024} MB; the limit is 20 MB")
    return found


def _rewrite(description: str, mention: str, seen: str) -> str:
    return re.sub(rf"(?:(?<=\s)|^)@{re.escape(mention)}", seen, description, flags=re.M)


def attach(description: str, found: Mentions, folder: Path, clone: Path | None = None) -> str:
    """Copies the outside files into folder, and rewrites the description to the paths the agent
    sees: the copies, and the project's files in its clone."""
    for mention, inside in found.in_project.items():
        description = _rewrite(description, mention, f"{clone}/{inside}")
    if found.copies:
        folder.mkdir(exist_ok=True)
    for mention, path in found.copies.items():
        name, n = path.name, 1
        while (folder / name).exists():
            n += 1
            name = f"{path.stem}-{n}{path.suffix}"
        if path.is_dir():
            # Links are copied as links: they point at your disk, which the agent cannot see.
            shutil.copytree(path, folder / name, symlinks=True)
        else:
            shutil.copy2(path, folder / name)
        description = _rewrite(description, mention, f"{MOUNT}/{name}")
    return description


def complete(
    partial: str, cwd: Path, limit: int = 40, repo: Path | None = None, paths: list[str] | None = None
) -> list[str]:
    """Paths that complete what follows an @, like a shell: 'src/ma' -> ['src/main/']. From where
    you are and, for a path that is not explicit, from the project's root too; then, like Claude
    Code, any path in the project's tree that contains what you typed: 'OrderSer' or 'orders/OrderSer'
    -> ['src/main/java/com/acme/orders/OrderService.java']. Directories end with '/'; hidden entries
    show only once you type the dot. `paths` is the project's tree, from `project_paths`, when the
    caller keeps it instead of listing it on every keystroke."""
    found = _complete(partial, cwd)
    if repo and not partial.startswith(EXPLICIT):
        found = list(dict.fromkeys(found + _complete(partial, repo)))
        if partial:
            tree = project_paths(repo) if paths is None else paths
            found = list(dict.fromkeys(found + _search(partial, tree)))
    return found[:limit]


def project_paths(repo: Path) -> list[str]:
    """Every file git knows or would add in the project, and every folder on the way to one, relative
    to its root; folders end with '/'. What git ignores (build output, caches) is not part of the
    project. Nothing for a folder that is no repository."""
    try:
        p = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repo, capture_output=True, text=True,
        )  # fmt: skip
    except OSError:
        return []
    if p.returncode != 0:
        return []
    paths: set[str] = set()
    for rel in filter(None, p.stdout.split("\0")):
        paths.add(rel)
        parts = rel.split("/")
        paths.update("/".join(parts[:n]) + "/" for n in range(1, len(parts)))
    return sorted(paths)


def _search(partial: str, paths: list[str]) -> list[str]:
    """The paths that contain what was typed, the best first: a name that starts with its last piece,
    then a name that contains it, then a match elsewhere in the path; within those, the shortest path."""
    query = partial.lower()
    last = query.rstrip("/").rpartition("/")[2]
    hidden_asked = any(part.startswith(".") for part in query.split("/"))
    ranked = []
    for path in paths:
        low = path.lower()
        if query not in low:
            continue
        if not hidden_asked and any(part.startswith(".") for part in path.split("/")):
            continue
        name = low.rstrip("/").rpartition("/")[2]
        rank = 0 if name.startswith(last) else 1 if last in name else 2
        ranked.append((rank, low.count("/"), low, path))
    return [path for _, _, _, path in sorted(ranked)]


def _complete(partial: str, cwd: Path) -> list[str]:
    folder, _, name = partial.rpartition("/")
    if not folder and partial.startswith("/"):
        folder = "/"
    elif folder:
        folder += "/"
    base = (cwd / Path(folder or ".").expanduser()) if folder != "/" else Path("/")
    try:
        entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    found = [
        f"{folder}{p.name}{'/' if p.is_dir() else ''}"
        for p in entries
        if p.name.lower().startswith(name.lower()) and (name.startswith(".") or not p.name.startswith("."))
    ]
    if not partial:
        found.insert(0, "~/")
    return found
