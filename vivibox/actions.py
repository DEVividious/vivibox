"""What you can do with a task. The command line and the interactive view both call these; neither has
task logic of its own, and neither prints or asks from here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from . import context, gate, ide, image, opencode, repo, secrets, supervisor, toolchain, ui
from . import init as project_init
from .config import PROJECT_NAME, Config, ConfigError, Project, config_dir, load_config, load_project
from .plan import KINDS, parse_plan
from .pod import Mount, Pod, PodError
from .risky import Approvals
from .states import State
from .task import Task, create_task, find_task, now

SUPERVISOR_PID = "supervisor.pid"


def load(task_id: str) -> tuple[Task, Project]:
    task = find_task(load_config().tasks_dir, task_id)
    return task, load_project(task.read_state().project)


# --- pod and tmux -------------------------------------------------------------------------------


def task_pod(task_id: str) -> Pod:
    task, project = load(task_id)
    # Mount sources must exist, or Docker creates them as root-owned directories.
    runtime = secrets.runtime_dir(task.id)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    (task.meta / "harness").mkdir(exist_ok=True)
    mounts = [
        *repo.agent_mounts(task.repo, task.meta),
        # The plan, harness files and gate results: readable, not writable, for the agent.
        Mount(str(task.meta), "/task", read_only=True),
        Mount(str(task.meta / "handoff"), "/task/handoff"),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]
    ref = image.image_ref()
    env = {"OPENCODE_CONFIG": opencode.CONFIG, **toolchain.agent_env(project.java, image.env(ref))}
    return Pod(task.id, task.repo, ref, project.host_services, mounts, env, gate_dir=task.root / "gate")


# The agent view runs on a tmux server of its own: your own tmux sessions and key bindings stay as they
# are, and Ctrl-q leaves the view from anywhere in it.
TMUX = ["tmux", "-L", "vivibox", "-f", "/dev/null"]
LEAVE_KEY = "C-q"


def tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([*TMUX, *args], capture_output=True, text=True, check=check)


def tmux_session(task_id: str) -> str:
    return f"vivibox-{task_id}"


def tmux_has(target: str) -> bool:
    return tmux("has-session", "-t", target).returncode == 0


def agent_view(task: Task, command: list[str]) -> None:
    """A tmux session showing the agent, opened when missing; closing it never touches the agent."""
    session = tmux_session(task.id)
    if tmux_has(session):
        return
    tmux("new-session", "-d", "-s", session, "-n", "agent", shlex.join(command), check=True)
    for option in (
        ["bind-key", "-n", LEAVE_KEY, "detach-client"],
        ["set-option", "-g", "status-right", " Ctrl-q: back to vivibox "],
        ["set-option", "-g", "status-left", f" {task.id} "],
        ["set-option", "-g", "status-left-length", "40"],
    ):
        tmux(*option)


def attach_command(task_id: str) -> list[str]:
    task, _ = load(task_id)
    st = task.read_state()
    if not tmux_has(tmux_session(task_id)):
        if not st.session or not supervisor_running(task):
            raise PodError(f"{task_id}: the agent is not working now; nothing to watch")
        agent_view(task, opencode.OpenCode(task_pod(task_id)).attach_command(st.session))
    return [*TMUX, "attach-session", "-t", tmux_session(task_id)]


def writer(config: Config) -> tuple[str, str]:
    role = config.roles["writer"]
    if role.harness != "opencode":
        raise ConfigError(f"harness '{role.harness}' is not supported yet; use opencode")
    return role.harness, role.model


# --- setting up a project ------------------------------------------------------------------------


def git_root(path: Path) -> Path | None:
    top = (
        repo.git("rev-parse", "--show-toplevel", cwd=path, check=False).stdout.strip()
        if path.is_dir()
        else ""
    )
    return Path(top) if top else None


def project_files() -> list[Path]:
    folder = config_dir() / "projects"
    return sorted(folder.glob("*.toml")) if folder.is_dir() else []


def project_at(path: Path) -> str:
    """The name of the project already set up for this repository, if there is one."""
    for other in project_files():
        try:
            if load_project(other.stem).repo.expanduser().resolve() == path:
                return other.stem
        except ConfigError:
            continue  # a project file that cannot be read says nothing about this folder
    return ""


def broken_projects() -> dict[str, str]:
    """Projects that cannot be worked in, by name: the folder is gone, or the file does not parse.

    A repository you move or delete leaves its project file behind; that is a thing to mention, not a
    reason for vivibox to stop.
    """
    broken = {}
    for file in project_files():
        try:
            project = load_project(file.stem)
        except ConfigError as e:
            broken[file.stem] = str(e.args[0] if e.args else e)
            continue
        if not project.repo.expanduser().is_dir():
            broken[file.stem] = f"{project.repo} is gone"
    return broken


def forget_project(name: str) -> Path:
    """Removes a project's file. The repository, wherever it is, is left alone."""
    path = config_dir() / "projects" / f"{name}.toml"
    if not path.exists():
        raise ConfigError(f"there is no project '{name}'")
    path.unlink()
    return path


