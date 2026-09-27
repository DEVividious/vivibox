"""What you can do with a task. The command line and the interactive view both call these; neither has
task logic of its own, and neither prints or asks from here.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from . import (
    claudecode,
    code,
    context,
    firstrun,
    gate,
    ide,
    image,
    manual,
    opencode,
    prepare,
    prompts,
    providers,
    repo,
    reviewing,
    secrets,
    supervisor,
    toolchain,
    ui,
)
from .box import (  # noqa: F401
    BOX_COMMIT,
    BOX_GOAL,
    box_pod_running,
    box_providers,
    box_shell_command,
    close_box,
    commit_in_box,
    open_box,
    projects_named,
    start_box,
)
from .config import (
    ORCHESTRATION_MODES,
    Config,
    ConfigError,
    Project,
    Role,
    config_dir,
    load_config,
    load_project,
)
from .demo import (  # noqa: F401
    BASH_BLOCK,
    DEMO_ASK,
    DEMO_FILE,
    DEMO_QUESTION,
    DEMO_SESSION,
    MAX_INSTRUCTION,
    Demo,
    ask_agent_how_to_run,
    demo,
    demo_allowed,
    demo_commands,
    demo_from_history,
    demo_instruction,
    demo_stop,
    instruction_commands,
    use_instruction,
    write_instruction,
)
from .orchestration import mode_of
from .orchestration import problem as orchestration_problem
from .plan import KINDS
from .pod import Mount, Pod, PodError
from .projects import (  # noqa: F401
    GROUNDWORK,
    broken_projects,
    empty_project,
    forget_project,
    git_root,
    project_at,
    project_files,
    project_problem,
    propose_project,
    setup_project,
    start_repository,
)
from .proposal import (  # noqa: F401
    missing_command,
    narrowed_proposal,
    proposed,
    verify_commands,
)
from .review import (  # noqa: F401
    ARCHIVED,
    Finished,
    accepted_criteria,
    apply_work,
    archive,
    archive_path,
    changed_files,
    commit_work,
    current_branch,
    editor_command,
    fetch_work,
    finish,
    forget,
    history,
    history_path,
    open_in_ide,
    prepare_review,
    remember,
    remember_removed,
    review_copy,
    suggested_message,
)
from .risky import Approvals
from .roles import (  # noqa: F401
    MODELS_CACHE_SECONDS,
    POPULAR,
    Choice,
    available_models,
    choice_label,
    choices,
    choose_role,
    configured_choice,
    fetch_provider_catalog,
    harness_for,
    machine_problem,
    model_missing,
    models_cache,
    models_offered,
    needs_provider,
    not_offered,
    parse_choice,
    provider_catalog,
    provider_keys,
    provider_models,
    record_settings,
    role_of,
    task_settings,
    writer,
)
from .states import State
from .task import Task, create_task, find_task
from .window import (  # noqa: F401
    LEAVE_BINDING,
    LEAVE_KEY,
    SHOWS,
    TMUX,
    VERIFICATION,
    agent_view,
    attach_command,
    close_agent_view,
    leave_key,
    outside_tmux,
    shown,
    tmux,
    tmux_has,
    tmux_session,
    verification_log,
    view_command,
    watchable_session,
    watchable_sessions,
)

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
    (task.meta / "review").mkdir(exist_ok=True)
    mounts = [
        *repo.agent_mounts(task.repo, task.meta),
        # The plan, harness files and gate results: readable, not writable, for the agent.
        Mount(str(task.meta), "/task", read_only=True),
        Mount(str(task.meta / "handoff"), "/task/handoff"),
        # Where a supervisor reading in the pod writes its review, as the reviewer's container does.
        Mount(str(task.meta / "review"), reviewing.MOUNT),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]
    ref = image.image_ref()
    env = {
        "OPENCODE_CONFIG": opencode.CONFIG,
        "CLAUDE_CONFIG_DIR": claudecode.CONFIG_DIR,
        **toolchain.agent_env(project.java, image.env(ref)),
    }
    return Pod(
        task.id, task.repo, ref, project.host_services, mounts, env, project.pass_env,
        gate_dir=task.root / "gate", review_dir=task.root / ".review",
        network_pool=load_config().network_pool,
    )  # fmt: skip


# --- lifecycle ----------------------------------------------------------------------------------


def title_of(description: str) -> str:
    """What the list shows of a task until its plan names it in a line of its own: the start of
    its description, whatever that is, so nothing has to be written as a title."""
    first = next((line.strip() for line in description.splitlines() if line.strip()), "")
    return ui.shorten(first.lstrip("# ").strip(), 120)


def used_numbers(project: Project) -> int:
    """The highest task number of this project with a branch or review ref in your repository, or in
    the history: a new task must not reuse it, or its work would overwrite that branch."""
    refs = repo.git("for-each-ref", "--format=%(refname)", "refs/heads/vivibox/", "refs/vivibox/",
                    cwd=project.repo, check=False).stdout  # fmt: skip
    pattern = re.compile(rf"/{re.escape(project.name)}-(\d+)$")
    found = [int(m.group(1)) for line in refs.split() if (m := pattern.search(line))]
    # A task you removed is in the history under its number; a new one taking it would be two there.
    found += [int(m.group(1)) for e in history(limit=100_000)
              if (m := re.fullmatch(rf"{re.escape(project.name)}-(\d+)", e["id"]))]  # fmt: skip
    return max(found, default=0)


def create(
    project_name: str,
    description: str,
    auto: bool = False,
    kind: str = "feature",
    cwd: Path | None = None,
    roles: dict[str, Choice] | None = None,
    orchestration: str = "",
    max_rounds: int = 0,
    no_build: bool = False,
    base_ref: str = "",
) -> Task:
    """no_build: nothing to build or test in this task (research, a ticket analysis): verify = false
    in its plan, for this task only.
    description: one line, or a whole ticket; it all goes into the plan the agent starts from.
    @path mentions in it are copied into the task (relative ones from cwd). roles: what a role runs
    on for this task only, as m would set it; config.toml's own choice is no choice at all.
    orchestration: how this task is shared between the roles (config.ORCHESTRATION_MODES); "" is
    config.toml's. max_rounds: this task's fix turns before the work comes to you; 0 is config.toml's."""
    config = load_config()
    if orchestration not in ("", *ORCHESTRATION_MODES):
        raise ConfigError(f"orchestration must be one of {', '.join(ORCHESTRATION_MODES)}")
    if not isinstance(max_rounds, int) or max_rounds < 0:
        raise ConfigError("max_rounds is a whole number of fix turns; 0 for config.toml's")
    chosen: dict[str, Choice] = {}
    for role, (harness, model) in (roles or {}).items():
        if role not in config.roles:
            raise ConfigError(f"no role '{role}' in config.toml; there are {', '.join(sorted(config.roles))}")
        if role != "planner" and harness == manual.NAME:
            raise ConfigError(f"only the planner can be you, in your own chat; not the {role}")
        if role == "writer" and harness != opencode.NAME:
            raise ConfigError("only opencode can write yet; give the writer a provider/model")
        if (harness, model) != configured_choice(config, role):
            chosen[role] = (harness, model)
    for role in config.roles:
        harness, model = chosen.get(role) or configured_choice(config, role)
        if harness != manual.NAME and not model:
            raise ConfigError(f"the {role} has no model yet; pick one, or add a provider")
    planner = Role(*(chosen.get("planner") or configured_choice(config, "planner")))
    if why := orchestration_problem(orchestration or config.orchestration, planner):
        raise ConfigError(why)
    project = load_project(project_name)
    if not project.repo.is_dir():
        raise ConfigError(f"{project.repo} is gone; project {project_name} has nothing to work on")
    if not (project.repo / ".git").exists():
        raise ConfigError(f"{project.repo} is not a git repository")
    if not (title := title_of(description)):
        raise ConfigError("the task needs a description")
    if kind not in KINDS:
        raise ConfigError(f"kind must be one of {KINDS}")
    # All checked before anything is created; the project's own files point at the clone.
    found = context.resolve(description, cwd or Path.cwd(), repo=project.repo)
    template = files("vivibox").joinpath("templates/plan-bug.md" if kind == "bug" else "templates/plan.md")
    plan = template.read_text().replace("{{kind}}", kind)
    if no_build:
        plan = plan.replace("\nverify = []\n", "\nverify = false\n", 1)
    task = create_task(config.tasks_dir, project.name, title, plan, after=used_numbers(project))
    if (orchestration and orchestration != config.orchestration) or (
        max_rounds and max_rounds != config.max_rounds
    ):
        task.set_orchestration(
            orchestration if orchestration != config.orchestration else "",
            max_rounds if max_rounds != config.max_rounds else 0,
        )
    try:
        described = context.attach(description.strip(), found, task.meta / "context", clone=task.repo)
        task.plan_path.write_text(plan.replace("{{goal}}", described))
        base = repo.prepare(project.repo, task.repo, task.id, task.meta, base_ref=base_ref)
    except BaseException:
        shutil.rmtree(task.root, ignore_errors=True)
        raise
    task.set_base_commit(base)
    task.event("base", ref=base_ref or "HEAD", commit=base)
    if found.notes:  # what the agent sees differs from what you have: said once, kept with the task
        task.event("context", notes=found.notes)
    for role, (harness, model) in list(chosen.items()):
        if not config.roles[role].model and harness == config.roles[role].harness:
            firstrun.remember(role, harness, model)  # the first model you pick becomes the default
            del chosen[role]
    for role, (harness, model) in chosen.items():
        # The harness is kept only when it differs, so a task on another model keeps following
        # config.toml's harness, as m has always left it.
        task.set_role(role, harness if harness != config.roles[role].harness else "", model)
    if auto:
        task.set_auto_plan(True)
    # The risky files as they are in your repository are the starting approval.
    Approvals(task.meta, task.repo, project.risky_extra).approve()
    return task


