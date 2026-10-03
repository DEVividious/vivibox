"""What the pod hit of its limits, read from the kernel's cgroup files and the disk's mount
(vivibox/limits.py): an OOM kill once, attributed to the container whose scope saw it, and a
full disk; nothing read where nothing of the pod runs, and said where it cannot be read."""

import os
from pathlib import Path

import pytest

from vivibox import limits
from vivibox.podspec import Limits

CID = {"agent": "a" * 64, "sidecar": "b" * 64, "gate": "c" * 64}


def tree(root: Path, oom: dict[str, int] | None = None, peak: dict[str, int] | None = None) -> Path:
    """A task's slice as cgroup v2 has it: memory.events of the slice counting every scope's,
    each scope's own in memory.events.local, memory.peak on both."""
    oom, peak = oom or {}, peak or {}
    slice_ = root / "vivibox.slice" / "vivibox-shop_1.slice"
    slice_.mkdir(parents=True)
    total = sum(oom.values())
    (slice_ / "memory.events").write_text(f"low 0\nhigh 0\nmax 3\noom {total}\noom_kill {total}\n")
    (slice_ / "memory.peak").write_text(f"{peak.get('pod', 0)}\n")
    for role, cid in CID.items():
        scope = slice_ / f"docker-{cid}.scope"
        scope.mkdir()
        n = oom.get(role, 0)
        (scope / "memory.events.local").write_text(f"low 0\nhigh 0\nmax {n}\noom {n}\noom_kill {n}\n")
        (scope / "memory.peak").write_text(f"{peak.get(role, 0)}\n")
    return slice_


class Stat:
    def __init__(self, blocks, free, avail, frsize=4096):
        self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, free, avail, frsize


def watch(tmp_path, scopes=None, stat=None, mounted=True):
    return limits.Watch(
        "shop-1", Limits(), tmp_path / "disk", scopes=lambda: dict(CID if scopes is None else scopes),
        root=tmp_path, statvfs=lambda p: stat or Stat(5_000_000, 4_000_000, 3_000_000),
        is_mount=lambda p: mounted,
    )  # fmt: skip


def test_an_oom_kill_is_attributed_to_the_container_whose_scope_saw_it(tmp_path):
    tree(tmp_path, oom={"agent": 1}, peak={"agent": 4 * 2**30, "pod": 5 * 2**30})
    [hit] = watch(tmp_path).check({})
    assert (hit.kind, hit.workload, hit.limit, hit.used) == ("memory", "agent", 4 * 2**30, 4 * 2**30)
    assert hit.words == "oom_kill 1"
    assert hit.counts == {"pod": 1, "agent": 1, "sidecar": 0, "gate": 0}, "for the record, reported once"


def test_an_oom_kill_is_reported_once(tmp_path):
    tree(tmp_path, oom={"agent": 1})
    w = watch(tmp_path)
    [hit] = w.check({})
    assert w.check(hit.counts) == [], "seen already"
    tree(tmp_path / "later", oom={"agent": 1, "gate": 1})
    [again] = watch(tmp_path / "later").check(hit.counts)
    assert again.workload == "gate", "the new one, not the agent's again"


