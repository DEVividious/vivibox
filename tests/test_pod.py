import ipaddress
import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import review_made

from vivibox import pod_network, podspec, toolchain
from vivibox.config import DEFAULT_NETWORK_POOL, HostService
from vivibox.pod import HELPER as HELPER_PATH
from vivibox.pod import SOCKET_DIR, Mount, Pod, PodError
from vivibox.pod_egress import HELPER_VERSION
from vivibox.probe import Listener


def binds(cmd):
    """The -v arguments of a docker run command, the way Docker keeps them in HostConfig.Binds."""
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]


def made_now(pod, state="exited"):
    """A sidecar as this vivibox would make it today: script, mounts and CA bundle all current."""
    from vivibox.pod import CA_LABEL, LIMITS_LABEL, ca_digest

    cmd = pod.sidecar_command()
    pod.runner.states[pod.sidecar] = state
    pod.runner.cmds[pod.sidecar] = ["-c", cmd[-1]]
    pod.runner.binds[pod.sidecar] = binds(cmd)
    pod.runner.labels[pod.sidecar] = {
        "vivibox.task": pod.task_id, CA_LABEL: ca_digest(), LIMITS_LABEL: pod.limits.digest()
    }  # fmt: skip


class FakeDocker:
    """Records commands and answers the few queries Pod makes."""

    def __init__(self, states=None):
        self.calls: list[list[str]] = []
        self.states = states or {}
        self.cmds: dict[str, list[str]] = {}
        self.binds: dict[str, list[str]] = {}
        self.labels: dict[str, dict[str, str]] = {}
        self.subnets = ""
        self.networks: set[str] = set()  # the networks it has made, by name
        self.sys_net: Path | None = None  # where it makes their bridges, a /sys/class/net of the test's
        self.proc_net_tcp = ""
        self.leftovers = ""  # what the agent container has listening: pid, port in hex, command
        self.alive = False
        self.started = True  # the demo tests turn this off first
        self.route = ""  # what `ip route get` says; nothing, and the host is taken for Ethernet
        self.links = ""  # what `ip -o link` says
        self.refuses = ""  # what the root helper says when it refuses; "" and it does its job
        self.refuses_only = ""  # the one operation it refuses, when not every one
        self.helper_version = HELPER_VERSION  # what the root helper answers --version with

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[:1] == ["sudo"] and cmd[-1] == "--version":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"vivibox-host {self.helper_version}\n", stderr=""
            )
        if cmd[:1] == ["sudo"] and self.refuses and cmd[3] == (self.refuses_only or cmd[3]):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=self.refuses)
        if cmd[:2] == ["docker", "inspect"]:
            state = self.states.get(cmd[-1])
            if state and "Config.Cmd" in cmd[3]:
                made = {
                    "cmd": self.cmds.get(cmd[-1], []),
                    "binds": self.binds.get(cmd[-1], []),
                    "labels": self.labels.get(cmd[-1], {}),
                }
                out, rc = json.dumps(made), 0
            else:
                out, rc = (state or "", 0 if state else 1)
        elif cmd[:4] == ["docker", "run", "-d", "--name"]:
            self.states[cmd[4]] = "running"
            self.cmds[cmd[4]] = cmd[cmd.index("-c") + 1 :] if "-c" in cmd else cmd[-1:]
            self.binds[cmd[4]] = binds(cmd)
            self.labels[cmd[4]] = dict(cmd[i + 1].split("=", 1) for i, a in enumerate(cmd) if a == "--label")
        elif cmd[:3] == ["docker", "rm", "-f"]:
            self.states.pop(cmd[3], None)
        elif cmd[:2] == ["docker", "stop"] and cmd[-1] in self.states:
            self.states[cmd[-1]] = "exited"
        elif cmd[:2] == ["docker", "exec"] and cmd[-2:] == ["cat", "/etc/hosts"]:
            out = "127.0.0.1\tlocalhost\n172.20.0.1\thost.docker.internal\n"
        elif cmd[:2] == ["docker", "exec"] and "cmdline" in cmd[-1]:
            out = self.leftovers
        elif cmd[:2] == ["docker", "exec"] and "/proc/net/tcp" in cmd[-1]:
            out = self.proc_net_tcp if self.started else self.proc_net_tcp.split("\n")[0] + "\n  sl\n"
        elif cmd[:3] == ["docker", "exec", "-d"]:
            self.started = True
        elif "kill -0" in cmd[-1]:
            rc = 0 if self.alive else 1
        elif "kill -TERM" in cmd[-1]:
            pass
        elif cmd[:3] == ["ip", "-o", "route"]:
            out = self.route
        elif cmd[:3] == ["ip", "-o", "link"]:
            out = self.links
        elif cmd[:3] == ["docker", "network", "ls"]:
            out = "net1\n" if self.subnets else ""
        elif cmd[:3] == ["docker", "network", "create"]:
            self.networks.add(cmd[-1])
            named = [a for a in cmd if a.startswith("com.docker.network.bridge.name=")]
            where = self.sys_net or pod_network.SYS_NET  # the kernel's view of what Docker made
            if named and where != Path("/sys/class/net"):
                (where / named[0].partition("=")[2] / "bridge").mkdir(parents=True, exist_ok=True)
        elif cmd[:3] == ["docker", "network", "rm"]:
            self.networks.discard(cmd[-1])
        elif cmd[:3] == ["docker", "network", "inspect"] and cmd[-1] in self.networks:
            out, rc = "", 0
        elif cmd[:3] == ["docker", "network", "inspect"]:
            # A task's network it has not made is not there; anything else is the listing of ids.
            out, rc = (self.subnets, 0) if not cmd[-1].startswith("vivibox-") else ("", 1)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def find(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def allocation(w: str, r: str, p: str) -> str:
    """A task's allocation file as vivibox writes it: every role's range."""
    return json.dumps(
        {"networks": [{"role": "w", "subnet": w}, {"role": "r", "subnet": r}, {"role": "p", "subnet": p}]}
    )


def applied_policy(pod) -> dict:
    """The JSON the last apply-policy was given."""
    return json.loads([c for c in pod.runner.find("sudo") if c[3] == "apply-policy"][-1][5])


@pytest.fixture
def sys_net() -> Path:
    """The kernel's interfaces, as /sys/class/net shows them: a folder of the test's own (conftest)."""
    return pod_network.SYS_NET


@pytest.fixture
def pod(tmp_path: Path, sys_net: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docker = FakeDocker()
    docker.sys_net = sys_net
    return Pod(
        "shop-1", repo, "vivibox-agent:abc", [HostService("host.docker.internal", 5432)], runner=docker,
        folder=tmp_path,
    )  # fmt: skip


def test_agent_container_is_unprivileged(pod):
    cmd = pod.agent_command()
    for flag in (["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"], ["--read-only"]):
        i = cmd.index(flag[0])
        assert cmd[i : i + len(flag)] == flag
    assert "--privileged" not in cmd and "--user" not in cmd
    assert "--network" in cmd and "container:vivibox-shop-1-dind" in cmd
    assert not any("/var/run/docker.sock" in a for a in cmd), "never the host's Docker socket"


def test_passed_variables_reach_both_containers_by_name_only(pod, tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_TOKEN", "s3cr3t-token")
    monkeypatch.delenv("ACCOUNT_ID", raising=False)
    pod.passed_env = ["REPO_TOKEN", "ACCOUNT_ID"]
    pod.gate_dir = tmp_path / "gate"
    for cmd in (pod.agent_command(), pod.gate_command()):
        i = cmd.index("REPO_TOKEN")
        assert cmd[i - 1] == "-e", "docker takes the value from its own environment"
        assert not any("s3cr3t-token" in a for a in cmd), "never on a command line"
    assert pod.missing_env() == ["ACCOUNT_ID"]
    assert pod.passed_values() == ["s3cr3t-token"]


def test_sidecar_mounts_repo_read_only_and_listens_only_on_socket(pod):
    cmd = pod.sidecar_command()
    assert f"{pod.repo}:{pod.repo}:ro" in cmd
    assert "--runtime=sysbox-runc" in cmd and "--privileged" not in cmd
    script = cmd[-1]
    assert "--host=unix:///run/vivibox-docker/docker.sock" in script and "tcp://" not in script
    # After a restart the old socket is still there; chmod on it would miss the daemon's new one.
    assert script.index("rm -f /run/vivibox-docker/docker.sock") < script.index("dockerd")
    assert "DOCKER_MIN_API_VERSION=1.24 dind dockerd" in script, "older Testcontainers still finds Docker"


def test_mavens_build_cache_has_a_volume_of_its_own(pod):
    """The Maven build cache extension keeps its cache beside the local repository, /cache/build-cache.
    Only the volumes are writable in the pod, so without one of its own every module logged a
    read-only file system and the build ran without the cache, slowly."""
    assert "vivibox-cache-build-cache:/cache/build-cache" in pod.agent_command()


def test_the_gate_reuses_only_what_it_built_itself(pod, tmp_path):
    """A hit in the build cache skips a module's build and its tests, taking what was stored as is.
    What the writer stored, the gate does not trust; what the gate built from committed work, a
    later attempt of the same task may reuse: a volume of its own, kept between verifications."""
    pod.gate_dir = tmp_path / "gate"
    gate = pod.gate_command()
    assert f"{pod.disk}/gate-build-cache:/cache/build-cache" in gate
    assert not any(arg.startswith("vivibox-cache-build-cache:") for arg in gate)
    pod.ensure_network()
    (pod.disk / "gate-build-cache" / "kept").mkdir(parents=True)
    pod.gate_up()
    assert (pod.disk / "gate-build-cache" / "kept").is_dir(), "kept between verifications"


def test_agent_gets_repo_rw_caches_and_extra_mounts(pod):
    pod.agent_mounts.append(
        Mount("/srv/vivibox/shop-1/repo/.git/config", "/srv/vivibox/shop-1/repo/.git/config", True)
    )
    args = pod.agent_command()
    assert f"{pod.repo}:{pod.repo}" in args
    assert "vivibox-cache-m2:/cache/m2" in args
    assert args.index(
        "/srv/vivibox/shop-1/repo/.git/config:/srv/vivibox/shop-1/repo/.git/config:ro"
    ) > args.index(f"{pod.repo}:{pod.repo}"), "protective mounts must come after the repo mount to cover it"


def test_up_prepares_the_host_then_the_policy_before_anything_of_the_pod_runs(pod):
    """The host's rules for the task's bridge are loaded before its sidecar exists: no packet of
    the pod's leaves before them. The bridge's name is vivibox's, so the rules can be keyed by it
    before Docker has made it."""
    pod.up()
    steps = [
        c[4] if c[:2] == ["docker", "run"] else c[3] if c[0] == "sudo" else "network"
        for c in pod.runner.calls
        if c[:4] == ["docker", "run", "-d", "--name"]
        or c[0] == "sudo"
        or c[:3] == ["docker", "network", "create"]
    ]
    assert steps == [
        "--version",
        "prepare-engine",
        "attach-disk",
        "network",
        "network",
        "apply-policy",
        "vivibox-shop-1-dind",
        "vivibox-shop-1-agent",
    ]
    prepare = pod.runner.find("sudo")[1]
    assert prepare[:5] == ["sudo", "-n", "/usr/local/libexec/vivibox-host", "prepare-engine", "host"]
    assert json.loads(prepare[5]) == {"host_address": "203.0.113.1/32", "pool": DEFAULT_NETWORK_POOL}
    applied = pod.runner.find("sudo")[3]
    assert applied[:5] == ["sudo", "-n", "/usr/local/libexec/vivibox-host", "apply-policy", "shop-1"]
    assert json.loads(applied[5]) == {
        "state": "present",
        "networks": [
            {
                "role": "w",
                "bridge": "vbx-shop-1-w",
                "subnet": "198.51.100.0/28",
                "policy": "open",
                "hostsvc": ["198.51.100.1:5432"],
            },
            {
                "role": "r",
                "bridge": "vbx-shop-1-r",
                "subnet": "198.51.100.16/29",
                "policy": "open",
                "hostsvc": [],
            },
        ],  # fmt: skip
        "pool": DEFAULT_NETWORK_POOL,
        "mtu": 1500,
        "dns": "203.0.113.1",
        "dns_port": 5053,
    }


def test_up_makes_the_writers_and_the_reviewers_networks_before_the_rules(pod):
    """Two Docker networks per task, the writer's first, each on its recorded range with its
    gateway and on the bridge vivibox names it by, both before the host's rules are loaded; the
    rules are for those two. The planner's range is recorded and reserved, and has no network
    nor rules until the planner has a container of its own."""
    pod.up()
    made = pod.runner.find("docker", "network", "create")
    assert [c[-1] for c in made] == ["vivibox-shop-1-net", "vivibox-shop-1-r"]
    assert [c[c.index("--subnet") + 1] for c in made] == ["198.51.100.0/28", "198.51.100.16/29"]
    assert [c[c.index("--gateway") + 1] for c in made] == ["198.51.100.1", "198.51.100.17"]
    assert [c[c.index("-o") + 1] for c in made] == [
        "com.docker.network.bridge.name=vbx-shop-1-w", "com.docker.network.bridge.name=vbx-shop-1-r",
    ]  # fmt: skip
    calls = pod.runner.calls
    applied = next(i for i, c in enumerate(calls) if c[:1] == ["sudo"] and c[3] == "apply-policy")
    assert all(calls.index(c) < applied for c in made), "both before the rules"
    policy = applied_policy(pod)
    assert [(n["role"], n["subnet"]) for n in policy["networks"]] == [
        ("w", "198.51.100.0/28"), ("r", "198.51.100.16/29"),
    ], "the planner's reserved range is not in the policy"  # fmt: skip
    assert [n.role for n in pod.networks()] == ["w", "r", "p"], "but it is recorded"


def test_a_reviewers_network_gone_while_the_task_was_stopped_is_made_again_on_its_range(pod):
    """The sidecar is there, on the writer's network; the reviewer's was removed by hand. It is
    made again on the range recorded for it, and the writer's is left alone."""
    pod.up()
    pod.runner.networks.discard("vivibox-shop-1-r")
    pod.runner.states["vivibox-shop-1-dind"] = "exited"
    made_now(pod, state="exited")
    before = len(pod.runner.calls)
    pod.up(timeout=1)
    made = [c for c in pod.runner.calls[before:] if c[:3] == ["docker", "network", "create"]]
    assert [c[-1] for c in made] == ["vivibox-shop-1-r"] and made[0][
        made[0].index("--subnet") + 1
    ] == "198.51.100.16/29"
    assert not [c for c in pod.runner.calls[before:] if c[:3] == ["docker", "network", "rm"]]


def starts(calls) -> list[str]:
    """What was asked for, in order, of what starts a container or sets the host's rules: the
    name a docker run or docker start is given, or the helper's operation and state."""
    found = []
    for c in calls:
        if c[:4] == ["docker", "run", "-d", "--name"]:
            found.append(c[4])
        elif c[:2] == ["docker", "start"]:
            found.append(c[2])
        elif c[:1] == ["sudo"] and c[3] == "apply-policy":
            found.append(f"apply-policy {json.loads(c[5])['state']}")
    return found


def starting_the_gate(pod, tmp_path):
    pod.gate_dir = tmp_path / "gate"
    pod.gate_up()


def starting_the_review(pod, tmp_path):
    pod.review_dir = tmp_path / "review"
    review_made(pod)
    pod.review_start([Mount(str(tmp_path / "out"), "/task/review")], {})


def test_up_applies_the_hosts_rules_before_the_first_container_starts(pod):
    pod.up()
    assert starts(pod.runner.calls) == ["apply-policy present", "vivibox-shop-1-dind", "vivibox-shop-1-agent"]


@pytest.mark.parametrize(
    ("start", "container"),
    [(starting_the_gate, "vivibox-shop-1-gate"), (starting_the_review, "vivibox-shop-1-review")],
)
def test_the_rules_are_applied_again_before_the_gate_and_the_review_run(pod, tmp_path, start, container):
    """Every operation that can start a container of the pod reconciles the host's rules first,
    fail-closed, like up: the gate and the review join the sidecar's network, and a caller that
    starts one when the rules are gone (the host restarted, the table with it) must not get a
    container without them."""
    pod.up()
    before = len(pod.runner.calls)
    start(pod, tmp_path)
    assert starts(pod.runner.calls[before:]) == ["apply-policy present", container]


@pytest.mark.parametrize(
    ("start", "said"), [(starting_the_gate, "the verification"), (starting_the_review, "the review")]
)
def test_a_helper_that_refuses_before_the_gate_or_the_review_starts_no_container(pod, tmp_path, start, said):
    pod.up()
    before = len(pod.runner.calls)
    pod.runner.refuses = "vivibox-host: nft: Operation not permitted"
    with pytest.raises(PodError) as refused:
        start(pod, tmp_path)
    what, why = f"{said} could not start", "this machine's rules for pods could not be set"
    assert str(refused.value).startswith(f"{what}: {why}: vivibox-host: nft: Operation not permitted")
    assert "run host/setup.sh" in str(refused.value), "what to do"
    assert not [c for c in pod.runner.calls[before:] if c[:2] in (["docker", "run"], ["docker", "start"])]


WIFI = (
    "3: wlp0s20f3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DORMANT"
    " group default qlen 1000\\    link/ether f0:b6:1e:3a:62:72 brd ff:ff:ff:ff:ff:ff"
)
WARP = (
    "9: CloudflareWARP: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1280 qdisc fq_codel state UNKNOWN"
    " mode DEFAULT group default qlen 500\\    link/none "
)
TUN_DOWN = (
    "7: tun0: <POINTOPOINT,MULTICAST,NOARP> mtu 1400 qdisc noop state DOWN mode DEFAULT group default"
    " qlen 500\\    link/none "
)
VETH = (
    "12: veth1a2b3c4@if11: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue master docker0 state UP"
    " mode DEFAULT group default \\    link/ether 02:42:ac:14:00:02 brd ff:ff:ff:ff:ff:ff link-netnsid 0"
)
ROUTE_WIFI = "1.1.1.1 via 192.168.50.1 dev wlp0s20f3 src 192.168.50.219 uid 1000 \\    cache \n"


def test_a_tunnel_smaller_than_ethernet_clamps_the_pods_mss(pod):
    """The work laptop's case, third round: behind Cloudflare WARP (MTU 1280) the pod's daemon fetched an
    image's manifest and then got 0 of a layer's 115 MB: "unexpected EOF". The pod's interfaces
    have 1500 and offer an MSS the tunnel cannot carry; the host's rules clamp it to the tunnel's.
    WARP steers traffic through rules of its own, so `ip route get` still names the Wi-Fi: a
    tunnel that is up counts whatever the route says."""
    pod.runner.route = ROUTE_WIFI
    pod.runner.links = "\n".join([WIFI, WARP, VETH]) + "\n"
    pod.up()
    assert applied_policy(pod)["mtu"] == 1280


def test_an_ethernet_uplink_needs_no_clamping(pod):
    """A tunnel that is down does not count either: the route's own MTU is clamped to."""
    pod.runner.route = ROUTE_WIFI
    pod.runner.links = "\n".join([WIFI, TUN_DOWN, VETH]) + "\n"
    pod.up()
    assert applied_policy(pod)["mtu"] == 1500


def test_up_with_running_pod_only_reapplies_firewall(pod):
    pod.ensure_network()
    made_now(pod, state="running")
    pod.runner.states["vivibox-shop-1-agent"] = "running"
    pod.up()
    assert not pod.runner.find("docker", "run")
    assert [c[3] for c in pod.runner.find("sudo")] == [
        "--version",
        "prepare-engine",
        "attach-disk",
        "apply-policy",
    ]


def test_a_running_sidecar_from_before_the_hosts_certificates_changed_is_made_again(pod):
    """The work laptop's case, fourth round: logging in to the VPN again replaced the corporate authority
    in the host's bundle, and the sidecar's daemon, which reads the authorities once at its
    start, failed every pull with "x509: certificate signed by unknown authority" while the
    same bundle was mounted in it. The sidecar remembers the bundle it started with."""
    made_now(pod, state="running")
    pod.runner.states["vivibox-shop-1-agent"] = "running"
    pod.runner.labels["vivibox-shop-1-dind"]["vivibox.ca"] = "0" * 64
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-dind")
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-agent"), "it shares the sidecar's network"
    made = pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-dind")
    assert made and f"vivibox.ca={pod.runner.labels['vivibox-shop-1-dind']['vivibox.ca']}" in made[0]
    assert pod.runner.labels["vivibox-shop-1-dind"]["vivibox.ca"] != "0" * 64, "made with today's bundle"


def test_restarted_sidecar_gets_a_new_agent(pod):
    pod.ensure_network()
    made_now(pod, state="exited")
    pod.runner.states["vivibox-shop-1-agent"] = "running"
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-agent")
    assert pod.runner.find("docker", "start", "vivibox-shop-1-dind")
    assert not pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-dind"), "its script is current"
    steps = [
        c[1] if c[0] == "docker" else c[3]
        for c in pod.runner.calls
        if c[:2] == ["docker", "start"] or c[0] == "sudo"
    ]
    assert steps == ["--version", "prepare-engine", "attach-disk", "apply-policy", "start"], (
        "the rules before it starts"
    )


def test_a_task_started_again_passes_what_its_project_passes_now(env, tmp_path, monkeypatch):
    """A task started before its project passed any variables: after a stop and a start its agent
    has them, the names as the project gives them now, by name for docker to take from vivibox's
    environment."""
    from functools import partial

    from vivibox import actions, image
    from vivibox.cli import main

    docker = FakeDocker()
    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=docker))
    monkeypatch.setattr(image, "env", lambda ref: {})
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    before = actions.task_pod("demo-1")
    made_now(before, state="running")
    docker.states[before.agent] = "running"
    before.down()

    project = env / "config" / "projects" / "demo.toml"
    project.write_text(project.read_text() + 'pass_env = ["AWS_SESSION_TOKEN", "AWS_ACCOUNT_ID"]\n')
    monkeypatch.setenv("AWS_SESSION_TOKEN", "s3cr3t-token")
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    actions.task_pod("demo-1").up(timeout=1)

    made = docker.find("docker", "run", "-d", "--name", before.agent)
    assert made, "the agent is made again, with what the project passes now"
    passed = [made[-1][i + 1] for i, arg in enumerate(made[-1]) if arg == "-e"]
    assert "AWS_SESSION_TOKEN" in passed and "AWS_ACCOUNT_ID" in passed


def test_the_agent_reads_your_replies_from_a_read_only_mount_over_its_folder(env, monkeypatch):
    """Kept by the host in .task/, and mounted where the prompts read them, over the folder the agent
    writes: the agent can neither write them nor put another file in their place."""
    from functools import partial

    from vivibox import actions, image

    docker = FakeDocker()
    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=docker))
    monkeypatch.setattr(image, "env", lambda ref: {})
    task = actions.create("demo", "Add divide")
    actions.task_pod(task.id).up(timeout=1)
    made = docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-agent")[-1]
    binds = [made[i + 1] for i, arg in enumerate(made) if arg == "-v"]
    ro = f"{task.meta / 'comments.md'}:/task/handoff/comments.md:ro"
    assert ro in binds
    assert binds.index(ro) > binds.index(f"{task.meta / 'handoff'}:/task/handoff")
    assert (task.meta / "handoff" / "comments.md").read_text() == "", "the mount point, made first"


