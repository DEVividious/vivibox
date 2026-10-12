"""The planner's control container: which flows have one, its network and its rules, what it is
made of, and what the writer's container no longer holds."""

import json
import shutil
import subprocess
from functools import partial
from pathlib import Path

import pytest
from conftest import as_verified, global_git_config
from test_pod import FakeDocker, applied_policy, flag, starts

from vivibox import (
    actions,
    claudecode,
    control,
    gate,
    image,
    keys,
    opencode,
    orchestration,
    pod_network,
    secrets,
    supervise,
)
from vivibox import task as task_module
from vivibox.config import Config, HostService, Role, load_config
from vivibox.harness import Turn
from vivibox.pod import CACHES, SOCKET_DIR, ContainerSide, Mount, Pod, PodError
from vivibox.states import State
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
    task = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE, orchestration=mode)
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
    for with_one in (True, False):
        pod = a_pod(tmp_path, with_one)
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


# --- the container ------------------------------------------------------------------------------


@pytest.fixture
def docker(env, monkeypatch) -> FakeDocker:
    """A machine whose planner and writer run on different providers, a key for each, and a
    Docker that records what tasks' pods ask of it."""
    cfg = env / "config" / "config.toml"
    cfg.write_text(
        f'tasks_dir = "{env / "tasks"}"\n'
        '[roles.planner]\nharness = "opencode"\nmodel = "strong/plans"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "cheap/writes"\n'
    )
    for name in ("strong", "cheap", "anthropic"):
        keys.set_key(name, f"key-{name}")
    fake = FakeDocker()
    fake.sys_net = pod_network.SYS_NET
    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=fake))
    monkeypatch.setattr(image, "env", lambda ref: {})
    return fake


def made(docker: FakeDocker, name: str) -> list[str]:
    """The docker run that made the container of that name, last."""
    return docker.find("docker", "run", "-d", "--name", name)[-1]


def binds(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]


def variables(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]


def test_the_control_container_is_a_readers_with_a_home_and_a_network_of_its_own(docker):
    """The reviewer's recipe: the root read-only, no capability, no Docker socket, no build
    cache, nothing a build installed, the writer's clone read-only; and its own: a home on the
    task's disk that outlives a turn, its secrets, its harness files, the planner's network at
    its pinned address with the host's forwarder for DNS, and the control container's limits."""
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    cmd = made(docker, f"vivibox-{task.id}-control")
    for given in (["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"], ["--read-only"]):
        i = cmd.index(given[0])
        assert cmd[i : i + len(given)] == given
    assert "--privileged" not in cmd and "--user" not in cmd
    joined, mounted = " ".join(cmd), binds(cmd)
    assert SOCKET_DIR not in joined and "vivibox-cache-" not in joined and "DOCKER_HOST" not in joined
    assert not any(path in joined for path in CACHES.values()), "none of the build's caches"
    assert "installed" not in joined, "nothing a build installed"
    copy = f"{task.root}/disk/control-src:{task.repo}:ro"
    assert copy in mounted, "its own copy of the commits, read-only, where the writer has its clone"
    assert [m for m in mounted if str(task.repo) in m] == [copy], "the writer's clone is not mounted"
    assert f"{task.root}/disk/config-control:/config" in mounted
    for made_here in ("config-control", "control-src", "control-work"):
        assert (task.root / "disk" / made_here).is_dir(), "made by vivibox, on the task's disk"
    handoff = f"{task.meta / 'handoff'}:/task/handoff"
    replies = f"{task.meta / 'comments.md'}:/task/handoff/comments.md:ro"
    assert mounted.index(f"{task.meta}:/task:ro") < mounted.index(handoff) < mounted.index(replies)
    assert f"{task.meta / 'review'}:/task/review" in mounted, "where a planner that reviews writes"
    assert f"{task.meta / 'harness-control'}:/task/harness:ro" in mounted
    assert f"{secrets.runtime_dir(task.id + '-control')}:/run/vivibox-secrets:ro" in mounted
    assert flag(cmd, "--network") == f"vivibox-{task.id}-p" and not any("container:" in a for a in cmd)
    assert flag(cmd, "--ip") == "198.51.100.26"
    assert flag(cmd, "--add-host") == "host.docker.internal:198.51.100.25"
    assert flag(cmd, "--dns") == "203.0.113.1" and flag(cmd, "--dns-search") == "."
    assert flag(cmd, "--memory") == str(pod.limits.memory_of("control")) == str(3 * 2**29)
    assert flag(cmd, "--memory-swap") == flag(cmd, "--memory")
    assert flag(cmd, "--pids-limit") == "1024"
    assert flag(cmd, "--cgroup-parent") == f"vivibox-{task.id.replace('-', '_')}.slice"
    assert flag(cmd, "--tmpfs") == f"/tmp:exec,mode=1777,size={3 * 2**28}", "half of its memory"
    assert f"{task.root}/disk/control-work:/work:ro" in mounted, "its harness's working directory"
    assert set(variables(cmd)) == {
        "GIT_OPTIONAL_LOCKS=0",  # git never writes to the tree it reads
        f"GIT_DIR={task.repo}/.git",  # and reads the copy of the commits wherever it is run from:
        f"GIT_WORK_TREE={task.repo}",  # the review's `git diff <base>..HEAD` is asked from /work
        "OPENCODE_DISABLE_LSP_DOWNLOAD=true",
        "OPENCODE_CONFIG=/task/harness/opencode.json",
        "CLAUDE_CONFIG_DIR=/config/claude",
        "GIT_CONFIG_COUNT=1",  # git there never takes a committed folder for a bare repository
        "GIT_CONFIG_KEY_0=safe.bareRepository",
        "GIT_CONFIG_VALUE_0=explicit",
    }, "nothing of the build's"
    assert f"{task.root}/disk/control-src:{task.repo}:ro" in mounted, "what GIT_DIR names is the copy"
    assert cmd[cmd.index("-w") + 1] == "/work" and cmd[-3:] == [pod.image, "sleep", "infinity"]


@pytest.mark.parametrize(("mode", "has_one"), FLOWS)
def test_no_container_but_the_planners_mounts_anything_of_the_planners(docker, mode, has_one):
    """No path in a container the writer's code runs in, or in the reviewer's, leads to the
    planner's home, its copy of the work or its secrets: none of them is anywhere in the argv
    of the sidecar, the agent, the verification's container or the reviewer's, in any flow."""
    from vivibox import reviewing

    task = actions.create("demo", "Add divide", orchestration=mode)
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    pod._copying_clone = lambda *cmd, **kw: None  # no clone to copy here
    pod.gate_up()
    as_verified(task)
    reviewing.up(task, pod, load_config())
    runs = {run[4].rsplit("-", 1)[1]: " ".join(run) for run in docker.find("docker", "run", "-d", "--name")}
    assert set(runs) == {"dind", "agent", "gate", "review"} | ({"control"} if has_one else set())
    for name, argv in runs.items():
        if name == "control":
            continue
        for planners in (
            "config-control",
            "control-src",
            "control-work",
            f"{task.id}-control",
            "harness-control:",
        ):
            assert planners not in argv, f"{name} has {planners}"
    assert f"{task.root}/disk/config:/config" in runs["agent"], "its own home, as before"


