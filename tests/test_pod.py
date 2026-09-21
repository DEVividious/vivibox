import subprocess
from pathlib import Path

import pytest

from vivibox import toolchain
from vivibox.config import DEFAULT_NETWORK_POOL, HostService
from vivibox.pod import Listener, Mount, Pod, PodError


class FakeDocker:
    """Records commands and answers the few queries Pod makes."""

    def __init__(self, states=None):
        self.calls: list[list[str]] = []
        self.states = states or {}
        self.subnets = ""
        self.proc_net_tcp = ""
        self.alive = False
        self.started = True  # the demo tests turn this off first

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[:2] == ["docker", "inspect"]:
            state = self.states.get(cmd[-1])
            out, rc = (state or "", 0 if state else 1)
        elif cmd[:4] == ["docker", "run", "-d", "--name"]:
            self.states[cmd[4]] = "running"
        elif cmd[:2] == ["docker", "exec"] and cmd[-2:] == ["cat", "/etc/hosts"]:
            out = "127.0.0.1\tlocalhost\n172.20.0.1\thost.docker.internal\n"
        elif cmd[:2] == ["docker", "exec"] and "/proc/net/tcp" in cmd[-1]:
            out = self.proc_net_tcp if self.started else self.proc_net_tcp.split("\n")[0] + "\n  sl\n"
        elif cmd[:3] == ["docker", "exec", "-d"]:
            self.started = True
        elif "kill -0" in cmd[-1]:
            rc = 0 if self.alive else 1
        elif "kill -TERM" in cmd[-1]:
            pass
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


def test_up_with_running_pod_only_reapplies_firewall(pod):
    pod.runner.states = {"vivibox-shop-1-dind": "running", "vivibox-shop-1-agent": "running"}
    pod.up()
    assert not pod.runner.find("docker", "run")
    assert len(pod.runner.find("sudo")) == 1


def test_restarted_sidecar_gets_a_new_agent(pod):
    pod.runner.states = {"vivibox-shop-1-dind": "exited", "vivibox-shop-1-agent": "running"}
    pod.up(timeout=1)
    assert pod.runner.find("docker", "rm", "-f", "vivibox-shop-1-agent")
    assert pod.runner.find("docker", "start", "vivibox-shop-1-dind")


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
