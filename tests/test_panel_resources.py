"""The panel's Resources section: each container's limits and, while the pod runs, its use, the
pod's budget and the disk; and the next step when a limit was hit."""

from test_limits import CID, tree
from test_tui import implementing

from vivibox import limits, panel, panel_resources, ui
from vivibox.panel import detail
from vivibox.podspec import Limits


def test_the_panel_shows_each_containers_limits_the_pods_budget_and_the_disk(env):
    task = implementing()
    shown = detail(task, task.read_state(), 3, running=True)
    assert "#### Resources" in shown
    assert "- **Agent** · 4 GiB, 2048 processes" in shown
    assert "- **Docker** · 2 GiB, 4096 processes" in shown
    assert "- **Verification** · 4 GiB, 4096 processes" in shown
    assert "- **Reviewer** · 1.5 GiB, 1024 processes" in shown
    assert "- **Pod** · 6 CPUs; budget 6 GiB, 8192 processes" in shown
    assert "- **Disk** · 20 GiB" in shown
    assert shown.index("#### Roles") < shown.index("#### Resources") < shown.index("#### The plan")
    assert "pod is down" in shown, "nothing runs: no use to show, said"


def test_the_tasks_own_limits_show_in_the_panel(env):
    task = implementing()
    limits.write_overrides(task.meta, {"agent_memory": "6g"}, Limits())
    shown = detail(task, task.read_state(), 3, running=True)
    assert "- **Agent** · 6 GiB, 2048 processes" in shown and "its own" in shown


def test_while_the_pod_runs_the_panel_shows_what_each_container_uses(env, tmp_path, monkeypatch):
    task = implementing()
    slice_ = tree(tmp_path, peak={"pod": 3 * 2**30})
    slice_ = slice_.rename(slice_.with_name("vivibox-demo_1.slice"))  # the tree is shop-1's
    (slice_ / "memory.current").write_text(str(2 * 2**30) + "\n")
    (slice_ / "pids.current").write_text("49\n")
    agent = slice_ / f"docker-{CID['agent']}.scope"
    (agent / "memory.current").write_text(str(1_288_490_189) + "\n")  # 1.2 GiB
    (agent / "pids.current").write_text("37\n")
    monkeypatch.setattr(limits, "CGROUP", tmp_path)
    pod = panel.PodView(address="198.51.100.2", scopes={"agent": CID["agent"]})
    shown = detail(task, task.read_state(), 3, running=True, pod=pod)
    assert "- **Agent** · 4 GiB, 2048 processes · using 1.2 GiB, 37 processes" in shown
    assert (
        "- **Pod** · 6 CPUs; budget 6 GiB, 8192 processes · using 2 GiB (peak 3 GiB), 49 processes" in shown
    )
    assert "pod is down" not in shown


def test_the_disk_is_shown_with_its_use_and_warns_at_four_fifths(env, monkeypatch):
    task = implementing()

    class Stat:
        f_frsize, f_blocks, f_bfree, f_bavail = 4096, 5_242_880, 1_000_000, 1_000_000

    used, size = (5_242_880 - 1_000_000) * 4096, 5_242_880 * 4096
    monkeypatch.setattr(panel_resources, "disk_state", lambda folder: (used, size, size - used))
    shown = detail(task, task.read_state(), 3, running=True)
    assert "- **Disk** · 16.2 GiB of 20 GiB (81 %), nearly full: `vivibox disk grow demo-1 40g`" in shown
    monkeypatch.setattr(panel_resources, "disk_state", lambda folder: (2 * 2**30, 20 * 2**30, 18 * 2**30))
    shown = detail(task, task.read_state(), 3, running=True)
    assert "- **Disk** · 2 GiB of 20 GiB (10 %)" in shown and "nearly full" not in shown
    # Full by the rule the supervisor stops the task by (limits.disk_full), not by the warning's.
    monkeypatch.setattr(
        panel_resources, "disk_state", lambda folder: (20 * 2**30 - 3 * 2**20, 20 * 2**30, 3 * 2**20)
    )
    shown = detail(task, task.read_state(), 3, running=True)
    assert "- **Disk** · 20 GiB of 20 GiB (100 %), full: `vivibox disk grow demo-1 40g`" in shown
    assert "nearly full" not in shown


