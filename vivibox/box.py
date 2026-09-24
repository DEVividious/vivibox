"""The box: the project's pod without a task or an agent, for you to work in by hand. Reached
through actions, like everything the view and the command line call.
"""

from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from . import (
    actions,
    gate,
    image,
    keys,
    opencode,
    providers,
    repo,
    secrets,
    toolchain,
)
from .config import (
    ConfigError,
    Project,
    load_config,
    load_project,
)
from .pod import PodError
from .risky import Approvals
from .states import State
from .task import Task, create_task

BOX_GOAL = "Box"
BOX_COMMIT = "Work in the box"


def projects_named() -> list[str]:
    return [p.stem for p in actions.project_files()]


def open_box(project_name: str) -> Task:
    """A box for the project: its clone in a pod, your tools and keys in it, and no agent. You
    work in it by hand, opencode or claude included, where a mode without permission prompts is
    safe and Docker works. The box is a task without a plan, so the pod, the review and the
    accepting are the task's own."""
    config = load_config()
    project = load_project(project_name)
    if not (project.repo / ".git").exists():
        raise ConfigError(f"{project.repo} is not a git repository")
    plan = files("vivibox").joinpath("templates/plan.md").read_text().replace("{{kind}}", "other")
    task = create_task(config.tasks_dir, project.name, BOX_GOAL, plan, after=actions.used_numbers(project))
    try:
        base = repo.prepare(project.repo, task.repo, task.id, task.meta)
    except BaseException:
        shutil.rmtree(task.root, ignore_errors=True)
        raise
    task.set_base_commit(base)
    task.set_box()
    task.transition(State.CHECKPOINT_PLAN, reason="a box has no plan")
    task.transition(State.IMPLEMENT, reason="box opened")
    Approvals(task.meta, task.repo, project.risky_extra).approve()
    start_box(task.id)
    return task


def box_providers(task: Task) -> list[str]:
    """Every provider you have a key for and have on: in a box you pick the model yourself."""
    return [
        name
        for name in keys.list_keys()
        if providers.enabled(providers.PROVIDER, name) and providers.is_provider_key(name)
    ]


def start_box(task_id: str) -> None:
    """The pod up, with your keys and opencode's configuration in it, as for a task's agent."""
    config = load_config()
    task, project = actions.load(task_id)
    pod = actions.task_pod(task.id)
    if missing := pod.missing_env():
        raise PodError(f"{', '.join(missing)} not set: the {project.name} project passes them (pass_env)")
    if not image.exists(image.image_ref()):
        raise PodError("the agent image is not built; run 'vivibox image build'")
    used = box_providers(task)
    secrets.prepare(task.id, used + providers.mcp_secrets())
    model = actions.role_of(task, "writer", config).model
    opencode.prepare(task, model, project.verify, used)
    pod.up()
    toolchain.ensure(pod, project.java)
    if task.read_state().paused:
        task.set_paused(False)
    task.event("started", model="you")


def box_shell_command(task_id: str) -> list[str]:
    """A shell in the box, in the clone, through tmux like the agent's window: Ctrl-q leaves,
    the box stays."""
    task, _ = actions.load(task_id)
    if task.read_state().paused:
        raise PodError(f"{task_id} is stopped; start it again with: vivibox start {task_id}")
    pod = actions.task_pod(task_id)
    actions.agent_view(task, ["docker", "exec", "-it", "-w", str(task.repo), pod.agent, "bash", "-l"])
    return [*actions.TMUX, "attach-session", "-t", actions.tmux_session(task_id)]


def commit_in_box(task: Task) -> None:
    """What you left uncommitted is committed, in the pod, as you: accepting flattens the work
    to uncommitted changes in your checkout anyway, so the message is never seen."""
    pod = actions.task_pod(task.id)
    pod.exec("git", "add", "-A")
    if pod.exec("git", "diff", "--cached", "--quiet", check=False).returncode != 0:
        pod.exec("git", "commit", "-q", "-m", BOX_COMMIT)


def close_box(task: Task, project: Project) -> Path | None:
    """The box's work goes to review, the way a task's does: committed, fetched, and held for
    your approval when it touched risky files. Returns the review copy, or None while risky
    changes wait. Raises when nothing changed."""
    st = task.read_state()
    if not st.box or st.state is not State.IMPLEMENT:
        raise gate.GateError(f"{task.id} is not an open box")
    commit_in_box(task)
    head = repo.git("rev-parse", "HEAD", cwd=task.repo).stdout.strip()
    if head == st.base_commit:
        raise gate.GateError(f"nothing changed in {task.id}; there is nothing to bring back")
    task.transition(State.VERIFY, reason="box closed")
    if Approvals(task.meta, task.repo, project.risky_extra).changes():
        task.transition(State.APPROVAL_RISKY, reason="box closed", then=str(State.CHECKPOINT_FINAL))
        return None
    task.transition(State.CHECKPOINT_FINAL, reason="box closed")
    return actions.prepare_review(task, project)
