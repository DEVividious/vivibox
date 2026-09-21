"""What vivibox brings over from opencode: providers it does not know by itself, such as your
employer's endpoint, and MCP servers, as an opencode.json defines them.

    ~/.config/vivibox/providers.json    the providers, each as opencode defines one
    ~/.config/vivibox/mcp.json          the MCP servers, each as opencode defines one

Neither file holds a secret. A provider's key, and every value of an MCP server's headers and
environment, go to vivibox's key store; the definitions point at the copies mounted in a task's
pod ({file:/run/vivibox-secrets/...}), which opencode reads there. Endpoints stay in your config
directory, never in a repository.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import keys
from .config import ConfigError, config_dir
from .secrets import MOUNT

# Marks a provider whose endpoint takes no key, so a task is not refused for want of one.
KEYLESS = "keyless"
DEFAULT_SOURCE = Path("~/.config/opencode/opencode.json")
REFERENCE = re.compile(r"^\{(env|file):(.+)\}$")
MOUNTED = re.compile(rf"\{{file:{re.escape(MOUNT)}/([^}}]+)\}}")
PROVIDER, MCP = "provider", "mcp"
# Where the secrets of an MCP server sit in its definition.
MCP_SECRETS = ("headers", "environment")


def path() -> Path:
    return config_dir() / "providers.json"


def mcp_path() -> Path:
    return config_dir() / "mcp.json"


def _load(file: Path) -> dict[str, dict]:
    try:
        return json.loads(file.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ConfigError(f"{file}: {e}") from None


def _save(file: Path, data: dict) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2) + "\n")


def load() -> dict[str, dict]:
    return _load(path())


def load_mcp() -> dict[str, dict]:
    return _load(mcp_path())


def models() -> dict[str, list[str]]:
    """The models of each provider defined here, as opencode names them: provider/model."""
    return {name: [f"{name}/{m}" for m in d.get("models", {})] for name, d in load().items()}


def keyless(provider: str) -> bool:
    return bool(load().get(provider, {}).get(KEYLESS))


def definition(provider: str) -> dict:
    """The provider as a task's opencode.json gives it, without vivibox's own marks."""
    found = copy.deepcopy(load().get(provider, {}))
    found.pop(KEYLESS, None)
    return found


# MCP servers vivibox brings itself, installed in the agent image: off until you turn one on.
BUILTIN_MCP = {
    "serena": {
        "type": "local",
        "command": ["serena", "start-mcp-server", "--context", "ide", "--project", "/task/repo",
                    "--enable-web-dashboard", "false", "--open-web-dashboard", "false"],
    },
}  # fmt: skip


def state_path() -> Path:
    return config_dir() / "enabled.json"


def _state() -> dict:
    return _load(state_path())


def enabled(kind: str, name: str) -> bool:
    """Yours are on until you turn them off; vivibox's own MCP servers are off until you turn them on."""
    state = _state()
    if kind == MCP and name in BUILTIN_MCP:
        return name in state.get("builtin_on", [])
    return name not in state.get("off", {}).get(kind, [])


def set_enabled(kind: str, name: str, on: bool) -> None:
    state = _state()
    if kind == MCP and name in BUILTIN_MCP:
        names = set(state.get("builtin_on", []))
        state["builtin_on"] = sorted(names | {name} if on else names - {name})
    else:
        off = state.setdefault("off", {})
        names = set(off.get(kind, []))
        off[kind] = sorted(names - {name} if on else names | {name})
    _save(state_path(), state)


def task_mcp() -> dict[str, dict]:
    """The MCP servers a task gets: yours that are on, and vivibox's own you turned on."""
    found = {name: entry for name, entry in load_mcp().items() if enabled(MCP, name)}
    found |= {name: copy.deepcopy(entry) for name, entry in BUILTIN_MCP.items() if enabled(MCP, name)}
    return found


def is_mcp_secret(name: str) -> bool:
    """A key store entry of an MCP server, not a provider's key: their names start so."""
    return name.startswith("mcp.")


def mcp_secrets() -> list[str]:
    """The key store entries the MCP servers read in a task's pod."""
    return sorted(set(MOUNTED.findall(json.dumps(task_mcp()))))


