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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from . import (
    brief,
    claudecode,
    code,
    context,
    firstrun,
    gate,
    ide,
    image,
    keys,
    manual,
    opencode,
    providers,
    repo,
    secrets,
    supervisor,
    toolchain,
    ui,
)
from . import init as project_init
from . import pod as pod_module
from .config import (
    PROJECT_NAME,
    Config,
    ConfigError,
    Project,
    Role,
    config_dir,
    load_config,
    load_project,
)
from .plan import KINDS, PlanError, parse_plan
from .pod import Mount, Pod, PodError
from .risky import Approvals
from .states import State
from .task import Task, TaskState, create_task, find_task, now

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
    env = {
        "OPENCODE_CONFIG": opencode.CONFIG,
        "CLAUDE_CONFIG_DIR": claudecode.CONFIG_DIR,
        **toolchain.agent_env(project.java, image.env(ref)),
    }
    return Pod(
        task.id, task.repo, ref, project.host_services, mounts, env, project.pass_env,
        gate_dir=task.root / "gate", network_pool=load_config().network_pool,
    )  # fmt: skip


# The agent view runs on a tmux server of its own: your own tmux sessions and key bindings stay as they
# are, and Ctrl-q leaves the view from anywhere in it.
TMUX = ["tmux", "-L", "vivibox", "-f", "/dev/null"]
LEAVE_KEY = "C-q"
# '-E true' replaces the detaching client with a command that does nothing. Without it tmux
# prints "[detached (from session ...)]" after leaving its own screen, so the line lands on the
# normal one and is still in your scrollback once vivibox closes.
LEAVE_BINDING = ["bind-key", "-n", LEAVE_KEY, "detach-client", "-E", "true"]


def tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([*TMUX, *args], capture_output=True, text=True, check=check)


def tmux_session(task_id: str) -> str:
    return f"vivibox-{task_id}"


def tmux_has(target: str) -> bool:
    return tmux("has-session", "-t", target).returncode == 0


def leave_key() -> None:
    tmux(*LEAVE_BINDING)


# The command a task's tmux session shows, kept in the session's environment: the planner's
# conversation and the writer's are two, and a window opened during planning must not go on
# showing the planner once the writer is at work.
SHOWS = "VIVIBOX_SHOWS"


def shown(session: str) -> str:
    out = tmux("show-environment", "-t", session, SHOWS).stdout or ""
    return out.partition("=")[2].strip() if out.startswith(f"{SHOWS}=") else ""


def agent_view(task: Task, command: list[str]) -> None:
    """A tmux session showing the agent, opened when missing or when it shows another conversation;
    closing it never touches the agent."""
    session = tmux_session(task.id)
    wanted = shlex.join(command)
    if tmux_has(session):
        if shown(session) == wanted:
            # The keys live on the server, which outlives any one session, so a server still
            # running from before carries an older binding. Setting it again is cheap.
            leave_key()
            return
        tmux("kill-session", "-t", session)
    tmux("new-session", "-d", "-s", session, "-n", "agent", wanted, check=True)
    tmux("set-environment", "-t", session, SHOWS, wanted)
    for option in (
        LEAVE_BINDING,
        ["set-option", "-g", "status-right", " Ctrl-q: back to vivibox "],
        ["set-option", "-g", "status-left", f" {task.id} "],
        ["set-option", "-g", "status-left-length", "40"],
    ):
        tmux(*option)


def watchable_sessions(task: Task, st: TaskState | None = None) -> list[tuple[str, str]]:
    """The opencode conversations there are to look at, as (role, session): only opencode has a
    window to attach to, and a claude-code turn is watched through its log. The writer's first,
    then the planner's, which stays readable once the writer is at work."""
    st = st or task.read_state()
    return [
        (role, st.sessions[role])
        for role in brief.ROLES[::-1]
        if st.sessions.get(role) and role_of(task, role).harness == opencode.NAME
    ]


def watchable_session(task: Task, st: TaskState | None = None, role: str = "") -> str:
    """The conversation w shows: the role's you name, else the first there is; "" for none."""
    found = dict(watchable_sessions(task, st))
    return found.get(role, "") if role else next(iter(found.values()), "")


