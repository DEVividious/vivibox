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

from pathlib import Path

from . import repo


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
