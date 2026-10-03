"""The panel's Resources section, and `vivibox limits`' lines: each container's limits
(config.toml's [limits], the task's own over them) and, while the pod runs, what it uses, read
from the kernel's cgroup files; the pod's budget; the disk's use from its mount. No `docker
stats`: the panel refreshes every two seconds and never waits on it; the containers' ids are
asked of Docker only while the pod runs, as a label for a scope each."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import limits
from .config import ConfigError, load_config
from .pod import DISK_MOUNT
from .podspec import ROLES, Limits, size_option, size_text
from .task import Task, TaskState

# What each container is to a person, in the panel and on the command line.
NAMES = {
    "agent": "Agent",
    "sidecar": "Docker",
    "gate": "Verification",
    "review": "Reviewer",
    "control": "Planner",
}
# The disk's use past which the panel warns, of its size.
NEARLY_FULL = 0.8


@dataclass(frozen=True)
class Row:
    name: str
    limit: str
    use: str = ""


def disk_state(folder: Path) -> tuple[int, int, int]:
    """(used, size, free) of the task's disk, from its mount, free as the supervisor reads it
    (f_bavail: XFS keeps blocks in reserve); (0, 0, 0) while it is not mounted."""
    disk = folder / DISK_MOUNT
    if not limits.is_mount(disk):
        return 0, 0, 0
    try:
        st = os.statvfs(disk)
    except OSError:
        return 0, 0, 0
    return (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


def task_limits(task: Task) -> tuple[Limits, dict]:
    """The task's limits and its own overrides; ValueError, naming the file or config.toml, for
    ones that cannot be read."""
    try:
        machine = load_config().limits
    except ConfigError as e:
        raise ValueError(str(e)) from None
    return limits.for_task(machine, task.meta), limits.read_overrides(task.meta)


def percent(used: int, size: int) -> int:
    return round(100 * used / size) if size else 0


def rows(
    task_id: str, found: Limits, use: dict[str, tuple[int, int, int]], disk: tuple[int, int]
) -> list[Row]:
    """A row per container, the pod's and the disk's. use: limits.current's, {} for a pod that
    is down; disk: (used, size), (0, 0) for one not mounted."""
    out = []
    for role in ROLES:
        limit = f"{size_text(found.memory_of(role))}, {found.pids_of(role)} processes"
        using = ""
        if role in use:
            memory, _, pids = use[role]
            using = f"using {size_text(memory)}, {pids} processes"
        out.append(Row(NAMES[role], limit, using))
    budget = f"{found.pod_cpus:g} CPUs; budget {size_text(found.pod_memory)}, {found.pod_pids} processes"
    using = ""
    if "pod" in use:
        memory, peak, pids = use["pod"]
        using = f"using {size_text(memory)} (peak {size_text(peak)}), {pids} processes"
    out.append(Row("Pod", budget, using))
    used, size, free = disk
    if size:
        shown = f"{size_text(used)} of {size_text(size)} ({percent(used, size)} %)"
        grown = size_option(2 * size)
        if limits.disk_full(free, size):  # the rule the task is stopped by, on the same field
            shown += f", full: `vivibox disk grow {task_id} {grown}`"
        elif used >= NEARLY_FULL * size:
            shown += f", nearly full: `vivibox disk grow {task_id} {grown}`"
        out.append(Row("Disk", shown))
    else:
        out.append(Row("Disk", size_text(found.disk)))
    return out


def gather(task: Task, scopes: dict[str, str]) -> tuple[list[Row], dict, bool]:
    """The rows, the task's own overrides, and whether use was read. scopes: the pod's
    containers' ids by limits role, from the pod's view (panel.PodView) or `Pod.scopes()`; {}
    while the pod is down, and nothing is asked of Docker here."""
    found, own = task_limits(task)
    use = limits.current(task.id, scopes, limits.CGROUP) if scopes else {}
    return rows(task.id, found, use, disk_state(task.root)), own, bool(use)


def section(task: Task, st: TaskState, scopes: dict[str, str]) -> list[str]:
    """For the panel: a list, never a table (UX 7), with what cannot be read said."""
    try:
        found, own, live = gather(task, scopes)
    except ValueError as e:
        return ["", "#### Resources", "", f"*Limits not readable: {e}*"]
    lines = ["", "#### Resources", ""]
    lines += [f"- **{r.name}** · {r.limit}" + (f" · {r.use}" if r.use else "") for r in found]
    where = "config.toml's `[limits]`" + (
        f", with its own over them ({', '.join(f'{k} {v}' for k, v in own.items())})" if own else ""
    )
    note = f"From {where}" + ("" if live else "; the pod is down, so nothing is in use") + "."
    return [*lines, "", f"*{note}*"]


def text(task: Task, scopes: dict[str, str]) -> str:
    """For the command line: the same rows, plain."""
    found, own, live = gather(task, scopes)
    width = max(len(r.name) for r in found)
    lines = [f"  {r.name.lower():<{width}}  {r.limit}" + (f"  ·  {r.use}" if r.use else "") for r in found]
    where = "config.toml's [limits]" + (
        f", with its own from .task/{limits.OVERRIDES} ({', '.join(f'{k} {v}' for k, v in own.items())})"
        if own
        else ""
    )
    head = (
        f"{task.id}'s limits, from {where}"
        + ("" if live else "; the pod is down, so nothing is in use")
        + ":"
    )
    return "\n".join([head, *lines])


def next_step(st: TaskState) -> str:
    """What raises the limit the task hit, for the panel's Next; "" for another problem."""
    command = limits.raising_command(st.problem or "")
    return f"`{command}` in a shell, then `s`" if command else ""
