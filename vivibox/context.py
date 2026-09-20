"""Files you point a task at with @path in its description: copied into the task, read-only for the agent.

The agent never sees your disk; it gets copies under /task/context/, and the description refers to
those. Files that look like credentials are refused, so a stray @~/.aws does not hand keys to a model.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
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


def resolve(description: str, cwd: Path) -> dict[str, Path]:
    """Checks every @path before anything is created. Returns mention -> file or directory."""
    found, total = {}, 0
    for mention in mentions(description):
        path = (cwd / Path(mention).expanduser()).resolve()
        if not path.exists():
            if mention.startswith(EXPLICIT):
                raise ContextError(f"@{mention}: no such file or directory")
            continue
        for f in _files(path):
            if _secret(f):
                raise ContextError(f"@{mention}: {f} looks like a credential; not giving it to the agent")
            total += f.stat().st_size
        found[mention] = path
    if total > LIMIT_BYTES:
        raise ContextError(f"the files you mention take {total // 1024 // 1024} MB; the limit is 20 MB")
    return found


def attach(description: str, found: dict[str, Path], folder: Path) -> str:
    """Copies the files into folder and rewrites the description to the paths the agent sees."""
    folder.mkdir(exist_ok=True)
    for mention, path in found.items():
        name, n = path.name, 1
        while (folder / name).exists():
            n += 1
            name = f"{path.stem}-{n}{path.suffix}"
        if path.is_dir():
            # Links are copied as links: they point at your disk, which the agent cannot see.
            shutil.copytree(path, folder / name, symlinks=True)
        else:
            shutil.copy2(path, folder / name)
        description = re.sub(
            rf"(?:(?<=\s)|^)@{re.escape(mention)}", f"{MOUNT}/{name}", description, flags=re.M
        )
    return description


def complete(partial: str, cwd: Path, limit: int = 40) -> list[str]:
    """Paths that complete what follows an @, like a shell: 'src/ma' -> ['src/main/'].
    Directories end with '/'; hidden entries show only once you type the dot."""
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
    return found[:limit]
