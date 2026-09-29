"""The verification command a writer proposes, for a project that has none: it runs the build while
it works, so it knows what builds and tests the project; a guess from the build files does not.
The gate verifies that task with it, and once you have accepted the work you decide, on its own,
whether the project keeps it."""

from __future__ import annotations

import re
from pathlib import Path

from . import repo
from .config import MODULES_TOKEN, Project, by_modules
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


# The whole build of a command that names each module its own way: Gradle runs a task without a
# project path in every project, npm and pnpm run it in every workspace.
EVERY_MODULE = (("%p:", ""), ("--workspace=%s", "--workspaces"), ("--filter=%s", "-r"))


def _every(form: str) -> str:
    for part, every in EVERY_MODULE:
        if part in form:
            return form.replace(part, every)
    return ""


def whole(command: str) -> str:
    """The command with nothing chosen: the whole build, for work that reaches past the modules."""
    if not by_modules(command):
        return command
    command = MODULES_TOKEN.sub(lambda m: _every(m.group(1)) if m.group(1) else "{modules}", command)
    return re.sub(r"\s{2,}", " ", CHOOSES.sub("", command)).strip()


def _each(form: str, module: str) -> str:
    return form.replace("%p", ":" + module.replace("/", ":")).replace("%s", module)


def for_modules(commands: list[str], modules: list[str]) -> list[str]:
    def written(m: re.Match) -> str:
        if not m.group(1):
            return ",".join(modules)
        return " ".join(_each(m.group(1), module) for module in modules)

    return [MODULES_TOKEN.sub(written, c) for c in commands]


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


# What makes a folder a module of the build: Maven, Gradle, npm or pnpm workspaces.
BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json")


def module_of(root: Path, path: str) -> str | None:
    """The module a file of the repository belongs to: the nearest folder above it with a build
    file of its own. None for a file at the root, or in no module, which every module builds on."""
    parts = Path(path).parts[:-1]
    for depth in range(len(parts), 0, -1):
        folder = root.joinpath(*parts[:depth])
        if any((folder / name).is_file() for name in BUILD_FILES):
            return "/".join(parts[:depth])
    return None


def _changed(task: Task) -> list[str]:
    base = task.read_state().base_commit
    return repo.git("diff", "--name-only", f"{base}..HEAD", cwd=task.repo).stdout.split()


def _placed(task: Task, project: Project) -> tuple[list[str], list[str], list[str]]:
    """The planned modules, the modules the work reached beyond them, and the files in no module."""
    if not project.by_module:
        return [], [], []
    planned = planned_modules(task, project)
    inside = tuple(f"{m.rstrip('/')}/" for m in planned)
    added: list[str] = []
    outside: list[str] = []
    for path in _changed(task):
        module = module_of(task.repo, path)
        if module is not None:
            if module not in planned and module not in added:
                added.append(module)
        elif not path.startswith(inside):
            outside.append(path)
    return planned, added, outside


def _under(task: Task, chosen: list[str]) -> list[str]:
    """The build's modules nested under the chosen ones: moshi-adapters/japicmp, the API check of
    moshi-adapters, is a project of its own that a build of moshi-adapters never runs."""
    from . import init  # init reads the build files, which this module has no other need of

    inside = tuple(f"{m.rstrip('/')}/" for m in chosen)
    return [m for m in init.modules(task.repo) if m.startswith(inside) and m not in chosen]


def scope(task: Task, project: Project) -> tuple[list[str], list[str]]:
    """The modules a verification builds: the plan's, the ones the work reached, and the modules
    nested under them (ADR-0033, change of 2026-09-29); and which came from the changes. Nothing
    when the work reached past every module, and the whole project is built."""
    planned, added, outside = _placed(task, project)
    if outside or not (planned or added):
        return [], []
    chosen = planned + added
    return chosen + _under(task, chosen), added


def nested(task: Task, project: Project) -> list[str]:
    """The modules in scope only because they are nested under one that is."""
    modules, added = scope(task, project)
    planned, _, _ = _placed(task, project)
    return [m for m in modules if m not in planned and m not in added]


def outside_modules(task: Task, project: Project) -> list[str]:
    """The files the task's commits change in no module, such as the root's build file: every
    module builds on those, so the task is verified with the whole build."""
    return _placed(task, project)[2]


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
    if modules := scope(task, project)[0]:
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
