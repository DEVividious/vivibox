"""Your repository at a task's base commit, as git keeps it in your checkout: which links it had,
with their targets, and how large each file was. The base commit is the one the task recorded
when it was made (TaskState.base_commit, in the task's .task/state.json, which only the host
writes); your checkout is the project's repository it was cloned from. The clone's own .git is
the agent's to write, so its objects are never asked."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# A commit id as git writes it, SHA-1 or SHA-256: nothing else reaches git's command line.
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LINK = b"120000"


@dataclass
class Baseline:
    links: dict[str, str] = field(default_factory=dict)  # path from the root: its target
    sizes: dict[str, int] = field(default_factory=dict)  # path from the root: its size in bytes
    # Why vivibox could not read it, for the person; "" when it did. Then nothing is yours.
    problem: str = ""


def of_task(meta: Path) -> Baseline:
    """The base of the task whose metadata folder is meta."""
    from .config import ConfigError, load_project  # what vivibox knows of your projects
    from .task import Task

    try:
        st = Task(meta.parent).read_state()
    except (OSError, ValueError, KeyError, TypeError):
        return Baseline(problem="the task's state could not be read")
    if not st.base_commit:
        return Baseline(problem="the task has no base commit on record")
    try:
        source = load_project(st.project).repo
    except ConfigError as e:
        return Baseline(problem=f"its project could not be read ({e})")
    return read(source, st.base_commit)


def read(source: Path, commit: str) -> Baseline:
    if not COMMIT.fullmatch(commit):
        return Baseline(problem=f"the task's base commit on record ({commit[:20]!r}) is not a commit id")
    if not source.is_dir():
        return Baseline(
            problem=f"{source} is gone; if your repository moved, set its new path as the project's repo"
        )
    listed = _git(source, "ls-tree", "-r", "-l", "-z", "--full-tree", commit)
    if listed is None:
        return Baseline(
            problem=f"{source} does not have the task's base commit {commit[:12]} (gone from its history, "
            "or another repository)"
        )
    found, oids = Baseline(), {}
    for entry in filter(None, listed.split(b"\0")):
        head, _, name = entry.partition(b"\t")
        mode, kind, oid, size = head.split()
        if mode == LINK:
            oids[os.fsdecode(name)] = oid.decode()
        elif kind == b"blob":
            found.sizes[os.fsdecode(name)] = int(size)
    if any(not COMMIT.fullmatch(oid) for oid in oids.values()):
        return Baseline(problem=f"{source} listed something other than an object id")
    targets = _git(source, "cat-file", "--batch", input="".join(f"{oid}\n" for oid in oids.values()).encode())
    if targets is None:
        return Baseline(problem=f"{source} could not show the links of the task's base commit {commit[:12]}")
    try:
        for name in oids:  # in the order asked: "<oid> blob <size>\n<target>\n" each
            header, _, targets = targets.partition(b"\n")
            size = int(header.split()[2])
            found.links[name], targets = os.fsdecode(targets[:size]), targets[size + 1 :]
    except (IndexError, ValueError):  # "<oid> missing"
        return Baseline(problem=f"{source} is missing a link of the task's base commit {commit[:12]}")
    return found


def _git(source: Path, *args: str, input: bytes = b"") -> bytes | None:
    """git's output in your checkout, None when it failed; an argument list, never a shell."""
    try:
        done = subprocess.run(
            ["git", "-C", str(source), *args], input=input, capture_output=True, check=False
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None
