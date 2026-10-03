"""The planner's control container, as a task gives it to its pod: what it mounts, its own keys
and server password, and its own harness files.

Where a flow has a planner other than the writer (orchestration.has_control), the planner, which
in supervisor_worker also reads the work, runs in a container of its own for the whole task,
in a network of its own (podspec's recipe for it). Its conversation is kept in a home that only
that container mounts, its harness server listens in that container's network namespace with a
password of its own, and its keys are in a directory of its own: nothing the writer runs has a
path to any of them, so what the planner reads as its own history is what it wrote. What the
writer does write reaches it as data only: the commits, in a copy the host makes (refresh),
which is never its harness's working directory (podspec.CONTROL_WORK), and the handoff.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import brief, claudecode, comments, opencode, providers, repo, reviewing, roles, secrets
from .config import Config
from .pod import Mount, Pod
from .states import State
from .task import Task

# The planner's harness files in the task's folder, written by the host and mounted read-only
# where the harness reads them (opencode.HARNESS_MOUNT), over the writer's.
HARNESS = "harness-control"
ENV = {"OPENCODE_CONFIG": opencode.CONFIG, "CLAUDE_CONFIG_DIR": claudecode.CONFIG_DIR}
PLANNERS = ("planner",)
HEX = re.compile(r"[0-9a-f]{40,64}")


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


# git on the host, in the planner's copy, reads no configuration but the copy's own, which is
# the host's making: your global and system configuration (a filter driver, git-lfs, a submodule
# setting) would otherwise run your programs on what the writer committed.
HOST_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def workdir(pod: Pod) -> None:
    """The working directory of the planner's harness, made once: a git repository of the
    host's with one empty commit and nothing else, no hook and no file. Mounted read-only in the
    control container, it is where the harness looks for a project's configuration and finds
    none; a repository with a commit, so opencode knows the directory as one project from turn
    to turn without writing there."""
    work = pod.control_work
    if not work.is_dir() or work.is_symlink():
        raise repo.RepoError(f"the planner's working directory has no folder at {work}; start the task again")
    if (work / ".git").exists():
        return
    repo.git("init", "--quiet", "--template=", "--initial-branch", "main", str(work), env=HOST_GIT)
    who = ("-c", "user.name=vivibox", "-c", "user.email=vivibox@localhost")
    repo.git(
        *who,
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "The planner's working directory",
        cwd=work,
        env=HOST_GIT,
    )


def reviewed_commit(task: Task) -> str:
    """The commit the planner reads: the one the verification passed while the task is in its
    review, so the supervisor reads what was verified whatever the branch points at since; ""
    else (planning), for the branch as it is."""
    st = task.read_state()
    return st.verified_commit if st.state is State.REVIEW else ""


def refresh(task: Task, pod: Pod, commit: str = "") -> None:
    """The planner's copy of the committed work: made by git on the host, in a folder of the
    task's disk that only the control container mounts, read-only. The writer's clone is read
    as every git of the host's reads it, once it is as recorded (repo.guard) and within a time
    limit, and only its commits are taken: its .git, where the writer can leave what git follows
    to a command, is never what the planner's git works in. The copy stands on commit when one
    is given (the verified one, for a review; not on the task's branch, it is refused), else on
    the task's branch as it is. Before the planner's first turn and before each one after it;
    the harness's working directory (workdir) is made with it."""
    workdir(pod)
    src, branch = pod.control_src, repo.branch_name(task.id)
    if not src.is_dir() or src.is_symlink():
        raise repo.RepoError(f"the planner's copy of the work has no folder at {src}; start the task again")
    if not (src / ".git").exists():
        repo.git("init", "--quiet", "--initial-branch", branch, str(src), env=HOST_GIT)
    try:
        repo.in_clone(
            task.repo, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", str(task.repo),
            f"refs/heads/{branch}", cwd=src, timeout=repo.COPY_TIMEOUT, env=HOST_GIT,
        )  # fmt: skip
    except repo.GitTimeout as e:
        raise repo.GitTimeout(
            f"the planner's copy of the work could not be brought up to date: {e}"
        ) from None
    if commit:
        if not HEX.fullmatch(commit):
            raise repo.RepoError(f"not a commit to read the work at: {commit[:60]!r}")
        known = repo.git(
            "merge-base", "--is-ancestor", commit, "FETCH_HEAD", cwd=src, check=False, env=HOST_GIT
        )
        if known.returncode != 0:
            raise repo.RepoError(
                f"the verified commit {commit[:7]} is not on the task's branch {branch}, so the "
                "planner's copy cannot show the work that was verified"
            )
    repo.git(
        "reset", "--quiet", "--hard", "--no-recurse-submodules", commit or "FETCH_HEAD", cwd=src,
        env=HOST_GIT,
    )  # fmt: skip


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
