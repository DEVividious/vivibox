"""A project's preparation: commands run once in a task's clone, in the agent's container, while
the plan is made; the writer's first turn waits for them. A build of the whole project without
its tests, say, so the writer builds one module at a time.

How it ended is in the task's handoff, which outlives the container: a stop and a start do not run
it again. A stop that cut it short leaves no ending, and the next start runs it again."""

from __future__ import annotations

from typing import Protocol

from .config import Project
from .task import Task

LOG = "prepare.log"
EXIT = "prepare.exit"
# Where the agent's container has the task's handoff.
HANDOFF = "/task/handoff"


class Preparing(Protocol):
    def prepare_start(self, commands: list[str], log: str, exit_file: str) -> None: ...

    def prepare_running(self) -> bool: ...


def ending(task: Task) -> int | None:
    """The exit code the commands ended with; None while they have not ended."""
    try:
        return int((task.meta / "handoff" / EXIT).read_text().strip())
    except (OSError, ValueError):
        return None


def status(task: Task, pod: Preparing) -> str:
    """ "done", "failed", "running", or "" when it never ran or a stop cut it short."""
    code = ending(task)
    if code is not None:
        return "done" if code == 0 else "failed"
    return "running" if pod.prepare_running() else ""


def begin(task: Task, project: Project, pod: Preparing) -> bool:
    """Starts the preparation unless it ended or runs already. True when it started now."""
    if not project.prepare or status(task, pod):
        return False
    pod.prepare_start(project.prepare, f"{HANDOFF}/{LOG}", f"{HANDOFF}/{EXIT}")
    task.event("prepare_started", commands=project.prepare)
    return True


def waiting(task: Task, project: Project, pod: Preparing) -> bool:
    """True while the writer has to wait. How a run ended goes to the timeline once."""
    if not project.prepare:
        return False
    if status(task, pod) == "running":
        return True
    said = [e["type"] for e in task.events() if e["type"] in ("prepare_started", "prepared")]
    if (code := ending(task)) is not None and said and said[-1] == "prepare_started":
        task.event("prepared", ok=code == 0, code=code)
    return False
