import subprocess
from pathlib import Path

import pytest

from vivibox.config import HostService
from vivibox.pod import Mount, Pod, PodError


class FakeDocker:
    """Records commands and answers the few queries Pod makes."""

    def __init__(self, states=None):
        self.calls: list[list[str]] = []
        self.states = states or {}

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
    assert pod.runner.find("sudo")[0][3:] == ["apply", "vivibox-shop-1-dind", "172.20.0.1:5432"]


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