def verification_log(task: Task, st: TaskState | None = None) -> Path | None:
    """The log of the verification under way: the newest written since the task started
    verifying. None before the gate has opened it, and when the task is not verifying."""
    st = st or task.read_state()
    if st.state is not State.VERIFY:
        return None
    # A second's slack: the log is opened right after the transition, and mtimes are coarse.
    since = datetime.fromisoformat(st.updated).timestamp() - 1
    logs = [p for p in (task.meta / "log").glob("verify-*.log") if p.stat().st_mtime >= since]
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


VERIFICATION_VIEW = ["tail", "-n", "+1", "-F"]


def view_command(task: Task, st: TaskState, role: str = "") -> list[str]:
    """What w shows: the verification's log while it runs, else the agent's opencode window; the
    conversation of the role you name, whatever runs."""
    if not supervisor_running(task):
        raise PodError(f"{task.id}: the agent is not working now; nothing to watch")
    if st.state is State.VERIFY and not role:
        log = verification_log(task, st)
        if log is None:
            raise PodError(
                f"{task.id}: the verification is starting and has no log yet; try again in a moment"
            )
        return [*VERIFICATION_VIEW, str(log)]
    session = watchable_session(task, st, role)
    if not session:
        raise PodError(f"{task.id}: the agent is not working now; nothing to watch")
    return opencode.OpenCode(task_pod(task.id)).attach_command(session)


def attach_command(task_id: str, role: str = "") -> list[str]:
    task, _ = load(task_id)
    st = task.read_state()
    if st.box:
        return box_shell_command(task_id)
    agent_view(task, view_command(task, st, role))
    return [*TMUX, "attach-session", "-t", tmux_session(task_id)]


def writer(config: Config, task: Task | None = None) -> tuple[str, str]:
    role = role_of(task, "writer", config)
    if role.harness != opencode.NAME:
        raise ConfigError(
            f"the writer runs on '{role.harness}', and only opencode can write yet. "
            "Put claude-code on the planner instead."
        )
    return role.harness, role.model


def role_of(task: Task | None, role_name: str, config: Config | None = None) -> Role:
    """A role as this task runs it: the configured one, on the model the task chose if it chose one.
    Every reader comes through here, so an override cannot apply in one place and not another."""
    role = (config or load_config()).roles[role_name]
    if not task:
        return role
    st = task.read_state()
    harness, model = st.harnesses.get(role_name, ""), st.models.get(role_name, "")
    if harness:
        return Role(harness, model)
    return replace(role, model=model) if model else role


# A choice for a role: the harness it runs in and the model, "" for a manual role.
Choice = tuple[str, str]
MODELS_CACHE_SECONDS = 24 * 3600


def models_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "vivibox" / "models.json"


# The providers most people come for, first; the rest follow by name.
POPULAR = ("anthropic", "openai", "google", "deepseek", "openrouter", "mistral", "xai", "groq")


def fetch_provider_catalog() -> list[tuple[str, str]]:
    """Every provider opencode knows, as (id, name): opencode fetches the list from models.dev
    when it first lists models, and keeps it where a throwaway container can print it."""
    cmd = ["docker", "run", "--rm", "--tmpfs", f"/config:uid={os.getuid()},gid={os.getgid()}",
           image.image_ref(), "sh", "-c",
           "opencode models >/dev/null 2>&1; cat /config/.cache/opencode/models.json"]  # fmt: skip
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return []
    found = [(pid, str(d.get("name") or pid)) for pid, d in data.items() if isinstance(d, dict)]
    return sorted(
        found,
        key=lambda f: (f[0] not in POPULAR, POPULAR.index(f[0]) if f[0] in POPULAR else 0, f[1].casefold()),
    )


