"""Docker Hub kept once on this machine: the mirror's container on your Docker, and how a task's
pod is pointed at it."""

import pytest

from vivibox import mirror
from vivibox.pod import PodError


class FakeDocker:
    """The host's Docker as the mirror sees it: one container, maybe, and the bridge's gateway."""

    def __init__(self, state=None, listen=""):
        self.calls: list[list[str]] = []
        self.state = state
        self.listen = listen

    def __call__(self, cmd):
        import subprocess

        cmd = list(cmd)
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[:3] == ["docker", "network", "inspect"]:
            out = "172.20.0.1\n"
        elif cmd[:2] == ["docker", "inspect"]:
            if self.state is None:
                rc = 1
            else:
                out = f"{self.state} {self.listen}\n"
        elif cmd[:2] == ["docker", "run"]:
            self.state = "running"
        return subprocess.CompletedProcess(cmd, rc, out, "" if rc == 0 else "No such object")

    def made(self):
        return [c for c in self.calls if c[:2] == ["docker", "run"]]


def ready(address):
    return True


def test_a_missing_mirror_is_made_listening_only_where_the_pods_reach_the_host():
    docker = FakeDocker()
    url = mirror.ensure(5055, runner=docker, ready=ready)
    assert url == "http://host.docker.internal:5055"
    [cmd] = docker.made()
    assert cmd[cmd.index("--name") + 1] == mirror.NAME and "--network" in cmd and "host" in cmd
    env = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    # The bridge's gateway, not every address: the LAN has no business with it.
    assert "REGISTRY_HTTP_ADDR=172.20.0.1:5055" in env
    assert "REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io" in env
    assert any(e.startswith("REGISTRY_PROXY_TTL=") for e in env), "it forgets what nobody pulls"
    assert "-p" not in cmd and "--publish" not in cmd
    volumes = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert f"{mirror.VOLUME}:/var/lib/registry" in volumes
    assert "/etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro" in volumes
    assert "--restart" in cmd, "it comes back with Docker, as the pods' pulls expect it"


def test_a_running_mirror_on_the_same_address_is_left_alone():
    docker = FakeDocker("running", "172.20.0.1:5055")
    mirror.ensure(5055, runner=docker, ready=ready)
    assert not docker.made() and not any(c[:3] == ["docker", "rm", "-f"] for c in docker.calls)


def test_a_stopped_mirror_is_started_and_one_on_another_address_made_again():
    docker = FakeDocker("exited", "172.20.0.1:5055")
    mirror.ensure(5055, runner=docker, ready=ready)
    assert ["docker", "start", mirror.NAME] in docker.calls and not docker.made()

    docker = FakeDocker("running", "172.17.0.1:5055")  # the bridge moved, or the port changed
    mirror.ensure(5055, runner=docker, ready=ready)
    assert ["docker", "rm", "-f", mirror.NAME] in docker.calls and len(docker.made()) == 1


def test_a_mirror_that_does_not_answer_says_why():
    docker = FakeDocker()
    with pytest.raises(PodError, match="did not answer"):
        mirror.ensure(5055, runner=docker, ready=lambda address: False, timeout=0)


def test_the_pods_daemon_pulls_through_the_mirror(tmp_path):
    from vivibox.pod import Pod

    pod = Pod("shop-1", tmp_path, "img", hub_mirror="http://host.docker.internal:5055")
    assert "--registry-mirror=http://host.docker.internal:5055" in pod.sidecar_command()[-1]
    assert "--registry-mirror" not in Pod("shop-1", tmp_path, "img").sidecar_command()[-1]


def test_a_task_pod_reaches_the_mirror_through_its_firewall(env, monkeypatch):
    from vivibox import actions, image
    from vivibox.cli import main
    from vivibox.config import HostService
    from vivibox.pod import HOST_GATEWAY

    monkeypatch.setattr(image, "env", lambda ref: {})
    config = env / "config" / "config.toml"
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    assert actions.task_pod("demo-1").hub_mirror == ""
    config.write_text(config.read_text() + "[network]\nhub_mirror = true\n")
    pod = actions.task_pod("demo-1")
    assert pod.hub_mirror == "http://host.docker.internal:5055"
    assert HostService(HOST_GATEWAY, 5055) in pod.host_services


@pytest.mark.real_start
def test_a_task_starts_the_mirror_first_and_goes_on_without_it(env, monkeypatch, tmp_path):
    """A pull that the mirror cannot serve goes to Docker Hub by the daemon's own fallback, so a
    mirror that does not start is said on the task and does not keep it from starting."""
    from vivibox import actions, image, opencode, secrets
    from vivibox.cli import main
    from vivibox.pod import Pod

    config = env / "config" / "config.toml"
    config.write_text(config.read_text() + "[network]\nhub_mirror = true\n")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(secrets, "prepare", lambda task_id, keys: None)
    monkeypatch.setattr(opencode, "prepare", lambda task, model, verify, used: False)
    monkeypatch.setattr(opencode, "provider_of", lambda model: "p")
    order = []

    def down(port, **kw):
        order.append(("mirror", port))
        raise PodError("the Docker Hub mirror did not start: port is taken")

    def up(self):
        order.append(("pod", self.hub_mirror))
        raise RuntimeError("enough")

    monkeypatch.setattr(mirror, "ensure", down)
    monkeypatch.setattr(Pod, "up", up)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    with pytest.raises(RuntimeError, match="enough"):
        actions.start("demo-1")
    assert order == [("mirror", 5055), ("pod", "http://host.docker.internal:5055")]
    task, _ = actions.load("demo-1")
    said = [e["data"] for e in task.events() if e["type"] == "mirror"]
    assert said and "port is taken" in said[-1]["problem"]


def test_vivibox_mirror_says_whether_it_runs_and_what_it_holds(env, monkeypatch, capsys):
    from vivibox.cli import main

    monkeypatch.setattr(mirror, "found", lambda: ("", ""))
    assert main(["mirror"]) == 0
    out = capsys.readouterr().out
    assert "off" in out and "hub_mirror = true" in out, "said how to turn it on"

    config = env / "config" / "config.toml"
    config.write_text(config.read_text() + "[network]\nhub_mirror = true\n")
    assert main(["mirror"]) == 0
    assert "not running" in capsys.readouterr().out

    monkeypatch.setattr(mirror, "found", lambda: ("running", "172.20.0.1:5055"))
    monkeypatch.setattr(mirror, "size", lambda: "275M")
    assert main(["mirror"]) == 0
    out = capsys.readouterr().out
    assert "172.20.0.1:5055" in out and "275M" in out

    removed = []
    monkeypatch.setattr(mirror, "remove", lambda: removed.append(True))
    assert main(["mirror", "remove"]) == 0 and removed


def test_a_box_starts_the_mirror_before_its_pod_too(env, monkeypatch, tmp_path):
    from vivibox import actions, box, image, opencode, secrets
    from vivibox.cli import main
    from vivibox.pod import Pod

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(secrets, "prepare", lambda task_id, keys: None)
    monkeypatch.setattr(opencode, "prepare", lambda task, model, verify, used: False)
    order = []
    monkeypatch.setattr(actions, "ensure_mirror", lambda task, config: order.append("mirror"))

    def up(self):
        order.append("pod")
        raise RuntimeError("enough")

    monkeypatch.setattr(Pod, "up", up)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    with pytest.raises(RuntimeError, match="enough"):
        box.start_box("demo-1")
    assert order == ["mirror", "pod"]
