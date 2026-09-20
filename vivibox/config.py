"""Machine and project configuration. Lives outside the repo, in ~/.config/vivibox/."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HARNESSES = ("opencode", "claude-code")
JAVA = re.compile(r"^([a-z]+-)?[0-9][0-9.]*$|^$")
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


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
    # A JDK other than the image's Java 21, as a mise version: "17" means Corretto 17.
    java: str = ""
    # The editor for this project's review copies, when it differs from the one in config.toml.
    ide: str = ""


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
        raise ConfigError(f"Missing {path} (see templates/ for an example)") from None
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
    roles = {}
    for name, role in _expect(data, "roles", dict, path).items():
        harness = role.get("harness")
        if harness not in HARNESSES:
            raise ConfigError(f"{path}: roles.{name}.harness must be one of {HARNESSES}")
        if not isinstance(role.get("model"), str):
            raise ConfigError(f"{path}: roles.{name}.model must be a string")
        roles[name] = Role(harness, role["model"])
    if "writer" not in roles:
        raise ConfigError(f"{path}: missing role 'writer'")
    desktop = data.get("notifications", {}).get("desktop", True)
    if not isinstance(desktop, bool):
        raise ConfigError(f"{path}: notifications.desktop must be true or false")
    ide = data.get("review", {}).get("ide", "")
    if not isinstance(ide, str):
        raise ConfigError(f'{path}: review.ide must be a command, e.g. "idea"')
    return Config(tasks_dir, max_iterations, roles, desktop, ide)


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
    # Empty for a project that does not exist yet: the plan you accept sets it (see plan.verify).
    verify = _expect(data, "verify", list, path)
    if not all(isinstance(c, str) and c.strip() for c in verify):
        raise ConfigError(f"{path}: verify must be a list of commands")
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
    return Project(name, repo, verify, risky_extra, services, java, ide)
