"""Egress firewall on a real Sysbox container. Needs host/setup.sh to have run: uv run pytest -m docker"""

import http.server
import subprocess
import threading

import pytest

pytestmark = pytest.mark.docker

SIDECAR = "vivibox-fwtest-1-dind"
HELPER = "/usr/local/libexec/vivibox-netns"


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


@pytest.fixture
def sidecar():
    if sh("sudo", "-n", "-l", HELPER, check=False).returncode != 0:
        pytest.skip("firewall helper not installed; run host/setup.sh")
    sh("docker", "rm", "-f", SIDECAR, check=False)
    sh("docker", "run", "-d", "--runtime=sysbox-runc", "--name", SIDECAR,
       "--add-host", "host.docker.internal:host-gateway", "alpine:3", "sleep", "600")  # fmt: skip
    yield
    sh("docker", "rm", "-f", SIDECAR, check=False)


@pytest.fixture
def host_servers():
    servers = [
        http.server.ThreadingHTTPServer(("0.0.0.0", 0), http.server.SimpleHTTPRequestHandler)
        for _ in range(2)
    ]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield [s.server_address[1] for s in servers]
    for s in servers:
        s.shutdown()


def reachable(gateway: str, port: int) -> bool:
    cmd = f"wget -q -T 3 -O /dev/null http://{gateway}:{port}/"
    return sh("docker", "exec", SIDECAR, "sh", "-c", cmd, check=False).returncode == 0


def test_only_allowed_host_port_is_reachable(sidecar, host_servers):
    allowed, denied = host_servers
    gateway = sh(
        "docker", "exec", SIDECAR, "awk", "/host.docker.internal/ {print $1; exit}", "/etc/hosts"
    ).stdout.strip()
    assert reachable(gateway, allowed) and reachable(gateway, denied), (
        "host services unreachable before rules"
    )

    sh("sudo", "-n", HELPER, "apply", SIDECAR, f"{gateway}:{allowed}")
    assert reachable(gateway, allowed)
    assert not reachable(gateway, denied)
    assert f"--dport {allowed}" in sh("sudo", "-n", HELPER, "show", SIDECAR).stdout

    sh("sudo", "-n", HELPER, "apply", SIDECAR, f"{gateway}:{allowed}")
    rules = sh("sudo", "-n", HELPER, "show", SIDECAR).stdout
    assert rules.count(f"--dport {allowed}") == 1, "apply must replace rules, not add to them"

    sh("sudo", "-n", HELPER, "clear", SIDECAR)
    assert reachable(gateway, denied)


def test_refuses_container_without_sysbox():
    if sh("sudo", "-n", "-l", HELPER, check=False).returncode != 0:
        pytest.skip("firewall helper not installed; run host/setup.sh")
    name = "vivibox-fwtest-2-dind"
    sh("docker", "rm", "-f", name, check=False)
    sh("docker", "run", "-d", "--name", name, "alpine:3", "sleep", "60")
    try:
        result = sh("sudo", "-n", HELPER, "show", name, check=False)
        assert result.returncode == 1 and "sysbox-runc" in result.stderr
    finally:
        sh("docker", "rm", "-f", name, check=False)