# What a repository holds before anything has been written in it.
GROUNDWORK = {"README.md", "README", "readme.md", "LICENSE", "LICENSE.md", ".gitignore"}


def empty_project(name: str) -> bool:
    """True while a project has no code yet, so there is nothing in it that could work wrong."""
    try:
        tracked = repo.git("ls-files", cwd=load_project(name).repo, check=False).stdout.split()
    except (ConfigError, repo.RepoError):
        return False
    return all(path in GROUNDWORK for path in tracked)


def propose_project(path: Path) -> project_init.Detected:
    """What a project for this folder would look like: its name and how to build and test it."""
    return project_init.detect(git_root(path) or path)


def start_repository(path: Path) -> Path:
    """A new git repository for a project from scratch: an empty one with a first commit."""
    path.mkdir(parents=True, exist_ok=True)
    if any(p.name != ".git" for p in path.iterdir()):
        raise ConfigError(f"{path} is not empty and not a git repository; set up git there yourself")
    repo.git("init", "--quiet", "-b", "main", str(path))
    repo.identity(path)  # your name and e-mail, or this tells you to set them
    (path / "README.md").write_text(f"# {path.name}\n")
    repo.git("add", "README.md", cwd=path)
    repo.git("commit", "--quiet", "-m", "Initial commit", cwd=path)
    return path


def setup_project(path: Path, name: str, verify: list[str], java: str = "", create: bool = False) -> Path:
    """Writes ~/.config/vivibox/projects/<name>.toml for this repository. Returns the file."""
    path = path.expanduser().resolve()
    top = git_root(path)
    if top is None:
        if not create:
            raise ConfigError(f"{path} is not a git repository; nothing set up")
        top = start_repository(path)
    if taken := project_at(top):
        raise ConfigError(f"this repository is already project {taken}")
    if not PROJECT_NAME.match(name):
        raise ConfigError(f"project name '{name}': lowercase letters, digits and '-', up to 31 characters")
    target = config_dir() / "projects" / f"{name}.toml"
    if target.exists():
        raise ConfigError(f"project {name} is already set up in {target}; pick another name")
    found = project_init.Detected(name, top, [c.strip() for c in verify if c.strip()], java)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(project_init.render(found))
    return target


# --- lifecycle ----------------------------------------------------------------------------------


def title_of(description: str) -> str:
    """The first line of a task's description names it in lists and commit suggestions."""
    first = next((line.strip() for line in description.splitlines() if line.strip()), "")
    return ui.shorten(first.lstrip("# ").strip(), 120)


def used_numbers(project: Project) -> int:
    """The highest task number of this project with a branch or review ref in your repository: a new
    task must not reuse it, or its work would overwrite that branch."""
    refs = repo.git("for-each-ref", "--format=%(refname)", "refs/heads/vivibox/", "refs/vivibox/",
                    cwd=project.repo, check=False).stdout  # fmt: skip
    pattern = re.compile(rf"/{re.escape(project.name)}-(\d+)$")
    return max((int(m.group(1)) for line in refs.split() if (m := pattern.search(line))), default=0)


def create(
    project_name: str, description: str, auto: bool = False, kind: str = "feature", cwd: Path | None = None
) -> Task:
    """description: one line, or a whole ticket; it all goes into the plan the agent starts from.
    @path mentions in it are copied into the task (relative ones from cwd)."""
    config = load_config()
    project = load_project(project_name)
    if not project.repo.is_dir():
        raise ConfigError(f"{project.repo} is gone; project {project_name} has nothing to work on")
    if not (project.repo / ".git").exists():
        raise ConfigError(f"{project.repo} is not a git repository")
    if not (title := title_of(description)):
        raise ConfigError("the task needs a description")
    if kind not in KINDS:
        raise ConfigError(f"kind must be one of {KINDS}")
    found = context.resolve(description, cwd or Path.cwd())  # all checked before anything is created
    template = files("vivibox").joinpath("templates/plan-bug.md" if kind == "bug" else "templates/plan.md")
    plan = template.read_text().replace("{{kind}}", kind)
    task = create_task(config.tasks_dir, project.name, title, plan, after=used_numbers(project))
    try:
        described = context.attach(description.strip(), found, task.meta / "context")
        task.plan_path.write_text(plan.replace("{{goal}}", described))
        base = repo.prepare(project.repo, task.repo, task.id, task.meta)
    except BaseException:
        shutil.rmtree(task.root, ignore_errors=True)
        raise
    task.set_base_commit(base)
    if auto:
        task.set_auto_plan(True)
    # The risky files as they are in your repository are the starting approval.
    Approvals(task.meta, task.repo, project.risky_extra).approve()
    return task