def without_comments(text: str) -> str:
    """JSON from JSONC, which opencode reads: // and /* */ comments and trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


# --- finding opencode's configuration -----------------------------------------------------------


def opencode_candidates(repos: list[Path] = (), env: dict[str, str] | None = None) -> list[Path]:
    """Files an opencode configuration may be in: one just downloaded (from a portal that generates
    it), the file $OPENCODE_CONFIG names, the global one, and a project's own in its repository."""
    env = dict(os.environ) if env is None else env
    home = Path(env.get("HOME") or Path.home())
    downloads = home / "Downloads"
    found = []
    if downloads.is_dir():
        fresh = [p for p in downloads.glob("*opencode*") if p.suffix in (".json", ".jsonc") and p.is_file()]
        found += sorted(fresh, key=lambda p: p.stat().st_mtime, reverse=True)
    if env.get("OPENCODE_CONFIG"):
        found.append(Path(os.path.expanduser(env["OPENCODE_CONFIG"])))
    base = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    found += [base / "opencode.json", base / "opencode.jsonc", base / "config.json"]
    for repo in repos:
        found += [repo / "opencode.json", repo / "opencode.jsonc", repo / ".opencode" / "opencode.json"]
    return list(dict.fromkeys(found))


def discover(repos: list[Path] = (), env: dict[str, str] | None = None) -> list[tuple[Path, int]]:
    """The opencode configurations on this machine with something to bring over, with how much."""
    found = []
    for candidate in opencode_candidates(repos, env):
        if not candidate.is_file():
            continue
        try:
            found.append((candidate, len(read_opencode(candidate, env).found)))
        except ConfigError:
            continue  # nothing in it to bring over, or not readable: nothing to offer
    return found


# --- reading one --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """A provider or an MCP server of an opencode.json, read and not yet brought over."""

    kind: str
    name: str
    # What it is, in a few words: "3 models", "remote https://...", "local uvx ...".
    what: str
    # Where its secrets come from, or why there are none; never a secret itself.
    key: str
    # "new"; "replaces" when you have a different one by this name; "same" when you have this one.
    status: str
    entry: dict = field(repr=False)
    # Key store name -> value.
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def own(self) -> bool:
        """A provider with an endpoint or models of its own; otherwise it is opencode's provider,
        and only its key comes over. An MCP server is always its own."""
        return self.kind == MCP or any(k != KEYLESS for k in self.entry)


@dataclass(frozen=True)
class Reading:
    found: list[Found]
    # The rest of the file (agent, command, ...): vivibox brings over providers and MCP servers.
    left: list[str]


def _resolve(value: str, env: dict[str, str], base: Path) -> tuple[str, str]:
    """A secret as opencode.json gives it: the value itself, {env:NAME} or {file:path}."""
    m = REFERENCE.match(value.strip())
    if not m:
        return value, "from the file"
    kind, ref = m.groups()
    if kind == "env":
        found = env.get(ref, "")
        return found, f"from ${ref}" if found else f"${ref} is not set here"
    p = Path(os.path.expanduser(ref))
    p = p if p.is_absolute() else base / p
    try:
        return p.read_text().strip(), f"from {p}"
    except OSError:
        return "", f"{p} cannot be read"


def _secret_name(*parts: str) -> str:
    """A key store name for one secret of an MCP server."""
    name = ".".join(re.sub(r"[^a-z0-9_-]+", "-", p.lower()).strip("-") for p in parts)
    return name[:64].rstrip(".-")


def _stored_secret(name: str) -> str:
    try:
        return keys.get_key(name)
    except keys.KeyStoreError:
        return ""


def _provider(name: str, given: dict, env: dict[str, str], base: Path, yours: dict) -> Found:
    entry = copy.deepcopy(given)
    options = entry.setdefault("options", {})
    raw = options.pop("apiKey", None)
    secret, said = _resolve(raw, env, base) if isinstance(raw, str) else ("", "none needed")
    if raw is None:
        entry[KEYLESS] = True
    if not options:
        entry.pop("options")
    n = len(entry.get("models", {}))
    own = any(k != KEYLESS for k in entry)
    what = f"{n} model{'s' * (n != 1)}" if own else "opencode's provider"
    had_key = _stored_secret(name)
    if name not in yours and not had_key:
        status = "new"
    elif yours.get(name, {}) == (entry if own else {}) and (not secret or secret == had_key):
        status = "same"
    else:
        status = "replaces"
    return Found(PROVIDER, name, what, said, status, entry, {name: secret} if secret else {})


