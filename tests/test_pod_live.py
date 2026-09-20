"""Isolation and capability checks on a real task pod.

Needs host/setup.sh to have run, Docker, and internet access (images, Maven Central).
Run with: uv run pytest -m docker tests/test_pod_live.py
"""

import http.server
import os
import random
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from vivibox import image
from vivibox.config import HostService
from vivibox.pod import FIREWALL, Pod

pytestmark = pytest.mark.docker

TASKS_ROOT = Path("/srv/vivibox")
FIXTURE = Path(__file__).parent / "fixtures" / "pod-project"


class Empty(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def serve() -> http.server.ThreadingHTTPServer:
    # All interfaces: the pod reaches the host through the Docker gateway.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Empty)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def lan_ip() -> str:
    out = subprocess.run(
        ["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True
    ).stdout.split()
    return out[out.index("src") + 1]


@pytest.fixture(scope="module")
def env():
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if subprocess.run(["sudo", "-n", "-l", FIREWALL], capture_output=True).returncode != 0:
        pytest.skip("firewall helper not installed; run host/setup.sh")
    allowed, denied = serve(), serve()
    tasks_dir = TASKS_ROOT / f".pytest-{secrets.token_hex(4)}"
    task_id = f"podcheck-{random.randint(1000, 9999)}"
    repo = tasks_dir / task_id / "repo"
    shutil.copytree(FIXTURE, repo)
    marker = secrets.token_hex(8)
    index = repo / "www" / "index.html"
    index.write_text(index.read_text().replace("__MARKER__", marker))
    ref, _ = image.build()
    pod = Pod(task_id, repo, ref, [HostService("host.docker.internal", allowed.server_address[1])])
    try:
        pod.up()
        yield {
            "pod": pod,
            "repo": repo,
            "marker": marker,
            "allowed": allowed.server_address[1],
            "denied": denied.server_address[1],
        }
    finally:
        pod.remove()
        shutil.rmtree(tasks_dir, ignore_errors=True)
        allowed.shutdown()
        denied.shutdown()


def agent(env, script: str, check: bool = True) -> subprocess.CompletedProcess:
    return env["pod"].exec("bash", "-c", script, check=check)


def test_agent_is_unprivileged(env):
    assert agent(env, "id -u").stdout.strip() == str(os.getuid())
    assert "CapEff:\t0000000000000000" in agent(env, "cat /proc/self/status").stdout
    assert agent(env, "touch /usr/local/x", check=False).returncode != 0, "root filesystem must be read-only"


def test_docker_in_pod_does_not_use_vfs(env):
    assert agent(env, "docker info --format '{{.Driver}}'").stdout.strip() not in ("", "vfs")


def test_compose_ports_on_localhost_and_bind_mount(env):
    agent(env, "docker compose -p podcheck up -d --wait")
    agent(env, "exec 3<>/dev/tcp/127.0.0.1/5432")
    assert env["marker"] in agent(env, "curl -fsS http://localhost:8080/").stdout


def test_nested_container_cannot_write_to_repo(env):
    repo = env["repo"]
    write = f"cp /bin/busybox {repo}/suid && chmod 4755 {repo}/suid"
    agent(env, f"docker run --rm -v {repo}:{repo} alpine:3 sh -c '{write}'", check=False)
    assert not (repo / "suid").exists()


def test_privileged_nested_container_cannot_reach_host(env):
    marker = Path.home() / f".vivibox-pod-check-{secrets.token_hex(4)}"
    marker.write_text("secret")
    dev = os.stat("/").st_dev
    probe = (
        f"test -e /host{marker} && echo FOUND_MARKER; "
        f"mknod /tmp/rootdev b {os.major(dev)} {os.minor(dev)} 2>/dev/null && mkdir -p /mnt/r "
        "&& mount -o ro /tmp/rootdev /mnt/r 2>/dev/null "
        "&& test -e /mnt/r/etc/shadow && echo MOUNTED_HOST_ROOT; "
        "echo PROBE_DONE"
    )
    try:
        out = agent(
            env, f"docker run --rm --privileged -v /:/host alpine:3 sh -c '{probe}'", check=False
        ).stdout
    finally:
        marker.unlink()
    assert "PROBE_DONE" in out
    assert "FOUND_MARKER" not in out and "MOUNTED_HOST_ROOT" not in out


def test_only_allowed_host_service_is_reachable(env):
    gw, ok, no = env["pod"].gateway(), env["allowed"], env["denied"]
    curl = "curl -fsS -m 5 -o /dev/null http://{}:{}/"
    wget = "docker run --rm alpine:3 wget -q -T 5 -O /dev/null http://{}:{}/"
    assert agent(env, curl.format(gw, ok), check=False).returncode == 0
    assert agent(env, curl.format(gw, no), check=False).returncode != 0
    assert agent(env, curl.format(lan_ip(), ok), check=False).returncode != 0, (
        "your LAN address must be blocked"
    )
    assert agent(env, wget.format(gw, ok), check=False).returncode == 0
    assert agent(env, wget.format(gw, no), check=False).returncode != 0


def test_internet_is_reachable(env):
    central = "https://repo.maven.apache.org/maven2/"
    assert agent(env, f"curl -fsS -m 10 -o /dev/null {central}", check=False).returncode == 0


def test_parallel_maven_builds_share_a_local_repository(env):
    # A throwaway repository on tmpfs: the point is two resolvers filling an empty one at once.
    go = "mvn -B -q -Dmaven.repo.local=/tmp/m2-parallel -f app/pom.xml dependency:go-offline"
    agent(env, f"{go} & a=$!; {go} & b=$!; wait $a && wait $b")


def test_maven_build_with_testcontainers(env):
    result = agent(env, "mvn -B -f app/pom.xml verify", check=False)
    assert result.returncode == 0, result.stdout[-3000:]
    assert "Tests run: 1, Failures: 0, Errors: 0" in result.stdout
    assert agent(env, "ls /cache/m2/org/testcontainers").stdout.strip(), "the shared Maven cache is used"


def test_pod_restart_keeps_images_and_firewall(env):
    pod = env["pod"]
    pod.down()
    pod.up()
    assert pod.sidecar_docker("image", "inspect", "postgres:16-alpine", check=False).returncode == 0
    no = env["denied"]
    assert (
        agent(env, f"curl -fsS -m 5 -o /dev/null http://{pod.gateway()}:{no}/", check=False).returncode != 0
    )
