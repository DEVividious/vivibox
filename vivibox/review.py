"""The work coming back to you: the review copy, accepting it into your checkout, and the
history and archive a finished task leaves. Reached through actions, like everything the view and
the command line call.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    actions,
    gate,
    ide,
    repo,
    ui,
)
from .config import (
    Config,
    ConfigError,
    Project,
)
from .plan import PlanError, parse_plan
from .risky import Approvals
from .states import State
from .task import Task, now


def fetch_work(task: Task, project: Project) -> str:
    # Your IDE runs build files and IDE settings on import: unapproved changes to them stay in the clone.
    if Approvals(task.meta, task.repo, project.risky_extra).changes():
        raise gate.GateError(f"risky files changed and are not approved yet; see 'vivibox risky {task.id}'")
    return repo.fetch_to(project.repo, task.repo, task.meta, task.id)


def prepare_review(task: Task, project: Project) -> Path:
    """The review copy: the agent's work as uncommitted changes, next to your untouched checkout."""
    commit = fetch_work(task, project)
    path = repo.update_review_worktree(project.repo, task.root, commit, task.read_state().base_commit)
    task.event("review", commit=commit, path=str(path))
    return path


def review_copy(task: Task, project: Project) -> Path:
    """The review copy, prepared if it does not exist yet."""
    path = repo.review_worktree_path(project.repo, task.root)
    return path if path.exists() else prepare_review(task, project)


def editor_command(config: Config, project: Project | None = None) -> str:
    """The editor to open a review copy with: the project's, else the one in config.toml."""
    return (project.ide if project and project.ide else "") or config.ide


def open_in_ide(config: Config, path: Path, project: Project | None = None) -> None:
    command = editor_command(config, project)
    if not command:
        raise ConfigError('no editor chosen yet; set [review] ide in config.toml, e.g. ide = "code {path}"')
    ide.open_folder(path, command)


def changed_files(task: Task, project: Project) -> str:
    """The agent's work as 'git diff --stat', from the review copy's commits in your repository."""
    base = task.read_state().base_commit
    ref = repo.review_ref(task.id)
    if repo.git("rev-parse", "--verify", "--quiet", ref, cwd=project.repo, check=False).returncode != 0:
        return ""
    return repo.git("diff", "--stat", f"{base}...{ref}", cwd=project.repo).stdout


def history_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "vivibox" / "history.jsonl"


def history(limit: int = 20) -> list[dict]:
    """Tasks you accepted, newest first. The task itself is gone; this is what it left behind."""
    path = history_path()
    if not path.exists():
        return []
    done = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return list(reversed(done))[:limit]


def forget(task_id: str) -> None:
    """A finished task's line in the history, and its archive, go."""
    shutil.rmtree(archive_path(task_id), ignore_errors=True)
    path = history_path()
    if not path.exists():
        return
    kept = [
        line for line in path.read_text().splitlines() if line.strip() and json.loads(line)["id"] != task_id
    ]
    path.write_text("".join(line + "\n" for line in kept))


def archive_path(task_id: str) -> Path:
    return history_path().with_name("archive") / task_id


# What is worth keeping of a task that is gone: the plan it was held to, what happened to it and
# what it delivered, a few dozen kilobytes; not its clone, not its logs.
ARCHIVED = (
    "plan.md",
    gate.ACCEPTED_PLAN,
    "events.jsonl",
    "handoff/" + gate.CRITERIA_FILE,
    "handoff/" + actions.DEMO_FILE,
)


def archive(task: Task) -> Path:
    """Keeps the task's record before its directory goes, so a finished task can still show its
    plan, and what it cost can still be traced to the gate runs that cost it."""
    kept = archive_path(task.id)
    kept.mkdir(parents=True, exist_ok=True)
    for name in ARCHIVED:
        source = task.meta / name
        if source.is_file():
            shutil.copy2(source, kept / source.name)
    return kept


def accepted_criteria(task: Task) -> list[str]:
    """What the task set out to deliver. Read before its directory goes, or nothing is left of it."""
    try:
        return [c.text for c in parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria]
    except (OSError, PlanError):
        return []