def test_the_panel_and_the_supervisor_read_free_space_from_the_same_field(env, monkeypatch):
    """XFS keeps blocks in reserve: f_bfree is above f_bavail by them. The supervisor stops the
    task by f_bavail; the panel must not say "nearly full" of a disk the task was stopped on."""

    class Stat:
        f_frsize, f_blocks = 4096, 5_242_880
        f_bfree, f_bavail = 40 * 256, 2 * 256  # 40 MiB free by f_bfree, 2 MiB by f_bavail

    monkeypatch.setattr(panel_resources.os, "statvfs", lambda p: Stat())
    monkeypatch.setattr(limits, "is_mount", lambda p: True)
    used, size, free = panel_resources.disk_state(implementing().root)
    assert free == 2 * 2**20 and used == (Stat.f_blocks - Stat.f_bfree) * 4096 and size == 20 * 2**30
    assert limits.disk_full(free, size)


def test_next_is_the_command_that_raises_a_limit_the_task_hit(env):
    task = implementing()
    hit = limits.Exceeded("memory", "agent", 4 * 2**30, 4 * 2**30, "oom_kill 1", 6 * 2**30)
    task.set_paused(True, problem=limits.problem(task.id, [hit]))
    st = task.read_state()
    seen = ui.view(task, st, True, 3)
    assert seen.status == "out of memory"
    nxt = panel.next_steps(task, st, seen, True, panel.PodView())
    assert nxt == f"`vivibox limits {task.id} --agent-memory 6g` in a shell, then `s`"
    full = limits.Exceeded(
        "disk", "pod", 20 * 2**30, 20 * 2**30, "statvfs: 0 of 5242880 blocks free", 6 * 2**30
    )
    task.set_paused(True, problem=limits.problem(task.id, [full]))
    st = task.read_state()
    nxt = panel.next_steps(task, st, ui.view(task, st, True, 3), True, panel.PodView())
    assert nxt == f"`vivibox disk grow {task.id} 40g` in a shell, then `s`"
    task.set_paused(True, problem="agent turn failed: 429")
    st = task.read_state()
    assert panel.next_steps(task, st, ui.view(task, st, True, 3), True, panel.PodView()) == "`s` try again"
    pod = limits.Exceeded("memory", "pod", 6 * 2**30, 5 * 2**30, "oom_kill 1", 6 * 2**30)
    task.set_paused(True, problem=limits.problem(task.id, [pod]))
    st = task.read_state()
    nxt = panel.next_steps(task, st, ui.view(task, st, True, 3), True, panel.PodView())
    assert nxt == f"`vivibox limits {task.id}` in a shell, then `s`", "the command alone, nothing after it"


def test_the_panel_asks_docker_nothing_the_pod_view_worker_brings_the_ids(env, monkeypatch):
    """The panel refreshes every two seconds on the view's thread: the use is read from the
    kernel's files, and the containers' ids come with the pod's view, which the pod-view worker
    fills off that thread (UX §6: the view never waits on Docker)."""
    from vivibox import pod as pod_module

    task = implementing()

    def never(self):
        raise AssertionError("detail() asked Docker")

    monkeypatch.setattr(pod_module.Pod, "scopes", never)
    detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    detail(task, task.read_state(), 3, running=True, pod=panel.PodView(address="198.51.100.2"))
    asked = []
    monkeypatch.setattr(
        pod_module.Pod, "scopes", lambda self: asked.append(self.task_id) or {"agent": "a" * 64}
    )
    monkeypatch.setattr(
        pod_module, "addresses", lambda names, *a, **k: {f"vivibox-{task.id}-dind": "198.51.100.2"}
    )
    monkeypatch.setattr(
        pod_module.Pod, "probe", lambda self: type("P", (), {"listening": [], "demo": False, "log": ""})()
    )
    views = panel.pod_views([task.id, "demo-9"])
    assert views[task.id].scopes == {"agent": "a" * 64} and asked == [task.id], "once, for the pod that is up"
    assert views["demo-9"].scopes == {} and views["demo-9"].address == ""
