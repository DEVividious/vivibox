"""The command line on a task's limits and its disk: `vivibox limits <id>` says and sets what the
task's containers may use, `vivibox disk grow <id> <size>` makes its disk larger."""

import json
import subprocess
from functools import partial

import pytest

from vivibox import actions
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.pod import Pod
from vivibox.task import find_task


class Helper:
    """A root helper that does what it is asked, and records it; the rest of Docker is not there."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:1] == ["sudo"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "Error: No such object")

    def disk_ops(self):
        return [(c[3], json.loads(c[5])) for c in self.calls if c[:1] == ["sudo"] and c[3].endswith("-disk")]


@pytest.fixture
def task(env, monkeypatch):
    helper = Helper()
    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=helper))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    found = find_task(load_config().tasks_dir, "demo-1")
    found.helper = helper
    return found


def test_limits_says_the_tasks_limits_and_where_they_come_from(task, capsys):
    assert main(["limits", "demo-1"]) == 0
    out = capsys.readouterr().out
    assert "agent" in out and "4 GiB" in out and "2048 processes" in out
    assert "pod" in out and "6 GiB" in out and "6 CPUs" in out and "8192 processes" in out and "budget" in out
    assert "disk" in out and "20 GiB" in out
    assert "config.toml" in out and "pod is down" in out, "no use to show: nothing runs"


def test_limits_with_an_option_writes_the_tasks_own_and_says_when_it_applies(task, capsys):
    assert main(["limits", "demo-1", "--agent-memory", "6g", "--gate-pids", "100"]) == 0
    out = capsys.readouterr().out
    assert json.loads((task.meta / "limits.json").read_text()) == {"agent_memory": "6g", "gate_pids": 100}
    assert "6 GiB" in out and "100 processes" in out
    assert "vivibox start demo-1" in out, "applies at the next start: the containers are made with the flags"
    assert actions.task_pod("demo-1").limits.memory_of("agent") == 6 * 2**30, "the pod reads them"
    assert main(["limits", "demo-1", "--review-memory", "1g"]) == 0
    assert json.loads((task.meta / "limits.json").read_text()) == {
        "agent_memory": "6g", "gate_pids": 100, "review_memory": "1g",
    }, "merged with what was set before"  # fmt: skip


@pytest.mark.parametrize(
    ("option", "value", "key"),
    [
        ("--agent-memory", "lots", "agent_memory"),
        ("--agent-memory", "8g", "agent_memory"),  # past the pod's 6 GiB
        ("--gate-pids", "0", "gate_pids"),
        ("--pod-cpus", "0", "pod_cpus"),
    ],
)
def test_a_limit_that_is_not_one_is_refused_by_name_and_nothing_is_written(task, capsys, option, value, key):
    assert main(["limits", "demo-1", option, value]) == 1
    assert f"limits.{key}" in capsys.readouterr().err
    assert not (task.meta / "limits.json").exists()


def test_a_limits_file_that_is_not_right_stops_the_pod_from_being_made(task, capsys):
    (task.meta / "limits.json").write_text('{"agent_memory": "8g"}')
    with pytest.raises(Exception, match="limits.json.*limits.agent_memory"):
        actions.task_pod("demo-1")
    assert main(["limits", "demo-1"]) == 1
    assert "limits.json" in capsys.readouterr().err


def test_disk_has_no_option_under_limits(task, capsys):
    with pytest.raises(SystemExit) as left:  # argparse: vivibox disk grow is the way
        main(["limits", "demo-1", "--disk", "40g"])
    assert left.value.code == 2


def test_disk_grow_makes_the_disk_larger_through_the_helper_and_says_the_size(task, capsys):
    assert main(["disk", "grow", "demo-1", "40g"]) == 0
    assert task.helper.disk_ops() == [
        ("attach-disk", {"dir": str(task.root), "size": "20g"}),
        ("grow-disk", {"dir": str(task.root), "size": "40g"}),
    ], "attached first (a restart of the machine detaches every disk), then grown"
    assert "40g" in capsys.readouterr().out


def test_disk_grow_refuses_a_size_the_helper_would_before_asking_it(task, capsys):
    for size in ("500m", "3t", "twenty", "20"):
        assert main(["disk", "grow", "demo-1", size]) == 1, size
        assert "size" in capsys.readouterr().err
    assert task.helper.disk_ops() == []


def test_a_grow_the_helper_refused_is_said_in_its_words(task, capsys, monkeypatch):
    def refusing(cmd, timeout=None):
        if list(cmd)[:1] == ["sudo"] and cmd[3] == "grow-disk":
            said = (
                "vivibox-host: /srv/vivibox/demo-1/disk.img is 42949672960 bytes already; a disk only grows"
            )
            return subprocess.CompletedProcess(list(cmd), 1, "", said)
        return task.helper(cmd)

    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=refusing))
    assert main(["disk", "grow", "demo-1", "40g"]) == 1
    err = capsys.readouterr().err
    assert "the task's disk could not be grown" in err and "a disk only grows" in err


def with_machine_limits(env, text: str) -> None:
    """config.toml's [limits] on this machine."""
    path = env / "config" / "config.toml"
    path.write_text(path.read_text() + "[limits]\n" + text)


def test_a_tasks_limit_is_checked_against_this_machines_pod_not_the_defaults(env, task, capsys):
    """The reviewer's case A: a strong machine, pod_memory 16g; raising the agent to 8g for one
    task is what the setting is for."""
    with_machine_limits(env, 'pod_memory = "16g"\n')
    assert main(["limits", "demo-1", "--agent-memory", "8g"]) == 0
    assert json.loads((task.meta / "limits.json").read_text()) == {"agent_memory": "8g"}
    assert actions.task_pod("demo-1").limits.memory_of("agent") == 8 * 2**30


def test_a_tasks_limit_past_this_machines_pod_is_refused_though_the_default_would_take_it(env, task, capsys):
    """The reviewer's case B: pod_memory 4g; 5g for the agent would be written against the
    defaults' 6 GiB and refused at the task's start instead, with the task stuck on its file."""
    with_machine_limits(env, 'pod_memory = "4g"\nagent_memory = "4g"\n')
    assert main(["limits", "demo-1", "--agent-memory", "5g"]) == 1
    err = capsys.readouterr().err
    assert "limits.agent_memory" in err and "4 GiB" in err
    assert not (task.meta / "limits.json").exists()
    assert actions.task_pod("demo-1").limits.memory_of("agent") == 4 * 2**30, "the task starts as before"


def test_cpus_past_this_machines_are_refused_for_a_task_too(task, capsys, monkeypatch):
    import os

    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(4)))
    assert main(["limits", "demo-1", "--pod-cpus", "8"]) == 1
    err = capsys.readouterr().err
    assert "limits.pod_cpus" in err and "4 CPUs" in err
    assert not (task.meta / "limits.json").exists()


def test_disk_grow_hands_the_helper_the_size_in_its_own_shape_and_echoes_yours(task, capsys):
    """The helper takes 1536m or 40g, never 1.5g or 40GiB; the command line takes what you
    write, and says it back as you wrote it."""
    assert main(["disk", "grow", "demo-1", "1.5g"]) == 0
    assert main(["disk", "grow", "demo-1", "40GiB"]) == 0
    sizes = [spec["size"] for op, spec in task.helper.disk_ops() if op == "grow-disk"]
    assert sizes == ["1536m", "40g"]
    out = capsys.readouterr().out
    assert "1.5g" in out and "40GiB" in out