def test_a_stopped_sidecar_made_by_an_older_vivibox_is_made_again(pod):
    """docker start runs the script a container was made with, so a fix to it would never arrive."""
    pod.runner.states = {"vivibox-shop-1-dind": "exited", "vivibox-shop-1-agent": "exited"}
    pod.runner.cmds = {"vivibox-shop-1-dind": ["-c", "dind dockerd ...; chmod 666 ...; wait"]}
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-dind")
    assert not pod.runner.find("docker", "start", "vivibox-shop-1-dind")
    assert pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-dind")


def test_a_stopped_sidecar_with_other_mounts_is_made_again(pod):
    """The work laptop's case, second round: the CA bundle mount arrived with a pull, and the task's sidecar
    from before it, stopped and started again, still had no bundle. Mounts are given at docker
    run, so a sidecar with other mounts than this vivibox gives is made again."""
    made_now(pod, state="exited")
    pod.runner.states["vivibox-shop-1-agent"] = "exited"
    pod.runner.binds["vivibox-shop-1-dind"] = binds(pod.sidecar_command())[:-1]
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-dind")
    assert not pod.runner.find("docker", "start", "vivibox-shop-1-dind")
    assert pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-dind")


def test_the_pod_says_whether_its_agent_container_runs(pod):
    assert not pod.running()
    pod.runner.states[pod.agent] = "exited"
    assert not pod.running()
    pod.runner.states[pod.agent] = "running"
    assert pod.running()