@pytest.mark.parametrize(("mode", "has_one"), FLOWS)
def test_each_flow_makes_its_containers_and_networks(docker, mode, has_one):
    """Which containers and networks a start makes: the control container and the planner's
    network exactly where the flow has a planner other than the writer; a single agent has
    neither, and its rules list no planner's network."""
    task = actions.create("demo", "Add divide", orchestration=mode)
    pod = actions.task_pod(task.id)
    assert pod.has_control is has_one
    pod.up(timeout=1)
    ran = [c[4] for c in docker.find("docker", "run", "-d", "--name")]
    nets = [c[-1] for c in docker.find("docker", "network", "create")]
    pair = [f"vivibox-{task.id}-dind", f"vivibox-{task.id}-agent"]
    both = [f"vivibox-{task.id}-net", f"vivibox-{task.id}-r"]
    assert ran == pair + ([f"vivibox-{task.id}-control"] if has_one else [])
    assert nets == both + ([f"vivibox-{task.id}-p"] if has_one else [])
    listed = applied_policy(pod)["networks"]
    assert [n["role"] for n in listed] == ["w", "r"] + (["p"] if has_one else [])
    assert [n["hostsvc"] for n in listed[1:]] == [[] for _ in listed[1:]]


def test_every_container_that_mounts_the_handoff_mounts_your_replies_over_it(docker, tmp_path):
    """The handoff is the agents' to write; your replies are a read-only mount over it, in every
    container that gets the folder, read-write or under /task: the agent's, the control
    container's and the reviewer's."""
    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    pod._copying_clone = lambda *cmd, **kw: None  # no clone to copy here
    from vivibox import reviewing

    as_verified(task)
    reviewing.up(task, pod, load_config())
    pod.gate_up()
    replies = f"{task.meta / 'comments.md'}:/task/handoff/comments.md:ro"
    seen, made = [], docker.find("docker", "run", "-d", "--name")
    for run in made:
        mounted = binds(run)
        over = [m for m in mounted if m.split(":")[1] in ("/task", "/task/handoff")]
        if over:
            seen.append(run[4].rsplit("-", 1)[1])
            assert replies in mounted, run[4]
            assert all(mounted.index(replies) > mounted.index(m) for m in over), run[4]
    assert seen == ["agent", "control", "review"]
    # Every container a task has was made here; the two that are not named above have nothing
    # of the task's folder at all, by any path in the container.
    assert [run[4].rsplit("-", 1)[1] for run in made] == ["dind", "agent", "control", "review", "gate"]
    for run in (made[0], made[-1]):
        assert not [m for m in binds(run) if m.split(":")[0].startswith(str(task.meta))], run[4]


def test_the_control_container_starts_after_the_agent_on_rules_already_loaded(docker):
    task = actions.create("demo", "Add divide", orchestration="planner_executor")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    names = [f"vivibox-{task.id}-{end}" for end in ("dind", "agent", "control")]
    assert starts(docker.calls) == ["apply-policy present", *names]
    before = len(docker.calls)
    pod.up(timeout=1)
    assert not [c for c in docker.calls[before:] if c[:2] == ["docker", "run"]], "running: left as it is"


def test_a_control_container_whose_network_is_not_on_its_bridge_is_not_started(docker):
    """Fail-closed, as at any start: the planner's network made another way is on a bridge the
    host's rules are not keyed by, and nothing of the pod starts on it."""
    task = actions.create("demo", "Add divide", orchestration="planner_executor")
    pod = actions.task_pod(task.id)
    pod.ensure_networks()
    shutil.rmtree(pod_network.SYS_NET / f"vbx-{task.id}-p")  # there by name, on another bridge
    with pytest.raises(PodError, match=rf"vivibox-{task.id}-p is not on the bridge vbx-{task.id}-p"):
        pod.up(timeout=1)
    assert not docker.find("docker", "run"), "no container at all"


def test_a_control_container_is_not_started_when_the_host_refuses_the_rules(docker):
    task = actions.create("demo", "Add divide", orchestration="planner_executor")
    pod = actions.task_pod(task.id)
    docker.refuses, docker.refuses_only = "nft: no", "apply-policy"
    with pytest.raises(PodError, match="nft: no"):
        pod.up(timeout=1)
    assert not docker.find("docker", "run")


def test_a_pod_without_a_control_container_gives_the_planner_no_container(tmp_path):
    """Never the writer's in its place: asking for the control container's side, or its argv, of
    a pod that has none is an error, not the agent's container."""
    pod = a_pod(tmp_path, control=False)
    with pytest.raises(PodError, match="no container for the planner"):
        pod.control_side()
    with pytest.raises(PodError, match="no container for the planner"):
        pod.control_command()
    with_one = a_pod(tmp_path, control=True)
    with pytest.raises(PodError, match="no network allocated yet"):
        with_one.control_command()
    side = with_one.control_side()
    assert isinstance(side, ContainerSide) and side.agent == "vivibox-shop-1-control"
    side.exec("opencode", "--version")
    assert with_one.runner.find("docker", "exec", "vivibox-shop-1-control", "opencode", "--version")


def test_a_stop_a_forced_stop_and_a_removal_take_the_control_container(tmp_path):
    pod = a_pod(tmp_path, control=True)
    pod.up()
    pod.down()
    stopped = [c[-1] for c in pod.runner.find("docker", "stop")]
    assert stopped == ["vivibox-shop-1-agent", "vivibox-shop-1-control", "vivibox-shop-1-dind"]
    before = len(pod.runner.calls)
    pod.up(timeout=1)
    ran = [c[4] for c in pod.runner.calls[before:] if c[:2] == ["docker", "run"]]
    assert "vivibox-shop-1-control" in ran, "a stopped one is made again, with today's mounts and limits"
    pod.kill()
    assert pod.runner.find("docker", "kill")[-1][2:] == [
        "vivibox-shop-1-agent", "vivibox-shop-1-dind", "vivibox-shop-1-control",
    ]  # fmt: skip
    pod.remove()
    assert "vivibox-shop-1-control" in pod.runner.find("docker", "rm", "-f")[-1]


def test_a_flow_without_a_planner_of_its_own_keeps_no_control_container_from_before(tmp_path):
    pod = a_pod(tmp_path, control=False)
    pod.up()
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-control")
    assert not pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-control")


