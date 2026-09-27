"""Machine and project configuration. Lives outside the repo, in ~/.config/vivibox/."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# manual: you plan in a chat of your own and vivibox takes the plan you paste (see manual.py).
HARNESSES = ("opencode", "claude-code", "manual")
# TEST-NET-2 (RFC 5737): reserved for documentation, so no real network and no product uses it.
DEFAULT_NETWORK_POOL = "198.51.100.0/24"
# What goes to ntfy: what the desktop gets (decisions), or every stage of a task too.
NTFY_LEVELS = ("decisions", "all")
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


@dataclass(frozen=True)
class Orchestration:
    """One way of sharing a task between the roles: what the view and the docs say about it, in
    the order the view shows it. P, W and R are the planner, the writer and the reviewer, Gate the
    verification; roles joined with + are one agent: one session, one model."""

    label: str  # the mode's name in the view
    subtitle: str  # a few words beside the name, to compare the four in the list
    flow: str  # where each agent works and where the verification runs, in P, W, R and Gate
    summary: str  # what happens, in a sentence
    sessions: str
    review: str
    models: str  # the usual choice of models, whatever the ones picked
    rounds: str  # what sends the writer back for a fix turn, the thing limits.max_rounds counts
    best_for: str
    tradeoff: str
    # The agents, in the order they work: the role whose model runs the agent, the agent's name
    # in the view, and the roles it plays when it plays more than one.
    agents: tuple[tuple[str, str, str], ...]
    # The mode in a line, for a short terminal.
    compact: str


# How a task is shared between the planner, the writer and the reviewer, and where the
# verification runs. Roles joined with + are one agent in one session, on the model of the
# first of them; ⇄ is rounds of fixes, up to limits.max_rounds.
ORCHESTRATION_MODES = {
    "single_agent": Orchestration(
        "Single agent",
        "1 session · reviews its own work",
        "P+W+R → Gate",
        "One agent plans, implements and reviews its own work, in one session.",
        "1, shared by the planner, the writer and the reviewer",
        "Its own, before every Gate",
        "One model for everything: the agent's, an opencode model",
        "a failed verification",
        "small, routine tasks",
        "The least orchestration, but nobody else questions its assumptions.",
        (("planner", "Agent", "planner + writer + reviewer"),),
        "1 session · self-review · one model",
    ),
    "planner_executor": Orchestration(
        "Planner → Executor",
        "2 sessions · strong plan, self-review",
        "P → W+R → Gate",
        "A planner plans; one executor implements and reviews its own work.",
        "2: the planner; the executor, writer and reviewer in one",
        "The executor's own, before every Gate",
        "A strong planner, used once; a cheaper executor",
        "a failed verification",
        "routine work that needs a good plan",
        "A strong plan without an independent reviewer.",
        (("planner", "Planner", ""), ("writer", "Executor", "writer + reviewer")),
        "2 sessions · strong planner once · executor self-reviews",
    ),
    "planner_maker_checker": Orchestration(
        "Planner → Writer → Reviewer",
        "3 sessions · independent review",
        "P → W → Gate → R ⇄ W",
        "Three agents: plan, implement, and review only work that passed the Gate.",
        "3, independent",
        "Independent, after Gate passes",
        "A strong planner, used once; a cheaper writer; a reviewer of another family",
        "a failed verification or blocking reviewer notes",
        "most larger programming tasks",
        "The most turns per task, and the most checks.",
        (("planner", "Planner", ""), ("writer", "Writer", ""), ("reviewer", "Reviewer", "")),
        "3 sessions · independent review after Gate · strong planner once",
    ),
    "supervisor_worker": Orchestration(
        "Supervisor ⇄ Worker",
        "2 sessions · strong model every round",
        "P → W → Gate → (P+R) ⇄ W",
        "A strong supervisor plans, then reviews every round of a separate worker.",
        "2: the supervisor, planner and reviewer in one; the worker",
        "The supervisor's, after Gate passes; the strong model every round",
        "A strong supervisor, used again every round; a cheaper worker",
        "a failed verification or blocking supervisor notes",
        "hard, multi-step changes and refactors",
        "Guidance with the plan in mind; the strong model is spent every round.",
        (("planner", "Supervisor", "planner + reviewer"), ("writer", "Worker", "")),
        "2 sessions · supervisor reviews after Gate · strong model every round",
    ),
}
DEFAULT_ORCHESTRATION = "planner_maker_checker"
ORCHESTRATION_LEGEND = (
    "P planner · W writer · R reviewer · Gate the verification (build, tests, criteria, commits) · "
    "+ one agent, one session, one model · → then · ⇄ rounds of fixes, up to Rounds"
)
DEFAULT_MAX_ROUNDS = 3
# What the view says of limits.max_rounds.
ROUNDS_HELP = (
    "Fix turns the writer gets on its own, from the gate or from the review, before the work "
    "comes to you; your reply gives it as many again."
)
# A topic is a name: letters, digits, - and _, as ntfy has it.
NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# One task needs one address, for its sidecar; the agent and the gate share that container's network.
TASK_NETWORK_BITS = 28
JAVA = re.compile(r"^([a-z]+-)?[0-9][0-9.]*$|^$")
# A toolchain as mise names it, <tool>@<version>: "go@1.25.3", "rust@stable".
TOOL = re.compile(r"^[a-z][a-z0-9-]*@[A-Za-z0-9][A-Za-z0-9.+_-]*$")
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Set by vivibox in the pod: passing your own value would break the pod, not configure the project.
RESERVED_ENV = {
    "DOCKER_HOST",
    "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE",
    "TESTCONTAINERS_HOST_OVERRIDE",
    "HUSKY",
    "OPENCODE_CONFIG",
    "CLAUDE_CONFIG_DIR",
    "JAVA_HOME",
    "PATH",
    "HOME",
}


DEFAULT_VERIFY_TIMEOUT = 1800


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Role:
    harness: str
    model: str


@dataclass(frozen=True)
class Config:
    tasks_dir: Path
    # Fix turns the writer gets on its own, from the gate or from the review, before the work
    # comes to you (limits.max_rounds).
    max_rounds: int
    roles: dict[str, Role]
    desktop_notifications: bool = True
    # Command that opens a directory in your IDE ("idea", "code"); offered when work is ready for review.
    ide: str = ""
    # Addresses the task networks are cut from, one /28 per task. The default is TEST-NET-2, which
    # RFC 5737 reserves for documentation: nothing may use it in a real network, so nothing collides.
    network_pool: str = DEFAULT_NETWORK_POOL
    # Seconds one verification command may take before it is stopped and counted as a failure of
    # the environment, not of the code.
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT
    # Dollars a task may cost before you are told, and before it stops for you; 0 is no limit.
    cost_warning: float = 0.0
    cost_limit: float = 0.0
    # How a task is shared between the roles (agent_orchestration_mode, one of ORCHESTRATION_MODES).
    orchestration: str = DEFAULT_ORCHESTRATION
    # Keys the file still has from before, in one line to say once; "" when there are none.
    notice: str = ""
    # The ntfy topic the supervisor's messages go to as well ("" for none), on which server, and
    # which of them.
    ntfy: str = ""
    ntfy_server: str = DEFAULT_NTFY_SERVER
    ntfy_events: str = "decisions"


@dataclass(frozen=True)
class HostService:
    host: str
    port: int


@dataclass(frozen=True)
class Project:
    name: str
    repo: Path
    verify: list[str]
    risky_extra: list[str] = field(default_factory=list)
    host_services: list[HostService] = field(default_factory=list)
    # How to run the project so you can look at it, in order; the last one is the app itself.
    demo: list[str] = field(default_factory=list)
    # A JDK other than the image's Java 21, as a mise version: "17" means Corretto 17.
    java: str = ""
    # The editor for this project's review copies, when it differs from the one in config.toml.
    ide: str = ""
    # Variables the build needs from your shell, e.g. a package registry token your login sets:
    # the agent and the gate get their values from the environment vivibox was started in.
    pass_env: list[str] = field(default_factory=list)
    # This project's time limit for one verification command; 0 means config.toml's.
    verify_timeout: int = 0
    # There is nothing to build or test here (verify = false): the gate checks the rest.
    no_build: bool = False
    # Commands run once in a task's clone while the plan is made, e.g. an install without tests, so
    # the writer builds one module instead of the whole project; its first turn waits for them.
    prepare: list[str] = field(default_factory=list)
    # Toolchains the image does not have, as mise versions ("go@1.25.3"), installed in the pod for
    # the agent and for the gate; nothing is written into the repository.
    tools: list[str] = field(default_factory=list)


def config_dir() -> Path:
    if env := os.environ.get("VIVIBOX_CONFIG_DIR"):
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "vivibox"


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Missing {path}; run vivibox to set it up") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def _expect(data: dict, key: str, kind: type, where: Path):
    value = data.get(key)
    if not isinstance(value, kind):
        raise ConfigError(f"{where}: '{key}' must be a {kind.__name__}")
    return value


def load_config(base: Path | None = None) -> Config:
    path = (base or config_dir()) / "config.toml"
    data = _read_toml(path)
    tasks_dir = Path(_expect(data, "tasks_dir", str, path))
    if not tasks_dir.is_absolute():
        raise ConfigError(f"{path}: tasks_dir must be an absolute path")
    limits = data.get("limits", {})
    old = [f"limits.{k}" for k in ("max_iterations", "max_reviews") if k in limits]
    # A limit from before there was one: its figure carries over, so a task fixes as often as it did.
    max_rounds = limits.get("max_rounds", limits.get("max_iterations", DEFAULT_MAX_ROUNDS))
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
        raise ConfigError(f"{path}: limits.max_rounds must be an integer >= 1")
    orchestration = data.get("agent_orchestration_mode", DEFAULT_ORCHESTRATION)
    if orchestration not in ORCHESTRATION_MODES:
        raise ConfigError(f"{path}: agent_orchestration_mode must be one of {', '.join(ORCHESTRATION_MODES)}")
    verify_timeout = data.get("limits", {}).get("verify_timeout", DEFAULT_VERIFY_TIMEOUT)
    if not isinstance(verify_timeout, int) or verify_timeout < 1:
        raise ConfigError(f"{path}: limits.verify_timeout must be a number of seconds >= 1")
    costs = {}
    for name in ("cost_warning", "cost_limit"):
        value = data.get("limits", {}).get(name, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{path}: limits.{name} is dollars per task, e.g. 2.5; 0 for none")
        costs[name] = float(value)
    roles = {}
    for name, role in _expect(data, "roles", dict, path).items():
        harness = role.get("harness")
        if harness not in HARNESSES:
            raise ConfigError(f"{path}: roles.{name}.harness must be one of {HARNESSES}")
        # A manual role runs no model of vivibox's; its model is only a label, and optional.
        if harness == "manual":
            role.setdefault("model", "")
        if not isinstance(role.get("model"), str):
            raise ConfigError(f"{path}: roles.{name}.model must be a string")
        if "auth" in role:
            # Said rather than ignored: a config that still reads auth = "subscription" would
            # otherwise run on a key while you believe it runs on your plan.
            raise ConfigError(
                f"{path}: roles.{name}.auth is gone. vivibox does not use subscription logins; "
                "claude-code runs on an Anthropic API key (vivibox auth set anthropic). To plan "
                'with your subscription, use harness = "manual" and plan in your own chat.'
            )
        if name == "reviewer":
            if harness != "opencode":
                raise ConfigError(
                    f"{path}: roles.reviewer.harness must be opencode: the reviewer reads in a pod"
                )
            if "mode" in role:
                old.append("roles.reviewer.mode")
        roles[name] = Role(harness, role["model"])
    notice = (
        f"{path}: {', '.join(old)} is from before orchestration modes and is not read; the limit is"
        f" limits.max_rounds ({max_rounds}) and the flow is agent_orchestration_mode ({orchestration})"
        if old
        else ""
    )
    for needed in ("planner", "writer"):
        if needed not in roles:
            raise ConfigError(
                f"{path}: missing role '{needed}'. Planning and writing are chosen separately so "
                "the model that decides need not be the model that types."
            )
    notifications = data.get("notifications", {})
    desktop = notifications.get("desktop", True)
    if not isinstance(desktop, bool):
        raise ConfigError(f"{path}: notifications.desktop must be true or false")
    ntfy = notifications.get("ntfy", "")
    if not isinstance(ntfy, str) or (ntfy and not NTFY_TOPIC.match(ntfy)):
        raise ConfigError(f"{path}: notifications.ntfy is a topic's name: letters, digits, - and _")
    ntfy_server = notifications.get("ntfy_server", DEFAULT_NTFY_SERVER)
    if not isinstance(ntfy_server, str) or not ntfy_server.startswith(("https://", "http://")):
        raise ConfigError(f'{path}: notifications.ntfy_server is an address, e.g. "{DEFAULT_NTFY_SERVER}"')
    ntfy_events = notifications.get("ntfy_events", NTFY_LEVELS[0])
    if ntfy_events not in NTFY_LEVELS:
        raise ConfigError(f"{path}: notifications.ntfy_events must be one of {', '.join(NTFY_LEVELS)}")
    ide = data.get("review", {}).get("ide", "")
    if not isinstance(ide, str):
        raise ConfigError(f'{path}: review.ide must be a command, e.g. "idea"')
    pool = data.get("network", {}).get("pool", DEFAULT_NETWORK_POOL)
    if not isinstance(pool, str):
        raise ConfigError(f'{path}: network.pool must be a range, e.g. "{DEFAULT_NETWORK_POOL}"')
    try:
        parsed = ipaddress.IPv4Network(pool, strict=True)
    except ValueError as e:
        raise ConfigError(f"{path}: network.pool: {e}") from None
    if parsed.prefixlen > TASK_NETWORK_BITS:
        raise ConfigError(
            f"{path}: network.pool {pool} is smaller than the /{TASK_NETWORK_BITS} one task needs"
        )
    return Config(
        tasks_dir,
        max_rounds,
        roles,
        desktop,
        ide,
        str(parsed),
        verify_timeout=verify_timeout,
        ntfy=ntfy,
        orchestration=orchestration,
        notice=notice,
        **costs,
        ntfy_server=ntfy_server.rstrip("/"),
        ntfy_events=ntfy_events,
    )


def _host_service(text: str, where: Path) -> HostService:
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"{where}: host_services: '{text}' is not host:port")
    return HostService(host, int(port))


def load_project(name: str, base: Path | None = None) -> Project:
    if not PROJECT_NAME.match(name):
        raise ConfigError(f"Project name '{name}': lowercase letters, digits and '-', up to 31 characters")
    path = (base or config_dir()) / "projects" / f"{name}.toml"
    data = _read_toml(path)
    repo = Path(_expect(data, "repo", str, path)).expanduser()
    # Empty for a project that does not exist yet: the plan you accept sets it (see plan.verify);
    # false for one with nothing to build or test.
    no_build = data.get("verify") is False
    verify = [] if no_build else _expect(data, "verify", list, path)
    if not all(isinstance(c, str) and c.strip() for c in verify):
        raise ConfigError(f"{path}: verify must be a list of commands, or false")
    risky_extra = data.get("risky_extra", [])
    if not all(isinstance(p, str) for p in risky_extra):
        raise ConfigError(f"{path}: risky_extra must be a list of patterns")
    services = [_host_service(s, path) for s in data.get("host_services", [])]
    java = data.get("java", "")
    if not isinstance(java, str) or not JAVA.match(java):
        raise ConfigError(f'{path}: java must look like "17" or "temurin-17"')
    ide = data.get("ide", "")
    if not isinstance(ide, str):
        raise ConfigError(f'{path}: ide must be a command, e.g. "code {{path}}"')
    demo = data.get("demo", [])
    if not isinstance(demo, list) or not all(isinstance(c, str) and c.strip() for c in demo):
        raise ConfigError(f'{path}: demo must be a list of commands, e.g. ["npm run dev"]')
    prepare = data.get("prepare", [])
    if not isinstance(prepare, list) or not all(isinstance(c, str) and c.strip() for c in prepare):
        raise ConfigError(f'{path}: prepare must be a list of commands, e.g. ["mvn -B install -DskipTests"]')
    pass_env = data.get("pass_env", [])
    if not isinstance(pass_env, list) or not all(isinstance(n, str) and ENV_NAME.match(n) for n in pass_env):
        raise ConfigError(f'{path}: pass_env must be a list of variable names, e.g. ["NPM_TOKEN"]')
    if reserved := sorted(set(pass_env) & RESERVED_ENV):
        raise ConfigError(f"{path}: pass_env: vivibox sets {', '.join(reserved)} in the pod itself")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) and TOOL.match(t) for t in tools):
        raise ConfigError(f'{path}: tools must be a list of mise versions, e.g. ["go@1.25.3"]')
    verify_timeout = data.get("verify_timeout", 0)
    if not isinstance(verify_timeout, int) or verify_timeout < 0:
        raise ConfigError(f"{path}: verify_timeout must be a number of seconds")
    return Project(
        name, repo, verify, risky_extra, services, demo, java, ide, pass_env, verify_timeout, no_build,
        prepare, tools,
    )  # fmt: skip
