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
# How the reviewer works: blocking notes go back to the writer by themselves (loop), or every
# note comes to you (supervised).
REVIEW_MODES = ("loop", "supervised")
# A topic is a name: letters, digits, - and _, as ntfy has it.
NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# One task needs one address, for its sidecar; the agent and the gate share that container's network.
TASK_NETWORK_BITS = 28
JAVA = re.compile(r"^([a-z]+-)?[0-9][0-9.]*$|^$")
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
    max_iterations: int
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
    # The reviewer, when roles has one: how it works, and how many rounds of blocking notes go
    # back to the writer before the work comes to you as it is.
    review_mode: str = "loop"
    max_reviews: int = 2
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
    max_iterations = data.get("limits", {}).get("max_iterations", 3)
    if not isinstance(max_iterations, int) or max_iterations < 1:
        raise ConfigError(f"{path}: limits.max_iterations must be an integer >= 1")
    verify_timeout = data.get("limits", {}).get("verify_timeout", DEFAULT_VERIFY_TIMEOUT)
    if not isinstance(verify_timeout, int) or verify_timeout < 1:
        raise ConfigError(f"{path}: limits.verify_timeout must be a number of seconds >= 1")
    costs = {}
    for name in ("cost_warning", "cost_limit"):
        value = data.get("limits", {}).get(name, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{path}: limits.{name} is dollars per task, e.g. 2.5; 0 for none")
        costs[name] = float(value)
    max_reviews = data.get("limits", {}).get("max_reviews", 2)
    if not isinstance(max_reviews, int) or max_reviews < 1:
        raise ConfigError(f"{path}: limits.max_reviews must be an integer >= 1")
    review_mode = REVIEW_MODES[0]
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
            review_mode = role.get("mode", REVIEW_MODES[0])
            if review_mode not in REVIEW_MODES:
                raise ConfigError(f"{path}: roles.reviewer.mode must be one of {', '.join(REVIEW_MODES)}")
        roles[name] = Role(harness, role["model"])
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
        max_iterations,
        roles,
        desktop,
        ide,
        str(parsed),
        verify_timeout=verify_timeout,
        ntfy=ntfy,
        review_mode=review_mode,
        max_reviews=max_reviews,
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
    pass_env = data.get("pass_env", [])
    if not isinstance(pass_env, list) or not all(isinstance(n, str) and ENV_NAME.match(n) for n in pass_env):
        raise ConfigError(f'{path}: pass_env must be a list of variable names, e.g. ["NPM_TOKEN"]')
    if reserved := sorted(set(pass_env) & RESERVED_ENV):
        raise ConfigError(f"{path}: pass_env: vivibox sets {', '.join(reserved)} in the pod itself")
    verify_timeout = data.get("verify_timeout", 0)
    if not isinstance(verify_timeout, int) or verify_timeout < 0:
        raise ConfigError(f"{path}: verify_timeout must be a number of seconds")
    return Project(
        name, repo, verify, risky_extra, services, demo, java, ide, pass_env, verify_timeout, no_build
    )