def context_notes(task: Task) -> list[str]:
    """What the task said about its @files when it was created."""
    return [n for e in task.events() if e["type"] == "context" for n in e["data"].get("notes", [])]


# Held while a task starts or stops: one at a time, whichever view or command asks.
START_LOCK = "start.lock"


@contextlib.contextmanager
def one_at_a_time(task: Task):
    """A second view that starts or stops the same task is told, instead of making the same pod
    twice. The lock goes with the process that holds it, so a crash leaves nothing behind."""
    with (task.meta / START_LOCK).open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PodError(
                f"{task.id}: a start or a stop is under way elsewhere; wait for it, or stop it by force (S)"
            ) from None
        yield


def start(
    task_id: str,
    resume: bool = False,
    on_step: Callable[[str], None] = lambda step: None,
    supervise: bool = True,
) -> str:
    """Starts or resumes the task's pod, agent and supervisor. Returns the model. A start that
    fails leaves its reason with the task: the message you get once is gone in seconds, and the row
    would go on saying "not started" with nothing to say why. on_step hears each step before it
    runs, for a row to say what a slow start is doing. supervise=False brings everything up and
    leaves the supervising to the caller (the behavioural tests drive the supervisor themselves)."""
    task, _ = load(task_id)
    with one_at_a_time(task):
        try:
            if task.read_state().box:
                start_box(task_id)
                return "you"
            return _start(task_id, resume, on_step, supervise)
        except Exception as e:
            task.set_problem(f"could not start: {e.args[0] if e.args else e}")
            raise


