"""What the pod hit of its limits, read after a turn and after a verification: the kernel's
count of OOM kills in the task's cgroup slice (memory.events), which covers every container and
what the pod's own Docker runs in them, attributed to the container whose scope saw the kill
(memory.events.local), and the disk's mount (statvfs), full at no block left. Read from the
kernel's files, which need no root; nothing of Docker's output is more than a label for a scope
under the slice, read where it exists. The supervisor asks through Ports.limits_exceeded, the
command line wires it (supervise.py); a reported kill is kept with the task's events, so it is
reported once."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .podspec import GIB, Limits, size_option, size_text, slice_of

CGROUP = Path("/sys/fs/cgroup")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# A task's own limits over the machine's, written by `vivibox limits`, host-owned like network.json.
OVERRIDES = "limits.json"
RAISING = re.compile(r"(vivibox (?:limits|disk grow) \S+ [^,]+?)(?:,| in a shell)")
# How a limit is said to be raised: half as much again, in whole gigabytes.
RAISE_BY = 1.5
# What each workload is to a person, and the option of `vivibox limits` that raises its memory.
WORKLOADS = {
    "agent": "the agent's container",
    "sidecar": "the pod's Docker",
    "gate": "the verification's container",
    "review": "the reviewer's container",
    "control": "the planner's container",
    "pod": "the pod",
}


class Unreadable(Exception):
    """What should be there to read, while the pod runs, is not: said, never passed over."""


@dataclass(frozen=True)
class Exceeded:
    kind: str  # "memory" or "disk"
    workload: str  # the container's limits role, or "pod" when no scope saw the kill
    limit: int  # bytes
    used: int  # the peak of memory, or the disk's used bytes
    words: str  # the kernel's: "oom_kill 1", "statvfs: 0 of N blocks free"
    # The OOM kill counts as read, by workload and "pod" for the slice's: the record a later
    # check compares with, so one kill is reported once.
    counts: dict[str, int] = field(default_factory=dict)


def event_data(hit: Exceeded) -> dict:
    """The event as the plan names it: kind, workload, limit, and peak (memory) or used (disk)."""
    used = "peak" if hit.kind == "memory" else "used"
    return {"kind": hit.kind, "workload": hit.workload, "limit": hit.limit, used: hit.used,
            "words": hit.words, "counts": hit.counts}  # fmt: skip


def recorded_counts(events: list[dict]) -> dict[str, int]:
    """The counts the last memory report kept with the task; {} before any."""
    for event in reversed(events):
        if event["type"] == "resource.limit.exceeded" and event["data"].get("kind") == "memory":
            return dict(event["data"].get("counts") or {})
    return {}


def slice_path(task_id: str, root: Path = CGROUP) -> Path:
    return root / "vivibox.slice" / slice_of(task_id)


def counter(path: Path, key: str) -> int:
    """One line of a cgroup events file, "<key> <n>"; 0 when the key is not there."""
    for line in path.read_text().splitlines():
        name, _, value = line.partition(" ")
        if name == key and value.strip().isdigit():
            return int(value)
    return 0


def number(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def raise_option(workload: str, limit: int) -> str:
    """The option of `vivibox limits` that raises a container's memory: half as much again, in
    whole gigabytes; for the pod, none (its containers' limits are its)."""
    if workload == "pod":
        return ""
    raised = math.ceil(limit * RAISE_BY / GIB) * GIB
    return f"--{workload}-memory {size_option(raised)}"


def problem(task_id: str, hits: list[Exceeded]) -> str:
    """What stops the task, in three parts: what happened, the kernel's words, what to do."""
    hit = hits[0]
    if hit.kind == "disk":
        grown = size_option(math.ceil(hit.limit * 2 / GIB) * GIB)
        return (
            f"out of disk space: the task's disk is full ({size_text(hit.limit)}): {hit.words}. "
            f"Grow it: vivibox disk grow {task_id} {grown}, then vivibox start {task_id}"
        )
    what = f"{WORKLOADS.get(hit.workload, hit.workload)} ran out of memory ({size_text(hit.limit)})"
    option = raise_option(hit.workload, hit.limit)
    if option:
        todo = f"Raise it: vivibox limits {task_id} {option}, then vivibox start {task_id}"
    else:
        todo = f"See the limits: vivibox limits {task_id}; raise one, then vivibox start {task_id}"
    return f"out of memory: {what}: {hit.words}. {todo}"


def raising_command(problem: str) -> str:
    """The command a problem of a limit's names to raise it; "" for any other problem."""
    if not problem.startswith(("out of memory: ", "out of disk space: ")):
        return ""
    found = RAISING.search(problem)
    return found.group(1).strip() if found else ""


def read_overrides(meta: Path) -> dict:
    """The task's own limits, by their keys in [limits]; {} without the file. ValueError names
    the file for one that is not an object of them."""
    path = meta / OVERRIDES
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not an object of limits")
    return data


def for_task(machine: Limits, meta: Path) -> Limits:
    """The machine's limits with the task's own over them, checked as config.toml's are."""
    given = read_overrides(meta)
    try:
        return machine.with_values(given)
    except ValueError as e:
        raise ValueError(f"{meta / OVERRIDES}: {e}") from None


def write_overrides(meta: Path, values: dict) -> dict:
    """The given keys over the task's own, checked whole over the defaults first (a key past the
    pod's is refused by name, and nothing is written); the disk's size is the image's, grown with
    `vivibox disk grow`, not a key here. Returns what the file holds now."""
    if "disk" in values:
        raise ValueError("limits.disk is the disk's size: vivibox disk grow <id> <size> changes it")
    found = {**read_overrides(meta), **values}
    Limits().with_values(found)
    path = meta / OVERRIDES
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(found) + "\n")
    os.replace(part, path)
    return found


