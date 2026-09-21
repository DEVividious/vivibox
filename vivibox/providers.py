"""Providers opencode does not know by itself, such as your employer's endpoint, and bringing them
over from an opencode.json you already use.

    ~/.config/vivibox/providers.json

Each entry is opencode's own provider definition (npm package, baseURL, models) as your
opencode.json had it, without its key: the key goes to vivibox's key store, and a task's
opencode.json points at the copy mounted in its pod. Endpoints stay in your config directory,
never in a repository.
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

# Marks a provider whose endpoint takes no key, so a task is not refused for want of one.
KEYLESS = "keyless"
DEFAULT_SOURCE = Path("~/.config/opencode/opencode.json")
REFERENCE = re.compile(r"^\{(env|file):(.+)\}$")


def path() -> Path:
    return config_dir() / "providers.json"


def load() -> dict[str, dict]:
    try:
        return json.loads(path().read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ConfigError(f"{path()}: {e}") from None


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


@dataclass(frozen=True)
class Found:
    """A provider of an opencode.json, read and not yet brought over."""

    name: str
    models: int
    # Where the key comes from, or why there is none; never the key itself.
    key: str
    # You already have a provider by this name, which bringing this one over would replace.
    replaces: bool
    entry: dict = field(repr=False)
    secret: str = field(default="", repr=False)

    @property
    def own(self) -> bool:
        """Defines an endpoint or models of its own; otherwise it is opencode's provider, and
        only its key comes over."""
        return any(k != KEYLESS for k in self.entry)


@dataclass(frozen=True)
class Reading:
    found: list[Found]
    # The rest of the file (mcp, agent, ...): vivibox brings over providers only.
    left: list[str]


def _resolve(value: str, env: dict[str, str], base: Path) -> tuple[str, str]:
    """A key as opencode.json gives it: the value itself, {env:NAME} or {file:path}."""
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


def opencode_candidates(repos: list[Path] = (), env: dict[str, str] | None = None) -> list[Path]:
    """Where opencode reads its configuration: the file $OPENCODE_CONFIG names, the global one, and
    a project's own in its repository."""
    env = dict(os.environ) if env is None else env
    found = [Path(os.path.expanduser(env["OPENCODE_CONFIG"]))] if env.get("OPENCODE_CONFIG") else []
    base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    found += [base / "opencode.json", base / "opencode.jsonc", base / "config.json"]
    for repo in repos:
        found += [repo / "opencode.json", repo / "opencode.jsonc", repo / ".opencode" / "opencode.json"]
    return list(dict.fromkeys(found))


def discover(repos: list[Path] = (), env: dict[str, str] | None = None) -> list[tuple[Path, int]]:
    """The opencode configurations on this machine that define providers, with how many."""
    found = []
    for candidate in opencode_candidates(repos, env):
        if not candidate.is_file():
            continue
        try:
            found.append((candidate, len(read_opencode(candidate, env).found)))
        except ConfigError:
            continue  # no providers in it, or not readable: nothing to offer
    return found


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
    given = data.get("provider") if isinstance(data, dict) else None
    if not isinstance(given, dict) or not given:
        raise ConfigError(f"{source} defines no providers")
    yours = set(load()) | set(keys.list_keys())
    found = []
    for name, definition in given.items():
        if not keys.PROVIDER.match(name) or not isinstance(definition, dict):
            raise ConfigError(f"{source}: provider '{name}' cannot be named that way in vivibox")
        entry = copy.deepcopy(definition)
        options = entry.setdefault("options", {})
        raw = options.pop("apiKey", None)
        secret, said = _resolve(raw, env, source.parent) if isinstance(raw, str) else ("", "none needed")
        if raw is None:
            entry[KEYLESS] = True
        if not options:
            entry.pop("options")
        found.append(Found(name, len(entry.get("models", {})), said, name in yours, entry, secret))
    left = [k for k in data if k not in ("provider", "$schema")]
    return Reading(found, left)


def forget(name: str) -> bool:
    """Removes a provider of yours and its key; True if there was either."""
    stored = load()
    had = stored.pop(name, None) is not None
    if had:
        path().write_text(json.dumps(stored, indent=2) + "\n")
    return keys.remove(name) or had


def bring_over(chosen: list[Found]) -> None:
    """Keeps the providers you chose, and their keys; a key the file did not give is left as it is."""
    stored = load()
    for f in chosen:
        if f.secret:
            keys.set_key(f.name, f.secret)
        if f.own:
            stored[f.name] = f.entry
        else:
            stored.pop(f.name, None)  # opencode's own provider again, on your key
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(stored, indent=2) + "\n")


def import_opencode(source: Path, env: dict[str, str] | None = None) -> list[Found]:
    """Brings every provider of an opencode.json over; returns what came."""
    found = read_opencode(source, env).found
    bring_over(found)
    return found