def provider_catalog(refresh: bool = False) -> list[tuple[str, str]]:
    """The providers to pick from when adding one; kept for a day, and the last list kept when
    opencode cannot reach models.dev. Empty when it never could: then you type the name."""
    path = models_cache().with_name("providers.json")
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = {}
    if not refresh and cached.get("providers") and time.time() - cached.get("at", 0) < MODELS_CACHE_SECONDS:
        return [tuple(p) for p in cached["providers"]]
    try:
        found = fetch_provider_catalog()
    except (OSError, subprocess.SubprocessError):
        found = []
    if not found:
        return [tuple(p) for p in cached.get("providers", [])]
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "providers": found}) + "\n")
    return found


def provider_models(provider: str) -> list[str]:
    """What opencode knows for a provider, asked in a throwaway container: a second or two."""
    env = ["-e", f"{provider.upper().replace('-', '_').replace('.', '_')}_API_KEY=placeholder"]
    cmd = ["docker", "run", "--rm", "--tmpfs", f"/config:uid={os.getuid()},gid={os.getgid()}", *env,
           image.image_ref(), "opencode", "models", provider]  # fmt: skip
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    return [line.strip() for line in p.stdout.splitlines() if line.strip().startswith(f"{provider}/")]


def available_models(refresh: bool = False) -> dict[str, list[str]]:
    """The models of every provider you have a key for, by provider. Kept for a day: a list that
    changes a few times a year is not worth a container each time you open a dialog."""
    path = models_cache()
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = {}
    # Your own providers list their models themselves; opencode knows nothing of them.
    on = lambda name: providers.enabled(providers.PROVIDER, name)  # noqa: E731
    found = {name: listed for name, listed in providers.models().items() if listed and on(name)}
    now_ = time.time()
    for provider in keys.list_keys():
        if provider in found or not on(provider) or providers.is_mcp_secret(provider):
            continue
        entry = cached.get(provider) or {}
        if not refresh and entry.get("models") and now_ - entry.get("at", 0) < MODELS_CACHE_SECONDS:
            found[provider] = entry["models"]
            continue
        try:
            listed = provider_models(provider)
        except (OSError, subprocess.SubprocessError):
            listed = []
        found[provider] = listed or entry.get("models") or []
        if listed:
            cached[provider] = {"at": now_, "models": listed}
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cached, indent=2) + "\n")
    return found


def choices(role_name: str, config: Config, available: dict[str, list[str]] | None = None) -> list[Choice]:
    """What a role can run on: planning yourself (the planner), a model of each provider you have
    a key for through opencode, and Anthropic's through Claude Code. config.toml's own choice
    comes first, and is there even when the list could not be read."""
    found: list[Choice] = [configured_choice(config, role_name)]
    if role_name == "planner":
        found.append((manual.NAME, ""))
    for provider, models in (available or {}).items():
        found += [(opencode.NAME, m) for m in models]
        if provider == "anthropic" and role_name != "writer":  # only opencode can write yet
            found += [(claudecode.NAME, m.split("/", 1)[1]) for m in models]
    # The models your roles already name, for when the list could not be read.
    found += [(r.harness, r.model) for r in config.roles.values() if r.harness == opencode.NAME]
    if role_name == "writer":
        found = [c for c in found if c[0] == opencode.NAME]
    # A role with no model yet shows that it has none, and no model of its own twice.
    found = [c for c in found if c[1] or c[0] == manual.NAME or c == found[0]]
    return list(dict.fromkeys(found))


def needs_provider(config: Config) -> bool:
    """True on a first run: no provider set up and a role without a model. A task's lists would
    have nothing to pick, so the view sends you to k instead of opening them."""
    if keys.list_keys() or providers.load():
        return False
    return any(r.harness != manual.NAME and not r.model for r in config.roles.values())


def configured_choice(config: Config, role_name: str) -> Choice:
    role = config.roles[role_name]
    return role.harness, role.model if role.harness != manual.NAME else ""


def choice_label(choice: Choice, config_choice: Choice | None = None) -> str:
    harness, model = choice
    if not model and harness != manual.NAME:
        return "no model yet: pick one below"
    text = "you, in your own chat" if harness == manual.NAME else model
    if harness == claudecode.NAME:
        text += " (Claude Code)"
    # config.toml's own choice. Named after the file it read "you, in your own chat · config.toml",
    # as if the chat were in the file.
    return text + ("  (default)" if choice == config_choice else "")