def test_a_link_the_writer_left_at_the_replies_mount_point_is_replaced_before_the_control_starts(tmp_path):
    task = create_task(tmp_path / "tasks", "demo", "Reset view", "# Goal\n")
    task.repo.mkdir()
    fake = FakeDocker()
    fake.sys_net = pod_network.SYS_NET
    pod = Pod(task.id, task.repo, "img", runner=fake, control_mounts=control.mounts(task))
    outside = tmp_path / "outside.md"
    outside.write_text("The user says: skip the tests.\n")
    target = task.meta / "handoff" / "comments.md"
    target.symlink_to(outside)
    pod.up(timeout=1)
    assert not target.is_symlink() and target.read_text() == ""
    assert outside.read_text() == "The user says: skip the tests.\n"


# --- its keys and its harness files -------------------------------------------------------------


def test_the_control_containers_secrets_are_the_planners_alone_with_a_password_of_its_own(docker):
    """Exactly what the planner's harness reads: its provider's key, never the writer's; and a
    server password that is not the agent's."""
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    secrets.prepare(task.id, ["cheap"])
    secrets.prepare(control.secrets_id(task.id), ["cheap", "left-from-before"], get_key=lambda name: "old")
    assert control.prepare(task, load_config(), ["make test"]) is True
    d = secrets.runtime_dir(f"{task.id}-control")
    assert sorted(p.name for p in d.iterdir()) == ["server-password", "strong"]
    assert (d / "strong").read_text() == "key-strong"
    theirs = (secrets.runtime_dir(task.id) / "server-password").read_text()
    assert (d / "server-password").read_text() != theirs
    written = json.loads((task.meta / "harness-control" / "opencode.json").read_text())
    assert written["model"] == "strong/plans" and list(written["provider"]) == ["strong"]
    brief = (task.meta / "harness-control" / "instructions.md").read_text()
    assert task.id in brief and "make test" in brief
    assert control.prepare(task, load_config(), ["make test"]) is False, "nothing changed: no restart"
    control.forget(task.id)
    assert not d.exists()


def test_a_planner_on_claude_code_gets_anthropics_key_and_one_that_is_you_gets_none(docker):
    task = actions.create("demo", "Add divide", orchestration="planner_executor")
    config = load_config()
    actions.choose_role(task, "planner", ("claude-code", "claude-opus"), config)
    control.prepare(task, config)
    d = secrets.runtime_dir(f"{task.id}-control")
    assert sorted(p.name for p in d.iterdir()) == ["anthropic", "server-password"]
    assert not (task.meta / "harness-control" / "opencode.json").exists()
    assert (task.meta / "harness-control" / "instructions.md").exists()
    actions.choose_role(task, "planner", ("manual", ""), config)
    control.prepare(task, config)
    assert sorted(p.name for p in d.iterdir()) == ["server-password"]


def test_mounts_are_made_before_docker_could_make_them_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    task = create_task(tmp_path / "tasks", "demo", "Reset view", "# Goal\n")
    found = control.mounts(task)
    assert all(isinstance(m, Mount) for m in found)
    assert all(Path(m.source).exists() for m in found)
    assert (secrets.runtime_dir(f"{task.id}-control").stat().st_mode & 0o777) == 0o700


# --- what the planner reads ---------------------------------------------------------------------


