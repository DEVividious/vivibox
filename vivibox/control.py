"""The planner's control container, as a task gives it to its pod: what it mounts, its own keys
and server password, and its own harness files.

Where a flow has a planner other than the writer (orchestration.has_control), the planner, which
in supervisor_worker also reads the work, runs in a container of its own for the whole task,
in a network of its own (podspec's recipe for it). Its conversation is kept in a home that only
that container mounts, its harness server listens in that container's network namespace with a
password of its own, and its keys are in a directory of its own: nothing the writer runs has a
path to any of them, so what the planner reads as its own history is what it wrote. What the
writer does write reaches it as data only: the commits, in a copy the host makes (refresh),
which is never its harness's working directory (podspec.READER_WORK), and the handoff.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import brief, claudecode, comments, opencode, providers, reading, repo, reviewing, roles, secrets
from .config import DEFAULT_HISTORY_DEPTH, Config
from .pod import Mount, Pod
from .states import State
from .task import Task

# The planner's harness files in the task's folder, written by the host and mounted read-only
# where the harness reads them (opencode.HARNESS_MOUNT), over the writer's.
HARNESS = "harness-control"
ENV = {"OPENCODE_CONFIG": opencode.CONFIG, "CLAUDE_CONFIG_DIR": claudecode.CONFIG_DIR}


def env(task: Task) -> dict[str, str]:
    """The control container's environment. git there, wherever it is run, works on the copy of
    the commits: the planner's harness stands in a directory of the host's (podspec.READER_WORK),
    and its brief says git answers for the project from any directory (the review's turn asks
    for `git diff <base>..HEAD` and names no place to run it). The copy's
    .git is the host's and read-only there, so its hooks and configuration are the host's; what
    a harness loads as a project's configuration follows its working directory, not git (seen
    in the image: opencode then names the copy as its project's worktree, and still loads
    nothing from it). And no language server is downloaded (opencode.READER_ENV)."""
    return {**ENV, **reading.git_env(task.repo), **opencode.READER_ENV}


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
    # A reader's configuration: no language server, no Serena of vivibox's (opencode.READER).
    reads = role.harness == opencode.NAME
    settings = opencode.config(role.model, (), task.repo, reader=True) if reads else None
    secrets.prepare(secrets_id(task.id), providers.mounted(settings) if settings else needed)
    d = harness_dir(task)
    d.mkdir(exist_ok=True)
    before = {p.name: p.read_text() for p in d.iterdir()}
    # The reader's brief: the project as a read-only view at the clone's path, what this
    # container has not; the writer's brief would describe the writer's container.
    branch = repo.branch_name(task.id)
    told = brief.reader(task.id, task.repo, branch, verify, history_depth=config.history_depth)
    (d / "instructions.md").write_text(told)
    if settings:
        (d / "opencode.json").write_text(json.dumps(settings, indent=2) + "\n")
    else:
        (d / "opencode.json").unlink(missing_ok=True)
    return before != {p.name: p.read_text() for p in d.iterdir()}


HOST_GIT = reading.HOST_GIT
PLANNERS_OWN = "the planner's"


def workdir(pod: Pod) -> None:
    """The working directory of the planner's harness (reading.workdir), on the task's disk."""
    reading.workdir(pod.control_work, PLANNERS_OWN)


def reviewed_commit(task: Task) -> str:
    """The commit the planner reads: the one the verification passed while the task is in its
    review, so the supervisor reads what was verified whatever the branch points at since; ""
    else (planning), for the branch as it is."""
    st = task.read_state()
    return st.verified_commit if st.state is State.REVIEW else ""


def refresh(task: Task, pod: Pod, commit: str = "", depth: int = DEFAULT_HISTORY_DEPTH) -> None:
    """The planner's copy of the committed work (reading.view), in a folder of the task's disk
    that only the control container mounts, read-only: on commit when one is given (the verified
    one, for a review), else on the task's branch as it is; made with depth commits of the
    branch (history_depth). Before the planner's first turn and before each one after it; the
    harness's working directory (workdir) is made with it."""
    workdir(pod)
    reading.view(task.repo, repo.branch_name(task.id), pod.control_src, commit, PLANNERS_OWN, depth)


class Reader:
    """The planner's harness in its control container, with its copy of the work brought up to
    date before each of its turns, and what it asks of the history at the end of one answered
    (answer_history: True when the supervisor is to give it a turn to read the answer, as
    history.answer has it); everything else is the harness's own."""

    def __init__(self, harness, bring_up_to_date, answer_history=lambda: False):
        self.harness, self.bring_up_to_date, self.answer_history = harness, bring_up_to_date, answer_history

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
