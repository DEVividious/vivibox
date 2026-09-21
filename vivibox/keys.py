"""API keys, stored by vivibox itself: one file per provider, readable only by you.

    ~/.local/share/vivibox/keys/<provider>

A key is the file's whole content, with no format of its own to change between versions. How a
harness receives it is up to its adapter. The provider is the first part of a model name in
config.toml ("deepseek" in "deepseek/deepseek-v4-flash").
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class KeyStoreError(Exception):
    pass


def store() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "vivibox" / "keys"


def _path(provider: str) -> Path:
    if not PROVIDER.match(provider):
        raise KeyStoreError(f"provider name '{provider}': lowercase letters, digits, '.', '_' and '-'")
    return store() / provider


def set_key(provider: str, value: str) -> None:
    value = value.strip()
    if not value or any(c.isspace() for c in value):
        raise KeyStoreError("a key is one word without spaces")
    path = _path(provider)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.chmod(path, 0o600)


def get_key(provider: str) -> str:
    path = _path(provider)
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        raise KeyStoreError(f"no key for '{provider}'; add it with: vivibox auth set {provider}") from None
    if path.stat().st_mode & 0o077:
        raise KeyStoreError(f"{path} is readable by others; run: chmod 600 {path}")
    return value


def remove(provider: str) -> bool:
    path = _path(provider)
    if not path.exists():
        return False
    path.unlink()
    return True


def masked(value: str) -> str:
    return f"{value[:3]}… ({len(value)})"


def list_keys() -> dict[str, str]:
    d = store()
    if not d.is_dir():
        return {}
    return {p.name: masked(p.read_text().strip()) for p in sorted(d.iterdir()) if PROVIDER.match(p.name)}