def git(where: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(clone: Path, name: str, text: str) -> str:
    (clone / name).write_text(text)
    git(clone, "add", name)
    git(clone, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", f"Add {name}")
    return git(clone, "rev-parse", "HEAD")


@pytest.fixture
def reading(docker):
    """A supervisor_worker task whose pod is up: the planner's copy has its folder."""
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    return task, pod


def test_the_planner_reads_a_copy_of_the_commits_that_the_host_keeps_up_to_date(reading):
    """git diff <base>..HEAD and git log work there, on the task's branch, at the writer's last
    commit after each refresh; what the writer has not committed is not there; and reading
    leaves the writer's clone as it was."""
    task, pod = reading
    base = task.read_state().base_commit
    control.refresh(task, pod)
    copy = pod.control_src
    assert git(copy, "rev-parse", "HEAD") == base
    assert git(copy, "branch", "--show-current") == f"vivibox/{task.id}"
    head = commit(task.repo, "calc.py", "def divide(a, b):\n    return a / b\n")
    (task.repo / "scratch.txt").write_text("not committed\n")
    before = git(task.repo, "status", "--porcelain")
    kept = sorted(p.name for p in (task.repo / ".git").iterdir())
    control.refresh(task, pod)
    assert git(copy, "rev-parse", "HEAD") == head
    assert "calc.py" in git(copy, "diff", "--stat", f"{base}..HEAD")
    assert "Add calc.py" in git(copy, "log", "--oneline")
    assert not (copy / "scratch.txt").exists(), "only what is committed"
    assert git(task.repo, "status", "--porcelain") == before
    assert sorted(p.name for p in (task.repo / ".git").iterdir()) == kept, "nothing written into the clone"
    (copy / "calc.py").write_text("changed in the copy\n")  # the mount is read-only; were it not
    control.refresh(task, pod)
    assert "return a / b" in (copy / "calc.py").read_text(), "the commits again, whatever was there"


def test_the_copy_is_checked_out_without_your_git_lfs_fetching_anything(reading, tmp_path, monkeypatch):
    """A filter of your own git configuration (git-lfs: required, and asking a remote the copy
    does not have) would stop the checkout; the copy holds what the commits hold, the pointer."""
    task, pod = reading
    yours = (
        "[user]\n\tname = T\n\temail = t@example.com\n"
        '[filter "lfs"]\n\tsmudge = false\n\tprocess = false\n\trequired = true\n'
    )
    # The pointer goes in as it is: a git-lfs this machine's /etc/gitconfig names (a CI runner's)
    # would clean it at the add and at the commit, and install its hooks in the clone, which the
    # refresh then refuses. Only the test's own git: vivibox's takes no GIT_* from here.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (task.repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    (task.repo / "model.bin").write_text("version https://git-lfs.github.com/spec/v1\n")
    subprocess.run(["git", "-C", str(task.repo), "add", "."], check=True)
    git(task.repo, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "Add model")
    global_git_config(monkeypatch, tmp_path / "home", yours)
    control.refresh(task, pod)
    assert (pod.control_src / "model.bin").read_text().startswith("version https://git-lfs")


def planted(task, tmp: Path) -> Path:
    """What a writer can leave in its clone for the next git there: a commondir whose config
    names a command for git diff. Returns the file the command would write."""
    ran, evil = tmp / "ran", tmp / "evil"
    script = tmp / "run.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {ran}\n")
    script.chmod(0o755)
    shutil.copytree(task.repo / ".git", evil)
    subprocess.run(["git", f"--git-dir={evil}", "config", "diff.external", str(script)], check=True)
    (task.repo / ".git" / "commondir").write_text(f"{evil}\n")
    return ran


def test_git_in_the_writers_clone_would_run_what_the_writer_planted(reading, tmp_path):
    """Why the control container does not mount the writer's clone, read-only or not: git there
    follows the clone's commondir to a config, and git diff runs the command it names."""
    task, _ = reading
    base = task.read_state().base_commit
    commit(task.repo, "calc.py", "x = 1\n")
    ran = planted(task, tmp_path)
    subprocess.run(["git", "-C", str(task.repo), "diff", f"{base}..HEAD"], capture_output=True)
    assert ran.exists(), "the reader of the clone ran the writer's command"


def test_git_in_the_planners_copy_runs_nothing_the_writer_planted(reading, tmp_path, monkeypatch):
    """The refresh refuses a clone that is not as recorded; and past the check (a file planted
    in the moment after it), the copy has the commits and a .git of the host's making: git diff
    there runs no command of the writer's."""
    from vivibox import repo

    task, pod = reading
    base = task.read_state().base_commit
    control.refresh(task, pod)
    head = commit(task.repo, "calc.py", "x = 1\n")
    ran = planted(task, tmp_path)
    with pytest.raises(repo.RepoError, match="commondir"):
        control.refresh(task, pod)
    assert git(pod.control_src, "rev-parse", "HEAD") == base, "nothing taken from a clone not as recorded"
    monkeypatch.setattr(repo, "guard", lambda clone: None)  # the race: planted after the check
    control.refresh(task, pod)
    assert git(pod.control_src, "rev-parse", "HEAD") == head
    assert "calc.py" in git(pod.control_src, "diff", "--stat", f"{base}..HEAD")
    git(pod.control_src, "diff", f"{base}..HEAD")
    assert not (pod.control_src / ".git" / "commondir").exists()
    assert not ran.exists(), "no command of the writer's ran, on the host or for the planner"


def test_a_copy_without_its_folder_is_not_made_elsewhere(docker):
    """Its folder is the pod's to make, on the task's disk: a refresh before the pod is up, or
    with a link in the folder's place, copies nothing."""
    from vivibox import repo

    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    pod = actions.task_pod(task.id)
    with pytest.raises(repo.RepoError, match="start the task again"):
        control.refresh(task, pod)


def test_the_planners_harness_reads_the_work_as_it_is_before_each_turn():
    class Harness:
        name, manual = "opencode", False

        def __init__(self):
            self.turns = []

        def turn(self, prompt, session="", title="", on_step=None):
            self.turns.append((prompt, session, title, on_step, list(order)))
            return "turn"

    order: list[str] = []
    inner = Harness()
    reader = control.Reader(inner, lambda: order.append("refreshed"))
    assert reader.turn("Review.", session="ses_1", title="t") == "turn"
    assert inner.turns == [("Review.", "ses_1", "t", None, ["refreshed"])], "first the copy, then the turn"
    assert reader.name == "opencode" and reader.manual is False, "the rest is the harness's own"


# --- which container a role's harness talks to --------------------------------------------------


@pytest.mark.parametrize(("mode", "has_one"), FLOWS)
def test_the_planners_harness_is_bound_to_its_own_container_and_the_writers_to_the_agents(
    docker, mode, has_one
):
    """The planner talks through a server in the control container wherever the task has one, on
    the usual port (another network namespace than the writer's); a single agent's planner is
    the writer's own agent. The writer's harness is the agent's container's in every flow."""
    task = actions.create("demo", "Add divide", orchestration=mode)
    pod = actions.task_pod(task.id)
    planner = actions.harness_for("planner", pod, task)
    writer = actions.harness_for("writer", pod, task)
    wanted = f"vivibox-{task.id}-control" if has_one else f"vivibox-{task.id}-agent"
    assert planner.pod.agent == wanted and planner.port == opencode.PORT
    assert writer.pod is pod and writer.pod.agent == f"vivibox-{task.id}-agent"
    assert planner.attach_command("ses_1")[:4] == ["docker", "exec", "-it", wanted]
    planner.healthy()
    probe = docker.find("docker", "exec", wanted, "bash", "-c")[-1][-1]
    assert "/run/vivibox-secrets/server-password" in probe and ":39417/session" in probe
    actions.choose_role(task, "planner", ("claude-code", "claude-opus"), load_config())
    if mode != "single_agent":  # a single agent writes, and only opencode writes yet
        on_claude = actions.harness_for("planner", pod, task)
        assert isinstance(on_claude, claudecode.ClaudeCode) and on_claude.pod.agent == wanted
        assert on_claude.attach_command("s")[:4] == ["docker", "exec", "-it", wanted]


def test_a_planner_is_never_given_the_writers_container_in_place_of_its_own(docker):
    """Fail-closed: a pod made without the control container, in a flow that has one, gives the
    planner no harness at all."""
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    without = Pod(task.id, task.repo, "img", runner=docker)
    with pytest.raises(PodError, match="its pod has none"):
        actions.harness_for("planner", without, task)
    assert actions.harness_for("writer", without, task).pod is without


def test_w_on_the_planners_conversation_enters_the_control_container(docker, monkeypatch):
    task = actions.create("demo", "Add divide", orchestration="planner_executor")
    task.set_session("planner", "ses_planner")
    task.set_session("writer", "ses_writer")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    st = task.read_state()
    planners = actions.view_command(task, st, "planner")
    assert planners[:4] == ["docker", "exec", "-it", f"vivibox-{task.id}-control"]
    assert "--session 'ses_planner'" in planners[-1] and ":39417" in planners[-1]
    writers = actions.view_command(task, st, "writer")
    assert writers[:4] == ["docker", "exec", "-it", f"vivibox-{task.id}-agent"]
    assert "--session 'ses_writer'" in writers[-1]
    alone = actions.create("demo", "Other", orchestration="single_agent")
    alone.set_session("planner", "ses_agent")
    shown = actions.view_command(alone, alone.read_state(), "planner")
    assert shown[:4] == ["docker", "exec", "-it", f"vivibox-{alone.id}-agent"], "one agent: the agent's"


# The flows where another agent than the writer's reads the work: a reviewer, or the planner.
READ_BY_ANOTHER = {"planner_writer_reviewer", "supervisor_worker"}


@pytest.mark.parametrize(("mode", "has_one"), FLOWS)
def test_the_writer_cannot_write_the_review_another_agent_writes(docker, mode, has_one):
    """The host reads the verdict in the task's review folder. Where the planner or a reviewer
    writes it from a container of its own, the agent's container has the folder read-only,
    under /task; where the writer reviews its own work, as before."""
    task = actions.create("demo", "Add divide", orchestration=mode)
    actions.task_pod(task.id).up(timeout=1)
    mounted = binds(made(docker, f"vivibox-{task.id}-agent"))
    writable = f"{task.meta / 'review'}:/task/review"
    assert (writable in mounted) is (mode not in READ_BY_ANOTHER)
    assert not any(m.split(":")[1] == "/task/review" and writable != m for m in mounted)
    assert f"{task.meta}:/task:ro" in mounted, "read-only there, with the rest of the task's files"
    if has_one:
        assert writable in binds(made(docker, f"vivibox-{task.id}-control")), "the planner's to write"


# --- the planner's conversation, carried on in its own container --------------------------------

PLAN = (
    '+++\nmode = "code-only"\nsummary = "Add divide"\nverify = []\n+++\n\n# Goal\n\nAdd divide\n\n'
    "## Acceptance criteria\n\n- [ ] divide(6, 3) returns 2\n"
)
BLOCKING = "## Blocking\n\n- calc.py:1 — divide(1, 0) raises\n\n## Not blocking\n\n## Checked\n\n- the diff\n"
CLEAN = "## Blocking\n\n## Not blocking\n\n## Checked\n\n- the diff\n"


class Seen(list):
    """The turns and refreshes seen, and the task they are of."""

    def __init__(self):
        super().__init__()
        self.task: list = []
        self.prompts: list[tuple[str, str]] = []  # every turn's whole prompt, by its container


@pytest.fixture
def turns(docker, monkeypatch):
    """Every turn opencode is asked for, as (container, session, what the prompt ends with),
    answered by writing what the state's prompt asks for; and every refresh of the planner's copy."""
    seen = Seen()
    reviews = [BLOCKING, CLEAN]

    def turn(self, prompt, session="", title="", on_step=None):
        task = seen.task[0]
        seen.append((self.pod.agent.rsplit("-", 1)[1], session, prompt.strip().splitlines()[-1]))
        seen.prompts.append((seen[-1][0], prompt))
        reviewing = prompt.rstrip().endswith("when the review is written.")
        if seen[-1][0] == "control" and not reviewing:  # a planning turn
            (task.meta / "handoff" / "plan-draft.md").write_text(PLAN)
        if reviewing:
            (task.meta / "review" / "review.md").write_text(reviews.pop(0))
        return Turn(session or f"ses_{len(seen)}", True, 0.01, 10, "done")

    monkeypatch.setattr(opencode.OpenCode, "turn", turn)
    monkeypatch.setattr(opencode.OpenCode, "start_session", lambda self, title: "")
    monkeypatch.setattr(
        control, "refresh", lambda task, pod, commit="", **_: seen.append(("copy", commit, "refreshed"))
    )
    return seen


# The commit the stand-in verification says it ran on.
VERIFIED = "c" * 40


def supervised(mode: str, turns) -> tuple:
    task = actions.create("demo", "Add divide", orchestration=mode)
    turns.task.append(task)
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, actions.task_pod(task.id), load_config())
    sup.ports.run_gate = lambda t: gate.GateResult(Path("/dev/null"), commit=VERIFIED)
    sup.ports.prepare_review = lambda: None
    sup.ports.head = lambda: ""
    return task, sup


