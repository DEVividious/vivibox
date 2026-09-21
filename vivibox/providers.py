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
from dataclasses import dataclass
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
class Imported:
    name: str
    models: int
    # Where the key came from, or why there is none.
    key: str


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


def import_opencode(source: Path, env: dict[str, str] | None = None) -> list[Imported]:
    """Brings every provider of an opencode.json over, with its key; returns what came."""
    env = dict(os.environ) if env is None else env
    source = Path(os.path.expanduser(source))
    try:
        data = json.loads(without_comments(source.read_text()))
    except OSError as e:
        raise ConfigError(f"cannot read {source}: {e.strerror}") from None
    except ValueError as e:
        raise ConfigError(f"{source} is not JSON: {e}") from None
    found = data.get("provider") if isinstance(data, dict) else None
    if not isinstance(found, dict) or not found:
        raise ConfigError(f"{source} defines no providers")
    stored, imported = load(), []
    for name, given in found.items():
        if not keys.PROVIDER.match(name) or not isinstance(given, dict):
            raise ConfigError(f"{source}: provider '{name}' cannot be named that way in vivibox")
        entry = copy.deepcopy(given)
        options = entry.setdefault("options", {})
        key, said = "", "none in the file"
        if isinstance(raw := options.pop("apiKey", None), str):
            key, said = _resolve(raw, env, source.parent)
        if key:
            keys.set_key(name, key)
        elif raw is None:
            entry[KEYLESS] = True
        if not options:
            entry.pop("options")
        stored[name] = entry
        imported.append(Imported(name, len(entry.get("models", {})), said if key or raw else "none needed"))
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(stored, indent=2) + "\n")
    return imported