def current(task_id: str, scopes: dict[str, str], root: Path = CGROUP) -> dict[str, tuple[int, int, int]]:
    """What the slice and each scope use now: (memory, its peak, processes), by workload and
    "pod"; {} without the slice. Read from the kernel's files, nothing of Docker but the labels."""
    slice_ = slice_path(task_id, root)
    if not slice_.is_dir():
        return {}
    found = {
        "pod": (
            number(slice_ / "memory.current"),
            number(slice_ / "memory.peak"),
            number(slice_ / "pids.current"),
        )
    }
    for role, cid in scopes.items():
        scope = slice_ / f"docker-{cid}.scope"
        if CONTAINER_ID.fullmatch(cid) and scope.is_dir():
            found[role] = (
                number(scope / "memory.current"),
                number(scope / "memory.peak"),
                number(scope / "pids.current"),
            )
    return found


def is_mount(path: Path) -> bool:
    try:
        return path.stat().st_dev != path.parent.stat().st_dev
    except OSError:
        return False


@dataclass
class Watch:
    """The task's slice and disk, as the kernel has them. scopes: the pod's containers' ids by
    their limits role, from Docker, a label each; {} while the pod is down."""

    task_id: str
    limits: Limits
    disk: Path
    scopes: Callable[[], dict[str, str]]
    root: Path = CGROUP
    statvfs: Callable[[Path], os.statvfs_result] = os.statvfs
    is_mount: Callable[[Path], bool] = is_mount

    def check(self, seen: dict[str, int]) -> list[Exceeded]:
        """What is exceeded since the counts seen: a kill the slice counts past them, blamed on
        every scope whose own count grew, else the pod's; and a disk with no block left. Nothing
        while no container of the pod is there."""
        scopes = {role: cid for role, cid in self.scopes().items() if CONTAINER_ID.fullmatch(cid)}
        if not scopes and not self.scopes():
            return []
        slice_ = slice_path(self.task_id, self.root)
        if not slice_.is_dir():
            raise Unreadable(f"the task's cgroup slice {slice_} is not there while its pod runs")
        counts = {"pod": counter(slice_ / "memory.events", "oom_kill")}
        peaks = {"pod": number(slice_ / "memory.peak")}
        for role, cid in scopes.items():
            scope = slice_ / f"docker-{cid}.scope"
            if scope.is_dir():
                counts[role] = counter(scope / "memory.events.local", "oom_kill")
                peaks[role] = number(scope / "memory.peak")
        found = []
        if counts["pod"] > seen.get("pod", 0):
            blamed = [r for r, n in counts.items() if r != "pod" and n > seen.get(r, 0)] or ["pod"]
            for role in blamed:
                limit = self.limits.pod_memory if role == "pod" else self.limits.memory_of(role)
                found.append(Exceeded("memory", role, limit, peaks[role], f"oom_kill {counts[role]}", counts))
        if not self.is_mount(self.disk):
            raise Unreadable(f"the task's disk is not mounted at {self.disk} while its pod runs")
        st = self.statvfs(self.disk)
        if st.f_bavail == 0:
            size, used = st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize
            found.append(
                Exceeded("disk", "pod", size, used, f"statvfs: 0 of {st.f_blocks} blocks free", counts)
            )
        return found


def exceeded(task, pod) -> list[Exceeded]:
    """What the task's pod hit, compared with what its events recorded last."""
    watch = Watch(task.id, pod.limits, pod.disk, pod.scopes)
    return watch.check(recorded_counts(task.events()))