def start(task_id: str, resume: bool = False) -> str:
    """Starts or resumes the task's pod, agent and supervisor. Returns the model."""
    config = load_config()
    task, project = load(task_id)
    _, model = writer(config)
    if not image.exists(image.image_ref()):
        raise PodError("the agent image is not built; run 'vivibox image build'")
    secrets.prepare(task.id, [opencode.provider_of(model)])
    changed = opencode.prepare(task, model, project.verify)
    pod = task_pod(task.id)
    pod.up()
    toolchain.ensure(pod, project.java)
    harness = opencode.OpenCode(pod)
    if changed:
        harness.restart_server()  # the server reads its configuration only at start
    else:
        harness.ensure_server()
    st = task.read_state()
    if st.session and not harness.session_exists(st.session):
        # The harness lost the conversation; the task goes on from its plan and handoff files.
        task.set_session("")
        task.event("session_lost", session=st.session)
    interrupted = st.state in (State.PLAN, State.IMPLEMENT) and (st.paused or resume)
    if interrupted and not (task.meta / supervisor.NEXT_PROMPT).exists():
        supervisor.set_next_prompt(task, supervisor.RESUME_PROMPT)
    if st.paused:
        task.set_paused(False)
    start_supervisor(task)
    task.event("started", model=model)
    return model


def supervisor_running(task: Task) -> bool:
    try:
        pid = int((task.meta / SUPERVISOR_PID).read_text())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return False
    return b"supervise" in cmdline and task.id.encode() in cmdline


