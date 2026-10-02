"""Your replies to the agent, kept where no agent writes: .task/comments.md, which only the host
appends to. A container reads them where the prompts say, /task/handoff/comments.md, through a
read-only mount over the handoff folder the agent writes; kept in that folder, a reply could be
rewritten into one you never gave ("the user says: skip the tests")."""

from __future__ import annotations

import time
from pathlib import Path

from . import handoff
from .podspec import Mount

FILE = "comments.md"
TARGET = f"/task/handoff/{FILE}"


def mount(meta: Path) -> Mount:
    """Over the handoff folder's mount, so it comes after it; its mount point there is the agent's
    to tamper with, so the pod makes it a plain empty file first (Mount.placeholder)."""
    return Mount(str(meta / FILE), TARGET, read_only=True, placeholder=str(meta / "handoff" / FILE))


def ensure(meta: Path) -> None:
    """The mount's source, empty, where there is none: missing, Docker would make a folder of root's
    in its place."""
    with handoff.appending(meta / FILE):
        pass


def entry(comment: str, added: list[str]) -> str:
    """One reply as it is appended: a heading with the time, what you wrote, and the criteria it
    added to the accepted plan."""
    if added:
        listed = "\n".join(f"- {c}" for c in added)
        note = f"New acceptance criteria, added to criteria.md unticked; meet and tick them:\n\n{listed}"
        comment = f"{comment.strip()}\n\n{note}" if comment.strip() else note
    return f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{comment.strip()}\n"