def parse_choice(text: str) -> Choice:
    """A choice as the command line takes it: manual, provider/model for opencode, or a Claude
    model id (no provider) for Claude Code."""
    text = text.strip()
    if text == manual.NAME:
        return manual.NAME, ""
    return (opencode.NAME, text) if "/" in text else (claudecode.NAME, text)


def models_offered(config: Config | None = None, harness: str = "") -> list[str]:
    """What a role can be put on without asking a provider: the models your own roles name. Asking
    opencode means a container per keypress, and nobody wants to scroll two hundred model ids."""
    seen: list[str] = []
    for role in (config or load_config()).roles.values():
        # A manual role's model is a label for a chat of yours, not something a harness can run.
        if role.harness != manual.NAME and role.model not in seen and harness in ("", role.harness):
            seen.append(role.model)
    return seen


def harness_for(role_name: str, pod: Pod, task: Task | None = None) -> object:
    """The tool a role talks through. Two roles on the same harness share nothing but the pod."""
    role = role_of(task, role_name)
    if role.harness == manual.NAME:
        return manual.Manual()
    if role.harness == claudecode.NAME:
        return claudecode.ClaudeCode(pod, role.model)
    return opencode.OpenCode(pod, role.model)


def provider_keys(config: Config, task: Task | None = None) -> list[str]:
    """Every metered provider a role needs, on the models this task runs them on: a task moved to
    another provider's model needs that provider's key, not the one config.toml's model uses."""
    found = []
    for name, role in ((n, role_of(task, n, config)) for n in config.roles):
        if not role.model and role.harness == opencode.NAME:
            raise ConfigError(f"the {name} has no model yet; pick one when you create a task, or press m")
        if role.harness == opencode.NAME and (p := opencode.provider_of(role.model)) not in found:
            if not providers.keyless(p):
                found.append(p)
        elif role.harness == claudecode.NAME and "anthropic" not in found:
            found.append("anthropic")
    return found


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