def needs_start(task: Task) -> bool:
    """True when the task has work to do and nobody to do it: stopped, failed, or its supervisor
    gone, as after a reboot."""
    st = task.read_state()
    if st.box:
        return False  # nothing runs in a box but you
    working = st.state in (State.PLAN, State.IMPLEMENT, State.VERIFY)
    return working and (st.paused or not supervisor_running(task))


def carry_on(task: Task) -> str:
    """After a decision of yours: the task goes on, started here when nobody is working on it.
    A decision means "go on", and saying the task moves on while nothing runs was a lie. Returns
    the model when it had to be started, "" when its supervisor carries on by itself."""
    return start(task.id, resume=True) if needs_start(task) else ""


def _start(
    task_id: str,
    resume: bool = False,
    on_step: Callable[[str], None] = lambda step: None,
    supervise: bool = True,
) -> str:
    config = load_config()
    task, project = load(task_id)
    pod = task_pod(task.id)
    if missing := pod.missing_env():
        # Docker would start the pod without them, and the build would fail later for a reason
        # the agent cannot fix.
        raise PodError(
            f"{', '.join(missing)} not set: the {project.name} project passes "
            f"{'it' if len(missing) == 1 else 'them'} to the task from the shell vivibox runs in "
            "(pass_env). Set them, e.g. with your login command, then start vivibox from that shell."
        )
    _, model = writer(config, task)
    if why := orchestration_problem(mode_of(task, config).name, role_of(task, "planner", config)):
        raise PodError(why)
    if not image.exists(image.image_ref()):
        raise PodError("the agent image is not built; run 'vivibox image build'")
    # A model the catalog retired fails the first turn with an opaque server error; said here,
    # with the names to pick from, once the day's list is refreshed to be sure.
    if model_missing(config, task, available_models()) and (
        why := model_missing(config, task, available_models(refresh=True))
    ):
        raise PodError(why)
    secrets.prepare(task.id, provider_keys(config, task) + providers.mcp_secrets())
    used = [opencode.provider_of(r.model) for r in (role_of(task, n, config) for n in config.roles)
            if r.harness == opencode.NAME]  # fmt: skip
    changed = opencode.prepare(task, model, project.verify, used)
    on_step("starting the pod…")
    pod.up()
    if project.java or project.tools:
        on_step(
            f"installing {', '.join(([f'Java {project.java}'] if project.java else []) + project.tools)}…"
        )
    toolchain.ensure(pod, project.java, tools=project.tools)
    prepare.begin(task, project, pod)
    on_step("starting opencode…")
    harness = opencode.OpenCode(pod)
    if changed:
        harness.restart_server()  # the server reads its configuration only at start
    else:
        harness.ensure_server()
    st = task.read_state()
    if (was := st.sessions.get("writer", "")) and not harness.session_exists(was):
        # The harness lost the conversation; the task goes on from its plan and handoff files.
        task.set_session("writer", "")
        task.event("session_lost", role="writer", harness=harness.name, session=was)
    interrupted = st.state in (State.PLAN, State.IMPLEMENT) and (st.paused or resume)
    if interrupted and not (task.meta / supervisor.NEXT_PROMPT).exists():
        supervisor.set_next_prompt(task, prompts.resume_prompt(st.state))
    if st.paused:
        task.set_paused(False)
    task.set_problem("")  # whatever kept it from starting before did not this time
    if supervise:
        start_supervisor(task)
    record_settings(task, project, config)
    task.event("started", model=model)
    return model


