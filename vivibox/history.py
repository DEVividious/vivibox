"""History on request: the planner's copy of the commits holds the last history_depth commits of
the task's branch (reading.view), and the host alone makes it deeper, for a commit the planner
names by its id in its handoff, at the end of its turn.

The older commits come from the task's clone first, on this machine, with no network: the clone
holds the whole history of your checkout. Where your checkout is shallow itself, and the clone
has no more, they come from the remote your base branch tracks, as recorded when the task was
made (remote), the way the submodules of a new clone are fetched: by your own means of reaching
it, and only for a task that starts from your own work (repo.yours). Never from an address the
task's clone or the request names: the request is commit ids and nothing else.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import control, display, handoff, reading, repo
from .config import DEFAULT_HISTORY_DEPTH, ConfigError, load_project

# A commit as the planner names one: its id in hexadecimal, from the seven characters git
# shortens one to up to a whole SHA-256 id. Nothing else is taken: no ref, range, option or
# address, which would have git on the host fetch, or run, what the planner chose.
COMMIT_ID = re.compile(r"[0-9a-f]{7,64}")
# Steps of history_depth commits each that one request may take, from the clone and the remote
# together: 2 000 commits behind the copy's oldest at the default depth, and never more.
STEPS = reading.STEPS
# Bytes one fetch may print, on both streams (counted.run): it prints a line or two.
LIMIT = reading.FETCH_LIMIT

AVAILABLE = "available"
NOT_AN_ID = "not a commit id"
NO_REMOTE = "no remote recorded"
NOT_FOUND = "not found within {commits} commits before the base on its branch"
NOT_FETCHED = "could not be fetched from {source}: {why}"
CLONE = "the task's clone"

# The reader's request, in the handoff: a commit id a line; and the host's answer beside it.
REQUEST, ANSWER = "history-request.md", "history.md"
# Characters of a request read: five ids and room to spare.
READ = 4096
# Ids looked for in one request, and requests answered in one phase of a task (a planning, a
# round's review): a reader that asks again past them is told to ask the person.
IDS, ROUNDS = 5, 3
PAST_IDS = f"not looked for: a request holds at most {IDS} ids"
PAST_ROUNDS = f"refused: {ROUNDS} requests were answered in this phase; ask the person"


def remote(source: Path, branch: str) -> str:
    """The address of the remote your branch tracks in your checkout at source, else origin's,
    else "": read when a task is made, by git on the host in its own environment
    (repo.git_environment), as the task's base is read. branch: "" for a task started on no
    branch, which tracks nothing."""
    for name in (_tracked(source, branch), "origin"):
        if name and (found := _config(source, f"remote.{name}.url")):
            return _as_recorded(source, found)
    return ""


def _as_recorded(source: Path, address: str) -> str:
    """The address as the host can fetch from it later, from a folder of its own: "" for one
    git would take for an option; a path on this machine from your checkout made whole, as git
    in your checkout reads it. A URL, and the user@host:path form, are as they are."""
    if address.startswith("-"):
        return ""
    if "://" in address or ":" in address.split("/", 1)[0] or os.path.isabs(address):
        return address
    return os.path.normpath(source / address)


def _tracked(source: Path, branch: str) -> str:
    return _config(source, f"branch.{branch}.remote") if branch else ""


def _config(source: Path, key: str) -> str:
    return repo.git("config", "--get", key, cwd=source, check=False).stdout.strip()


@dataclass(frozen=True)
class Outcome:
    """What became of one commit asked for: said, one line, as history.md has it; steps: the
    fetches it took."""

    commit: str
    said: str
    steps: int = 0

    @property
    def available(self) -> bool:
        return self.said == AVAILABLE


def ensure(task, pod, commit: str, depth: int = DEFAULT_HISTORY_DEPTH, source: Path | None = None) -> Outcome:
    """commit, and its parent (a root of the repository has none), in the planner's copy of
    the commits (pod.control_src), or why not. A commit id and nothing else, refused before
    any git runs; one the copy has is there with no fetch. Else the copy is made deeper along
    the task's branch, depth commits a step, from the task's clone (repo.guard first, as for
    every git of the host's that reads it), on this machine, until the commit is there or the
    clone has no more; then, where the clone is shallow (your checkout is), the same way along
    your base branch, from the remote recorded when the task was made (remote), by your own
    means of reaching it (repo.TO_YOUR_REMOTES), and only for a task that starts from your
    own work (repo.yours, in your checkout at source: the project's, when not given); a
    commit a whole clone lacks is not found. STEPS steps in all, each within
    repo.CLONE_TIMEOUT from the clone, and the whole within repo.COPY_TIMEOUT. A request that
    does not end with the commit there leaves the copy as deep as it was."""
    commit = as_id(commit)
    if not COMMIT_ID.fullmatch(commit):
        return Outcome(commit, NOT_AN_ID)
    walk = _Walk(Path(pod.control_src), depth)
    if walk.has(commit):
        return Outcome(commit, AVAILABLE)
    said = _deepened(task, walk, commit, source)
    if said != AVAILABLE:
        walk.as_it_was()
    return Outcome(commit, said, walk.steps)


def as_id(text: str) -> str:
    """A commit id in git's own small letters, the same commit in capitals included; anything
    else as it is, to be refused as no id."""
    lowered = text.lower()
    return lowered if COMMIT_ID.fullmatch(lowered) else text


def _deepened(task, walk: _Walk, commit: str, source: Path | None) -> str:
    branch = f"refs/heads/{repo.branch_name(task.id)}"
    why = walk.along(commit, str(task.repo), branch, lambda: repo.guard(task.repo))
    if why:
        return why if why == AVAILABLE else NOT_FETCHED.format(source=CLONE, why=why)
    if walk.steps >= STEPS:
        return NOT_FOUND.format(commits=STEPS * walk.depth)
    # The clone of a whole checkout holds the base branch's whole history as of the base: what
    # it lacks is on no history of that branch, and no remote is asked for it.
    try:
        shallow = repo.in_clone(task.repo, "rev-parse", "--is-shallow-repository").stdout.strip()
    except repo.RepoError as e:
        return NOT_FETCHED.format(source=CLONE, why=_one_line(str(e)))
    if shallow != "true":
        return NOT_FOUND.format(commits=STEPS * walk.depth)
    st = task.read_state()
    if not st.history_remote:
        return NO_REMOTE
    where = shown(st.history_remote)
    try:
        yours = source or load_project(st.project).repo
    except ConfigError as e:
        return NOT_FETCHED.format(source=where, why=_one_line(str(e)))
    if not repo.yours(yours, st.base_commit):
        return NOT_FETCHED.format(source=where, why="the task does not start from your own work")
    if not st.base_branch:
        return NOT_FETCHED.format(source=where, why="the task started on no branch to fetch along")
    why = walk.along(commit, st.history_remote, f"refs/heads/{st.base_branch}", from_remote=True)
    if why:
        return why if why == AVAILABLE else NOT_FETCHED.format(source=where, why=why)
    return NOT_FOUND.format(commits=STEPS * walk.depth)


def shown(address: str) -> str:
    """A remote's address as history.md and the timeline say it: without a user and password
    written into it (https://user:token@host/…), which are yours and no reader's."""
    return display.name(re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@", r"\1", address))


class _Walk:
    """The planner's copy of the commits, made deeper a step at a time (reading.deepen): its
    shallow boundary as it was before the first step, to put back, and the steps taken."""

    def __init__(self, view: Path, depth: int):
        self.view, self.depth, self.steps = view, depth, 0
        self.until = time.monotonic() + repo.COPY_TIMEOUT
        self.boundary = reading.boundary(view)

    def has(self, commit: str) -> bool:
        """The commit is on the copy's history, an ancestor of its HEAD as git shows it there,
        and git show there is its change: its parent is there too, or it is a root of the
        repository, whose raw object names no parent (read as kept, whatever the copy's
        boundary says), and whose change is against the empty tree. What a request that failed
        left in the object store, or a remote's commit past the base, is on no such history."""
        own = reading.own_git(self.view)
        ask = lambda *args: repo.git(*args, cwd=self.view, check=False, env=own)  # noqa: E731
        if ask("merge-base", "--is-ancestor", commit, "HEAD").returncode != 0:
            return False
        if ask("cat-file", "-e", f"{commit}^").returncode == 0:
            return True
        raw = ask("cat-file", "commit", commit)
        return raw.returncode == 0 and not any(
            line.startswith("parent ") for line in raw.stdout.partition("\n\n")[0].splitlines()
        )

    def along(self, commit: str, source: str, ref: str, check=None, from_remote: bool = False) -> str:
        """Steps from source along ref until the commit is there (AVAILABLE), until source has
        no more or the steps are spent (""), or until a step fails (why). check: what refuses
        source before each step, as an error whose words are why."""
        while self.steps < STEPS:
            before = reading.boundary(self.view)
            try:
                if check:
                    check()
            except repo.RepoError as e:
                return _one_line(str(e))
            if why := self._step(source, ref, from_remote):
                return why
            if self.has(commit):
                return AVAILABLE
            if reading.boundary(self.view) == before:
                return ""
        return ""

    def _step(self, source: str, ref: str, from_remote: bool) -> str:
        """One fetch of depth more commits; "" once it is done, else why not."""
        left = self.until - time.monotonic()
        if left <= 0:
            return f"no answer within {repo.COPY_TIMEOUT:g} s"
        seconds = left if from_remote else min(repo.CLONE_TIMEOUT, left)
        # From the clone: the host's git, which reads none of your configuration. From your
        # remote: your configuration too, where your credential helper and your URL rewrites are,
        # as for the submodules of a new clone; never waiting on the terminal.
        own = reading.own_git(self.view) if not from_remote else _to_your_remote(self.view)
        self.steps += 1
        return reading.deepen(self.view, source, ref, self.depth, seconds, own, to_remotes=from_remote)

    def as_it_was(self) -> None:
        """The copy's boundary as it was before the first step (reading.put_back)."""
        reading.put_back(self.view, self.boundary)


def _to_your_remote(view: Path) -> dict[str, str]:
    return {
        "GIT_DIR": f"{view}/.git", "GIT_WORK_TREE": str(view), "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }  # fmt: skip


def _one_line(text: str) -> str:
    return reading.one_line(text)


def answer(task, pod, depth: int = DEFAULT_HISTORY_DEPTH, source: Path | None = None) -> bool:
    """The reader's request for older commits, at the end of its turn in its control container:
    REQUEST in the handoff, a commit id a line, read as every file there is (handoff), and taken
    away. Each of the first IDS ids is looked for (ensure), the copy brought up to date
    (control.refresh), and ANSWER says what became of each, a line an id; on the record as
    history_requested, then history_acquired or history_refused for each. True when the reader
    is to be given a turn to read it. False for no request, for one that is not read (a link,
    a special file), and for one past ROUNDS in this phase of the task, which is refused in
    ANSWER as a whole, nothing fetched: the turn then goes on as if none had been made."""
    folder = task.meta / "handoff"
    path = folder / REQUEST
    try:
        text, _ = handoff.read_start(path, READ)
    except FileNotFoundError:
        return False
    except handoff.HandoffError as e:
        _away(path)
        _write(folder, [f"{REQUEST} was not read: {e.why}"])
        return False
    _away(path)
    asked = [as_id(display.name(line.strip())[:80]) for line in text.splitlines() if line.strip()]
    if not asked:
        return False
    if (answered := _rounds(task)) >= ROUNDS:
        refused = [Outcome(commit, PAST_ROUNDS) for commit in asked]
        _write(folder, [f"{o.commit}: {o.said}" for o in refused])
        _record(task, refused)
        return False
    task.event("history_requested", ids=asked[:IDS], round=answered + 1)
    found = [ensure(task, pod, commit, depth=depth, source=source) for commit in asked[:IDS]]
    found += [Outcome(commit, PAST_IDS) for commit in asked[IDS:]]
    control.refresh_for(task, pod, depth)
    _write(folder, [f"{o.commit}: {o.said}" for o in found])
    _record(task, found)
    return True


def _rounds(task) -> int:
    """The requests answered since the task entered the state it is in: this planning, or
    this round's review."""
    count = 0
    for event in reversed(task.events()):
        if event["type"] == "state":
            break
        count += event["type"] == "history_requested"
    return count


def _record(task, found: list[Outcome]) -> None:
    for o in found:
        if o.available:
            task.event("history_acquired", id=o.commit, outcome=o.said, steps=o.steps)
        else:
            task.event("history_refused", id=o.commit, outcome=o.said)


def _away(path: Path) -> None:
    """The name gone, whatever it is, nothing it may name opened or followed."""
    with contextlib.suppress(FileNotFoundError, IsADirectoryError, PermissionError):
        path.unlink()


def _write(folder: Path, lines: list[str]) -> None:
    """ANSWER, anew: whatever had its name goes first, a link the writer left there among it."""
    _away(folder / ANSWER)
    handoff.write_text(folder / ANSWER, "".join(f"{line}\n" for line in lines))