def project_problem(name: str) -> str:
    """Why no task of this project could start now, in a line; "" when they could. Shown on the
    project itself, so it is known before a task is created, not from a message that is gone
    in seconds after one fails to start."""
    try:
        project = load_project(name)
    except ConfigError as e:
        return str(e.args[0] if e.args else e)
    if not project.repo.expanduser().is_dir():
        return f"{project.repo} is gone"
    if missing := [v for v in project.pass_env if v not in os.environ]:
        return f"{', '.join(missing)} not set in this shell"
    return ""


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
    found = project_init.Detected(
        name, top, [c.strip() for c in verify if c.strip()], project_init.detect_demo(top), java
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(project_init.render(found))
    return target


# --- running the project for you to look at -------------------------------------------------------

DEMO_FILE = "demo.md"
# Its own question and its own session: the supervisor watches handoff/question.md to decide when a
# task stops for you, so working out how to run something must never be able to halt the work itself.
DEMO_QUESTION = "demo-question.md"
DEMO_SESSION = "demo-session"
BASH_BLOCK = re.compile(r"```(?:bash|sh|shell)\n(.*?)```", re.DOTALL)
# Longer than this and the agent has written documentation instead of an instruction.
MAX_INSTRUCTION = 3000

DEMO_ASK = """Work out how to start this project so a person can open it in a browser, and write
that down. Do not change what the project does.

Read the README and the build files first; they usually say. You may run commands to check that
what you found works.

Two things decide whether it is usable:
- The app must listen on 0.0.0.0. Bound to localhost it answers inside this container only, and
  nothing outside can reach it. Most dev servers bind localhost unless told otherwise.
- Anything the app needs to come up (a database, for instance) has to be started first. You have a
  Docker daemon here, so a container is a fine way to do that.

Write it to /task/handoff/demo.md as short markdown: the commands in ```bash blocks, in the order
they must run, with the app itself last. Around them put only what a person needs to know, in a
line or two. This is a note on how to start the project, not documentation: if it grows past a
screen, you have misunderstood it.

If a decision is mine rather than yours - which profile, which port, which of two ways the project
can be run - do not guess. Write the question to /task/handoff/demo-question.md and leave demo.md
alone. I will answer and you can carry on. End the turn when demo.md is written, or when the
question is."""


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


@dataclass
class Demo:
    """What happened when the project was started for you to look at."""

    commands: list[str] = field(default_factory=list)
    # Where it came from: this task's instruction, your project file, the repository, the agent.
    source: str = ""
    address: str = ""
    listening: list[pod_module.Listener] = field(default_factory=list)
    log: str = ""
    question: str = ""
    # An instruction from an earlier task, waiting for you to say it still applies.
    proposed: str = ""
    # Nothing listens yet, but the command is still running: a slow install is not a failure, and
    # calling it one sends you looking for a broken app instead of waiting a moment longer.
    starting: bool = False
    # What the agent had left listening in its container and was stopped to make room, so you
    # know what went and why the app you see is the current code.
    stopped: list[str] = field(default_factory=list)

    @property
    def urls(self) -> list[str]:
        return [f"http://{self.address}:{p.port}" for p in self.listening if p.reachable]

    @property
    def unreachable(self) -> list[pod_module.Listener]:
        return [p for p in self.listening if not p.reachable]


def instruction_commands(text: str) -> list[str]:
    """The commands of an instruction: its shell blocks, in order. The prose around them is yours."""
    return [line for block in BASH_BLOCK.findall(text) for line in block.strip().splitlines() if line.strip()]


def demo_instruction(task: Task) -> str:
    return _read(task.meta / "handoff" / DEMO_FILE)


def write_instruction(task: Task, text: str) -> Path:
    path = task.meta / "handoff" / DEMO_FILE
    path.write_text(text.rstrip() + "\n")
    return path


def demo_from_history(project_name: str) -> str:
    """How the last task you accepted in this project was run. A record, never run on its own."""
    for entry in history(limit=50):
        if entry["project"] == project_name and entry.get("demo") and not entry.get("deleted"):
            return entry["demo"]
    return ""


def demo_commands(project: Project, task: Task) -> tuple[list[str], str]:
    """This task's own instruction first, then what you set, then the repository's own answer."""
    if found := instruction_commands(demo_instruction(task)):
        return found, "task"
    if project.demo:
        return list(project.demo), "project"
    if found := project_init.detect_demo(task.repo):
        return found, "compose"
    return [], ""


def ask_agent_how_to_run(task: Task, pod: Pod, reply: str = "") -> tuple[str, str]:
    """A conversation of its own, in a session of its own, so that neither its questions nor its
    failures can touch the task's state. Returns the instruction it wrote and anything it asks."""
    handoff = task.meta / "handoff"
    (handoff / DEMO_QUESTION).unlink(missing_ok=True)
    kept = handoff / DEMO_SESSION
    session = _read(kept) if reply else ""
    turn = opencode.OpenCode(pod).turn(reply or DEMO_ASK, session=session, title=f"{task.id}-demo")
    if turn.session:
        kept.write_text(turn.session)
    task.event("turn", cost=turn.cost, tokens=turn.tokens, kind="demo")
    if not turn.ok:
        raise PodError(f"the agent could not work out how to run this: {turn.error or turn.text}")
    text = demo_instruction(task)
    if len(text) > MAX_INSTRUCTION:
        raise PodError(
            f"the agent wrote {len(text)} characters of instruction; that is documentation, not a "
            f"note on how to start the project. Ask it again, or write {DEMO_FILE} yourself"
        )
    return text, _read(handoff / DEMO_QUESTION)


def demo_allowed(st: TaskState) -> bool:
    """Whether the project may be run in the task's pod now: in a box, or once the work is back with
    you. While the agent implements or the gate verifies, the pod and its working tree are theirs:
    a second build on the same tree, or a server on a port their tests want, would get in the way.
    The view's v and the demo command ask here."""
    return bool(st.box) or st.state in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED)


