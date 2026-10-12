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
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from . import (
    claudecode,
    cli_session,
    cli_usage,
    code,
    comments,
    context,
    control,
    firstrun,
    gate,  # noqa: F401  # a test reaches GateError as actions.gate.GateError
    ide,
    image,
    limits,
    manual,
    mirror,
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
    waiting,
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
    HostService,
    Project,
    Role,
    current_mode,
    load_config,
    load_project,
)
from .decisions import (  # noqa: F401
    NO_BUILD,
    WRITER_PROPOSES,
    accept_command,
    accept_plan,
    answer_path,
    approve_risky,
    import_plan,
    plan_prompt,
    reply,
    risky_diffs,
    risky_target,
    risky_view,
    save_verify,
    verify_again,
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
from .history import remote as tracked_remote
from .orchestration import has_control, mode_of
from .orchestration import problem as orchestration_problem
from .plan import KINDS
from .pod import DISK_MOUNT, Mount, Pod, PodError, RulesLeft, prepare_host
from .projects import (  # noqa: F401
    GROUNDWORK,
    broken_projects,
    empty_project,
    forget_project,
    git_root,
    project_at,
    project_files,
    project_modules,
    project_problem,
    propose_project,
    setup_project,
    start_repository,
)
from .proposal import (  # noqa: F401
    missing_command,
    narrowed_proposal,
    nested,
    outside_modules,
    planned_modules,
    proposed,
    scope,
    verify_commands,
    whole,
)
from .review import (  # noqa: F401
    ARCHIVED,
    Finished,
    accepted_criteria,
    apply_work,
    archive,
    archive_path,
    branch_choices,
    changed_files,
    commit_work,
    current_branch,
    default_branch,
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
    AGENT_ROLES,
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
from .roles import agent_roles as agent_roles
from .states import State
from .task import DELETE, RUN, Task, TaskState, check_layout, create_task, find_task
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


def load(task_id: str, use: str = RUN) -> tuple[Task, Project]:
    """use: what the task is opened for (task.find_task); a task of another layout is not run."""
    task = find_task(load_config().tasks_dir, task_id, use)
    return task, load_project(task.read_state().project)


# --- pod and tmux -------------------------------------------------------------------------------


def task_pod(task_id: str) -> Pod:
    return pod_of(*load(task_id))


def pod_of(task: Task, project: Project) -> Pod:
    config = load_config()
    # Mount sources must exist, or Docker creates them as root-owned directories.
    runtime = secrets.runtime_dir(task.id)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    comments.ensure(task.meta)
    (task.meta / "harness").mkdir(exist_ok=True)
    (task.meta / "review").mkdir(exist_ok=True)
    apart = has_control(task, config)
    # The review folder is the reviewing agent's to write, and the host reads the verdict there:
    # the agent's container writes it only where the writer's agent is the one that reviews (a
    # single agent, a writer reading its own work, a box). Where a reviewer or the planner reads
    # the work in a container of its own, the writer's has it read-only, under /task.
    reviews_itself = task.read_state().box or not mode_of(task, config).reviews()
    mounts = [
        *repo.agent_mounts(task.repo, task.meta),
        # The plan, harness files and gate results: readable, not writable, for the agent.
        Mount(str(task.meta), "/task", read_only=True),
        Mount(str(task.meta / "handoff"), "/task/handoff"),
        comments.mount(task.meta),  # your replies, over the folder the agent writes
        *([Mount(str(task.meta / "review"), reviewing.MOUNT)] if reviews_itself else []),
        Mount(str(runtime), secrets.MOUNT, read_only=True),
    ]
    ref = image.image_ref()
    image_env = image.env(ref)
    env = {
        "OPENCODE_CONFIG": opencode.CONFIG,
        "CLAUDE_CONFIG_DIR": claudecode.CONFIG_DIR,
        **toolchain.agent_env(project.java, image_env),
    }
    # The mirror is reached as a service of the host's, through the pod's firewall.
    through = [HostService(config.host_ip, config.hub_mirror_port)] if config.hub_mirror else []
    disk = task.root / DISK_MOUNT
    try:
        task_limits = limits.for_task(config.limits, task.meta)
    except ValueError as e:
        raise PodError(str(e)) from None
    return Pod(
        task.id, task.repo, ref, [*project.host_services, *through], mounts, env, project.pass_env,
        gate_env=toolchain.gate_env(project.java, image_env, disk / "gate"),
        gate_dir=disk / "gate", review_dir=disk / "review", allocation=task.meta / "network.json",
        network_pool=config.network_pool, host_address=config.host_address,
        hub_mirror=mirror.url(config.host_ip, config.hub_mirror_port) if through else "",
        limits=task_limits, folder=task.root,
        # The planner's own container, where the flow has a planner other than the writer.
        control_mounts=control.mounts(task) if apart else None,
        control_env=control.env(task),
    )  # fmt: skip


# --- lifecycle ----------------------------------------------------------------------------------


def ensure_mirror(task: Task, config: Config) -> None:
    """The Docker Hub mirror running before a pod starts, when it is on. One that does not start is
    said on the task and stops nothing: the pod's daemon then pulls from Docker Hub itself."""
    if not config.hub_mirror:
        return
    try:
        prepare_host(config.host_address, config.network_pool)  # it listens on the host's address
        mirror.ensure(config.hub_mirror_port, config.host_ip)
    except PodError as e:
        task.event("mirror", problem=str(e))


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


# The header's line in a project verified by its modules (Project.by_module), for the planner.
MODULES_LINE = """# The directories this task changes, from the repository's root, e.g. ["core", "app"]. The
# verification builds and tests only these, with `{command}`; a change outside them makes it
# build the whole project.
modules = []
"""


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
    plan_in_cli: bool = False,
) -> Task:
    """no_build: nothing to build or test in this task (research, a ticket analysis): verify = false
    in its plan, for this task only.
    description: one line, or a whole ticket; it all goes into the plan the agent starts from.
    @path mentions in it are copied into the task (relative ones from cwd). roles: what a role runs
    on for this task only, as m would set it; config.toml's own choice is no choice at all.
    orchestration: how this task is shared between the roles (config.ORCHESTRATION_MODES); "" is
    config.toml's. max_rounds: this task's fix turns before the work comes to you; 0 is config.toml's.
    plan_in_cli: you plan this task in an agent's CLI on your machine, which makes you its planner."""
    config = load_config()
    orchestration = current_mode(orchestration)
    if orchestration not in ("", *ORCHESTRATION_MODES):
        raise ConfigError(f"orchestration must be one of {', '.join(ORCHESTRATION_MODES)}")
    if not isinstance(max_rounds, int) or max_rounds < 0:
        raise ConfigError("max_rounds is a whole number of fix turns; 0 for config.toml's")
    if plan_in_cli:
        if "planner" in (roles or {}):
            raise ConfigError("--plan-in-cli makes you the planner; leave out --model planner=")
        roles = {**(roles or {}), "planner": (manual.NAME, "")}
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
    if why := orchestration_problem(orchestration or config.orchestration, planner, plan_in_cli):
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
    elif project.by_module:
        modules = MODULES_LINE.format(command=" && ".join(project.verify))
        plan = plan.replace("\nverify = []\n", "\nverify = []\n" + modules, 1)
    # The flow is the task's from its creation, config.toml's of this moment when none was asked
    # for: which containers its pod has follows from it, and must not change under a task that runs.
    flow = orchestration or config.orchestration
    task = create_task(
        config.tasks_dir, project.name, title, plan, after=used_numbers(project), orchestration=flow
    )
    if max_rounds and max_rounds != config.max_rounds:
        task.set_orchestration(flow, max_rounds)
    try:
        described = context.attach(description.strip(), found, task.meta / "context", clone=task.repo)
        task.plan_path.write_text(plan.replace("{{goal}}", described))
        base = repo.prepare(project.repo, task.repo, task.id, task.meta, base_ref=base_ref)
    except BaseException:
        shutil.rmtree(task.root, ignore_errors=True)
        raise
    branch = repo.start_branch(project.repo, base_ref)
    task.set_base_commit(base, branch, tracked_remote(project.repo, branch))
    task.event("base", ref=base_ref or "HEAD", commit=base, **({"branch": branch} if branch else {}))
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
    if plan_in_cli:
        task.set_plan_in_cli(True)
        if session := cli_session.detect(os.environ, cwd or Path.cwd()):
            task.set_cli_session(session)
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
    planner = role_of(task, "planner", config)
    if why := orchestration_problem(mode_of(task, config).name, planner, task.read_state().plan_in_cli):
        raise PodError(why)
    if not image.exists(image.image_ref()):
        raise PodError("the agent image is not built; run 'vivibox image build'")
    # A model the catalog retired fails the first turn with an opaque server error; said here,
    # with the names to pick from, once the day's list is refreshed to be sure.
    if model_missing(config, task, available_models()) and (
        why := model_missing(config, task, available_models(refresh=True))
    ):
        raise PodError(why)
    if mode_of(task, config).separate_reviewer and "reviewer" in config.roles:
        provider_keys(config, task, ("reviewer",))  # its key is for its own container; its model is due now
    # The keys of the roles that run in the agent's container (the reviewer's never; the planner's
    # only where it has no container of its own), and the secrets of the MCP servers there; at
    # every start, so a key nobody here needs any more is gone.
    in_agent = agent_roles(task, config)
    secrets.prepare(task.id, provider_keys(config, task, in_agent) + providers.mcp_secrets())
    used = [opencode.provider_of(r.model) for r in (role_of(task, n, config) for n in in_agent)
            if r.harness == opencode.NAME]  # fmt: skip
    changed = opencode.prepare(task, model, project.verify, used)
    # The planner's own keys, password and harness files, for its control container alone.
    if pod.has_control:
        planners_changed = control.prepare(task, config, project.verify)
    else:
        control.forget(task.id)
    if config.hub_mirror:
        on_step("starting the Docker Hub mirror…")
    ensure_mirror(task, config)
    on_step("starting the pod…")
    pod.up()
    if pod.has_control:
        # The commits, for the planner to read, before anything of the project runs.
        on_step("copying the commits for the planner…")
        try:
            control.refresh(task, pod, control.reviewed_commit(task), depth=config.history_depth)
        except repo.Blocked as e:
            # What the task added blocks its clone, and no git of the host's reads it: the
            # planner's copy stays as it was, and the planner gets no turn on it (its copy is
            # asked for again before each one). The task starts all the same, so that it waits
            # where your reply is taken and the writer can take out what it added.
            task.event("view_not_refreshed", whose=control.PLANNERS_OWN, why=str(e)[:500])
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
    if pod.has_control:
        control.serve(task, pod, config, planners_changed)
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
    # The model the writing happens on: the planner's where the mode joins the two (P+W+R).
    writes_on = role_of(task, mode_of(task, config).agents["writer"], config).model
    task.event("started", model=writes_on)
    return writes_on


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


def wait(tasks: list[Task], timeout: float | None = None) -> list[tuple[Task, TaskState, str]]:
    """Until one of the tasks needs you or stops moving (waiting.reason); [] after the timeout."""
    return waiting.wait(tasks, supervisor_running, timeout=timeout)


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
    control.forget(task.id)
    if force:
        task.set_paused(True, problem=FORCED)
    elif not task.read_state().paused:
        task.set_paused(True)


def remove(task: Task, project: Project, accepted: bool = False) -> Path | None:
    """Everything of the task; a branch from accepting it stays in your repository. A task you
    remove is kept in the history as deleted; one you accepted is there as done already.
    Returns the review copy it removed, if there was one. A task from an earlier vivibox goes the
    same way, its supervisor and pod too; one from a newer vivibox is refused (check_layout)."""
    check_layout(task, DELETE)
    stop_supervisor(task)
    if not accepted:
        cli_usage.count(task, "conversation")
        remember_removed(task, project)
    archive(task)
    left = pod_of(task, project).remove()  # said once the rest is gone too
    secrets.remove(task.id)
    reviewing.forget(task.id)
    control.forget(task.id)
    worktree = repo.remove_review_worktree(project.repo, task.root)
    repo.drop_review_ref(project.repo, task.id)
    # Plain file removal: no git runs in the clone on the host.
    shutil.rmtree(task.root)
    if left:
        raise RulesLeft(task.id, left)
    return worktree


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