def test_up_requires_repo(tmp_path):
    with pytest.raises(PodError, match="does not exist"):
        Pod("shop-1", tmp_path / "missing", "img", runner=FakeDocker()).up()


def test_remove_keeps_shared_caches(pod):
    """The socket's volume, and the five a task from before the disk image had, by their old
    names (nothing else lists them once the task is gone); never a shared cache."""
    pod.remove()
    removed = pod.runner.find("docker", "volume", "rm")
    assert [c[3:] for c in removed] == [
        ["vivibox-shop-1-socket"],
        ["-f", "vivibox-shop-1-docker", "vivibox-shop-1-installed", "vivibox-shop-1-gate-installed",
         "vivibox-shop-1-gate-build-cache", "vivibox-shop-1-config"],
    ]  # fmt: skip
    assert not any(v.startswith("vivibox-cache-") for c in removed for v in c)


def test_what_a_task_installs_stays_in_a_directory_of_its_disk(pod, tmp_path):
    """Modules one task installs, its unfinished work in them, never reach another task's build;
    the gate's go under its own repository in its directory, fresh every time, and the shared
    repository's installed/ is the read-only, empty one of the volume. No directory of the
    disk is set aside for the gate's any more (it was `gate-installed`)."""
    from vivibox.pod import DISK_DIRS

    pod.gate_dir = tmp_path / "gate"
    assert f"{pod.disk}/installed:/cache/m2/installed" in pod.agent_command()
    assert not any("/cache/m2/installed" in m for m in binds(pod.gate_command())), "nothing of its own there"
    assert "gate-installed" not in pod.volumes and "gate-installed" not in DISK_DIRS
    assert set(DISK_DIRS) == {"docker", "config", "installed", "gate-build-cache", "gate", "review"}
    assert {Path(v).name for k, v in pod.volumes.items() if k != "socket"} <= set(DISK_DIRS), "the helper's"
    pod.ensure_network()
    pod.gate_up()
    assert not pod.runner.find("docker", "volume", "rm"), "no volume to remove"


def test_the_tasks_volumes_are_directories_of_its_disk_but_the_socket(pod, tmp_path):
    """What a task writes outside its clone lives on its disk image, whose size bounds it: the
    pod's Docker data, the agent's home, what Maven installs, the gate's own. The Docker socket
    stays in a volume the pair shares and never on the host."""
    pod.gate_dir = tmp_path / "gate"
    disk = str(pod.disk)
    assert disk == str(pod.folder / "disk") and pod.disk_image == pod.folder / "disk.img"
    assert f"{disk}/docker:/var/lib/docker" in binds(pod.sidecar_command())
    assert f"{disk}/config:/config" in binds(pod.agent_command())
    assert "vivibox-shop-1-socket:/run/vivibox-docker" in binds(pod.sidecar_command())
    assert "vivibox-shop-1-socket:/run/vivibox-docker" in binds(pod.agent_command())
    assert [b for b in binds(pod.gate_command()) if b.startswith("vivibox-shop-1-")] == [
        "vivibox-shop-1-socket:/run/vivibox-docker"
    ], "no volume of the task's but the socket"
    assert set(pod.volumes) == {"docker", "socket", "installed", "gate-build-cache", "config"}


def test_up_attaches_the_tasks_disk_after_the_host_and_before_any_container(pod):
    """The disk first: the sidecar's data and the agent's home are on it. The helper is given the
    task's own folder and the configured size, nothing from Docker's output."""
    pod.up()
    steps = [
        c[4] if c[:2] == ["docker", "run"] else c[3] if c[0] == "sudo" else "network"
        for c in pod.runner.calls
        if c[:4] == ["docker", "run", "-d", "--name"]
        or c[0] == "sudo"
        or c[:3] == ["docker", "network", "create"]
    ]
    assert steps == [
        "--version", "prepare-engine", "attach-disk", "network", "network", "apply-policy",
        "vivibox-shop-1-dind", "vivibox-shop-1-agent",
    ]  # fmt: skip
    attached = pod.runner.find("sudo")[2]
    assert attached[:5] == ["sudo", "-n", "/usr/local/libexec/vivibox-host", "attach-disk", "shop-1"]
    assert json.loads(attached[5]) == {"dir": str(pod.folder), "size": "20g"}


def test_a_disk_that_cannot_be_attached_stops_the_pod_before_any_container(pod):
    pod.runner.refuses = "vivibox-host: mkfs.xfs -q /srv/vivibox/shop-1/disk.img failed: not found"
    pod.runner.refuses_only = "attach-disk"
    with pytest.raises(PodError) as refused:
        pod.up()
    said = str(refused.value)
    assert said.startswith("the task's disk could not be attached: vivibox-host: mkfs.xfs")
    assert "host/setup.sh" in said, "what to do"
    assert not pod.runner.find("docker", "run"), "no container of the pod"


def test_remove_detaches_the_disk_deletes_its_image_then_the_rules_and_the_network(pod):
    pod.up()
    pod.disk_image.write_bytes(b"")
    pod.disk.mkdir(exist_ok=True)
    before = len(pod.runner.calls)
    assert pod.remove() == ""
    steps = [
        c[1] if c[:2] == ["docker", "rm"] else c[3]
        for c in pod.runner.calls[before:]
        if c[:2] == ["docker", "rm"] or c[0] == "sudo" or c[:3] == ["docker", "network", "rm"]
    ]
    assert steps == [
        "rm", "detach-disk", "apply-policy", "vivibox-shop-1-net", "vivibox-shop-1-r", "vivibox-shop-1-p",
    ]  # fmt: skip
    detached = next(c for c in pod.runner.calls[before:] if c[0] == "sudo" and c[3] == "detach-disk")
    assert json.loads(detached[5]) == {"dir": str(pod.folder)}
    assert not pod.disk_image.exists() and not pod.disk.exists(), "the image goes with the task"


def test_a_detach_the_host_refused_keeps_the_image_and_is_said(pod):
    pod.up()
    pod.disk_image.write_bytes(b"")
    pod.runner.refuses = "vivibox-host: umount /srv/vivibox/shop-1/disk failed: target is busy"
    pod.runner.refuses_only = "detach-disk"
    said = pod.remove()
    assert "the task's disk" in said and "target is busy" in said
    assert pod.disk_image.exists(), "a mounted image is never deleted"
    assert pod.runner.find("docker", "network", "rm", "vivibox-shop-1-net"), "the network goes all the same"


