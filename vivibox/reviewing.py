"""The reviewer's file: what it says, whether it is one, and where the task keeps each round.

A review is `handoff/review-N.md` with two sections, `## Blocking` and `## Not blocking`, each a
list of notes that name a place (`path:line`). The supervisor reads the blocking list and nothing
else decides: a review with no blocking notes lets the work go on to you, a review that is not
one comes back to the reviewer once, like a plan draft that is not a plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, opencode, providers, repo, roles, secrets
from .config import Config
from .pod import Mount, Pod
from .task import Task

# Where the reviewer writes, in its container: outside the handoff, which it only reads.
MOUNT = "/task/review"

BLOCKING, NOT_BLOCKING = "## Blocking", "## Not blocking"
# A note names where: a path, a colon, a line number, then what is wrong.
PLACE = re.compile(r"^\S+:\d+\b")
NOTE = re.compile(r"^\s*-\s*(?:\[[ xX]\]\s*)?(.*\S)\s*$")
FILE = "review-{n}.md"
NUMBERED = re.compile(r"^review-(\d+)\.md$")


@dataclass
class Review:
    blocking: list[str] = field(default_factory=list)
    not_blocking: list[str] = field(default_factory=list)


def parse_review(text: str) -> Review:
    review, section = Review(), None
    for line in text.splitlines():
        heading = line.strip()
        if heading.startswith("#"):
            section = (
                review.blocking
                if heading.lower() == BLOCKING.lower()
                else review.not_blocking
                if heading.lower() == NOT_BLOCKING.lower()
                else None
            )
        elif section is not None and (m := NOTE.match(line)):
            section.append(m.group(1))
    return review


def problem(text: str) -> str:
    """Why the text is not a review the supervisor can read, or "" when it is."""
    lines = {line.strip().lower() for line in text.splitlines()}
    for heading in (BLOCKING, NOT_BLOCKING):
        if heading.lower() not in lines:
            return f"the section {heading} is missing"
    review = parse_review(text)
    for note in review.blocking + review.not_blocking:
        if not PLACE.match(note):
            return f"a note has no place (path:line) to act on: {note[:80]}"
    return ""


def keep(task: Task, n: int, text: str) -> Path:
    """The round's review, kept in the handoff for the writer and for you."""
    path = task.meta / "handoff" / FILE.format(n=n)
    path.write_text(text if text.endswith("\n") else text + "\n")
    return path


def latest(task: Task) -> Path | None:
    handoff = task.meta / "handoff"
    found = [(int(m.group(1)), p) for p in handoff.glob("review-*.md") if (m := NUMBERED.match(p.name))]
    return max(found)[1] if found else None


def secrets_id(task_id: str) -> str:
    """The key store's runtime directory of the reviewer's container: the task's, apart."""
    return f"{task_id}-review"


def forget(task_id: str) -> None:
    """The reviewer's key is gone from the machine with the task's, on a stop and on a removal."""
    secrets.remove(secrets_id(task_id))


def up(task: Task, pod: Pod, config: Config) -> Path:
    """The reviewer's container, ready for a turn: a fresh clone, the reviewer's own key and
    harness files, the task's files to read, and the directory its review goes to, returned."""
    role = roles.role_of(task, "reviewer", config)
    provider = opencode.provider_of(role.model)
    needed = ([] if providers.keyless(provider) else [provider]) + providers.mcp_secrets()
    runtime = secrets.prepare(secrets_id(task.id), needed)
    pod.review_fresh()
    harness = pod.review_dir / "harness"
    harness.mkdir()
    branch = repo.branch_name(task.id)
    (harness / "instructions.md").write_text(brief.common(task.id, pod.review_src, branch))
    (harness / "opencode.json").write_text(
        json.dumps(opencode.config(role.model, (), task.repo), indent=2) + "\n"
    )
    out = pod.review_dir / "out"
    out.mkdir()
    mounts = [
        Mount(str(task.meta), "/task", read_only=True),
        Mount(str(harness), opencode.HARNESS_MOUNT, read_only=True),
        Mount(str(out), MOUNT),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]
    pod.review_start(mounts, {"OPENCODE_CONFIG": opencode.CONFIG})
    return out
