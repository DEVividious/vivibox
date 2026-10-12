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

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import counted, display, reading, repo
from .config import DEFAULT_HISTORY_DEPTH, ConfigError, load_project

# A commit as the planner names one: its id in hexadecimal, from the seven characters git
# shortens one to up to a whole SHA-256 id. Nothing else is taken: no ref, range, option or
# address, which would have git on the host fetch, or run, what the planner chose.
COMMIT_ID = re.compile(r"[0-9a-f]{7,64}")
# Steps of history_depth commits each that one request may take, from the clone and the remote
# together: 2 000 commits behind the copy's oldest at the default depth, and never more.
STEPS = 20
# Bytes one fetch may print, on both streams (counted.run): it prints a line or two.
LIMIT = 64 * 2**20

AVAILABLE = "available"
NOT_AN_ID = "not a commit id"
NO_REMOTE = "no remote recorded"
NOT_FOUND = "not found within {commits} commits before the base on its branch"
NOT_FETCHED = "could not be fetched from {source}: {why}"
CLONE = "the task's clone"


def remote(source: Path, branch: str) -> str:
    """The address of the remote your branch tracks in your checkout at source, else origin's,
    else "": read when a task is made, by git on the host in its own environment
    (repo.git_environment), as the task's base is read. branch: "" for a task started on no
    branch, which tracks nothing."""
    for name in (_tracked(source, branch), "origin"):
        if name and (found := _config(source, f"remote.{name}.url")):
            return found
    return ""


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
    """commit, and its parent, in the planner's copy of the commits (pod.control_src), or why
    not. A commit id and nothing else, refused before any git runs; one the copy has is there
    with no fetch. Else the copy is made deeper along the task's branch, depth commits a step,
    from the task's clone (repo.guard first, as for every git of the host's that reads it), on
    this machine, until the commit is there or the clone has no more; then, the same way along
    your base branch, from the remote recorded when the task was made (remote), by your own means
    of reaching it (repo.TO_YOUR_REMOTES), and only for a task that starts from your own work
    (repo.yours, in your checkout at source: the project's, when not given). STEPS steps in all,
    each within repo.CLONE_TIMEOUT from the clone, and the whole within repo.COPY_TIMEOUT. A
    request that does not end with the commit there leaves the copy as deep as it was."""
    if not COMMIT_ID.fullmatch(commit):
        return Outcome(commit, NOT_AN_ID)
    walk = _Walk(Path(pod.control_src), depth)
    if walk.has(commit):
        return Outcome(commit, AVAILABLE)
    said = _deepened(task, walk, commit, source)
    if said != AVAILABLE:
        walk.as_it_was()
    return Outcome(commit, said, walk.steps)


def _deepened(task, walk: _Walk, commit: str, source: Path | None) -> str:
    branch = f"refs/heads/{repo.branch_name(task.id)}"
    why = walk.along(commit, str(task.repo), branch, lambda: repo.guard(task.repo))
    if why:
        return why if why == AVAILABLE else NOT_FETCHED.format(source=CLONE, why=why)
    if walk.steps >= STEPS:
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
    """The planner's copy of the commits, made deeper a step at a time: its shallow boundary as
    it was before the first step, to put back, and the steps taken."""

    def __init__(self, view: Path, depth: int):
        self.view, self.depth, self.steps = view, depth, 0
        self.until = time.monotonic() + repo.COPY_TIMEOUT
        self.boundary = self._boundary()

    def _boundary(self) -> bytes | None:
        try:
            return (self.view / ".git" / "shallow").read_bytes()
        except FileNotFoundError:
            return None

    def has(self, commit: str) -> bool:
        """The commit and its parent are objects of the copy: git show there is its change."""
        own = reading.own_git(self.view)
        return all(
            repo.git("cat-file", "-e", name, cwd=self.view, check=False, env=own).returncode == 0
            for name in (f"{commit}^{{commit}}", f"{commit}^")
        )

    def along(self, commit: str, source: str, ref: str, check=None, from_remote: bool = False) -> str:
        """Steps from source along ref until the commit is there (AVAILABLE), until source has
        no more or the steps are spent (""), or until a step fails (why). check: what refuses
        source before each step, as an error whose words are why."""
        while self.steps < STEPS:
            before = self._boundary()
            try:
                if check:
                    check()
            except repo.RepoError as e:
                return _one_line(str(e))
            if why := self._step(source, ref, from_remote):
                return why
            if self.has(commit):
                return AVAILABLE
            if self._boundary() == before:
                return ""
        return ""

    def _step(self, source: str, ref: str, from_remote: bool) -> str:
        """One fetch of depth more commits; "" once it is done, else why not."""
        left = self.until - time.monotonic()
        if left <= 0:
            return f"no answer within {repo.COPY_TIMEOUT:g} s"
        seconds = left if from_remote else min(repo.CLONE_TIMEOUT, left)
        args = ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--update-shallow"]
        args += [f"--deepen={self.depth}", source, ref]
        # From the clone: the host's git, which reads none of your configuration. From your
        # remote: your configuration too, where your credential helper and your URL rewrites are,
        # as for the submodules of a new clone; never waiting on the terminal.
        own = reading.own_git(self.view) if not from_remote else _to_your_remote(self.view)
        self.steps += 1
        try:
            done = repo.run_git(
                args, cwd=self.view, env=own, to_remotes=from_remote, limit=LIMIT, timeout=seconds,
                stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return f"no answer within {seconds:g} s"
        except counted.TooMuch:
            return f"the fetch printed more than {LIMIT // 2**20} MiB"
        except OSError as e:
            return _one_line(str(e))
        if done.returncode != 0:
            said = display.said(done.stderr.decode("utf-8", "replace"))
            return _one_line(said) or f"exit {done.returncode}"
        return ""

    def as_it_was(self) -> None:
        """The copy's boundary as it was before the first step: what was fetched for a commit
        that did not come is out of every history git shows there. A fetch ended past its time
        leaves its lock, which would refuse the next one; the copy is the host's alone."""
        git_dir = self.view / ".git"
        (git_dir / "shallow.lock").unlink(missing_ok=True)
        if self._boundary() == self.boundary:
            return
        if self.boundary is None:
            (git_dir / "shallow").unlink(missing_ok=True)
            return
        kept = git_dir / "shallow.vivibox"
        kept.write_bytes(self.boundary)
        os.replace(kept, git_dir / "shallow")


def _to_your_remote(view: Path) -> dict[str, str]:
    return {
        "GIT_DIR": f"{view}/.git", "GIT_WORK_TREE": str(view), "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }  # fmt: skip


def _one_line(text: str) -> str:
    """The last line of what was said, which is what git ends on, as one line of history.md."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return display.name(lines[-1])[:300] if lines else ""