def demo(task_id: str, ask: bool = True, reply: str = "", wait: float = 40) -> Demo:
    """Runs the project in the task's pod and watches for it to listen. Nothing here moves the task
    between states: working out how to run something must not be able to stop the work."""
    task, project = load(task_id)
    if not demo_allowed(task.read_state()):
        raise gate.GateError(
            f"could not run the app: the agent is at work in the pod of {task_id}; "
            "run it once the work is back with you"
        )
    pod = task_pod(task_id)
    pod.up()
    commands, source = demo_commands(project, task)
    question = ""
    if not commands and (earlier := demo_from_history(project.name)) and not reply:
        return Demo(source="history", address=pod.address(), proposed=earlier)
    if not commands and (ask or reply):
        _, question = ask_agent_how_to_run(task, pod, reply)
        commands, source = instruction_commands(demo_instruction(task)), "agent"
    if not commands:
        return Demo([], source, pod.address(), question=question)
    stopped = pod.stop_leftovers()
    heard = pod.demo_start(commands, workdir=str(task.repo), wait=wait)
    still_going = not heard and pod.demo_running()
    return Demo(
        commands,
        source,
        pod.address(),
        heard,
        pod.demo_log(),
        question,
        starting=still_going,
        stopped=stopped,
    )


def use_instruction(task_id: str, text: str, wait: float = 40) -> Demo:
    """Takes on an instruction you have read and approved, for this task only."""
    task, project = load(task_id)
    write_instruction(task, text)
    return demo(task_id, ask=False, wait=wait)


def demo_stop(task_id: str) -> None:
    task_pod(task_id).demo_stop()


# --- the box: the pod without a task or an agent ------------------------------------------------

BOX_GOAL = "Box"
BOX_COMMIT = "Work in the box"


def projects_named() -> list[str]:
    return [p.stem for p in project_files()]


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
    task = create_task(config.tasks_dir, project.name, BOX_GOAL, plan, after=used_numbers(project))
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
        if providers.enabled(providers.PROVIDER, name) and not providers.is_mcp_secret(name)
    ]


def start_box(task_id: str) -> None:
    """The pod up, with your keys and opencode's configuration in it, as for a task's agent."""
    config = load_config()
    task, project = load(task_id)
    pod = task_pod(task.id)
    if missing := pod.missing_env():
        raise PodError(f"{', '.join(missing)} not set: the {project.name} project passes them (pass_env)")
    if not image.exists(image.image_ref()):
        raise PodError("the agent image is not built; run 'vivibox image build'")
    used = box_providers(task)
    secrets.prepare(task.id, used + providers.mcp_secrets())
    model = role_of(task, "writer", config).model
    opencode.prepare(task, model, project.verify, used)
    pod.up()
    toolchain.ensure(pod, project.java)
    if task.read_state().paused:
        task.set_paused(False)
    task.event("started", model="you")


def box_shell_command(task_id: str) -> list[str]:
    """A shell in the box, in the clone, through tmux like the agent's window: Ctrl-q leaves,
    the box stays."""
    task, _ = load(task_id)
    if task.read_state().paused:
        raise PodError(f"{task_id} is stopped; start it again with: vivibox start {task_id}")
    pod = task_pod(task_id)
    agent_view(task, ["docker", "exec", "-it", "-w", str(task.repo), pod.agent, "bash", "-l"])
    return [*TMUX, "attach-session", "-t", tmux_session(task_id)]


def commit_in_box(task: Task) -> None:
    """What you left uncommitted is committed, in the pod, as you: accepting flattens the work
    to uncommitted changes in your checkout anyway, so the message is never seen."""
    pod = task_pod(task.id)
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
    return prepare_review(task, project)


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
) -> Task:
    """description: one line, or a whole ticket; it all goes into the plan the agent starts from.
    @path mentions in it are copied into the task (relative ones from cwd). roles: what a role runs
    on for this task only, as m would set it; config.toml's own choice is no choice at all."""
    config = load_config()
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
    task = create_task(config.tasks_dir, project.name, title, plan, after=used_numbers(project))
    try:
        described = context.attach(description.strip(), found, task.meta / "context", clone=task.repo)
        task.plan_path.write_text(plan.replace("{{goal}}", described))
        base = repo.prepare(project.repo, task.repo, task.id, task.meta)
    except BaseException:
        shutil.rmtree(task.root, ignore_errors=True)
        raise
    task.set_base_commit(base)
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


