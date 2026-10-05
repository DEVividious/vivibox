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
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, cli_usage, comments, gate, handoff, opencode, providers, reading, repo, roles, secrets
from .config import Config, Role
from .harness import Harness
from .pod import Mount, Pod
from .prompts import CLI_REVIEW as CLI_REVIEW_TEXT
from .prompts import CLI_REVIEW_LEFT_OUT, CLI_REVIEW_NOT_WRITTEN, CLI_REVIEW_REPLY
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
# The writer's files that review reads, copied each round into a folder of the task's that only the
# host writes (/task is read-only in the pod): your CLI follows a link the agent put in handoff/.
CLI_INPUT = "review-input"


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


def _copied(task: Task, names: list[str], reply: str) -> tuple[list[Path], str]:
    """The host's copies of the writer's files, and why each one not copied was left out; a reply
    the writer did not write is said nothing of, as no reply to the last round."""
    source, folder = task.meta / "handoff", task.meta / CLI_INPUT
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    copies, said = [], ""
    for name in [*names, reply] if reply else names:
        try:
            handoff.copy_out(source / name, folder / name)
            copies.append(folder / name)
        except FileNotFoundError:
            said += "" if name == reply else CLI_REVIEW_NOT_WRITTEN.format(name=name)
        except handoff.HandoffError:
            refused = handoff.refused(source, name)
            why = "it is a link or not a plain file, and vivibox does not read through one"
            said += CLI_REVIEW_LEFT_OUT.format(name=name, why=why if refused else "vivibox could not read it")
    return copies, said


