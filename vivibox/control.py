"""The planner's control container, as a task gives it to its pod: what it mounts, its own keys
and server password, and its own harness files.

Where a flow has a planner other than the writer (orchestration.has_control), the planner, which
in supervisor_worker also reads the work, runs in a container of its own for the whole task,
in a network of its own (podspec's recipe for it). Its conversation is kept in a home that only
that container mounts, its harness server listens in that container's network namespace with a
password of its own, and its keys are in a directory of its own: nothing the writer runs has a
path to any of them, so what the planner reads as its own history is what it wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import brief, claudecode, comments, opencode, providers, repo, reviewing, roles, secrets
from .config import Config
from .pod import Mount, Pod
from .task import Task

# The planner's harness files in the task's folder, written by the host and mounted read-only
# where the harness reads them (opencode.HARNESS_MOUNT), over the writer's.
HARNESS = "harness-control"
ENV = {"OPENCODE_CONFIG": opencode.CONFIG, "CLAUDE_CONFIG_DIR": claudecode.CONFIG_DIR}
PLANNERS = ("planner",)


def secrets_id(task_id: str) -> str:
    """The key store's runtime directory of the control container: the task's, apart."""
    return f"{task_id}-control"


def forget(task_id: str) -> None:
    """The planner's key and its server's password are gone from the machine with the task's."""
    secrets.remove(secrets_id(task_id))


def harness_dir(task: Task) -> Path:
    return task.meta / HARNESS


def mounts(task: Task) -> list[Mount]:
    """What the control container mounts of the task's, besides its copy of the work and its home
    (the pod's own): the task's files to read, the handoff to write its draft and its question,
    your replies read-only over it, the folder its review goes to, and its own harness files and
    secrets. Mount sources are made here: missing, Docker would make them folders of root's."""
    runtime = secrets.runtime_dir(secrets_id(task.id))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    harness_dir(task).mkdir(exist_ok=True)
    (task.meta / "review").mkdir(exist_ok=True)
    comments.ensure(task.meta)
    return [
        Mount(str(task.meta), "/task", read_only=True),
        Mount(str(task.meta / "handoff"), "/task/handoff"),
        comments.mount(task.meta),  # your replies, over the folder the agents write
        Mount(str(harness_dir(task)), opencode.HARNESS_MOUNT, read_only=True),
        Mount(str(task.meta / "review"), reviewing.MOUNT),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]


def prepare(task: Task, config: Config, verify: list[str] | tuple = ()) -> bool:
    """The control container's keys and harness files, at every start: exactly what the planner's
    harness reads (its provider's key and its MCP servers' secrets on opencode, Anthropic's key
    on Claude Code, none for a planner that is you), and its own server password. True when the
    harness files changed, which a running server has to be started again to read."""
    role = roles.role_of(task, "planner", config)
    needed = roles.provider_keys(config, task, PLANNERS)  # refuses a planner without a model
    settings = opencode.config(role.model, (), task.repo) if role.harness == opencode.NAME else None
    secrets.prepare(secrets_id(task.id), providers.mounted(settings) if settings else needed)
    d = harness_dir(task)
    d.mkdir(exist_ok=True)
    before = {p.name: p.read_text() for p in d.iterdir()}
    (d / "instructions.md").write_text(brief.common(task.id, task.repo, repo.branch_name(task.id), verify))
    if settings:
        (d / "opencode.json").write_text(json.dumps(settings, indent=2) + "\n")
    else:
        (d / "opencode.json").unlink(missing_ok=True)
    return before != {p.name: p.read_text() for p in d.iterdir()}


def refresh(task: Task, pod: Pod) -> None:
    """The planner's copy of the committed work, as the task's branch is now: made by git on the
    host, in a folder of the task's disk that only the control container mounts, read-only. The
    writer's clone is read as every git of the host's reads it, once it is as recorded
    (repo.guard) and within a time limit, and only its commits are taken: its .git, where the
    writer can leave what git follows to a command, is never what the planner's git works in.
    Before the planner's first turn and before each one after it, so a review reads the commits
    the verification passed."""
    src, branch = pod.control_src, repo.branch_name(task.id)
    if not src.is_dir() or src.is_symlink():
        raise repo.RepoError(f"the planner's copy of the work has no folder at {src}; start the task again")
    if not (src / ".git").exists():
        repo.git("init", "--quiet", "--initial-branch", branch, str(src))
    try:
        repo.in_clone(
            task.repo, "fetch", "--quiet", "--no-tags", str(task.repo), f"refs/heads/{branch}",
            cwd=src, timeout=repo.COPY_TIMEOUT,
        )  # fmt: skip
    except repo.GitTimeout as e:
        raise repo.GitTimeout(
            f"the planner's copy of the work could not be brought up to date: {e}"
        ) from None
    repo.git("reset", "--quiet", "--hard", "FETCH_HEAD", cwd=src)


class Reader:
    """The planner's harness in its control container, with its copy of the work brought up to
    date before each of its turns; everything else is the harness's own."""

    def __init__(self, harness, bring_up_to_date):
        self.harness, self.bring_up_to_date = harness, bring_up_to_date

    def __getattr__(self, name):
        return getattr(self.harness, name)

    def turn(self, prompt: str, session: str = "", title: str = "", on_step=None):
        self.bring_up_to_date()
        return self.harness.turn(prompt, session=session, title=title, on_step=on_step)


def serve(task: Task, pod: Pod, config: Config, changed: bool) -> None:
    """The planner's own harness server in its container, where its harness has one: started
    again when its files changed (it reads its configuration only at its start)."""
    if roles.role_of(task, "planner", config).harness != opencode.NAME:
        return
    harness = opencode.OpenCode(pod.control_side())
    if changed:
        harness.restart_server()
    else:
        harness.ensure_server()