def test_the_planner_plans_again_after_your_comment_in_the_same_conversation_in_its_container(turns):
    """PLAN, your comment, PLAN again: the second turn resumes the session the first made, in
    the control container, with the planner's copy brought up to date before each."""
    task, sup = supervised("planner_executor", turns)
    assert isinstance(sup.planner, control.Reader)
    assert sup.planner.harness.pod.agent == f"vivibox-{task.id}-control"
    assert sup.harness.pod.agent == f"vivibox-{task.id}-agent"
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN
    first = task.read_state().sessions["planner"]
    actions.reply(task, "Also divide by zero: return None.")
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN
    # A copy's second word is the commit it was asked to stand on: none while planning, the branch.
    assert [(where, session) for where, session, _ in turns] == [
        ("copy", ""), ("control", ""), ("copy", ""), ("control", first),
    ]  # fmt: skip
    assert "updated" in turns[-1][2], "the comment's prompt, not the plan's again"
    assert task.read_state().sessions == {"planner": first}, "one conversation, the planner's"


def test_the_supervisor_reviews_round_after_round_in_the_conversation_it_planned_in(turns):
    """supervisor_worker: PLAN, then a review after each green verification, all in the planner's
    session in the control container; the writer's turns are in the agent's, in its own."""
    task, sup = supervised("supervisor_worker", turns)
    sup.step()  # plan
    planned_in = task.read_state().sessions["planner"]
    actions.accept_plan(task, actions.load(task.id)[1])
    for _ in range(6):  # implement, verify, review (blocking), implement, verify, review (clean)
        sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and st.reviews == 2
    where = [(place, session) for place, session, _ in turns if place != "copy"]
    assert where == [
        ("control", ""),
        ("agent", ""),
        ("control", planned_in),
        ("agent", st.sessions["writer"]),
        ("control", planned_in),
    ]
    assert st.sessions["planner"] == planned_in != st.sessions["writer"]
    before_each = [turns[i - 1][0] for i, t in enumerate(turns) if t[0] == "control"]
    assert before_each == ["copy"] * 3, "the commits as they are, before every turn of the planner's"
    assert sum(1 for t in turns if t[0] == "copy") == 3, "and before none of the writer's"
    assert st.verified_commit == VERIFIED
    assert [commit for place, commit, _ in turns if place == "copy"] == ["", VERIFIED, VERIFIED], (
        "planning reads the branch as it is; each review reads the commit the verification passed"
    )


# --- a start ------------------------------------------------------------------------------------


@pytest.fixture
def starting(docker, monkeypatch):
    """A real start whose pod and servers are stand-ins that say what they were asked, in order."""
    from vivibox import prepare, toolchain

    order: list[str] = []
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(actions, "available_models", lambda refresh=False: {})
    monkeypatch.setattr(Pod, "up", lambda self: order.append("up"))
    monkeypatch.setattr(control, "refresh", lambda task, pod, commit="", **_: order.append("copy"))
    monkeypatch.setattr(toolchain, "ensure", lambda pod, java, tools=(): order.append("tools"))
    monkeypatch.setattr(prepare, "begin", lambda task, project, pod: None)
    for what in ("ensure_server", "restart_server"):
        monkeypatch.setattr(
            opencode.OpenCode,
            what,
            lambda self, what=what: order.append(f"{what.split('_')[0]} {self.pod.agent.rsplit('-', 1)[1]}"),
        )
    monkeypatch.setattr(opencode.OpenCode, "session_exists", lambda self, session: True)
    return order


