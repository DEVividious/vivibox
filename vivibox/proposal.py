"""The verification command a writer proposes, for a project that has none: it runs the build while
it works, so it knows what builds and tests the project; a guess from the build files does not.
The gate verifies that task with it, and once you have accepted the work you decide, on its own,
whether the project keeps it."""

from __future__ import annotations

from .config import Project
from .gate import ACCEPTED_PLAN
from .plan import PlanError, parse_plan
from .task import Task

PROPOSAL = "verify-proposal.md"


def proposed(task: Task) -> str:
    """The command the writer wrote, its first line, without the backticks or the prompt sign a
    model tends to put around it; "" for none."""
    try:
        text = (task.meta / "handoff" / PROPOSAL).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        command = line.strip().strip("`").strip()
        if command.startswith("$ "):
            command = command[2:].strip()
        if command:
            return command
    return ""


def nothing_to_build(task: Task) -> bool:
    """Whether the task was made with nothing to build (verify = false in its accepted plan)."""
    try:
        return parse_plan((task.meta / ACCEPTED_PLAN).read_text()).no_build
    except (OSError, PlanError):
        return False


def asked(task: Task, project: Project) -> bool:
    """Whether this task's writer is to propose the command: the project has none, and neither
    it nor the task says there is nothing to build."""
    return not project.verify and not project.no_build and not nothing_to_build(task)