def start_supervisor(task: Task) -> None:
    """A process of its own, in the background: no window or key press of yours can end it by accident.
    Its output goes to .task/log/supervisor.log; your desktop session's variables reach it for
    notifications and the IDE button."""
    if supervisor_running(task):
        return
    log = task.meta / "log" / "supervisor.log"
    with log.open("a") as out:
        p = subprocess.Popen(
            [sys.executable, "-m", "vivibox", "supervise", task.id],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip
    (task.meta / SUPERVISOR_PID).write_text(str(p.pid))


def stop_supervisor(task: Task) -> None:
    if supervisor_running(task):
        pid = int((task.meta / SUPERVISOR_PID).read_text())
        # Its own process group: the agent turn it runs goes too.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
    tmux("kill-session", "-t", tmux_session(task.id))


def stop(task: Task) -> None:
    stop_supervisor(task)
    task_pod(task.id).down()
    secrets.remove(task.id)
    if not task.read_state().paused:
        task.set_paused(True)


def remove(task: Task, project: Project) -> Path | None:
    """Everything of the task; a branch from accepting it stays in your repository.
    Returns the review copy it removed, if there was one."""
    stop_supervisor(task)
    task_pod(task.id).remove()
    secrets.remove(task.id)
    worktree = repo.remove_review_worktree(project.repo, task.root)
    repo.drop_review_ref(project.repo, task.id)
    # Plain file removal: no git runs in the clone on the host.
    shutil.rmtree(task.root)
    return worktree


# --- your decisions -----------------------------------------------------------------------------


def accept_plan(task: Task, project: Project) -> None:
    if task.read_state().state is not State.CHECKPOINT_PLAN:
        raise gate.GateError(f"{task.id} has no plan waiting for you")
    supervisor.accept_plan(
        task, "plan accepted", project.verify, lambda commands: save_verify(project, commands)
    )


def save_verify(project: Project, commands: list[str]) -> None:
    """Keeps what the plan chose, so the project's next task does not decide again."""
    path = config_dir() / "projects" / f"{project.name}.toml"
    text = path.read_text()
    line = "verify = [" + ", ".join(f'"{c}"' for c in commands) + "]"
    path.write_text(re.sub(r"^verify = \[.*?\]", line, text, count=1, flags=re.MULTILINE | re.DOTALL))


def verify_commands(task: Task, project: Project) -> list[str]:
    """The project's commands, or the ones the plan you accepted brought for a new project."""
    accepted = task.meta / gate.ACCEPTED_PLAN
    if project.verify or not accepted.exists():
        return project.verify
    return parse_plan(accepted.read_text()).verify


def reply(task: Task, comment: str) -> State:
    """Your comment goes to the agent, and the task back to it. Returns where it goes."""
    if not comment.strip():
        raise gate.GateError("the comment is empty")
    st = task.read_state()
    handoff = task.meta / "handoff"
    targets = {
        State.CHECKPOINT_PLAN: State.PLAN,
        State.CHECKPOINT_BLOCKED: State.IMPLEMENT,
        State.CHECKPOINT_FINAL: State.IMPLEMENT,
    }
    if st.state is State.APPROVAL_RISKY:
        # Rejecting risky changes sends the agent back to where it came from.
        target = State.PLAN if risky_target(task) is State.CHECKPOINT_PLAN else State.IMPLEMENT
    elif st.state in targets:
        target = targets[st.state]
    else:
        raise gate.GateError(f"{task.id} is in {st.state}; replies are for checkpoints")
    with (handoff / "comments.md").open("a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{comment.strip()}\n")
    question = handoff / supervisor.QUESTION
    if question.exists():
        question.rename(handoff / f"question-answered-{time.strftime('%Y%m%d-%H%M%S')}.md")
    prompt = supervisor.PLAN_COMMENT_PROMPT if target is State.PLAN else supervisor.COMMENT_PROMPT
    supervisor.set_next_prompt(task, prompt)
    task.reset_iterations()
    task.transition(target, reason="your reply")
    return target


def risky_target(task: Task) -> State:
    """The checkpoint the risky change was holding back, recorded when the task entered approval."""
    entered = next(e["data"] for e in reversed(task.events()) if e["type"] == "state")
    if "then" in entered:
        return State(entered["then"])
    return State.CHECKPOINT_PLAN if entered["previous"] == State.PLAN else State.CHECKPOINT_FINAL


def risky_diffs(task: Task, project: Project) -> list[str]:
    approvals = Approvals(task.meta, task.repo, project.risky_extra)
    return [approvals.diff(change) for change in approvals.changes()]


def approve_risky(task: Task, project: Project) -> tuple[int, Path | None]:
    """Approves the risky files as they are now. Returns how many changed, and the review copy when
    the approval was all the finished work was waiting for."""
    approvals = Approvals(task.meta, task.repo, project.risky_extra)
    changes = approvals.changes()
    approvals.approve()
    task.event("risky_approved", paths=[c.path for c in changes])
    if task.read_state().state is State.APPROVAL_RISKY:
        target = risky_target(task)
        task.transition(target)
        if target is State.CHECKPOINT_FINAL:
            return len(changes), prepare_review(task, project)
    return len(changes), None


# --- review and accepting the work --------------------------------------------------------------


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
    path = history_path()
    if not path.exists():
        return
    kept = [
        line for line in path.read_text().splitlines() if line.strip() and json.loads(line)["id"] != task_id
    ]
    path.write_text("".join(line + "\n" for line in kept))


def remember(done: Finished, project: Project, commit: str) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": done.task_id,
        "project": project.name,
        "title": done.message,
        "cost": round(done.cost, 4),
        "commit": commit[:10],
        "branch": done.branch,
        "conflicts": done.conflicts,
        "finished": now(),
    }
    with path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@dataclass
class Finished:
    task_id: str
    source: Path
    cost: float
    message: str
    branch: str = ""
    conflicts: list[str] = field(default_factory=list)
    status: str = ""


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
    done = Finished(task.id, project.repo, ui.cost(task), suggested_message(project.repo, st.base_commit,
                                                                            commit, st.goal))  # fmt: skip
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
    remove(task, project)
    return done


def suggested_message(source: Path, base: str, commit: str, goal: str) -> str:
    """The agent's commit message when it made one commit (the gate checked it), else the goal, short."""
    subjects = repo.git("log", "--format=%s", f"{base}..{commit}", cwd=source).stdout.splitlines()
    if len(subjects) == 1:
        return subjects[0]
    first = re.split(r"[:;,.]\s", goal, maxsplit=1)[0].strip()
    return ui.shorten(first, gate.MAX_SUBJECT)


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


# --- notifications ------------------------------------------------------------------------------


def buttons(task: Task, project: Project, config: Config, kind: str) -> list[supervisor.Action]:
    """What a notification offers: your next step, one click away."""
    vivibox = [sys.executable, "-m", "vivibox"]
    if kind == "plan":
        return [
            supervisor.Action("show", "Show plan", ["xdg-open", str(task.plan_path)], detach=True),
            supervisor.Action("accept", "Accept plan", [*vivibox, "accept", task.id]),
        ]
    editor = editor_command(config, project)
    # A terminal editor has no window to open from a notification; that one is for the view.
    if kind == "review" and editor and not ide.is_terminal(editor):
        copy = repo.review_worktree_path(project.repo, task.root)
        command = ide.command_for(copy, editor)
        name = Path(command[0]).name
        return [supervisor.Action("open", f"Open in {name}", command, detach=True)]
    return []