@pytest.mark.real_start
def test_a_start_brings_up_the_planners_server_in_its_own_container(starting):
    """After the pod: the planner's copy of the commits before anything of the project runs,
    then each container's own server, started again when its own files changed (a server reads
    its configuration at its start). m on the planner restarts the planner's server alone."""
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    actions.start(task.id, supervise=False)
    assert starting == ["up", "copy", "tools", "restart agent", "restart control"]
    starting.clear()
    actions.start(task.id, supervise=False)
    assert starting == ["up", "copy", "tools", "ensure agent", "ensure control"], "nothing changed"
    starting.clear()
    actions.choose_role(task, "planner", ("opencode", "cheap/plans"), load_config())
    actions.start(task.id, supervise=False)
    assert starting == ["up", "copy", "tools", "ensure agent", "restart control"]
    starting.clear()
    actions.choose_role(task, "planner", ("claude-code", "claude-opus"), load_config())
    actions.start(task.id, supervise=False)
    assert starting == ["up", "copy", "tools", "ensure agent"], "claude has no server to keep"
    assert held(f"{task.id}-control") == ["anthropic"]


@pytest.mark.real_start
def test_a_single_agent_starts_one_server_and_keeps_no_secrets_for_a_planners_container(starting):
    task = actions.create("demo", "Add divide", orchestration="single_agent")
    secrets.prepare(control.secrets_id(task.id), [])  # left by a flow the task had before
    actions.start(task.id, supervise=False)
    assert starting == ["up", "tools", "restart agent"]
    assert not secrets.runtime_dir(f"{task.id}-control").exists()
    assert held(task.id) == ["cheap", "strong"], "one agent on both roles' models"


@pytest.mark.real_start
def test_a_stop_and_a_delete_take_the_planners_secrets(starting, monkeypatch):
    task = actions.create("demo", "Add divide", orchestration="planner_executor")
    actions.start(task.id, supervise=False)
    assert secrets.runtime_dir(f"{task.id}-control").is_dir()
    monkeypatch.setattr(actions, "tmux", lambda *a, **k: None)
    actions.stop(task)
    assert not secrets.runtime_dir(f"{task.id}-control").exists()
    actions.start(task.id, supervise=False)
    actions.remove(task, actions.load(task.id)[1])
    assert not secrets.runtime_dir(f"{task.id}-control").exists() and not task.root.exists()


def held(directory: str) -> list[str]:
    d = secrets.runtime_dir(directory)
    return sorted(p.name for p in d.iterdir() if p.name != "server-password") if d.exists() else []


# --- round 2: the harness's working directory, the task's own flow, the host's git --------------


def test_the_planners_harness_never_works_in_the_copy_of_the_writers_commits(docker):
    """A harness loads project configuration from its working directory and runs what it names
    (an MCP command, a plugin, a hook), and the copy holds what the writer committed. The
    control container's working directory is a folder of the host's making, mounted read-only:
    a git repository with one empty commit (opencode's attached run prints no events of a turn
    outside one), no file and no hook; the planner's opencode
    server and its turns are started there, with no directory of their own; Claude Code is
    given no directory besides (it loads skills from each, and the handoff is the writer's to
    write)."""
    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    cmd = made(docker, f"vivibox-{task.id}-control")
    work = cmd[cmd.index("-w") + 1]
    mounted = {m.split(":")[1]: m for m in binds(cmd)}
    assert work == "/work" and mounted[work] == f"{pod.control_work}:/work:ro", "the host's, read-only"
    assert not any(t.startswith(work + "/") or work.startswith(t + "/") for t in mounted), "nothing over it"
    assert str(task.repo) in mounted and mounted[str(task.repo)].startswith(str(pod.control_src))
    control.refresh(task, pod)
    assert sorted(p.name for p in pod.control_work.iterdir()) == [".git"], "nothing of anyone's but git's own"
    assert not (pod.control_work / ".git" / "hooks").exists(), "no hook, not even a sample"
    assert git(pod.control_work, "rev-list", "--count", "HEAD") == "1"
    assert git(pod.control_work, "ls-tree", "-r", "HEAD") == "", "one empty commit"
    root = git(pod.control_work, "rev-parse", "HEAD")
    control.refresh(task, pod)
    assert git(pod.control_work, "rev-parse", "HEAD") == root, "made once: the same project every turn"
    planner = actions.harness_for("planner", pod, task)
    planner.ensure_server()
    planner.turn("Plan.", session="ses_1")
    for call in docker.find("docker", "exec"):
        if f"vivibox-{task.id}-control" in call:
            assert "-w" not in call[: call.index(f"vivibox-{task.id}-control")], "the container's own"
            assert "cd " not in call[-1] and str(task.repo) not in call[-1]
    actions.choose_role(task, "planner", ("claude-code", "claude-opus"), load_config())
    on_claude = actions.harness_for("planner", pod, task)
    on_claude.turn("Plan.")
    asked = docker.find("docker", "exec", f"vivibox-{task.id}-control", "bash", "-c")[-1][-1]
    assert "claude" in asked and "--add-dir" not in asked
    elsewhere = claudecode.ClaudeCode(pod, "claude-opus")
    elsewhere.turn("Plan.")
    theirs = docker.find("docker", "exec", f"vivibox-{task.id}-agent", "bash", "-c")[-1][-1]
    assert "--add-dir /task/handoff /task/harness" in theirs, "outside the control container, as before"


def set_flow(mode: str) -> None:
    from vivibox import configfile
    from vivibox.config import config_dir

    configfile.set_value(config_dir() / "config.toml", "agent_orchestration_mode", mode)


@pytest.mark.parametrize(
    ("created_under", "changed_to", "where"),
    [("supervisor_worker", "single_agent", "control"), ("single_agent", "supervisor_worker", "agent")],
)
def test_a_tasks_flow_is_the_one_it_was_created_with(docker, created_under, changed_to, where):
    """config.toml's flow is for new tasks: one created under a flow keeps it, its containers
    and where its planner runs, whatever the file says later."""
    set_flow(created_under)
    task = actions.create("demo", "Add divide")
    assert task.read_state().orchestration == created_under, "recorded with the task"
    set_flow(changed_to)
    config = load_config()
    assert config.orchestration == changed_to
    assert orchestration.mode_of(task, config).name == created_under
    assert orchestration.has_control(task, config) is (where == "control")
    pod = actions.task_pod(task.id)
    assert pod.has_control is (where == "control")
    assert actions.harness_for("planner", pod, task).pod.agent == f"vivibox-{task.id}-{where}"
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, pod, config)
    assert sup.mode.name == created_under
    planner = sup.planner.harness if where == "control" else sup.planner
    assert planner.pod.agent == f"vivibox-{task.id}-{where}"
    other = actions.create("demo", "Another")
    assert orchestration.mode_of(other, config).name == changed_to, "a new task takes the file's"


