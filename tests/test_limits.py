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


def test_a_full_disk_is_said_with_its_size_and_what_is_left(tmp_path):
    tree(tmp_path)
    [hit] = watch(tmp_path, stat=Stat(5_000_000, 1_000, 0)).check({})
    assert (hit.kind, hit.workload) == ("disk", "pod")
    assert hit.limit == 5_000_000 * 4096 and hit.used == (5_000_000 - 1_000) * 4096
    assert hit.words == "statvfs: 0 B of 19.1 GiB free"


def test_a_disk_is_full_below_a_floor_no_build_can_work_with(tmp_path):
    """A write that failed with ENOSPC leaves XFS a few blocks free (the last extent did not fit,
    reserved blocks): a disk the agent and the gate see as full must be reported as full. Free
    space below max(64 MiB, 1 % of the size) counts as none; one place decides."""
    gib, mib = 2**30, 2**20
    tree(tmp_path)
    blocks = lambda n: n // 4096  # noqa: E731

    def check(size, free):
        return watch(tmp_path, stat=Stat(blocks(size), blocks(free), blocks(free))).check({})

    assert check(20 * gib, 2 * 20 * gib // 100) == [], "2 % free: not full"
    [hit] = check(20 * gib, 20 * gib // 200)
    assert hit.words == "statvfs: 102.4 MiB of 20 GiB free", "0.5 % free: full, with the real numbers"
    [hit] = check(gib, 64 * mib - 4096)
    assert hit.words == "statvfs: 64 MiB of 1 GiB free" and hit.used == gib - (64 * mib - 4096)
    assert check(gib, 200 * mib) == [], "a small disk with room: not full"
    assert check(gib, 64 * mib) == [], "at the floor: not below it"
    assert limits.disk_floor(gib) == 64 * mib and limits.disk_floor(20 * gib) == 20 * gib // 100
    assert limits.disk_full(3 * mib, gib) and not limits.disk_full(65 * mib, gib)


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


def test_a_tasks_own_limits_come_over_the_machines(tmp_path):
    """.task/limits.json, written by vivibox limits, host-owned like network.json: its keys over
    config.toml's [limits]; checked the same way, the error naming the file and the key."""
    meta = tmp_path / ".task"
    meta.mkdir()
    assert limits.for_task(Limits(), meta) == Limits(), "no file: the machine's"
    limits.write_overrides(meta, {"agent_memory": "6g"})
    limits.write_overrides(meta, {"gate_pids": 100})
    found = limits.for_task(Limits(), meta)
    assert found.memory_of("agent") == 6 * 2**30 and found.pids_of("gate") == 100
    assert limits.read_overrides(meta) == {"agent_memory": "6g", "gate_pids": 100}
    (meta / "limits.json").write_text('{"agent_memory": "9g"}')
    with pytest.raises(ValueError, match="limits.json.*limits.agent_memory"):
        limits.for_task(Limits(), meta)
    (meta / "limits.json").write_text("[]")
    with pytest.raises(ValueError, match="limits.json"):
        limits.for_task(Limits(), meta)
    with pytest.raises(ValueError, match="limits.disk"):
        limits.write_overrides(meta, {"disk": "40g"})  # the image's size is the disk's: vivibox disk grow


def test_the_command_that_raises_a_limit_is_read_from_the_problem():
    hit = limits.Exceeded("memory", "gate", 4 * 2**30, 4 * 2**30, "oom_kill 1", {})
    assert limits.raising_command(limits.problem("shop-1", [hit])) == "vivibox limits shop-1 --gate-memory 6g"
    full = limits.Exceeded("disk", "pod", 20 * 2**30, 20 * 2**30, "statvfs: 0 of 1 blocks free", {})
    assert limits.raising_command(limits.problem("shop-1", [full])) == "vivibox disk grow shop-1 40g"
    assert limits.raising_command("agent turn failed: 429") == ""


def test_current_use_is_read_from_the_slice_and_its_scopes(tmp_path):
    slice_ = tree(tmp_path, peak={"pod": 3 * 2**30, "agent": 2 * 2**30})
    (slice_ / "memory.current").write_text(str(2 * 2**30) + "\n")
    (slice_ / "pids.current").write_text("49\n")
    agent = slice_ / f"docker-{CID['agent']}.scope"
    (agent / "memory.current").write_text(str(2**30) + "\n")
    (agent / "pids.current").write_text("37\n")
    found = limits.current("shop-1", {"agent": CID["agent"], "gate": "x" * 64}, root=tmp_path)
    assert found == {"pod": (2 * 2**30, 3 * 2**30, 49), "agent": (2**30, 2 * 2**30, 37)}
    assert limits.current("shop-1", {}, root=tmp_path / "none") == {}, "no slice: nothing"
