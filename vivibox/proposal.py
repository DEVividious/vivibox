"""The verification command a writer proposes, for a project that has none: it runs the build while
it works, so it knows what builds and tests the project; a guess from the build files does not.
The gate verifies that task with it, and once you have accepted the work you decide, on its own,
whether the project keeps it."""

from __future__ import annotations

import re

from . import repo
from .config import Project
from .gate import ACCEPTED_PLAN
from .gate import narrowed_proposal as narrowed_proposal_in
from .plan import Plan, PlanError, parse_plan
from .task import Task

PROPOSAL = "verify-proposal.md"


# A fence's opening line with its language (```bash), and a command in backticks within a line.
FENCE = re.compile(r"^(`{3,}|~{3,})\s*[\w+-]*$")
INLINE = re.compile(r"`([^`]+)`")


def proposed(task: Task) -> str:
    """The command the writer wrote, its first line, without what a model puts around it in a
    Markdown file: a heading, a fenced block and its language, a comment in it, a list marker, a
    sentence around a command in backticks, the prompt sign; "" for none."""
    try:
        text = (task.meta / "handoff" / PROPOSAL).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        command = line.strip()
        if not command or command.startswith("#") or FENCE.match(command):
            continue
        if found := INLINE.search(command):
            command = found.group(1).strip()
        command = command.removeprefix("- ").removeprefix("* ").strip("`").strip()
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


# What chooses the modules in a command, besides the {modules} token itself: its flag, when the
# token stands on its own after it, and Maven's "and what they need", which means nothing without it.
CHOOSES = re.compile(r"\s+(?:(?:-pl|--projects)\s+)?\S*\{modules\}\S*|\s+(?:-am|--also-make)(?=\s|$)")


def whole(command: str) -> str:
    """The command with nothing chosen: the whole build, for work that reaches past the modules."""
    return CHOOSES.sub("", command) if "{modules}" in command else command


def for_modules(commands: list[str], modules: list[str]) -> list[str]:
    return [c.replace("{modules}", ",".join(modules)) for c in commands]


def planned_modules(task: Task, project: Project) -> list[str]:
    """The modules the accepted plan names, in a project verified by its modules; [] otherwise."""
    if not project.by_module:
        return []
    try:
        return parse_plan((task.meta / ACCEPTED_PLAN).read_text()).modules
    except (OSError, PlanError):
        return []


def modules_missing(plan: Plan, by_module: bool) -> str:
    """Why a draft in a project verified by its modules is not ready: it names none, and the task
    would be verified with the whole build. "" when it names some, or there is nothing to build."""
    if not by_module or plan.modules or plan.no_build:
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
        return for_modules(project.verify, modules)
    if project.by_module:
        return [whole(c) for c in project.verify]
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