def test_the_planners_container_is_decided_without_reading_the_configuration_again(docker, monkeypatch):
    """The binding asks the pod it was given and the flow the task records. A config.toml that
    says another flow by the time the harness is bound changes nothing; a pod and a recorded
    flow that disagree are an error; and the agent's container is never the answer for a flow
    with a control container."""
    from vivibox import roles

    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    pod = actions.task_pod(task.id)
    monkeypatch.setattr(roles.orchestration, "has_control", lambda task, config: False)  # were it asked
    set_flow("single_agent")
    assert roles.container_of("planner", pod, task).agent == f"vivibox-{task.id}-control"
    alone = actions.create("demo", "Other", orchestration="single_agent")
    with_one = Pod(alone.id, alone.repo, "img", runner=docker, control_mounts=[])
    with pytest.raises(PodError, match="its pod has one"):
        roles.container_of("planner", with_one, alone)
    without = Pod(task.id, task.repo, "img", runner=docker)
    with pytest.raises(PodError, match="its pod has none"):
        roles.container_of("planner", without, task)

    class NotAPod:
        agent = f"vivibox-{task.id}-agent"

    with pytest.raises(PodError, match="its pod|agent's is not given"):
        roles.container_of("planner", NotAPod(), task)
    assert roles.container_of("writer", pod, task) is pod


def test_the_hosts_git_in_the_copy_reads_none_of_your_git_configuration(reading, tmp_path, monkeypatch):
    """A filter driver of your global configuration, picked by a .gitattributes the writer
    committed, would run your program on the writer's content at the checkout; the copy's git
    reads the copy's own configuration alone."""
    task, pod = reading
    ran = tmp_path / "filter-ran"
    program = tmp_path / "smudge.sh"
    program.write_text(f"#!/bin/sh\ntouch {ran}\ncat\n")
    program.chmod(0o755)
    yours = (
        "[user]\n\tname = T\n\temail = t@example.com\n"
        f'[filter "mine"]\n\tsmudge = {program}\n\trequired = true\n'
        "[submodule]\n\trecurse = true\n"
    )
    (task.repo / ".gitattributes").write_text("*.txt filter=mine\n")
    (task.repo / "notes.txt").write_text("notes\n")
    git(task.repo, "add", ".")
    git(task.repo, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "Add notes")
    global_git_config(monkeypatch, tmp_path / "home", yours)
    control.refresh(task, pod)
    assert (pod.control_src / "notes.txt").read_text() == "notes\n"
    assert not ran.exists(), "your filter ran on what the writer committed"


def test_a_review_reads_the_commit_that_was_verified_not_the_branch_as_it_is_now(reading):
    """The writer leaves HEAD on the verified commit and moves the branch on: the copy for the
    review stands on the verified commit. One that is not on the task's branch is refused, and
    while planning the copy is the branch as it is."""
    from vivibox import repo

    task, pod = reading
    verified = commit(task.repo, "calc.py", "x = 1\n")
    later = commit(task.repo, "calc.py", "x = 2  # not verified\n")
    git(task.repo, "checkout", "-q", "--detach", verified)
    assert control.reviewed_commit(task) == "", "planning: the branch"
    control.refresh(task, pod, control.reviewed_commit(task))
    assert git(pod.control_src, "rev-parse", "HEAD") == later
    task.set_verified_commit(verified)
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.REVIEW):
        task.transition(state)
    assert control.reviewed_commit(task) == verified
    control.refresh(task, pod, control.reviewed_commit(task))
    assert git(pod.control_src, "rev-parse", "HEAD") == verified
    assert "x = 1" in (pod.control_src / "calc.py").read_text()
    git(task.repo, "checkout", "-q", "--orphan", "elsewhere")
    stray = commit(task.repo, "stray.py", "y = 1\n")
    with pytest.raises(repo.RepoError, match="is not on the task's branch"):
        control.refresh(task, pod, stray)
    with pytest.raises(repo.RepoError, match="not a commit"):
        control.refresh(task, pod, "--upload-pack=x")
    assert git(pod.control_src, "rev-parse", "HEAD") == verified, "left as it was"


def test_the_planners_working_directory_is_not_made_through_a_link_or_without_its_folder(docker, tmp_path):
    from vivibox import repo

    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    pod = actions.task_pod(task.id)
    with pytest.raises(repo.RepoError, match="working directory has no folder"):
        control.workdir(pod)
    pod.disk.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    pod.control_work.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(repo.RepoError, match="working directory has no folder"):
        control.workdir(pod)
    assert not list((tmp_path / "elsewhere").iterdir())


def test_a_turn_that_printed_no_events_stops_the_task_as_a_failed_turn(tmp_path):
    """opencode ending well with nothing printed is no turn: the supervisor is told it failed,
    does not wait and try again (nothing about it passes with time), and the task stops for
    you with the reason, instead of going on as if the agent had worked."""
    from test_supervisor import TEMPLATE as PLAN_TEMPLATE
    from test_supervisor import make

    class Silent:
        agent, task_id = "vivibox-demo-1-control", "demo-1"

        def exec(self, *cmd, check=True):
            return subprocess.CompletedProcess(cmd, 0, "", "")

    harness = opencode.OpenCode(Silent(), "a/m")
    done = harness.turn("Plan.", session="ses_1")
    assert not done.ok and done.error == opencode.NO_EVENTS
    task = create_task(tmp_path, "demo", "Add health endpoint", PLAN_TEMPLATE)
    waits = []
    sup, notes = make(task, harness)
    sup.ports.sleep = waits.append
    sup.step()
    st = task.read_state()
    assert st.paused and st.problem == f"agent turn failed: {opencode.NO_EVENTS}"
    assert st.state is State.PLAN and waits == [], "no plan checkpoint, and no retry"
    assert notes == [f"agent turn failed, task paused: {opencode.NO_EVENTS}"]


# --- round 5: a reader's configuration, and a task without a flow -------------------------------


def a_large_repository(task) -> None:
    """Enough source files in the clone for Serena's automatic mode to say on."""
    from vivibox import providers

    for n in range(providers.SERENA_MIN_FILES):
        (task.repo / f"module_{n}.py").write_text("x = 1\n")
    assert providers.serena_for(task.repo)[0], "a repository Serena is on for"