def supervising(task: Task) -> None:
    """Called by the supervisor itself: its pid, and the vivibox it runs, so the view can tell
    when an update on disk has left it behind."""
    (task.meta / SUPERVISOR_PID).write_text(str(os.getpid()))
    code.record(task.meta)


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


def stop_supervisor(task: Task, force: bool = False) -> None:
    if supervisor_running(task):
        pid = int((task.meta / SUPERVISOR_PID).read_text())
        # Its own process group: the agent turn it runs goes too.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    tmux("kill-session", "-t", tmux_session(task.id))


FORCED = "stopped by force"


def stop(task: Task, force: bool = False) -> None:
    """Stops the task and keeps its work. A stop asks the supervisor to finish and waits for the
    containers; force kills both instead, for a container that ignores the stop or a supervisor
    stuck in docker. The turn under way is lost then; the task's files are on the host."""
    with contextlib.nullcontext() if force else one_at_a_time(task):
        stop_supervisor(task, force=force)
        pod = task_pod(task.id)
        if force:
            pod.kill()
        else:
            pod.down()
    secrets.remove(task.id)
    reviewing.forget(task.id)
    if force:
        task.set_paused(True, problem=FORCED)
    elif not task.read_state().paused:
        task.set_paused(True)


def remove(task: Task, project: Project, accepted: bool = False) -> Path | None:
    """Everything of the task; a branch from accepting it stays in your repository. A task you
    remove is kept in the history as deleted; one you accepted is there as done already.
    Returns the review copy it removed, if there was one."""
    stop_supervisor(task)
    if not accepted:
        remember_removed(task, project)
    archive(task)
    task_pod(task.id).remove()
    secrets.remove(task.id)
    reviewing.forget(task.id)
    worktree = repo.remove_review_worktree(project.repo, task.root)
    repo.drop_review_ref(project.repo, task.id)
    # Plain file removal: no git runs in the clone on the host.
    shutil.rmtree(task.root)
    return worktree


# --- your decisions -----------------------------------------------------------------------------


def accept_plan(task: Task, project: Project) -> None:
    st = task.read_state()
    if st.state is not State.CHECKPOINT_PLAN:
        raise gate.GateError(f"{task.id} has no plan waiting for you")
    if st.awaiting_plan:
        raise gate.GateError(f"{task.id} has no plan yet; bring yours in with: vivibox plan import {task.id}")
    supervisor.accept_plan(task, "plan accepted")


def accept_command(task: Task, project: Project, command: str = "") -> None:
    """Keeps the command the writer proposed, or the one you give instead, for the project, and
    sends the task on to its first verification."""
    st = task.read_state()
    if st.state is not State.CHECKPOINT_COMMAND:
        raise gate.GateError(f"{task.id} has no command waiting for you")
    chosen = command.strip() or proposed(task)
    if not chosen:
        raise gate.GateError(
            f'no command came from the writer; give one: vivibox accept {task.id} --verify "…",'
            " or send it back with a reply"
        )
    save_verify(project, [chosen])
    supervisor.accept_command(task, chosen, "command accepted")


def plan_prompt(task: Task, cli: bool = False) -> str:
    """The prompt for planning this task in your own chat, in a browser or in a CLI."""
    path = task.meta / (manual.PROMPT_CLI if cli else manual.PROMPT)
    if not path.exists():
        raise gate.GateError(f"{task.id} has no plan prompt; its planner is not manual")
    return path.read_text()