def remember_removed(task: Task, project: Project) -> None:
    """A task you removed, kept in the list's history like one you accepted: what it was for, what
    it cost and how far it got. Its files and its work are gone."""
    st = task.read_state()
    spent = ui.cost(task)
    entry = {
        "id": task.id,
        "project": project.name,
        "title": st.goal,
        "cost": round(spent.total, 4),
        # A box has no planning to split off, and the list shows one number for an entry without it.
        **({"planning": round(spent.planning, 4)} if spent.split else {}),
        "created": st.created,
        "finished": now(),
        "deleted": str(st.state),
    }
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def remember(done: Finished, project: Project, commit: str) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": done.task_id,
        "project": project.name,
        "title": done.message.partition("\n")[0] or f"Box in {project.name}",
        "cost": round(done.cost.total, 4),
        **({"planning": round(done.cost.planning, 4)} if done.cost.split else {}),
        "commit": commit[:10],
        "branch": done.branch,
        "conflicts": done.conflicts,
        "criteria": done.criteria,
        "created": done.created,
        "demo": done.demo,
        "finished": now(),
    }
    with path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class Finished:
    task_id: str
    source: Path
    cost: ui.Spend
    message: str
    branch: str = ""
    conflicts: list[str] = field(default_factory=list)
    status: str = ""
    # The criteria the plan was accepted with; the gate passed, so the agent reported all of them met.
    criteria: list[str] = field(default_factory=list)
    # When you asked for the task, not when it finished; the list shows both.
    created: str = ""
    # How this task's project was run, kept as a record so the next task need not work it out again.
    demo: str = ""


def finish(task: Task, project: Project, branch_only: bool = False) -> Finished:
    """Accepted work lands in your checkout as uncommitted changes, or with branch_only as a branch.
    Everything else of the task goes. Committing is a separate step: commit_work."""
    copy = repo.review_worktree_path(project.repo, task.root)
    if copy.exists() and (changed := repo.local_changes(copy)):
        raise gate.GateError(
            f"{copy} has changes you made ({', '.join(changed[:3])}); ask the agent with a reply "
            "or discard them"
        )
    commit = fetch_work(task, project)
    st = task.read_state()
    spent = ui.cost(task)
    criteria = accepted_criteria(task)
    # A box's "Work in the box" is not a message for your history: that one is yours to write.
    message = "" if st.box else suggested_message(project.repo, st.base_commit, commit, st.goal, criteria)
    done = Finished(task.id, project.repo, spent, message)
    done.criteria = criteria
    done.created = st.created
    done.demo = actions.demo_instruction(task)
    if branch_only:
        done.branch = repo.create_branch(project.repo, task.id, commit)
    else:
        done.conflicts = apply_work(project.repo, task.id, commit)
        if done.conflicts:
            done.branch = repo.branch_name(task.id)
        else:
            done.status = repo.git("status", "--short", "--untracked-files=no", cwd=project.repo).stdout
    task.transition(State.DONE, reason="accepted")
    remember(done, project, commit)
    actions.remove(task, project, accepted=True)
    return done


def suggested_message(source: Path, base: str, commit: str, goal: str, criteria: list[str] = ()) -> str:
    """A commit message the way a history is kept: a subject of at most 72 characters saying what
    the task set out to do (the plan's summary, once there is one), then a list: the agent's commit
    subjects when it made several (the gate checked them), else the criteria the work met. A line
    that reads like a signature is left out."""
    log = repo.git("log", "--reverse", "--format=%s", f"{base}..{commit}", cwd=source).stdout
    subjects = log.splitlines()
    first = goal.strip().splitlines()[0].strip().rstrip(".") if goal.strip() else ""
    subject = ui.shorten(first, gate.MAX_SUBJECT)
    points = subjects if len(subjects) > 1 else list(criteria)
    points = [p.strip() for p in points if p.strip() and not gate.AI_MARKERS.search(p)]
    if not points:
        return subject
    return subject + "\n\n" + "\n".join(f"- {p}" for p in points)


def apply_work(source: Path, task_id: str, commit: str) -> list[str]:
    """Stages the task's work in your checkout, on top of whatever your branch is at. Returns the files
    that conflict; those are left for you to resolve, with the work kept on a branch as well."""
    if repo.git("diff", "--cached", "--quiet", cwd=source, check=False).returncode != 0:
        raise gate.GateError(f"{source} has staged changes; commit or unstage them first, or use --branch")
    p = repo.git("merge", "--squash", "--no-commit", commit, cwd=source, check=False)
    if p.returncode == 0:
        return []
    conflicts = repo.git("diff", "--name-only", "--diff-filter=U", cwd=source).stdout.split()
    if not conflicts:  # refused before touching anything, e.g. an untracked file in the way
        raise gate.GateError(f"cannot apply the work to {source}: {(p.stderr or p.stdout).strip()[:300]}")
    repo.create_branch(source, task_id, commit)
    return conflicts


def current_branch(source: Path) -> str:
    return repo.git("branch", "--show-current", cwd=source).stdout.strip() or "(detached)"


def commit_work(source: Path, message: str) -> str:
    """Commits what is staged in your checkout, with your identity and your hooks. Returns 'hash subject'."""
    if not message.strip():
        raise gate.GateError("the commit message is empty")
    p = repo.git("commit", "--quiet", "-m", message.strip(), cwd=source, check=False)
    if p.returncode != 0:
        raise gate.GateError(
            f"the commit failed; the changes stay staged: {(p.stderr or p.stdout).strip()[:300]}"
        )
    return repo.git("log", "-1", "--format=%h %s", cwd=source).stdout.strip()