def _mcp(name: str, given: dict, env: dict[str, str], base: Path, yours: dict) -> Found:
    entry = copy.deepcopy(given)
    secrets, said = {}, []
    for section in MCP_SECRETS:
        values = entry.get(section)
        if not isinstance(values, dict):
            continue
        for var, raw in values.items():
            if not isinstance(raw, str):
                continue
            value, where = _resolve(raw, env, base)
            secret = _secret_name("mcp", name, var)
            secrets[secret] = value
            values[var] = f"{{file:{MOUNT}/{secret}}}"
            said.append(f"{var} {where}")
    if entry.get("type") == "remote":
        what = f"remote {entry.get('url', '')}"
    else:
        command = entry.get("command", [])
        what = "local " + (" ".join(command) if isinstance(command, list) else str(command))
    same = yours.get(name) == entry and all(_stored_secret(k) == v for k, v in secrets.items())
    status = "new" if name not in yours else "same" if same else "replaces"
    if name in BUILTIN_MCP:
        # vivibox's own runs in the pod; a command from your machine would not be there.
        status = "builtin"
    return Found(MCP, name, what, ", ".join(said) or "none needed", status, entry, secrets)


def read_opencode(source: Path, env: dict[str, str] | None = None) -> Reading:
    """What an opencode.json has to bring over, changing nothing yet."""
    env = dict(os.environ) if env is None else env
    source = Path(os.path.expanduser(source))
    try:
        data = json.loads(without_comments(source.read_text()))
    except OSError as e:
        raise ConfigError(f"cannot read {source}: {e.strerror}") from None
    except ValueError as e:
        raise ConfigError(f"{source} is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{source} is not an opencode configuration")
    found = []
    for kind, read, yours in ((PROVIDER, _provider, load()), (MCP, _mcp, load_mcp())):
        section = data.get(kind) or {}
        if not isinstance(section, dict):
            raise ConfigError(f"{source}: '{kind}' is not a table of names")
        for name, given in section.items():
            if not keys.PROVIDER.match(name) or not isinstance(given, dict):
                raise ConfigError(f"{source}: {kind} '{name}' cannot be named that way in vivibox")
            found.append(read(name, given, env, source.parent, yours))
    if not found:
        raise ConfigError(f"{source} defines no providers and no MCP servers")
    left = [k for k in data if k not in (PROVIDER, MCP, "$schema")]
    return Reading(found, left)


# --- keeping what you chose ---------------------------------------------------------------------


def bring_over(chosen: list[Found]) -> None:
    """Keeps what you chose, with its secrets; a secret the file did not give is left as it is."""
    stored, servers = load(), load_mcp()
    for f in chosen:
        for name, value in f.secrets.items():
            if value:
                keys.set_key(name, value, spaces=f.kind == MCP)
        if f.kind == MCP:
            servers[f.name] = f.entry
        elif f.own:
            stored[f.name] = f.entry
        else:
            stored.pop(f.name, None)  # opencode's own provider again, on your key
    _save(path(), stored)
    _save(mcp_path(), servers)


def import_opencode(source: Path, env: dict[str, str] | None = None) -> list[Found]:
    """Brings everything of an opencode.json over; returns what came."""
    found = read_opencode(source, env).found
    bring_over(found)
    return found


def forget(name: str, kind: str = PROVIDER) -> bool:
    """Removes a provider or an MCP server of yours, and its secrets; True if there was any."""
    if kind == MCP:
        servers = load_mcp()
        entry = servers.pop(name, None)
        if entry is None:
            return False
        for secret in MOUNTED.findall(json.dumps(entry)):
            keys.remove(secret)
        _save(mcp_path(), servers)
        return True
    stored = load()
    had = stored.pop(name, None) is not None
    if had:
        _save(path(), stored)
    return keys.remove(name) or had