def test_deleting_a_task_that_never_had_a_disk_tolerates_the_helpers_answer(pod):
    """A task from before there were disks has no image: the helper says the disk is detached
    already, and the removal goes on."""
    assert not pod.disk_image.exists()
    assert pod.remove() == ""
    assert [c[3] for c in pod.runner.find("sudo")] == ["detach-disk", "apply-policy"]


def test_the_preparation_runs_in_the_clone_and_outlives_the_exec_that_started_it(pod):
    pod.prepare_start(["npm ci", "npm run build"], "/task/handoff/prepare.log", "/task/handoff/prepare.exit")
    started = pod.runner.find("docker", "exec", "-d", "-w", str(pod.repo), pod.agent)
    assert started, "in the clone, in the background"
    script = started[0][-1]
    assert "setsid" in script and "npm ci && npm run build" in script
    assert "> /task/handoff/prepare.log" in script and "> /task/handoff/prepare.exit" in script
    assert script.index("rm -f /task/handoff/prepare.exit") < script.index("setsid"), "no old ending"
    pod.runner.alive = True
    assert pod.prepare_running()
    pod.runner.alive = False
    assert not pod.prepare_running()


def test_gate_container_sees_only_committed_work(pod, tmp_path):
    pod.gate_dir = tmp_path / "gate"
    pod.agent_mounts.append(Mount("/srv/vivibox/shop-1/.task/handoff", "/task/handoff"))
    cmd = pod.gate_command()
    assert f"{pod.repo}:{pod.repo}:ro" in cmd, "the agent's working tree only as a clone source"
    assert f"{pod.gate_dir}:{pod.gate_dir}" in cmd
    assert "/task/handoff" not in " ".join(cmd) and "vivibox-shop-1-config" not in " ".join(cmd)
    assert cmd[cmd.index("--tmpfs", cmd.index("--tmpfs") + 1) + 1].startswith("/config:"), "fresh home"
    assert "--cap-drop" in cmd and "--read-only" in cmd
    assert f"{pod.gate_dir}:{pod.gate_dir}:ro" in pod.sidecar_command()

    (pod.gate_dir / "src").mkdir(parents=True)
    (pod.gate_dir / "src" / "stale").write_text("old build output")
    pod.ensure_network()
    pod.gate_up()
    assert not (pod.gate_dir / "src").exists(), "every run starts from an empty directory"
    clone = pod.runner.find("docker", "exec", "-w", str(pod.gate_dir), pod.gate, "git", "clone")
    assert clone and clone[0][-2:] == [str(pod.repo), f"{pod.gate_dir}/src"]
    pod.gate_down()
    assert pod.runner.find("docker", "rm", "-f", pod.gate)


def image_variables() -> dict[str, str]:
    """The image's ENV block, as the Dockerfile writes it: what every tool in the pod starts with."""
    from importlib.resources import files

    dockerfile = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    block = dockerfile[dockerfile.index("ENV HOME=/config") :]
    block = block[: block.index("\n\n")]
    found = re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s\\]*))', block)
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3) for m in found}


def environment(cmd) -> dict[str, str]:
    """The variables a docker run command sets, by name; a name alone (passed from the shell) as ""."""
    found = {}
    for i, arg in enumerate(cmd):
        if arg == "-e":
            name, _, value = cmd[i + 1].partition("=")
            found[name] = value
    return found


def test_the_gate_reads_the_shared_caches_and_writes_only_its_own(pod, tmp_path):
    """A writer can put a program into any shared cache: a git under mise's shims, an init script
    under Gradle's home. The gate mounts every one of them read-only, finds its tools on the
    image's PATH without the shims, and gives each build tool a cache of its own under the gate's
    directory; what it keeps between verifications stays what it was, on the task's disk."""
    from vivibox.pod import CACHES

    pod.gate_dir = tmp_path / "gate"
    gate = pod.gate_command()
    mounts = binds(gate)
    shared = {name: path for name, path in CACHES.items() if name != "build-cache"}
    assert len(shared) == 10
    for name, path in shared.items():
        assert f"vivibox-cache-{name}:{path}:ro" in mounts, f"{name} is writable in the gate"
    assert f"{pod.disk}/gate-build-cache:/cache/build-cache" in mounts, "the gate's own, writable"
    assert not any("gate-installed" in m for m in mounts), "Maven installs under the gate's own repository"
    assert f"{pod.gate_dir}/settings.xml:/config/.m2/settings.xml:ro" in mounts, "Maven's settings"
    env = environment(gate)
    caches = f"{pod.gate_dir}/caches"
    assert "PATH" in env and "/cache/mise/shims" not in env["PATH"].split(":"), env.get("PATH")
    assert env["PATH"].split(":")[0] == f"{caches}/mise/shims", "a tool the project brings: the gate's own"
    for name, value in image_variables().items():
        if "/cache/" not in value:
            continue
        assert name in env, f"the image aims {name} at a shared cache and the gate keeps it"
        named = re.findall(r"/cache/[\w./-]*", env[name])
        left = [p for p in named if not p.startswith("/cache/build-cache")]
        assert not left, f"{name} would write into a shared cache: {env[name]}"
    assert env["MAVEN_ARGS"].startswith(f"-Dmaven.repo.local={caches}/m2 ")
    assert "-Daether.enhancedLocalRepository.split=true" in env["MAVEN_ARGS"], "as the image's"
    assert "-Dmaven.build.cache.location=/cache/build-cache" in env["MAVEN_ARGS"], "the sibling moved: named"
    assert env["GRADLE_USER_HOME"] == f"{caches}/gradle"
    assert env["GRADLE_RO_DEP_CACHE"] == "/cache/gradle/caches", "the directory with modules-2, read-only"
    assert env["MISE_DATA_DIR"] == f"{caches}/mise" and env["MISE_DISABLE_TOOLS"] == "java"
    # Yarn Classic's default is a user-level .yarnrc (gate_up), as the image's is the installation's
    # yarnrc: a project's own cache-folder outranks both, and YARN_CACHE_FOLDER would outrank the
    # project's (Berry reads it as cacheFolder too).
    assert "YARN_CACHE_FOLDER" not in env
    for name, value in (
        ("npm_config_cache", f"{caches}/npm"),
        ("YARN_GLOBAL_FOLDER", f"{caches}/yarn/berry"), ("COREPACK_HOME", f"{caches}/corepack"),
        ("UV_CACHE_DIR", f"{caches}/uv"), ("UV_PYTHON_INSTALL_DIR", f"{caches}/uv/python"),
        ("GOMODCACHE", f"{caches}/go/mod"), ("GOCACHE", f"{caches}/go/build"),
        ("CARGO_HOME", f"{caches}/cargo"), ("RUSTUP_HOME", f"{caches}/rustup"),
    ):  # fmt: skip
        assert env[name] == value, name
    assert "JAVA_HOME" not in env, "no project JDK: the image's, as its environment names it"


def test_the_gates_caches_are_made_fresh_and_the_shared_ones_are_left_alone(pod, tmp_path):
    """Every verification starts cold but for what reads from the shared caches; nothing in them is
    deleted for the gate's sake (Gradle's init.d was, when the gate's home was the shared one)."""
    from vivibox.podspec import GATE_CACHE_DIRS

    pod.gate_dir = tmp_path / "gate"
    stale = pod.gate_dir / "caches" / "npm" / "stale"
    stale.mkdir(parents=True)
    pod.ensure_network()
    pod.gate_up()
    assert not stale.exists()
    for name in GATE_CACHE_DIRS:
        assert (pod.gate_dir / "caches" / name).is_dir(), name
    assert set(GATE_CACHE_DIRS) >= {"m2", "gradle", "npm", "yarn/classic", "yarn/berry", "uv", "go", "cargo"}
    settings = (pod.gate_dir / "settings.xml").read_text()
    assert "<url>file:///cache/m2/cached</url>" in settings, "the shared downloads, as a repository"
    assert settings.count("file:///cache/m2/cached") == 2, "for plugins too"
    assert "<activeProfile>" in settings
    yarnrc = (pod.gate_dir / "yarnrc").read_text()
    assert yarnrc == f'cache-folder "{pod.gate_dir}/caches/yarn/classic"\n'
    execs = pod.runner.find("docker", "exec")
    assert not [c for c in execs if "rm" in c], "nothing of the shared caches is removed"
    copied = [c for c in execs if "cp" in c]
    where = ["docker", "exec", "-w", str(pod.gate_dir), pod.gate]
    assert copied == [[*where, "cp", f"{pod.gate_dir}/yarnrc", "/config/.yarnrc"]], "the user-level default"
    cloned = [c for c in execs if "git" in c and "clone" in c]
    assert cloned and execs.index(cloned[0]) > execs.index(copied[0]), "the clone is made, after it"