def answer_path(task: Task) -> Path:
    return task.meta / manual.ANSWER


def import_plan(task: Task, answer: str | None = None) -> str:
    """Your chat's plan becomes the task's plan, for you to accept. Without an answer, the one
    already in the answer file: what a CLI wrote there, or what you pasted into it."""
    if task.read_state().state is not State.CHECKPOINT_PLAN:
        raise gate.GateError(f"{task.id} is not at its plan; a plan can be brought in only there")
    path = answer_path(task)
    if answer is not None:
        path.write_text(answer)
    if not path.exists() or not path.read_text().strip():
        raise gate.GateError(f"no plan to bring in; paste your chat's answer into {path}")
    return manual.import_answer(task)


# What a project with nothing to build or test is told, and told about, in one wording.
NO_BUILD = "no build: the verification checks the criteria and the commits only"
# What an empty verification means, wherever it is shown.
WRITER_PROPOSES = "the next task's writer proposes it, for you to accept"


def save_verify(project: Project, commands: list[str], no_build: bool = False) -> None:
    """Keeps what the plan chose, or what you picked, so the project's next task does not decide
    again: the commands, or that there is nothing to build (verify = false)."""
    path = config_dir() / "projects" / f"{project.name}.toml"
    text = path.read_text()
    line = "verify = false" if no_build else "verify = [" + ", ".join(f'"{c}"' for c in commands) + "]"
    path.write_text(re.sub(r"^verify = (\[.*?\]|false)", line, text, count=1, flags=re.MULTILINE | re.DOTALL))


def reply(task: Task, comment: str, criteria: list[str] | tuple = ()) -> State:
    """Your comment goes to the agent, and the task back to it. Returns where it goes. Criteria,
    when the work has come back to you, join the accepted plan: what you found at review becomes
    something the gate holds the work to, not only a remark the agent may act on."""
    criteria = [c for c in criteria if c.strip()]
    if not comment.strip() and not criteria:
        raise gate.GateError("the comment is empty")
    st = task.read_state()
    if criteria and st.state not in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED):
        raise gate.GateError(
            "criteria are added when the work comes back to you; before that, they belong in the plan"
        )
    handoff = task.meta / "handoff"
    targets = {
        State.CHECKPOINT_PLAN: State.PLAN,
        State.CHECKPOINT_COMMAND: State.IMPLEMENT,
        State.CHECKPOINT_BLOCKED: State.IMPLEMENT,
        State.CHECKPOINT_FINAL: State.IMPLEMENT,
    }
    if st.state is State.APPROVAL_RISKY:
        # Rejecting risky changes sends the agent back to where it came from.
        target = State.PLAN if risky_target(task) is State.CHECKPOINT_PLAN else State.IMPLEMENT
    elif st.state is State.CHECKPOINT_PLAN and role_of(task, "planner").harness == manual.NAME:
        # Nobody here would read it: the planner is a chat of yours, and the comment belongs there.
        raise gate.GateError(
            "you plan this task in your own chat; say it there and bring the new plan back "
            f"with: vivibox plan import {task.id}"
        )
    elif st.state in targets:
        target = targets[st.state]
    else:
        raise gate.GateError(f"{task.id} is in {st.state}; replies are for checkpoints")
    added = gate.add_criteria(task, criteria) if criteria else []
    if added:
        listed = "\n".join(f"- {c}" for c in added)
        note = f"New acceptance criteria, added to criteria.md unticked; meet and tick them:\n\n{listed}"
        comment = f"{comment.strip()}\n\n{note}" if comment.strip() else note
    with (handoff / "comments.md").open("a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n\n{comment.strip()}\n")
    supervisor.put_question_away(task)
    prompt = prompts.PLAN_COMMENT_PROMPT if target is State.PLAN else prompts.COMMENT_PROMPT
    supervisor.set_next_prompt(task, prompt)
    task.reset_rounds()
    task.transition(target, reason="your reply")
    return target


def verify_again(task: Task) -> None:
    """Runs the verification once more on the work as it is, without a turn of the agent. For a
    failure whose cause was outside the code: an expired token, Docker, a service that was down,
    whether the gate saw that itself or the agent asked about it. A reply would cost a turn and
    invite the agent to change code that was fine. A question the agent asked is answered by the
    build running, so it goes out of the way like an answered one."""
    if task.read_state().state is not State.CHECKPOINT_BLOCKED:
        raise gate.GateError(
            f"{task.id} is not blocked on a failed verification; only that is verified again"
        )
    kept = supervisor.put_question_away(task)
    task.transition(State.VERIFY, reason="verify again", **({"question": kept.name} if kept else {}))


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
