"""The planner's control container: which flows have one, its network and its rules, what it is
made of, and what the writer's container no longer holds."""

import json
from pathlib import Path

import pytest
from test_pod import FakeDocker, applied_policy

from vivibox import orchestration, pod_network
from vivibox import task as task_module
from vivibox.config import Config, HostService, Role
from vivibox.pod import Pod
from vivibox.task import create_task

TEMPLATE = "+++\n+++\n\n## Acceptance criteria\n\n- [ ] x\n"
# Every flow, and whether its planner is an agent other than the writer's.
FLOWS = [
    ("single_agent", False),
    ("planner_executor", True),
    ("planner_writer_reviewer", True),
    ("supervisor_worker", True),
]


def config(mode: str) -> Config:
    roles = {"planner": Role("opencode", "a/m"), "writer": Role("opencode", "b/m")}
    return Config(Path("/t"), 3, roles, orchestration=mode)


def a_pod(tmp_path: Path, control: bool) -> Pod:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    docker = FakeDocker()
    docker.sys_net = pod_network.SYS_NET
    return Pod(
        "shop-1", repo, "vivibox-agent:abc", [HostService("host.docker.internal", 5432)], runner=docker,
        folder=tmp_path, control_mounts=[] if control else None,
    )  # fmt: skip


@pytest.mark.parametrize(("mode", "control"), FLOWS)
def test_a_flow_has_a_control_container_when_its_planner_is_not_its_writer(tmp_path, mode, control):
    """One answer for the pod, the keys, the harnesses and the window: the planner's conversation
    is kept from the writer where the two are different agents; one agent that plans and writes
    has nothing to keep apart, and a box has no planner."""
    assert orchestration.MODES[mode].control is control
    task = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)
    assert orchestration.has_control(task, config(mode)) is control
    assert orchestration.has_control(None, config(mode)) is control
    st = task.read_state()
    st.box = True
    task._write_state(st)
    assert orchestration.has_control(task, config(mode)) is False, "a box: you, in the agent's container"


def test_a_pod_with_a_control_container_has_the_planners_network_and_its_rules(tmp_path):
    """The planner's network is made with the writer's and the reviewer's, on its recorded range
    and its own bridge, before the host's rules; the rules list it, with none of the host's
    services: the planner reads and runs nothing."""
    pod = a_pod(tmp_path, control=True)
    pod.up()
    made = pod.runner.find("docker", "network", "create")
    assert [c[-1] for c in made] == ["vivibox-shop-1-net", "vivibox-shop-1-r", "vivibox-shop-1-p"]
    planners = made[2]
    assert planners[planners.index("--subnet") + 1] == "198.51.100.24/29"
    assert planners[planners.index("--gateway") + 1] == "198.51.100.25"
    assert "com.docker.network.bridge.name=vbx-shop-1-p" in planners
    calls = pod.runner.calls
    applied = next(i for i, c in enumerate(calls) if c[:1] == ["sudo"] and c[3] == "apply-policy")
    assert all(calls.index(c) < applied for c in made), "all three before the rules"
    nets = applied_policy(pod)["networks"]
    assert [(n["role"], n["bridge"], n["subnet"]) for n in nets] == [
        ("w", "vbx-shop-1-w", "198.51.100.0/28"),
        ("r", "vbx-shop-1-r", "198.51.100.16/29"),
        ("p", "vbx-shop-1-p", "198.51.100.24/29"),
    ]
    assert nets[0]["hostsvc"] == ["198.51.100.1:5432"]
    assert nets[1]["hostsvc"] == [] and nets[2]["hostsvc"] == [], "the writer's network alone"


def test_a_pod_without_a_control_container_has_no_planners_network_nor_rules_for_one(tmp_path):
    """A single agent: the planner's range stays reserved with the task, and nothing is made on it."""
    pod = a_pod(tmp_path, control=False)
    pod.up()
    assert [c[-1] for c in pod.runner.find("docker", "network", "create")] == [
        "vivibox-shop-1-net", "vivibox-shop-1-r",
    ]  # fmt: skip
    assert [n["role"] for n in applied_policy(pod)["networks"]] == ["w", "r"]
    assert [n.role for n in pod.networks()] == ["w", "r", "p"], "recorded all the same"


def test_a_planners_network_gone_while_the_task_was_stopped_is_made_again_on_its_range(tmp_path):
    pod = a_pod(tmp_path, control=True)
    pod.up()
    pod.runner.networks.discard("vivibox-shop-1-p")
    before = len(pod.runner.calls)
    pod.ensure_networks()
    made = [c for c in pod.runner.calls[before:] if c[:3] == ["docker", "network", "create"]]
    assert [c[-1] for c in made] == ["vivibox-shop-1-p"]
    assert made[0][made[0].index("--subnet") + 1] == "198.51.100.24/29"


def test_removing_a_task_removes_the_planners_network_whether_it_had_one(tmp_path):
    """By name, as the reviewer's: a task whose flow changed, or from before, leaves none behind."""
    for control in (True, False):
        pod = a_pod(tmp_path, control)
        pod.remove()
        gone = [c[-1] for c in pod.runner.find("docker", "network", "rm")]
        assert gone == ["vivibox-shop-1-net", "vivibox-shop-1-r", "vivibox-shop-1-p"]
        absent = json.loads([c for c in pod.runner.find("sudo") if c[3] == "apply-policy"][-1][5])
        assert [n["role"] for n in absent["networks"]] == ["w", "r", "p"]


def test_a_task_from_before_the_control_container_is_from_an_earlier_vivibox():
    """6: the planner in a container of its own. A task created under 5 has its planner's
    conversation in the writer's container, and is not carried over: deleted and created again."""
    assert task_module.TASK_SCHEMA_VERSION == 6
    assert task_module.classify(5) == task_module.EARLIER