def start(task_id: str, resume: bool = False, on_step: Callable[[str], None] = lambda step: None) -> str:
    """Starts or resumes the task's pod, agent and supervisor. Returns the model. A start that
    fails leaves its reason with the task: the message you get once is gone in seconds, and the row
    would go on saying "not started" with nothing to say why. on_step hears each step before it
    runs, for a row to say what a slow start is doing."""
    task, _ = load(task_id)
    try:
        if task.read_state().box:
            start_box(task_id)
            return "you"
        return _start(task_id, resume, on_step)
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


def _start(task_id: str, resume: bool = False, on_step: Callable[[str], None] = lambda step: None) -> str:
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
    if not image.exists(image.image_ref()):
        raise PodError("the agent image is not built; run 'vivibox image build'")
    secrets.prepare(task.id, provider_keys(config, task) + providers.mcp_secrets())
    used = [opencode.provider_of(r.model) for r in (role_of(task, n, config) for n in config.roles)
            if r.harness == opencode.NAME]  # fmt: skip
    changed = opencode.prepare(task, model, project.verify, used)
    on_step("starting the pod…")
    pod.up()
    if project.java:
        on_step(f"installing Java {project.java}…")
    toolchain.ensure(pod, project.java)
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
        supervisor.set_next_prompt(task, supervisor.resume_prompt(st.state))
    if st.paused:
        task.set_paused(False)
    task.set_problem("")  # whatever kept it from starting before did not this time
    start_supervisor(task)
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
    supervisor.accept_plan(
        task,
        "plan accepted",
        project.verify,
        lambda commands, no_build: save_verify(project, commands, no_build),
        project.no_build,
    )


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


def save_verify(project: Project, commands: list[str], no_build: bool = False) -> None:
    """Keeps what the plan chose, or what you picked, so the project's next task does not decide
    again: the commands, or that there is nothing to build (verify = false)."""
    path = config_dir() / "projects" / f"{project.name}.toml"
    text = path.read_text()
    line = "verify = false" if no_build else "verify = [" + ", ".join(f'"{c}"' for c in commands) + "]"
    path.write_text(re.sub(r"^verify = (\[.*?\]|false)", line, text, count=1, flags=re.MULTILINE | re.DOTALL))


def verify_commands(task: Task, project: Project) -> list[str]:
    """The project's commands, or the ones the plan you accepted brought for a new project."""
    accepted = task.meta / gate.ACCEPTED_PLAN
    if project.verify or project.no_build or not accepted.exists():
        return project.verify
    return parse_plan(accepted.read_text()).verify


def verify_from_plan(task: Task, project: Project) -> str:
    """What accepting this plan settles for the project from now on, for you to see first: the
    command it sets, in backticks, or that there is no build. "" when the project has that
    settled already, or the plan sets nothing."""
    if project.verify or project.no_build:
        return ""
    try:
        plan = parse_plan(task.plan_path.read_text())
    except PlanError:
        return ""
    if plan.no_build:
        return NO_BUILD
    return ", ".join(f"`{c}`" for c in plan.verify)


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
    prompt = supervisor.PLAN_COMMENT_PROMPT if target is State.PLAN else supervisor.COMMENT_PROMPT
    supervisor.set_next_prompt(task, prompt)
    task.reset_iterations()
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
    supervisor.put_question_away(task)
    task.transition(State.VERIFY, reason="verify again")


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
    "handoff/" + DEMO_FILE,
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
        "title": done.message or f"Box in {project.name}",
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
    # A box's "Work in the box" is not a message for your history: that one is yours to write.
    message = "" if st.box else suggested_message(project.repo, st.base_commit, commit, st.goal)
    done = Finished(task.id, project.repo, spent, message)
    done.criteria = accepted_criteria(task)
    done.created = st.created
    done.demo = demo_instruction(task)
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
    remove(task, project, accepted=True)
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