def test_the_planners_configuration_is_a_readers_no_language_server_and_no_serena(docker):
    """In its control container the planner reads the writer's commits beside its own key and
    conversation, so nothing there works on the project by itself: opencode's language servers
    are off and none is downloaded, and vivibox's own Serena, whose language servers start on
    the project, is left out, while the MCP servers you brought yourself stay. The agent's
    configuration is as it was: Serena where its mode says so."""
    from vivibox import providers

    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    a_large_repository(task)
    yours = {
        "type": "local",
        "command": ["tools"],
        "environment": {"TOKEN": "{file:/run/vivibox-secrets/mcp.tools.token}"},
    }
    providers._save(providers.mcp_path(), {"tools": yours})
    keys.set_key("mcp.tools.token", "secret")
    config = load_config()
    opencode.prepare(task, "cheap/writes", ["true"], ["cheap"])
    control.prepare(task, config, ["true"])
    agents = json.loads((task.meta / "harness" / "opencode.json").read_text())
    planners = json.loads((task.meta / "harness-control" / "opencode.json").read_text())
    assert sorted(agents["mcp"]) == ["serena", "tools"], "the agent's: yours and Serena"
    assert sorted(planners["mcp"]) == ["tools"], "the planner's: yours alone"
    assert "serena" not in json.dumps(planners)
    assert planners["lsp"] is False
    assert "snapshot" not in planners, "off, opencode 1.18.31 prints no events of a turn's last step"
    assert "lsp" not in agents, "the agent's is not changed"
    needed = providers.mounted(planners)
    assert needed == ["strong", "mcp.tools.token"] and not [n for n in needed if "serena" in n]
    held = sorted(p.name for p in secrets.runtime_dir(f"{task.id}-control").iterdir())
    assert held == ["mcp.tools.token", "server-password", "strong"]
    assert control.env(task)["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "true"
    assert opencode.config("a/m", (), task.repo, reader=True)["lsp"] is False
    assert "lsp" not in opencode.config("a/m", (), task.repo)
    assert list(providers.task_mcp(task.repo, builtin=False)) == ["tools"]
    assert sorted(providers.task_mcp(task.repo)) == ["serena", "tools"]


def without_a_flow(task) -> None:
    """The task's state as a vivibox of this layout wrote it before tasks recorded their flow."""
    path = task.meta / "state.json"
    state = json.loads(path.read_text())
    state["orchestration"] = ""
    path.write_text(json.dumps(state))


def test_a_task_that_records_no_flow_is_not_run_and_can_be_deleted(docker, capsys):
    """Which container its planner runs in cannot be told from such a task: it is from an
    earlier vivibox, shown so, not opened for anything that runs it, never bound to a
    container, and deleted like any task from before."""
    from vivibox import ui
    from vivibox.cli import main
    from vivibox.task import DELETE, EARLIER, SHOW, TaskLayoutError, find_task, layout

    task = actions.create("demo", "Add divide")
    assert task.read_state().orchestration == load_config().orchestration, "every task records one"
    without_a_flow(task)
    st = task.read_state()
    assert layout(st) == EARLIER
    tasks = load_config().tasks_dir
    with pytest.raises(TaskLayoutError, match="created by an earlier vivibox.*vivibox delete"):
        find_task(tasks, task.id)
    with pytest.raises(TaskLayoutError, match="created by an earlier vivibox"):
        actions.task_pod(task.id)
    with pytest.raises(TaskLayoutError):
        task.set_session("planner", "ses_1")
    assert ui.view(task, st, False, 3).status == "from an earlier vivibox"
    assert find_task(tasks, task.id, SHOW).id == task.id and find_task(tasks, task.id, DELETE).id == task.id
    assert main(["delete", task.id, "--yes"]) == 0
    assert f"Deleted {task.id}" in capsys.readouterr().out and not task.root.exists()


def test_a_task_always_records_a_flow_and_a_box_has_none(tmp_path):
    from vivibox.config import DEFAULT_ORCHESTRATION
    from vivibox.task import CURRENT, TaskLayoutError, layout

    plain = create_task(tmp_path, "demo", "Goal", TEMPLATE)
    assert plain.read_state().orchestration == DEFAULT_ORCHESTRATION and layout(plain.read_state()) == CURRENT
    named = create_task(tmp_path, "demo", "Goal", TEMPLATE, orchestration="planner_maker_checker")
    assert named.read_state().orchestration == "planner_writer_reviewer", "by its name of today"
    with pytest.raises(TaskLayoutError):
        plain.set_orchestration("")
    assert plain.read_state().orchestration == DEFAULT_ORCHESTRATION, "never written without one"
    box = create_task(tmp_path, "demo", "Box", TEMPLATE, box=True)
    st = box.read_state()
    assert st.box and st.orchestration == "" and layout(st) == CURRENT, "a box has no roles to share it"
    assert (
        orchestration.recorded_control(box) is None
        and orchestration.has_control(box, config("supervisor_worker")) is False
    )


# --- round 7: what the planner is told in its own container --------------------------------------


@pytest.mark.parametrize(("mode", "has_one"), FLOWS)
def test_the_planner_in_its_own_container_reads_the_readers_brief_and_everyone_else_the_writers(
    docker, mode, has_one
):
    """The standing instructions of each container are the brief of who runs there: the control
    container's is the reader's, with the view of the commits at the clone's path; the agent's
    is the common one, whatever the flow, as before; a single agent has no reader's brief."""
    from vivibox import brief, repo

    task = actions.create("demo", "Add divide", orchestration=mode)
    facts = (task.id, task.repo, repo.branch_name(task.id), ["make test"])
    opencode.prepare(task, "cheap/writes", ["make test"], ["cheap"])
    assert (task.meta / "harness" / "instructions.md").read_text() == brief.common(*facts)
    if not has_one:
        assert not (task.meta / "harness-control").exists()
        return
    control.prepare(task, load_config(), ["make test"])
    told = (task.meta / "harness-control" / "instructions.md").read_text()
    assert told == brief.reader(*facts) != brief.common(*facts)
    assert f"`{task.repo}` holds the task's committed work" in told and "`make test`" in told
    for false in ("Docker, docker compose and Testcontainers work", "mise use", "is done and committed"):
        assert false not in told, false


def test_the_supervisor_is_asked_for_its_review_without_a_repository_to_stand_in(turns):
    """The review turn of the supervisor, in its control container: the same rules as every
    reviewer's, and the diff asked for without "the repository you are in"."""
    from vivibox import prompts

    task, sup = supervised("supervisor_worker", turns)
    sup.step()  # plan
    actions.accept_plan(task, actions.load(task.id)[1])
    for _ in range(3):  # implement, verify, review
        sup.step()
    st = task.read_state()
    assert st.state is State.IMPLEMENT and st.reviews == 1, "the review ran"
    asked = [prompt for place, prompt in turns.prompts if place == "control"][-1]
    assert asked.endswith(prompts.REVIEW_PROMPT.format(base=st.base_commit))
    assert "in the repository you are in" not in asked
    assert "`git diff " in asked and "and the files it names at the project's path" in asked