def ask_cli(task: Task, n: int, copy: Path | None) -> None:
    """The prompt for round n of a review in the agent's CLI, which then waits for it."""
    meta = task.meta
    reply = f"review-{n - 1}-reply.md" if n > 1 else ""
    copies, left_out = _copied(task, ["criteria.md", "red.md"], reply)
    read = [meta / gate.ACCEPTED_PLAN, *(c for c in copies if c.name != reply)]
    replied = [c for c in copies if reply and c.name == reply]
    text = CLI_REVIEW_TEXT.format(
        task=task.id,
        n=n,
        files=" and ".join(filter(None, [", ".join(map(str, read[:-1])), str(read[-1])])),
        reply=CLI_REVIEW_REPLY.format(path=replied[0]) if replied else "",
        copy=copy or f"(prepare it: vivibox review {task.id})",
        answer=meta / CLI_ANSWER,
        left_out=left_out,
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
    handoff.write_text(path, text if text.endswith("\n") else text + "\n")
    return path


def unreadable(task: Task, n: int) -> str:
    """Why the supervisor could not read round n's review, "" when it could: its own record, since
    what it keeps for such a round is no review, and counted as one it would read as a clean one."""
    said = [e["data"] for e in task.events() if e["type"] == "review" and e["data"].get("round") == n]
    return said[-1].get("problem", "") if said else ""


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


REVIEWERS_OWN = "the reviewer's"


class Unverified(repo.RepoError):
    """The task records no commit that passed the verification, so there is nothing a reviewer's
    round could be made of. The supervisor answers it by verifying first (review_round), which
    is why a start of the task leads out of it."""


def env(task: Task) -> dict[str, str]:
    """The review container's environment: its harness's configuration, git answering for the
    view of the commits from wherever it is run (the harness stands in a directory of the
    host's, podspec.READER_WORK), and no language server downloaded (opencode.READER_ENV)."""
    return {"OPENCODE_CONFIG": opencode.CONFIG, **reading.git_env(task.repo), **opencode.READER_ENV}


def view(task: Task, pod: Pod) -> None:
    """The round's working directory for the reviewer's harness and its view of the commits,
    made on the host in the review directory (reading), before its container: on the commit the
    verification passed. One that cannot be made stops the review with the reason. A task
    that records no verified commit gets none (Unverified): its branch as it happens to be is
    not reviewed, and the supervisor verifies it first."""
    verified = task.read_state().verified_commit
    if not verified:
        raise Unverified(
            f"the review could not start: {task.id} records no commit that passed the verification; "
            f"start the task again: vivibox start {task.id}"
        )
    try:
        reading.workdir(pod.review_work, REVIEWERS_OWN)
        reading.view(task.repo, repo.branch_name(task.id), Path(pod.review_src), verified, REVIEWERS_OWN)
    except repo.GitTimeout as e:
        again = f"then start the task again: vivibox start {task.id}"  # the way to try again, here
        raise repo.GitTimeout(
            f"the review could not start: {str(e).removesuffix(repo.RETRY)}; {again}"
        ) from None
    except repo.Blocked:  # as it is: the supervisor sends it to the verification, which says it
        raise
    except repo.RepoError as e:
        raise repo.RepoError(f"the review could not start: {e}") from None


def who(task: Task, config: Config) -> Role:
    """What reviews in the review container, as the task runs it now: the reviewer where
    config.toml names one, else the writer's model (planner_writer_reviewer without
    [roles.reviewer]), on the model the task chose for it (m). Read once for a round, by
    whoever makes the round's container and its harness, so that the two are of one role."""
    return roles.role_of(task, "reviewer" if "reviewer" in config.roles else "writer", config)


class Reviewer:
    """The reviewer's harness as the supervisor holds it for the whole task: the round's own
    behind it, made anew with each round's container (begin), from the role that container was
    made for. A turn so asks for the model whose provider's configuration and key its container
    holds; what you choose under m while a round runs is the next round's, whole: its container,
    its key and its model together. Everything else is the round's harness's own."""

    def __init__(self, make: Callable[[Role], Harness], role: Role):
        self.make, self.role, self.harness = make, role, make(role)

    def begin(self, role: Role) -> None:
        """A round's container was made for role: its turns are that role's."""
        self.role, self.harness = role, self.make(role)

    def __getattr__(self, name):
        return getattr(self.harness, name)


def up(
    task: Task, pod: Pod, config: Config, verify: list[str] | tuple = (), role: Role | None = None
) -> Path:
    """The reviewer's container, ready for a turn: a working directory and a view of the commits
    that the host makes for the round, the reviewer's own key and harness files, the task's
    files to read, and the directory its review goes to, returned. verify: the project's
    verification commands, a fact its brief names. The view stands on the
    commit the verification passed; a task that records none is not reviewed.
    The container starts only once both are made: a view that cannot be made stops the review
    with the reason, and the reviewer is never started in the writer's tree instead.
    role: what reviews this round (who), when the caller read it for the round's harness too;
    the container's configuration and its secrets are that role's provider's and no other's."""
    role = role or who(task, config)
    # A reader's configuration: no language server, no Serena of vivibox's (opencode.READER).
    settings = opencode.config(role.model, (), task.repo, reader=True)
    # Exactly what its opencode.json reads: its provider's key and its MCP servers' secrets.
    runtime = secrets.prepare(secrets_id(task.id), providers.mounted(settings))
    pod.review_fresh()
    harness = pod.review_dir / "harness"
    harness.mkdir()
    branch = repo.branch_name(task.id)
    # The reader's brief: the project as a read-only view at the path the writer has its clone,
    # what this container has not; the handoff is not the reviewer's to write.
    told = brief.reader(task.id, task.repo, branch, verify, writes_handoff=False)
    (harness / "instructions.md").write_text(told)
    (harness / "opencode.json").write_text(json.dumps(settings, indent=2) + "\n")
    out = pod.review_dir / "out"
    out.mkdir()
    # The mount point inside /task, which the container has read-only: it has to exist already.
    (task.meta / "review").mkdir(exist_ok=True)
    view(task, pod)
    mounts = [
        Mount(str(task.meta), "/task", read_only=True),
        comments.mount(task.meta),  # REVIEW_PROMPT reads them; the file under /task is a mount point
        Mount(str(harness), opencode.HARNESS_MOUNT, read_only=True),
        Mount(str(out), MOUNT),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]
    pod.review_start(mounts, env(task))
    return out
