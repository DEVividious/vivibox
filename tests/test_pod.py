import json
import subprocess
from pathlib import Path

import pytest

from vivibox import toolchain
from vivibox.config import DEFAULT_NETWORK_POOL, HostService
from vivibox.pod import SOCKET_DIR, Listener, Mount, Pod, PodError


def binds(cmd):
    """The -v arguments of a docker run command, the way Docker keeps them in HostConfig.Binds."""
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]


def made_now(pod, state="exited"):
    """A sidecar as this vivibox would make it today: script, mounts and CA bundle all current."""
    from vivibox.pod import CA_LABEL, ca_digest

    cmd = pod.sidecar_command()
    pod.runner.states["vivibox-shop-1-dind"] = state
    pod.runner.cmds["vivibox-shop-1-dind"] = ["-c", cmd[-1]]
    pod.runner.binds["vivibox-shop-1-dind"] = binds(cmd)
    pod.runner.labels["vivibox-shop-1-dind"] = {"vivibox.task": "shop-1", CA_LABEL: ca_digest()}


class FakeDocker:
    """Records commands and answers the few queries Pod makes."""

    def __init__(self, states=None):
        self.calls: list[list[str]] = []
        self.states = states or {}
        self.cmds: dict[str, list[str]] = {}
        self.binds: dict[str, list[str]] = {}
        self.labels: dict[str, dict[str, str]] = {}
        self.subnets = ""
        self.proc_net_tcp = ""
        self.leftovers = ""  # what the agent container has listening: pid, port in hex, command
        self.alive = False
        self.started = True  # the demo tests turn this off first
        self.route = ""  # what `ip route get` says; nothing, and the host is taken for Ethernet
        self.links = ""  # what `ip -o link` says

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        out, rc = "", 0
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
        elif cmd[:3] == ["docker", "network", "inspect"]:
            out, rc = (self.subnets, 0) if cmd[-1] != "vivibox-shop-1-net" else ("", 1)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def find(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def pod(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return Pod(
        "shop-1", repo, "vivibox-agent:abc", [HostService("host.docker.internal", 5432)], runner=FakeDocker()
    )


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
    for command in (pod.agent_command(), pod.gate_command()):
        assert "vivibox-cache-build-cache:/cache/build-cache" in command


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


def test_up_creates_sidecar_then_firewall_then_agent(pod):
    pod.up()
    steps = [
        c[4] if c[:2] == ["docker", "run"] else "firewall"
        for c in pod.runner.calls
        if c[:4] == ["docker", "run", "-d", "--name"] or c[0] == "sudo"
    ]
    assert steps == ["vivibox-shop-1-dind", "firewall", "vivibox-shop-1-agent"]
    assert pod.runner.find("sudo")[0][3:] == [
        "apply", "vivibox-shop-1-dind", "--pool", DEFAULT_NETWORK_POOL, "172.20.0.1:5432"
    ]  # fmt: skip


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
    have 1500 and offer an MSS the tunnel cannot carry; the firewall clamps it to the tunnel's.
    WARP steers traffic through rules of its own, so `ip route get` still names the Wi-Fi: a
    tunnel that is up counts whatever the route says."""
    pod.runner.route = ROUTE_WIFI
    pod.runner.links = "\n".join([WIFI, WARP, VETH]) + "\n"
    pod.up()
    firewall = pod.runner.find("sudo")[0]
    assert firewall[firewall.index("--mtu") + 1] == "1280"
    assert firewall.index("--mtu") < firewall.index("172.20.0.1:5432"), "options before the targets"


def test_an_ethernet_uplink_needs_no_clamping(pod):
    """A tunnel that is down does not count either."""
    pod.runner.route = ROUTE_WIFI
    pod.runner.links = "\n".join([WIFI, TUN_DOWN, VETH]) + "\n"
    pod.up()
    assert "--mtu" not in pod.runner.find("sudo")[0]


def test_up_with_running_pod_only_reapplies_firewall(pod):
    made_now(pod, state="running")
    pod.runner.states["vivibox-shop-1-agent"] = "running"
    pod.up()
    assert not pod.runner.find("docker", "run")
    assert len(pod.runner.find("sudo")) == 1


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
    made_now(pod, state="exited")
    pod.runner.states["vivibox-shop-1-agent"] = "running"
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-agent")
    assert pod.runner.find("docker", "start", "vivibox-shop-1-dind")
    assert not pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-dind"), "its script is current"


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


def test_up_requires_repo(tmp_path):
    with pytest.raises(PodError, match="does not exist"):
        Pod("shop-1", tmp_path / "missing", "img", runner=FakeDocker()).up()


def test_remove_keeps_shared_caches(pod):
    pod.remove()
    removed = pod.runner.find("docker", "volume", "rm")[0]
    assert "vivibox-shop-1-docker" in removed and "vivibox-shop-1-config" in removed
    assert not any(v.startswith("vivibox-cache-") for v in removed)


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
    pod.gate_up()
    assert not (pod.gate_dir / "src").exists(), "every run starts from an empty directory"
    clone = pod.runner.find("docker", "exec", "-w", str(pod.gate_dir), pod.gate, "git", "clone")
    assert clone and clone[0][-2:] == [str(pod.repo), f"{pod.gate_dir}/src"]
    pod.gate_down()
    assert pod.runner.find("docker", "rm", "-f", pod.gate)


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
    assert env(gate) == env(agent), "the gate's environment drifted from the agent's"
    assert any(e.startswith("DOCKER_HOST=unix://") for e in env(gate))
    socket = [m for m in agent if m.endswith(f":{SOCKET_DIR}")]
    assert socket and socket[0] in gate, "the daemon's socket, from the same volume"


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
    assert str(pod.free_subnet()) == "198.51.100.32/28", "the lowest range nothing else holds"


def test_the_sidecar_is_pinned_to_the_same_address_every_time(pod):
    pod.up()
    run = pod.runner.find("docker", "run", "-d", "--name", "vivibox-shop-1-dind")[0]
    assert run[run.index("--network") + 1] == "vivibox-shop-1-net"
    assert run[run.index("--ip") + 1] == "198.51.100.2"
    assert pod.runner.find("docker", "network", "create")


def test_a_full_pool_says_what_to_do_about_it(pod):
    pod.network_pool = "198.51.100.0/28"
    pod.runner.subnets = "198.51.100.0/28"
    with pytest.raises(PodError, match="Remove tasks you have finished with"):
        pod.free_subnet()


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


def test_removing_a_task_takes_its_network_with_it(pod):
    pod.remove()
    assert pod.runner.find("docker", "network", "rm", "vivibox-shop-1-net")


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
    where = toolchain.agent_env("17", {"PATH": "/cache/mise/shims:/usr/bin"})
    pod = Pod("t1", tmp_path / "repo", "img", [], agent_env=where, gate_dir=tmp_path / "gate")
    args = pod.gate_command()
    assert f"PATH={toolchain.JDK_LINK}/bin:/cache/mise/shims:/usr/bin" in args
    assert f"JAVA_HOME={toolchain.JDK_LINK}" in args


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