class FakeProcess:
    """A Popen that prints its lines and ends with a code, or never ends."""

    def __init__(self, lines, returncode=0, hangs=False):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.hangs = hangs
        self.killed = False

    def wait(self, timeout=None):
        if self.hangs and not self.killed:
            raise subprocess.TimeoutExpired("docker", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def test_a_gate_command_streams_its_lines_and_returns_its_code(pod):
    started = []

    def popen(cmd, **kw):
        started.append((list(cmd), kw))
        return FakeProcess(["[INFO] Scanning\n", "[ERROR] boom\n"], returncode=1)

    pod.popen = popen
    got = []
    assert pod.gate_stream("bash", "-c", "mvn -B verify", sink=got.append) == 1
    assert got == ["[INFO] Scanning\n", "[ERROR] boom\n"]
    cmd, kw = started[0]
    assert cmd == ["docker", "exec", "-w", pod.gate_src, pod.gate, "bash", "-c", "mvn -B verify"]
    assert kw["stderr"] is subprocess.STDOUT, "errors in their place among the rest, as in a terminal"


def test_a_gate_command_past_its_time_limit_is_killed_and_reported(pod):
    process = FakeProcess(["[INFO] waiting for Docker…\n"], hangs=True)
    pod.popen = lambda cmd, **kw: process
    got = []
    with pytest.raises(subprocess.TimeoutExpired):
        pod.gate_stream("bash", "-c", "npm test", sink=got.append, timeout=5)
    assert process.killed and got == ["[INFO] waiting for Docker…\n"], "what it said so far reached the log"


def test_the_gate_container_gets_the_same_docker_as_the_agent(pod, tmp_path):
    """A build that passes for the agent must find the same daemon in the gate: the socket mounted
    at the same place, and the same DOCKER_HOST, TESTCONTAINERS_* and passed variables. What differs
    on purpose is the home directory, which the agent could have filled."""
    pod.gate_dir = tmp_path / "gate"
    pod.agent_env = {"OPENCODE_CONFIG": "/config/opencode.json"}
    pod.passed_env = ["ACME_KEY"]
    agent, gate = pod.agent_command(), pod.gate_command()
    env = lambda cmd: {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"}  # noqa: E731
    docker = {arg for arg in pod.docker_env() if arg != "-e"}
    assert docker <= env(gate) and docker <= env(agent), "the gate's Docker drifted from the agent's"
    assert "ACME_KEY" in env(gate) and "ACME_KEY" in env(agent)
    assert any(e.startswith("DOCKER_HOST=unix://") for e in env(gate))
    assert "OPENCODE_CONFIG=/config/opencode.json" not in env(gate), "the agent's tools are not the gate's"
    socket = [m for m in agent if m.endswith(f":{SOCKET_DIR}")]
    assert socket and socket[0] in gate, "the daemon's socket, from the same volume"


def test_testcontainers_in_the_pod_runs_the_ryuk_that_removes_images_by_id(pod, tmp_path):
    """The work laptop's case: a project's Testcontainers brought Ryuk 0.7.0, which prunes images when it
    cleans up, and the pod's daemon lost the image another test was pulling at that moment
    ("failed to extract layer"). The agent and the gate name the newer Ryuk, over what the project's
    library would pick; a project that passes the variable itself keeps its own."""
    from vivibox.pod import RYUK_IMAGE

    pod.gate_dir = tmp_path / "gate"
    fixed = f"TESTCONTAINERS_RYUK_CONTAINER_IMAGE={RYUK_IMAGE}"
    assert RYUK_IMAGE.startswith("testcontainers/ryuk:")
    for cmd in (pod.agent_command(), pod.gate_command()):
        assert fixed in cmd, cmd[:5]
    pod.passed_env = ["TESTCONTAINERS_RYUK_CONTAINER_IMAGE"]
    for cmd in (pod.agent_command(), pod.gate_command()):
        assert fixed not in cmd and "TESTCONTAINERS_RYUK_CONTAINER_IMAGE" in cmd, "the project's own"


def test_the_agent_builds_without_the_maven_build_cache_and_the_gate_keeps_its_own(pod, tmp_path):
    """The work laptop's case: the writer's build restored a module from the cache up to the phase an earlier
    command had reached and ran only the later phases, without `initialize`, so Failsafe got
    `-javaagent:${org.mockito:mockito-core:jar}` unresolved and its JVM died. The gate's cache holds
    whole verifications only, so it stays (ADR-0023)."""
    from importlib.resources import files

    from vivibox.pod import AGENT_MAVEN_OPTS

    dockerfile = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    image_opts = re.search(r'MAVEN_OPTS="([^"]*)"', dockerfile).group(1)
    assert f"{image_opts} -Dmaven.build.cache.enabled=false" == AGENT_MAVEN_OPTS, "the image's, plus one"
    pod.gate_dir = tmp_path / "gate"
    assert f"MAVEN_OPTS={AGENT_MAVEN_OPTS}" in pod.agent_command()
    gate_opts = [arg for arg in pod.gate_command() if arg.startswith("MAVEN_OPTS=")]
    own = f"-Dmaven.repo.local={pod.gate_dir}/caches/m2 -Dmaven.build.cache.location=/cache/build-cache"
    assert gate_opts == [f"MAVEN_OPTS={own}"], "the gate's own repository, and its build cache where it was"


def test_a_stop_and_a_forced_stop_take_the_gate_down_too(pod):
    """The work laptop's case: S killed the agent and the sidecar and left the gate's container, which shares
    the sidecar's network namespace, hanging without a network, and the next start struggled. The
    gate is made anew for every verification, so removing it loses nothing."""
    pod.kill()
    assert pod.runner.find("docker", "kill", pod.agent, pod.sidecar)
    assert pod.runner.find("docker", "rm", "-f", pod.gate), "a forced stop drops the gate"
    pod.runner.calls.clear()
    pod.down()
    assert pod.runner.find("docker", "rm", "-f", pod.gate), "so does a stop"
    assert not pod.runner.find("docker", "kill"), "a stop does not kill"


def test_the_pod_trusts_the_authorities_the_host_trusts(pod, tmp_path, monkeypatch):
    """The work laptop's case: the host pulls an image the pod's daemon could not, "x509: certificate signed
    by unknown authority". The authority was in the host's trust store and in no container's."""
    from vivibox import pod as pod_module

    bundle = tmp_path / "ca-certificates.crt"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(pod_module, "HOST_CA_BUNDLE", bundle)
    pod.gate_dir = tmp_path / "gate"
    for cmd in (pod.sidecar_command(), pod.agent_command(), pod.gate_command()):
        assert f"{bundle}:/etc/ssl/certs/ca-certificates.crt:ro" in cmd, cmd[:5]
    monkeypatch.setattr(pod_module, "HOST_CA_BUNDLE", tmp_path / "none")
    assert not any("ca-certificates.crt" in arg for arg in pod.sidecar_command()), "no bundle, no mount"


def test_each_task_gets_its_own_range_avoiding_what_docker_already_uses(pod):
    pod.runner.subnets = "172.20.0.0/16 198.51.100.0/28 fd00::/64 198.51.100.16/28"
    assert str(pod.free_subnet()) == "198.51.100.32/27", "the lowest block nothing else holds any of"


def test_a_task_takes_one_block_of_the_pool_split_by_role(pod, tmp_path):
    """A /27 of the pool per task, recorded whole at the first allocation: the writer's /28
    first, then the reviewer's /29 and the planner's /29 (reserved: its network is made later).
    Each role's address is the third of its range, and its gateway the first."""
    pod.allocation = tmp_path / "network.json"
    pod.ensure_network()
    assert json.loads(pod.allocation.read_text()) == {
        "networks": [
            {"role": "p", "subnet": "198.51.100.24/29"},
            {"role": "r", "subnet": "198.51.100.16/29"},
            {"role": "w", "subnet": "198.51.100.0/28"},
        ]
    }
    assert [n.role for n in pod.networks()] == ["w", "r", "p"], "in the roles' order"
    reviewer = pod.recorded_network("r")
    assert (reviewer.bridge, reviewer.gateway, reviewer.address) == (
        "vbx-shop-1-r",
        "198.51.100.17",
        "198.51.100.18",
    )
    assert (
        pod.recorded_network("w").address == "198.51.100.2"
        and pod.recorded_network("p").gateway == "198.51.100.25"
    )


def test_the_sidecar_is_pinned_to_the_same_address_every_time(pod):
    pod.up()
    run = pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-dind")[0]
    assert run[run.index("--network") + 1] == "vivibox-shop-1-net"
    assert run[run.index("--ip") + 1] == "198.51.100.2"
    [made] = [c for c in pod.runner.find("docker", "network", "create") if c[-1] == "vivibox-shop-1-net"]
    assert made[made.index("--subnet") + 1] == "198.51.100.0/28"
    assert made[made.index("--gateway") + 1] == "198.51.100.1", "the gateway is vivibox's choice too"
    assert made[made.index("-o") + 1] == "com.docker.network.bridge.name=vbx-shop-1-w"


def test_every_dns_client_in_the_pod_asks_the_hosts_forwarder(pod):
    """The sidecar asks the forwarder on the host's address, and Docker's resolver in it, the
    agent and the nested daemon with it; no search domain of the host's goes in."""
    pod.up()
    run = pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-dind")[0]
    assert run[run.index("--dns") + 1] == "203.0.113.1"
    assert run[run.index("--dns-search") + 1] == ".", "Docker's way of saying none"
    assert "--dns-opt" not in run
    assert run[run.index("--add-host") + 1] == "host.docker.internal:198.51.100.1", "the bridge's gateway"


def test_the_policy_holds_nothing_from_dockers_output(pod):
    """Invariant 13: the root helper is given what vivibox allocated and what the host resolved,
    never what a docker command printed, whatever it prints."""
    real = pod.runner
    evil = "6.6.6.6 host.docker.internal\n6.6.6.0/24 evil;}\n"

    def lying(cmd):
        done = real(cmd)
        cmd = list(cmd)
        listing = cmd[:3] in (["docker", "network", "inspect"], ["docker", "network", "ls"])
        if (
            listing
            or cmd[:2] == ["docker", "exec"]
            or (cmd[:2] == ["docker", "inspect"] and "IPAddress" in str(cmd))
        ):
            return subprocess.CompletedProcess(cmd, done.returncode, stdout=evil, stderr="")
        return done

    pod.runner = lying
    pod.up()
    sent = [c[5] for c in real.find("sudo") if len(c) > 5]  # every operation; --version has no JSON
    assert sent and not any("6.6.6" in s or "evil" in s for s in sent)
    policy = json.loads(sent[-1])
    assert policy["networks"][0]["subnet"] == "198.51.100.0/28"
    assert policy["networks"][0]["hostsvc"] == ["198.51.100.1:5432"]
    assert not [c for c in real.calls if c[-1:] == ["/etc/hosts"]], "nothing is read from the sidecar"


def test_your_hosts_services_are_the_writers_only(pod):
    """A project's host services are for its build: the writer's network reaches them, on its
    gateway; the reviewer, which reads and runs nothing, reaches none of your machine's."""
    pod.up()
    policy = applied_policy(pod)
    assert [(n["role"], n["hostsvc"]) for n in policy["networks"]] == [
        ("w", ["198.51.100.1:5432"]),
        ("r", []),
    ]
    assert "hostsvc" not in policy, "each network's own"


def test_an_older_root_helper_is_said_with_what_to_run():
    from vivibox.pod_egress import helper_missing

    def answering(text, rc=0):
        return lambda cmd: subprocess.CompletedProcess(list(cmd), rc, stdout=text, stderr="")

    assert helper_missing(answering("vivibox-host 3\n")) == ""
    said = helper_missing(answering("vivibox-host 2\n"))
    assert "another version" in said and "vivibox-host 2" in said and "vivibox-host 3" in said
    assert "host/setup.sh" in said
    assert "not installed" in helper_missing(answering("", rc=1))


def test_a_root_helper_of_another_version_stops_the_start_before_anything(pod):
    """The helper this vivibox speaks to is one version; another takes another policy. A task's
    start asks it first and stops, naming both and what to run, before the host is prepared."""
    pod.runner.helper_version = "2"
    with pytest.raises(PodError) as stopped:
        pod.up()
    said = str(stopped.value)
    assert "another version" in said and "vivibox-host 2" in said and "vivibox-host 3" in said
    assert "host/setup.sh" in said
    assert [c[3] for c in pod.runner.find("sudo")] == ["--version"], "nothing else of the host"
    assert not pod.runner.find("docker", "run") and not pod.runner.find("docker", "network", "create")


def test_the_gateway_is_the_allocations(pod):
    pod.ensure_network()
    assert pod.gateway() == "198.51.100.1"
    assert not pod.runner.find("docker", "exec")


def test_a_pod_without_an_allocation_has_no_gateway(pod):
    with pytest.raises(PodError, match="no network"):
        pod.gateway()


def test_the_allocation_is_kept_and_used_again_without_asking_docker(pod, tmp_path):
    pod.allocation = tmp_path / "network.json"
    first = pod.ensure_network()
    assert json.loads(pod.allocation.read_text())["networks"] == [
        {"role": "p", "subnet": "198.51.100.24/29"},
        {"role": "r", "subnet": "198.51.100.16/29"},
        {"role": "w", "subnet": "198.51.100.0/28"},
    ]
    again = Pod("shop-1", pod.repo, "img", runner=FakeDocker(), allocation=pod.allocation)
    again.runner.networks |= {"vivibox-shop-1-net", "vivibox-shop-1-r"}  # its own networks, which Docker has
    assert again.recorded_network() == first and again.gateway() == "198.51.100.1"
    again.runner.states["vivibox-shop-1-dind"] = "exited"
    made_now(again, state="exited")
    again.up(timeout=1)
    assert not again.runner.find("docker", "network", "create"), "the network is there: started, not made"
    assert json.loads(again.runner.find("sudo")[3][5])["networks"][0]["subnet"] == "198.51.100.0/28"


def test_an_allocation_outside_the_pool_now_is_made_again(pod, tmp_path):
    pod.allocation = tmp_path / "network.json"
    pod.allocation.write_text(allocation("10.9.9.0/28", "10.9.9.16/29", "10.9.9.24/29"))
    assert pod.recorded_network() is None, "the pool moved since"
    made_now(pod, state="exited")
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-dind"), "its address is outside the pool"
    assert pod.recorded_network().subnet.subnet_of(ipaddress.IPv4Network(DEFAULT_NETWORK_POOL))


def test_a_pod_does_not_start_when_the_host_cannot_be_prepared(pod):
    real = pod.runner

    def no_helper(cmd):
        if list(cmd)[:1] == ["sudo"]:
            return subprocess.CompletedProcess(list(cmd), 1, stdout="", stderr="sudo: a password is required")
        return real(cmd)

    pod.runner = no_helper
    with pytest.raises(PodError, match="host/setup.sh") as e:
        pod.up()
    assert "a password is required" in str(e.value)
    assert not real.find("docker", "run") and not real.find("docker", "network", "create")


def test_a_pod_does_not_start_while_the_dns_forwarder_is_down(pod, monkeypatch):
    from vivibox.egress import hostd

    def down(address, port=0, upstream="", timeout=0):
        raise hostd.HostdError("the DNS forwarder on 203.0.113.1:5053 did not start: it ended")

    monkeypatch.setattr(hostd, "ensure", down)
    with pytest.raises(PodError, match="DNS forwarder not running: .*did not start"):
        pod.up()
    assert not pod.runner.find("docker", "run"), "fail-closed: no pod without its DNS"


def test_a_full_pool_says_what_to_do_about_it(pod):
    """Every block of the pool taken: the message names the pool and its size in tasks, the three
    ranges a task takes, that deleting finished tasks frees their blocks, and the key to widen."""
    pod.runner.subnets = " ".join(f"198.51.100.{n * 32}/27" for n in range(8))
    with pytest.raises(PodError) as full:
        pod.free_subnet()
    said = str(full.value)
    assert "198.51.100.0/24" in said and "8 tasks" in said, "the pool and its size in tasks"
    assert "/28" in said and "/29" in said, "what one task takes"
    assert "Delete tasks you have finished with" in said and "vivibox delete" in said
    assert "network.pool" in said and "config.toml" in said
    pod.network_pool, pod.runner.subnets = "198.51.100.0/27", "198.51.100.0/27"
    with pytest.raises(PodError, match=r"room for 1 task\)"):
        pod.free_subnet()


def test_an_allocation_of_one_network_is_from_an_earlier_vivibox_and_is_still_deleted(pod, tmp_path):
    """A task allocated one network, the writer's alone, was made before every role had one: it
    is not started on it, nor given a reviewer's network after the fact; delete it and create it
    again. Deleting it still takes its network and its rules."""
    pod.allocation = tmp_path / "network.json"
    pod.allocation.write_text(json.dumps({"networks": [{"role": "w", "subnet": "198.51.100.0/28"}]}))
    with pytest.raises(PodError, match="from an earlier vivibox") as refused:
        pod.up()
    assert "vivibox delete shop-1" in str(refused.value), "what to do"
    assert not pod.runner.find("docker", "run") and not pod.runner.find("docker", "network", "create")
    assert not pod.runner.find("sudo"), "nothing of the host is touched for it"
    assert pod.remove() == ""
    assert pod.runner.find("docker", "network", "rm", "vivibox-shop-1-net")
    assert pod.runner.find("docker", "network", "rm", "vivibox-shop-1-r"), "whether it had one or not"


def test_listening_ports_are_read_from_the_kernel(pod):
    # sl local_address rem_address st ...; 0A is LISTEN, 01 is an established connection.
    pod.runner.proc_net_tcp = (
        "32768\t60999\n"
        "  sl  local_address rem_address   st\n"
        "   0: 00000000:1F90 00000000:0000 0A\n"
        "   1: 0100007F:1450 00000000:0000 0A\n"
        "   2: 00000000:0050 0A0A0A0A:E4C2 01\n"
        "   3: 00000000:A949 00000000:0000 0A\n"
    )
    assert pod.listening() == [Listener(5200, False), Listener(8080, True)], (
        "listening only, each port once, no ephemeral port, and where each one is bound"
    )
    assert "nothing outside can reach it" in pod.listening()[0].why_not
    assert not pod.listening()[1].why_not


def test_removing_a_task_takes_its_network_and_its_rules_with_it(pod):
    pod.remove()
    calls = pod.runner.calls
    gone = next(i for i, c in enumerate(calls) if c[:1] == ["sudo"] and c[3] == "apply-policy")
    assert calls.index(["docker", "network", "rm", "vivibox-shop-1-net"]) > gone, "the rules first"
    assert calls.index(["docker", "network", "rm", "vivibox-shop-1-r"]) > gone, "the reviewer's too"
    assert calls[gone][3:5] == ["apply-policy", "shop-1"]
    assert json.loads(calls[gone][5]) == {
        "state": "absent",
        "networks": [{"role": r, "bridge": f"vbx-shop-1-{r}"} for r in ("w", "r", "p")],
    }


def test_demo_reports_the_port_that_was_not_there_before(pod):
    pod.runner.proc_net_tcp = "32768\t60999\n  sl\n   0: 00000000:1F90 00000000:0000 0A\n"
    pod.runner.alive, pod.runner.started = True, False
    assert pod.demo_start(["npm run dev"], wait=2) == [Listener(8080, True)]
    started = pod.runner.find("docker", "exec", "-d")[0]
    assert "setsid" in started[-1] and "npm run dev" in started[-1], "outlives the exec that starts it"


def test_demo_gives_up_when_the_command_dies(pod):
    pod.runner.proc_net_tcp = "32768\t60999\n  sl\n"
    pod.runner.alive, pod.runner.started = False, False
    assert pod.demo_start(["false"], wait=5) == [], "nothing came up and the process is gone"


def test_a_service_on_several_addresses_is_allowed_on_all_of_them(pod, monkeypatch):
    """A Nexus behind a load balancer answers on several addresses. Allowing the first one only
    lets the build reach it whenever the resolver happens to pick that one."""
    everywhere = [(2, 1, 6, "", (ip, 8081)) for ip in ("10.1.1.9", "10.1.1.7", "10.1.1.7")]
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: everywhere)
    pod.host_services = [HostService("nexus.example", 8081)]
    assert pod.firewall_targets() == ["10.1.1.7:8081", "10.1.1.9:8081"], "each address once"


def test_the_gate_container_gets_the_project_jdk_too(tmp_path):
    """The build runs in the gate container, not the agent one. Its /config is a fresh tmpfs, so the
    JDK reaches it only through this env; without it the image's default Java would quietly win."""
    image = {"PATH": "/cache/mise/shims:/usr/bin"}
    gate_dir = tmp_path / "gate"
    pod = Pod(
        "t1", tmp_path / "repo", "img", [], agent_env=toolchain.agent_env("17", image),
        gate_env=toolchain.gate_env("17", image, gate_dir), gate_dir=gate_dir,
    )  # fmt: skip
    args = pod.gate_command()
    assert f"PATH={toolchain.JDK_LINK}/bin:{gate_dir}/caches/mise/shims:/usr/bin" in args, "no shared shims"
    assert f"JAVA_HOME={toolchain.JDK_LINK}" in args
    assert f"PATH={toolchain.JDK_LINK}/bin:/cache/mise/shims:/usr/bin" in pod.agent_command()


def test_a_gate_command_is_given_its_time_limit(tmp_path):
    seen = {}

    def runner(cmd, timeout=None):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    pod = Pod("t1", tmp_path / "repo", "img", [], gate_dir=tmp_path / "gate", runner=runner)
    pod.gate_exec("bash", "-c", "npm test", timeout=90)
    assert seen["timeout"] == 90


def test_what_the_agent_left_listening_is_stopped_before_the_app_runs(pod):
    """A server the agent started in the background during its turn holds the port the app wants,
    and the app would die on it. Only the agent container's own processes are stopped: a service
    in the pod's Docker lives in the sidecar, and opencode's server is behind the window you watch
    the agent in."""
    server, opencode = "python -m http.server 8000", "opencode serve --port 4096"
    pod.runner.leftovers = f"412\t1F40\t{server}\n7\t1000\t{opencode}\n412\t1F41\t{server}\n"
    assert pod.stop_leftovers() == ["python -m http.server 8000 (port 8000)"]
    kills = [c[-1] for c in pod.runner.calls if "kill -TERM" in c[-1]]
    assert kills == ["kill -TERM 412 2>/dev/null"], "once per process, and never opencode"
    pod.runner.leftovers = ""
    assert pod.stop_leftovers() == []


def test_the_leftovers_script_holds_no_null_byte():
    """tr '\\0' in a plain string was a real NUL in the script, which subprocess refuses with
    "embedded null byte": the demo died on it before it started. The shell still parses it."""
    from vivibox import probe

    assert "\0" not in probe.LEFTOVERS
    parsed = subprocess.run(["sh", "-n", "-c", probe.LEFTOVERS], capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr


def test_the_review_container_has_only_what_a_reader_needs(pod, tmp_path):
    """The reviewer reads a view of the commits the host made, from a working directory the host
    made, both read-only, and has none of the build's means: no Docker socket, no caches, no
    build variables, and not the writer's clone. Its own secrets and harness files come as the
    mounts the caller gives."""
    pod.review_dir = tmp_path / "review"
    pod.passed_env.append("REPO_TOKEN")
    pod.ensure_networks()
    mounts = [Mount("/srv/x/.task", "/task", read_only=True), Mount(str(tmp_path / "out"), "/task/review")]
    cmd = pod.review_command(mounts, {"OPENCODE_CONFIG": "/task/harness/opencode.json"})
    assert cmd[:5] == ["docker", "run", "-d", "--name", "vivibox-shop-1-review"]
    assert f"{pod.review_src}:{pod.repo}:ro" in cmd and f"{pod.review_work}:/work:ro" in cmd
    assert f"{pod.repo}:{pod.repo}:ro" not in cmd and f"{pod.review_dir}:{pod.review_dir}" not in cmd
    assert "/srv/x/.task:/task:ro" in cmd and f"{tmp_path / 'out'}:/task/review" in cmd
    joined = " ".join(cmd)
    assert SOCKET_DIR not in joined and "vivibox-cache-" not in joined and "REPO_TOKEN" not in joined
    assert "OPENCODE_CONFIG=/task/harness/opencode.json" in cmd
    assert "--read-only" in cmd and "--cap-drop" in cmd
    assert cmd[cmd.index("-w") + 1] == "/work", "commands run in the host's directory, not in the work"

    pod.review_fresh()
    for made_empty in (pod.review_dir / "src", pod.review_work):
        assert made_empty.is_dir() and not any(made_empty.iterdir())
    review_made(pod)
    pod.review_start(mounts, {})
    assert not pod.runner.find("docker", "exec", "-w", str(pod.review_dir), pod.review, "git", "clone")
    assert not [c for c in pod.runner.calls if c[:2] == ["docker", "exec"] and "git" in c], "no git there"
    side = pod.review_side()
    assert side.agent == pod.review and side.task_id == pod.task_id
    side.exec("opencode", "--version")
    assert pod.runner.find("docker", "exec", pod.review, "opencode", "--version")
    side.exec_detached("bash", "-c", "serve")
    assert pod.runner.find("docker", "exec", "-d", pod.review, "bash")
    pod.review_down()
    dropped = len(pod.runner.find("docker", "rm", "-f", pod.review))
    assert dropped
    pod.down()
    assert len(pod.runner.find("docker", "rm", "-f", pod.review)) == dropped + 1, "a stop drops the round too"
    pod.remove()
    assert any(pod.review in c for c in pod.runner.find("docker", "rm", "-f")), "gone with the pod"


def test_the_reviewer_runs_in_a_network_of_its_own_at_its_pinned_address(pod, tmp_path):
    """Not the sidecar's network namespace: the reviewer's own network, where the writer, the
    sidecar, the gate and the nested containers are not, at the address allocated for it, with
    every DNS query to the host's forwarder and host.docker.internal its own gateway, never the
    writer's. The gate stays with the sidecar: it shares its daemon."""
    pod.review_dir = tmp_path / "review"
    pod.ensure_networks()
    cmd = pod.review_command([], {})
    assert flag(cmd, "--network") == "vivibox-shop-1-r"
    assert flag(cmd, "--ip") == "198.51.100.18"
    assert flag(cmd, "--dns") == "203.0.113.1" and flag(cmd, "--dns-search") == "."
    assert flag(cmd, "--add-host") == "host.docker.internal:198.51.100.17"
    assert not any("container:" in arg for arg in cmd)
    pod.gate_dir = tmp_path / "gate"
    assert flag(pod.gate_command(), "--network") == f"container:{pod.sidecar}"


def test_a_review_does_not_start_without_the_reviewers_network(pod, tmp_path):
    """Fail-closed: a reviewer that cannot have its own network is not started on the writer's,
    and a review allocates nothing: a task with no network is one to start first."""
    pod.review_dir = tmp_path / "review"
    pod.review_fresh()
    with pytest.raises(PodError, match="the review could not start: shop-1 has no network allocated"):
        pod.review_start([], {})
    assert not pod.runner.find("docker", "run"), "no container of the review"
    assert not pod.runner.find("docker", "network") and not pod.runner.find("sudo"), (
        "nothing allocated, no rules"
    )


def test_a_review_remakes_the_reviewers_network_gone_between_rounds(pod, tmp_path):
    """Nothing is on the reviewer's network between rounds, so a prune of Docker's takes it;
    the next review makes it again on its recorded range, before the host's rules and the
    container, instead of failing on Docker's words."""
    pod.up()
    pod.review_dir = tmp_path / "review"
    pod.runner.networks.discard("vivibox-shop-1-r")
    before = len(pod.runner.calls)
    review_made(pod)
    pod.review_start([], {})
    after = pod.runner.calls[before:]
    made = next(i for i, c in enumerate(after) if c[:3] == ["docker", "network", "create"])
    assert (
        after[made][-1] == "vivibox-shop-1-r"
        and after[made][after[made].index("--subnet") + 1] == "198.51.100.16/29"
    )
    rules = next(i for i, c in enumerate(after) if c[:1] == ["sudo"] and c[3] == "apply-policy")
    run = next(i for i, c in enumerate(after) if c[:4] == ["docker", "run", "-d", "--name"])
    assert made < rules < run, "the network, then the rules, then the container"


def test_a_review_on_a_reviewers_network_not_on_its_bridge_is_refused(pod, tmp_path, sys_net):
    """The reviewer's network made again by hand under its name, on a bridge of Docker's: the
    host's rules are not keyed by it, so no reviewer runs there; the remedy is named."""
    import shutil

    pod.up()
    pod.review_dir = tmp_path / "review"
    shutil.rmtree(sys_net / "vbx-shop-1-r")
    before = len(pod.runner.calls)
    pod.review_fresh()
    with pytest.raises(PodError, match="the review could not start: .*not on the bridge vbx-shop-1-r") as e:
        pod.review_start([], {})
    assert "docker network rm vivibox-shop-1-r" in str(e.value)
    assert not [c for c in pod.runner.calls[before:] if c[:2] in (["docker", "run"], ["sudo"])]


def flag(cmd, name) -> str:
    """The value of a docker run flag given once, as `--flag value`."""
    found = [cmd[i + 1] for i, a in enumerate(cmd) if a == name]
    assert len(found) == 1, f"{name} given {len(found)} times"
    return found[0]


def commands_of(pod, tmp_path) -> dict[str, list[str]]:
    pod.gate_dir, pod.review_dir = tmp_path / "gate", tmp_path / "review"
    pod.ensure_networks()  # the reviewer's command names its network's address
    return {
        "sidecar": pod.sidecar_command(),
        "agent": pod.agent_command(),
        "gate": pod.gate_command(),
        "review": pod.review_command([], {}),
    }


def test_every_tmpfs_is_half_of_its_containers_memory_limit(pod, tmp_path):
    """A tmpfs without a size grows to half the host's memory: an agent that cloned itself twice
    into /tmp to test an install held 5 GB of RAM. Each scratch mount stops at half of what its
    container may hold, so the two together never pass the container's limit."""
    commands = commands_of(pod, tmp_path)
    for role in ("agent", "gate", "review"):
        cmd = commands[role]
        mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--tmpfs"]
        half = pod.limits.memory_of(role) // 2
        assert mounts and all(f"size={half}" in m for m in mounts), (role, mounts)
        assert all(m.split(":")[0] in ("/tmp", "/config") for m in mounts)
    assert pod.limits.memory_of("agent") == 4 * 2**30, "the default: half of 4 GiB"


def test_every_container_of_the_pod_runs_under_the_tasks_slice_with_its_limits(pod, tmp_path):
    """Memory, CPU and processes are bounded per container, all of them under one cgroup slice
    of the task's (a dash in the id nests a slice, so it is an underscore there), which counts
    every container and what the pod's own Docker runs in them."""
    commands = commands_of(pod, tmp_path)
    for role, cmd in commands.items():
        assert flag(cmd, "--cgroup-parent") == "vivibox-shop_1.slice", role
        assert flag(cmd, "--cpus") == "6", role
        assert cmd.index("--cgroup-parent") < cmd.index(pod.image if role != "sidecar" else "--entrypoint")
    assert flag(commands["agent"], "--memory") == str(4 * 2**30)
    assert flag(commands["gate"], "--memory") == str(4 * 2**30)
    assert flag(commands["sidecar"], "--memory") == str(2 * 2**30)
    assert flag(commands["review"], "--memory") == str(3 * 2**29)
    assert [flag(commands[r], "--pids-limit") for r in ("agent", "gate", "sidecar", "review")] == [
        "2048", "4096", "4096", "1024",
    ]  # fmt: skip
    assert not any("--oom-kill-disable" in cmd for cmd in commands.values())


def test_no_container_of_the_pod_is_made_to_come_back_by_itself(pod, tmp_path):
    """Docker's default restart policy is none, and docs/security.md's "no pod container starts
    by itself after" a restart of the machine stands on it: the host's rules are gone by then.
    No command the pod makes a container with names a policy, the planner's among them, nor
    does anything it runs while its containers come up."""
    pod.control_mounts = []
    commands = {**commands_of(pod, tmp_path), "control": pod.control_command()}
    assert set(commands) == set(podspec.ROLES), "every container a task's limits name"
    for role, cmd in commands.items():
        assert cmd[:3] == ["docker", "run", "-d"] and not [a for a in cmd if "restart" in a], role
    pod.up()
    pod.gate_up()
    review_made(pod)
    pod.review_start([], {})
    made = pod.runner.find("docker", "run")
    assert len(made) == 5 and not [a for cmd in pod.runner.calls for a in cmd if a.startswith("--restart")]


def test_memory_swap_equals_memory_so_a_container_cannot_swap_past_its_limit(pod, tmp_path):
    """--memory alone lets a container swap as much again: the limit would hold nothing on a
    host with swap."""
    for role, cmd in commands_of(pod, tmp_path).items():
        assert flag(cmd, "--memory-swap") == flag(cmd, "--memory"), role


def test_a_tasks_own_limits_reach_the_flags(pod, tmp_path):
    pod.limits = pod.limits.with_values({"agent_memory": "6g", "agent_pids": 3000, "pod_cpus": 2.5})
    commands = commands_of(pod, tmp_path)
    assert flag(commands["agent"], "--memory") == str(6 * 2**30)
    assert flag(commands["agent"], "--pids-limit") == "3000"
    assert flag(commands["agent"], "--cpus") == "2.5" and flag(commands["gate"], "--cpus") == "2.5"
    assert flag(commands["gate"], "--memory") == str(4 * 2**30), "the others as before"


def test_the_sidecars_logs_rotate_and_say_why_its_daemon_did_not_start(pod):
    """dockerd writes to the container's output, kept by Docker in three files of 10 MB, not to a
    file in the container that grows for as long as the task lives; a daemon that did not start is
    quoted from there."""
    cmd = pod.sidecar_command()
    assert flag(cmd, "--log-driver") == "json-file"
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--log-opt"] == ["max-size=10m", "max-file=3"]
    assert "/var/log/dockerd.log" not in cmd[-1] and ">" not in cmd[-1]
    real = pod.runner

    def never_up(c):
        c = list(c)
        if c[:2] == ["docker", "exec"] and "info" in c:
            return subprocess.CompletedProcess(c, 1, "", "Cannot connect")
        if c[:2] == ["docker", "logs"]:
            real.calls.append(c)
            return subprocess.CompletedProcess(c, 0, "", "failed to start daemon: no space")
        return real(c)

    pod.runner = never_up
    with pytest.raises(PodError, match="did not start") as e:
        pod._wait_for_daemon(timeout=0)
    assert "failed to start daemon: no space" in str(e.value)
    assert real.find("docker", "logs") == [["docker", "logs", "--tail", "20", "vivibox-shop-1-dind"]]
    assert not real.find("docker", "exec", "vivibox-shop-1-dind", "tail")


def test_a_stopped_sidecar_made_with_other_limits_is_made_again(pod):
    """docker start keeps the flags a container was made with: a limit raised under vivibox
    limits would never arrive. The sidecar carries its limits in a label, and a stopped one with
    other limits is made again, its data in the task's disk."""
    made_now(pod)
    pod.limits = pod.limits.with_values({"sidecar_memory": "3g"})
    pod.up()
    assert pod.runner.find("docker", "rm", "-f", pod.sidecar), "made again"
    made = pod.runner.find("docker", "run", "-d", "--name", pod.sidecar)
    assert made and flag(made[0], "--memory") == str(3 * 2**30)


def test_yarn_downloads_are_shared_but_gate_results_stay_private(pod):
    assert "vivibox-cache-yarn:/cache/yarn" in binds(pod.agent_command())
    assert "vivibox-cache-yarn:/cache/yarn:ro" in binds(pod.gate_command()), "the gate reads, never writes"
    assert not any("-gate-" in mount for mount in binds(pod.agent_command()))
    pod.remove()
    assert not any("vivibox-cache-yarn" in call for call in pod.runner.find("docker", "volume", "rm"))


def test_a_range_another_task_took_meanwhile_is_given_up_for_the_next(pod):
    """Two tasks started at once both find the same range free; Docker gives it to one, and the
    other was left with "Pool overlaps", a task that could not start."""
    real = pod.runner
    raced = []

    def racing(cmd):
        cmd = list(cmd)
        if cmd[:3] == ["docker", "network", "create"] and not raced:
            raced.append(cmd)
            real.subnets = "198.51.100.0/28"  # the other task's, created in between
            real.calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="Error response from daemon: Pool overlaps with other one on this address space",
            )
        return real(cmd)

    pod.runner = racing
    assert str(pod.ensure_network().subnet) == "198.51.100.32/28", "the writer's /28 of the next block"
    made = real.find("docker", "network", "create")
    assert [c[c.index("--subnet") + 1] for c in made] == [
        "198.51.100.0/28",
        "198.51.100.32/28",
        "198.51.100.48/29",
    ]


def test_a_block_whose_reviewers_range_was_taken_meanwhile_is_given_up_whole(pod):
    """The writer's network made, the reviewer's refused for an overlap: the writer's goes too,
    and the task takes the next block, so its ranges are one block's."""
    real = pod.runner
    raced = []

    def racing(cmd):
        cmd = list(cmd)
        if cmd[:3] == ["docker", "network", "create"] and cmd[-1] == "vivibox-shop-1-r" and not raced:
            raced.append(cmd)
            real.subnets = "198.51.100.16/29"  # the other's, created in between
            real.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Pool overlaps with other one")
        return real(cmd)

    pod.runner = racing
    nets = pod.ensure_networks()
    assert [(n.role, str(n.subnet)) for n in nets] == [("w", "198.51.100.32/28"), ("r", "198.51.100.48/29")]
    assert real.find("docker", "network", "rm", "vivibox-shop-1-net"), (
        "the first block's writer network, undone"
    )
    assert [str(s) for s in pod._allocated().values()] == [
        "198.51.100.32/28",
        "198.51.100.48/29",
        "198.51.100.56/29",
    ]
    assert real.networks == {"vivibox-shop-1-net", "vivibox-shop-1-r"}


def test_a_network_docker_refuses_for_another_reason_says_so(pod):
    real = pod.runner

    def refusing(cmd):
        if list(cmd)[:3] == ["docker", "network", "create"]:
            return subprocess.CompletedProcess(list(cmd), 1, stdout="", stderr="permission denied")
        return real(cmd)

    pod.runner = refusing
    with pytest.raises(PodError, match="permission denied"):
        pod.ensure_network()


def test_a_network_that_is_not_on_its_bridge_is_refused(pod, tmp_path, sys_net):
    """The rules hold for the bridge vivibox names; a network of that name made another way (by
    hand, by a script of someone else's) is on a bridge they do not know. The kernel says which.
    The reviewer's network is checked like the writer's."""
    import shutil

    pod.allocation = tmp_path / "network.json"
    pod.allocation.write_text(allocation("198.51.100.0/28", "198.51.100.16/29", "198.51.100.24/29"))
    pod.runner.networks.add("vivibox-shop-1-net")  # there, and not on vbx-shop-1-w
    with pytest.raises(PodError, match="not on the bridge vbx-shop-1-w") as e:
        pod.up()
    assert "docker network rm vivibox-shop-1-net" in str(e.value)
    assert not pod.runner.find("docker", "run"), "no container of the pod"
    pod.runner.networks.discard("vivibox-shop-1-net")
    shutil.rmtree(sys_net / "vbx-shop-1-r")  # the reviewer's, made another way: its bridge is not this
    with pytest.raises(PodError, match="vivibox-shop-1-r is not on the bridge vbx-shop-1-r") as e:
        pod.up()
    assert "docker network rm vivibox-shop-1-r" in str(e.value)
    assert not pod.runner.find("docker", "run") and not pod.runner.find(
        "sudo", "-n", HELPER_PATH, "apply-policy"
    )


def test_a_removal_the_host_refused_is_said_and_the_rest_still_goes(pod):
    real = pod.runner

    def refusing(cmd):
        if list(cmd)[:1] == ["sudo"]:
            return subprocess.CompletedProcess(list(cmd), 1, stdout="", stderr="sudo: a password is required")
        return real(cmd)

    pod.runner = refusing
    said = pod.remove()
    assert "a password is required" in said and "rules" in said
    assert real.find("docker", "network", "rm", "vivibox-shop-1-net"), "the network goes all the same"
    pod.runner = real
    assert pod.remove() == ""


def test_deleting_a_task_says_what_the_host_kept(env, monkeypatch, capsys):
    from functools import partial

    from vivibox import actions
    from vivibox.cli import main

    def refusing(cmd, timeout=None):
        if list(cmd)[:1] == ["sudo"]:
            return subprocess.CompletedProcess(list(cmd), 1, stdout="", stderr="sudo: a password is required")
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=refusing))
    assert main(["delete", "demo-1", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "deleted demo-1, but" in err and "a password is required" in err
    assert not (env / "tasks" / "demo-1").exists(), "the rest of the task went"
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    assert main(["pod", "rm", "demo-2"]) == 1
    assert "a password is required" in capsys.readouterr().err


def test_pods_starting_at_once_prepare_the_host_one_at_a_time():
    """Two at once both saw no vivibox0, both ran ip link add, and the second failed."""
    import threading
    import time

    from vivibox.pod_egress import prepare_host

    inside: list[int] = []
    most: list[int] = []

    def helper(cmd, timeout=None):
        inside.append(1)
        most.append(len(inside))
        time.sleep(0.2)
        inside.pop()
        answer = f"vivibox-host {HELPER_VERSION}\n" if cmd[-1] == "--version" else ""
        return subprocess.CompletedProcess(list(cmd), 0, answer, "")

    starts = [
        threading.Thread(target=prepare_host, args=("203.0.113.1/32", DEFAULT_NETWORK_POOL, helper))
        for _ in range(3)
    ]
    for t in starts:
        t.start()
    for t in starts:
        t.join()
    assert len(most) == 6 and max(most) == 1, "the version asked and the host prepared, one pod at a time"


def test_the_pods_scopes_are_its_containers_ids_by_their_limits_role(pod):
    """For limits.py: a label for the cgroup scope under the task's slice, nothing else; one that
    is not an id is dropped, and a container that is not there has none."""
    real = pod.runner

    def inspecting(cmd):
        cmd = list(cmd)
        if cmd[:3] == ["docker", "inspect", "-f"] and "{{.Id}}" in cmd[3]:
            real.calls.append(cmd)
            found = "\n".join(
                [
                    f"/{pod.agent} {'a' * 64}",
                    f"/{pod.sidecar} {'b' * 64}",
                    f"/{pod.gate} nonsense",
                    f"/{pod.control} {'c' * 64}",
                ]  # fmt: skip
            )
            return subprocess.CompletedProcess(
                cmd, 1, found + "\n", "Error: No such object: vivibox-shop-1-review"
            )
        return real(cmd)

    pod.runner = inspecting
    assert pod.scopes() == {"agent": "a" * 64, "sidecar": "b" * 64, "control": "c" * 64}
    [asked] = real.find("docker", "inspect", "-f")
    assert asked[4:] == [pod.agent, pod.sidecar, pod.gate, pod.review, pod.control], "one question for all"


def test_every_container_gets_the_machines_cpus_when_it_has_fewer_than_six(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(4)))
    repo = tmp_path / "repo"
    repo.mkdir()
    pod = Pod("shop-1", repo, "img", runner=FakeDocker(), folder=tmp_path)
    assert flag(pod.agent_command(), "--cpus") == "4" and flag(pod.sidecar_command(), "--cpus") == "4"
