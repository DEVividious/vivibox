"""What the live tasks' pods use now: CPU and memory of each container from one `docker stats
--no-stream`, the size of each task's volumes from one `docker system df -v`, and its folder from
`du`. Seconds of Docker's time, so only the u screen asks, in a thread of its own, while it is
open; the list never does.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .pod import CACHES

# The containers of a task's pod, by the ending of their names (pod.Pod).
ROLES = ("agent", "dind", "gate", "review")
# The volumes of a task (pod.Pod.volumes), with its opencode configuration.
VOLUMES = ("docker", "socket", "installed", "gate-installed", "gate-build-cache", "config")
SECONDS = 30
UNITS = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
         "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40}  # fmt: skip
SIZE = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]+)\s*$")


class Unavailable(Exception):
    """Docker did not answer, or answered with something else."""


@dataclass
class Container:
    role: str
    cpu: float  # percent of one CPU, as docker stats says it
    memory: int
    memory_limit: int


@dataclass
class Resources:
    containers: list[Container] = field(default_factory=list)
    # Bytes per volume, by its name in pod.Pod.volumes ("docker" is the pod's own image store).
    volumes: dict[str, int] = field(default_factory=dict)
    # Bytes of the task's folder: the clone and the task's files.
    files: int = 0

    @property
    def cpu(self) -> float:
        return round(sum(c.cpu for c in self.containers), 1)

    @property
    def memory(self) -> int:
        return sum(c.memory for c in self.containers)

    @property
    def disk(self) -> int:
        return sum(self.volumes.values()) + self.files


def parse_size(text: str) -> int:
    m = SIZE.match(text or "")
    if not m or m.group(2).lower() not in UNITS:
        return 0
    return int(float(m.group(1)) * UNITS[m.group(2).lower()])


def default_run(command: list[str]) -> str:
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(str(e)) from None
    if p.returncode != 0:
        raise Unavailable(p.stderr.strip()[:300])
    return p.stdout


def owner(name: str, task_ids: list[str], endings: tuple[str, ...]) -> tuple[str, str] | None:
    """(task, ending) for vivibox-<task>-<ending>; a task whose id is a prefix of another's is
    told apart by the ending, which has to be one of the known ones."""
    for task_id in task_ids:
        prefix = f"vivibox-{task_id}-"
        if name.startswith(prefix) and name[len(prefix) :] in endings:
            return task_id, name[len(prefix) :]
    return None


def sample(
    roots: dict[str, Path],
    run: Callable[[list[str]], str] | None = None,
    problems: list[str] | None = None,
    shared_caches: dict[str, int] | None = None,
) -> dict[str, Resources]:
    """roots: each live task's folder, by its id. {} when Docker does not answer, with why in
    problems, when given: a screen of dashes alone would read as pods doing nothing.
    Shared caches are measured once, separately, including when roots is empty."""
    if shared_caches is not None:
        shared_caches.clear()
    run = run or default_run
    ids = list(roots)
    found = {task_id: Resources() for task_id in ids}
    try:
        stats = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"]) if ids else ""
        df = json.loads(run(["docker", "system", "df", "-v", "--format", "{{json .}}"]) or "{}")
    except Unavailable as e:
        if problems is not None:
            problems.append(f"Docker did not answer: {e}")
        return {}
    except ValueError as e:
        if problems is not None:
            problems.append(f"docker system df gave something other than JSON: {e}")
        return {}
    for line in stats.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if mine := owner(row.get("Name", ""), ids, ROLES):
            used, _, limit = (row.get("MemUsage") or "").partition("/")
            cpu = float((row.get("CPUPerc") or "0").rstrip("%") or 0)
            found[mine[0]].containers.append(Container(mine[1], cpu, parse_size(used), parse_size(limit)))
    shared_names = {f"vivibox-cache-{name}": name for name in CACHES}
    for volume in df.get("Volumes") or []:
        name = volume.get("Name", "")
        if shared_caches is not None and name in shared_names:
            shared_caches[shared_names[name]] = parse_size(volume.get("Size", ""))
        if mine := owner(volume.get("Name", ""), ids, VOLUMES):
            found[mine[0]].volumes[mine[1]] = parse_size(volume.get("Size", ""))
    for task_id, root in roots.items():
        with contextlib.suppress(Unavailable, ValueError, IndexError):
            found[task_id].files = int(run(["du", "-sk", str(root)]).split()[0]) * 1024
    return found


def size(n: int) -> str:
    """Bytes as a person reads them: "900 B", "20 MB", "3.2 GB"; "-" for none."""
    if n <= 0:
        return "-"
    for unit, scale in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("kB", 10**3)):
        if n >= scale:
            value = n / scale
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{n} B"


def as_dict(found: Resources) -> dict:
    return {
        "containers": [
            {"role": c.role, "cpu": c.cpu, "memory": c.memory, "memory_limit": c.memory_limit}
            for c in found.containers
        ],
        "volumes": found.volumes,
        "files": found.files,
    }