def test_an_oom_kill_no_scope_saw_is_the_pods(tmp_path):
    """A nested container's, under the sidecar's scope with a limit of its own: counted by the
    slice, by no scope's own events."""
    tree(tmp_path, oom={}, peak={"pod": 3 * 2**30})
    slice_ = tmp_path / "vivibox.slice" / "vivibox-shop_1.slice"
    (slice_ / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    [hit] = watch(tmp_path).check({})
    assert (hit.workload, hit.limit, hit.used, hit.words) == ("pod", 6 * 2**30, 3 * 2**30, "oom_kill 1")


def test_a_full_disk_is_said_with_its_size_and_the_kernels_count(tmp_path):
    tree(tmp_path)
    [hit] = watch(tmp_path, stat=Stat(5_000_000, 1_000, 0)).check({})
    assert (hit.kind, hit.workload) == ("disk", "pod")
    assert hit.limit == 5_000_000 * 4096 and hit.used == (5_000_000 - 1_000) * 4096
    assert hit.words == "statvfs: 0 of 5000000 blocks free"
    assert watch(tmp_path, stat=Stat(5_000_000, 1_000, 1)).check({}) == [], "a block left: not full"


def test_a_container_id_is_a_label_never_a_path_outside_the_slice(tmp_path):
    """The ids come from docker inspect: one that is not 64 hex digits is dropped, and a path is
    only ever built under the slice, read where it exists."""
    tree(tmp_path, oom={"agent": 1})
    scopes = {"agent": "../../etc", "gate": CID["gate"], "review": "d" * 64}
    [hit] = watch(tmp_path, scopes=scopes).check({})
    assert hit.workload == "pod", "the agent's id was no id: its kill is nobody's"
    assert "review" not in hit.counts, "a scope that is not there is not read"


def test_a_slice_missing_while_the_pod_runs_is_said_not_passed_over(tmp_path):
    with pytest.raises(limits.Unreadable, match="vivibox-shop_1.slice"):
        watch(tmp_path).check({})
    tree(tmp_path)
    with pytest.raises(limits.Unreadable, match="not mounted"):
        watch(tmp_path, mounted=False).check({})


def test_nothing_is_read_while_the_pod_is_down(tmp_path):
    """No container: no slice to read, no disk mounted; nothing exceeded, nothing unreadable."""
    assert watch(tmp_path, scopes={}, mounted=False).check({}) == []


def test_the_problem_has_three_parts_and_names_the_option_that_raises_the_limit():
    hit = limits.Exceeded("memory", "agent", 4 * 2**30, 4 * 2**30, "oom_kill 1", {})
    said = limits.problem("shop-1", [hit])
    what, _, why = said.partition(": ")
    assert what == "out of memory"
    assert "the agent's container ran out of memory (4 GiB)" in why and "oom_kill 1" in why
    assert "vivibox limits shop-1 --agent-memory 6g" in why and "vivibox start shop-1" in why
    assert limits.raise_option("sidecar", 2 * 2**30) == "--sidecar-memory 3g"
    assert limits.raise_option("review", 3 * 2**29) == "--review-memory 3g"
    full = limits.Exceeded("disk", "pod", 20 * 2**30, 20 * 2**30, "statvfs: 0 of 5242880 blocks free", {})
    said = limits.problem("shop-1", [full])
    assert said.startswith("out of disk space: the task's disk is full (20 GiB)")
    assert "vivibox disk grow shop-1 40g" in said and "then vivibox start shop-1" in said
    pod = limits.Exceeded("memory", "pod", 6 * 2**30, 5 * 2**30, "oom_kill 1", {})
    assert "vivibox limits shop-1" in limits.problem("shop-1", [pod])


def test_the_counts_recorded_last_come_from_the_tasks_events():
    events = [
        {"type": "turn", "data": {}},
        {"type": "resource.limit.exceeded", "data": {"kind": "memory", "counts": {"pod": 1, "agent": 1}}},
        {"type": "resource.limit.exceeded", "data": {"kind": "disk", "counts": {"pod": 1, "agent": 1}}},
    ]
    assert limits.recorded_counts(events) == {"pod": 1, "agent": 1}
    assert limits.recorded_counts([]) == {}


def test_the_event_carries_what_the_plan_names():
    hit = limits.Exceeded("memory", "gate", 4 * 2**30, 3 * 2**30, "oom_kill 2", {"pod": 2})
    assert limits.event_data(hit) == {
        "kind": "memory", "workload": "gate", "limit": 4 * 2**30, "peak": 3 * 2**30,
        "words": "oom_kill 2", "counts": {"pod": 2},
    }  # fmt: skip
    full = limits.Exceeded("disk", "pod", 10, 10, "statvfs: 0 of 1 blocks free", {})
    assert limits.event_data(full)["used"] == 10 and "peak" not in limits.event_data(full)


def test_the_real_root_is_the_kernels():
    assert str(limits.CGROUP) == "/sys/fs/cgroup"
    assert str(limits.slice_path("shop-1")) == "/sys/fs/cgroup/vivibox.slice/vivibox-shop_1.slice"
    assert os.statvfs is limits.Watch.__dataclass_fields__["statvfs"].default
