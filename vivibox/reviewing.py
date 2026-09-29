"""The reviewer's file: what it says, whether it is one, and where the task keeps each round.

A review is `handoff/review-N.md` with three sections: `## Blocking` and `## Not blocking`, each a
list of notes that name a place (`path:line`), and `## Checked`, what the review read and checked,
which a review without notes stands on. The supervisor reads the blocking list and nothing
else decides: a review with no blocking notes lets the work go on to you, a review that is not
one comes back to the reviewer once, like a plan draft that is not a plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, cli_usage, gate, opencode, providers, repo, roles, secrets
from .config import Config
from .pod import Mount, Pod
from .prompts import CLI_REVIEW as CLI_REVIEW_TEXT
from .prompts import CLI_REVIEW_REPLY
from .states import State
from .task import Task

# Where the reviewer writes, in its container: outside the handoff, which it only reads.
MOUNT = "/task/review"

BLOCKING, NOT_BLOCKING, CHECKED = "## Blocking", "## Not blocking", "## Checked"
# A note names where: a path, a colon, a line number, then what is wrong.
PLACE = re.compile(r"^\S+:\d+\b")
# A note is a line of its section, as REVIEW_PROMPT asks: "path:line — …". A list mark in front
# (-, *, 1.) and a checkbox are taken off; a reviewer that left them out was read as having
# written nothing, and its blocking notes let the work through.
MARK = re.compile(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")
# What a reviewer writes under a section it leaves empty.
EMPTY = re.compile(r"^[(_*]*(?:none|nothing|n/a|-)[.)_*]*$", re.IGNORECASE)
FILE = "review-{n}.md"
# A review in an agent's CLI (ADR-0035), in the task's own directory: the prompt it reads, the file
# it writes, and the review it brought in, which the supervisor takes as a reviewer's turn.
CLI_PROMPT, CLI_ANSWER, CLI_REVIEW = "review-prompt-cli.md", "review-answer.md", "review-cli.md"
NUMBERED = re.compile(r"^review-(\d+)\.md$")


@dataclass
class Review:
    blocking: list[str] = field(default_factory=list)
    not_blocking: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)


def parse_review(text: str) -> Review:
    review, section = Review(), None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = (
                review.blocking
                if stripped.lower() == BLOCKING.lower()
                else review.not_blocking
                if stripped.lower() == NOT_BLOCKING.lower()
                else review.checked
                if stripped.lower() == CHECKED.lower()
                else None
            )
        elif section is None or not stripped or EMPTY.match(stripped):
            continue
        elif line[:1].isspace() and section and not MARK.match(stripped):
            section[-1] += " " + stripped  # a note wrapped onto the next line
        elif note := MARK.sub("", stripped):
            section.append(note)
    return review


def problem(text: str) -> str:
    """Why the text is not a review the supervisor can read, or "" when it is."""
    lines = {line.strip().lower() for line in text.splitlines()}
    for heading in (BLOCKING, NOT_BLOCKING, CHECKED):
        if heading.lower() not in lines:
            return f"the section {heading} is missing"
    review = parse_review(text)
    if not review.checked:
        return f"{CHECKED} says what was checked, one line each, and it is empty"
    for note in review.blocking + review.not_blocking:
        if not PLACE.match(note):
            return f"a note has no place (path:line) to act on: {note[:80]}"
    return ""


class ReviewError(Exception):
    pass


def ask_cli(task: Task, n: int, copy: Path | None) -> None:
    """The prompt for round n of a review in the agent's CLI, which then waits for it."""
    meta, handoff = task.meta, task.meta / "handoff"
    reply = handoff / f"review-{n - 1}-reply.md"
    text = CLI_REVIEW_TEXT.format(
        task=task.id,
        n=n,
        plan=meta / gate.ACCEPTED_PLAN,
        criteria=handoff / "criteria.md",
        red=handoff / "red.md",
        reply=CLI_REVIEW_REPLY.format(path=reply) if n > 1 and reply.exists() else "",
        copy=copy or f"(prepare it: vivibox review {task.id})",
        answer=meta / CLI_ANSWER,
    )
    (meta / CLI_ANSWER).unlink(missing_ok=True)
    (meta / CLI_REVIEW).unlink(missing_ok=True)
    (meta / CLI_PROMPT).write_text(text)
    task.set_awaiting_review(True)
    task.event("review_asked", round=n)


def _asked(task: Task) -> None:
    st = task.read_state()
    if st.state is not State.REVIEW or not st.awaiting_review:
        raise ReviewError(f"{task.id} is not waiting for a review from your CLI")


def cli_prompt(task: Task) -> str:
    _asked(task)
    return (task.meta / CLI_PROMPT).read_text()


def import_answer(task: Task, text: str | None = None) -> Review:
    """The CLI's review, checked as the supervisor would read it, handed to the supervisor. Without
    text, the answer file the CLI wrote."""
    _asked(task)
    if text is None:
        path = task.meta / CLI_ANSWER
        text = path.read_text() if path.exists() else ""
    if not text.strip():
        raise ReviewError(f"no review to bring in; write it to {task.meta / CLI_ANSWER}")
    if why := problem(text):
        raise ReviewError(f"that is not a review vivibox can read: {why}")
    temporary = task.meta / (CLI_REVIEW + ".tmp")
    temporary.write_text(text)
    temporary.replace(task.meta / CLI_REVIEW)
    cli_usage.count_review(task)
    return parse_review(text)


def brought_in(task: Task) -> str | None:
    """The review the CLI brought in, taken once by the supervisor."""
    path = task.meta / CLI_REVIEW
    if not path.exists():
        return None
    text = path.read_text()
    path.unlink()
    task.set_awaiting_review(False)
    return text


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
    # The reviewer's model where config.toml names one, else the writer's (planner_writer_reviewer
    # without [roles.reviewer]).
    role = roles.role_of(task, "reviewer" if "reviewer" in config.roles else "writer", config)
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
    # The mount point inside /task, which the container has read-only: it has to exist already.
    (task.meta / "review").mkdir(exist_ok=True)
    mounts = [
        Mount(str(task.meta), "/task", read_only=True),
        Mount(str(harness), opencode.HARNESS_MOUNT, read_only=True),
        Mount(str(out), MOUNT),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]
    pod.review_start(mounts, {"OPENCODE_CONFIG": opencode.CONFIG})
    return out
