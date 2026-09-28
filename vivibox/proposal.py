"""The verification command a writer proposes, for a project that has none: it runs the build while
it works, so it knows what builds and tests the project; a guess from the build files does not.
The gate verifies that task with it, and once you have accepted the work you decide, on its own,
whether the project keeps it."""

from __future__ import annotations

from . import repo
from .config import Project
from .gate import ACCEPTED_PLAN
from .gate import narrowed_proposal as narrowed_proposal_in
from .plan import Plan, PlanError, parse_plan
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


def planned_modules(task: Task, project: Project) -> list[str]:
    """The modules the accepted plan names, in a project verified by its modules; [] otherwise."""
    if not project.verify_scoped:
        return []
    try:
        return parse_plan((task.meta / ACCEPTED_PLAN).read_text()).modules
    except (OSError, PlanError):
        return []


def modules_missing(plan: Plan, verify_scoped: str) -> str:
    """Why a draft in a project verified by its modules is not ready: it names none, and the task
    would be verified with the whole build. "" when it names some, or there is nothing to build."""
    if not verify_scoped or plan.modules or plan.no_build:
        return ""
    return 'modules in the header is empty; name the directories this task changes, e.g. ["core"]'


def outside_modules(task: Task, project: Project) -> list[str]:
    """The files the task's commits change outside the modules its plan names: no one builds those
    with the modules' command, so the task is verified with the whole build."""
    if not (modules := planned_modules(task, project)):
        return []
    base = task.read_state().base_commit
    changed = repo.git("diff", "--name-only", f"{base}..HEAD", cwd=task.repo).stdout.split()
    inside = tuple(f"{m.rstrip('/')}/" for m in modules)
    return [path for path in changed if not path.startswith(inside)]


def asked(task: Task, project: Project) -> bool:
    """Whether this task's writer is to propose the command: the project has none, and neither
    it nor the task says there is nothing to build."""
    return not project.verify and not project.no_build and not nothing_to_build(task)


def verify_commands(task: Task, project: Project) -> list[str]:
    """The project's commands, or the one for the modules the task's plan names while its work stays
    inside them; for a project with none, the one this task's writer proposed. None
    for a task made with nothing to build, whatever the project builds for its other tasks."""
    if nothing_to_build(task):
        return []
    if (modules := planned_modules(task, project)) and not outside_modules(task, project):
        return [project.verify_scoped.replace("{modules}", ",".join(modules))]
    if project.verify or not asked(task, project):
        return project.verify
    if not (found := proposed(task)) or narrowed_proposal_in(found):
        return []
    return [found]


def narrowed_proposal(task: Task, project: Project) -> str:
    """The selection of tests in the command the writer proposed, for the gate to refuse; "" for
    a whole build, for no proposal, and for a project that has its own command, its choice."""
    if project.verify or not asked(task, project):
        return ""
    return narrowed_proposal_in(proposed(task))


def missing_command(task: Task, project: Project) -> str:
    """Why this task has no command to be verified with when it should: its writer proposed none.
    "" when it has one, or has nothing to build."""
    if verify_commands(task, project) or not asked(task, project) or narrowed_proposal(task, project):
        return ""
    return (
        f"the writer proposed no command in /task/handoff/{PROPOSAL} and {project.name} has "
        "none; set one under e on the project"
    )
