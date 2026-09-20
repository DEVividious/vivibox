"""Secrets handed to a running task: files on tmpfs, mounted read-only into the agent container.

The files live under $XDG_RUNTIME_DIR (memory, gone at logout), so keys never appear in images,
volumes, `docker inspect` or the repository. Only the keys the task's model needs are copied.
"""

from __future__ import annotations

import os
import secrets as random
import shutil
from collections.abc import Callable
from pathlib import Path

from . import keys

MOUNT = "/run/vivibox-secrets"


def runtime_dir(task_id: str) -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "vivibox" / task_id


def _write(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)


def prepare(task_id: str, providers: list[str], get_key: Callable[[str], str] = keys.get_key) -> Path:
    """Writes the provider keys and a server password for the task; keeps an existing password."""
    d = runtime_dir(task_id)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    for provider in providers:
        _write(d / provider, get_key(provider))
    if not (d / "server-password").exists():
        _write(d / "server-password", random.token_urlsafe(24))
    return d


def remove(task_id: str) -> None:
    shutil.rmtree(runtime_dir(task_id), ignore_errors=True)
