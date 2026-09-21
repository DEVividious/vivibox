"""Which vivibox is running. After `git pull` the view and every supervisor keep the code they
started with, and nothing said so: a fix that was on disk looked like a fix that did not work.

The signature is a hash of the package's files, so it follows what is on disk, and a touch or a
checkout that changes nothing changes nothing here.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

ROOT = Path(__file__).parent
# Written by a supervisor next to its pid: the vivibox it runs.
RECORD = "supervisor.code"
SUFFIXES = {".py", ".md", ".toml", ".sh", ".json", ""}


@functools.cache
def signature() -> str:
    """A hash of the package as it is on disk now. Cached: what runs does not change, and the
    view clears it when it wants to look again."""
    digest = hashlib.sha256()
    for path in sorted(p for p in ROOT.rglob("*") if p.is_file() and p.suffix in SUFFIXES):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def record(meta: Path) -> None:
    (meta / RECORD).write_text(signature() + "\n")


def older_supervisor(meta: Path) -> bool:
    """Whether the supervisor that recorded itself here runs an older vivibox than the one on
    disk. False when none recorded itself: before this file existed, nothing was said either."""
    try:
        return (meta / RECORD).read_text().strip() != signature()
    except OSError:
        return False
