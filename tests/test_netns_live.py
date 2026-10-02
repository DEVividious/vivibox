"""The host's egress rules (host/vivibox-host) and the DNS forwarder, on a real Sysbox container on
a task network. Needs the helper and its sudo rule, which host/setup.sh installs:

    uv run pytest -m docker tests/test_netns_live.py

Skipped, with the reason, where the helper is not installed. The sidecar here is a plain Alpine
under Sysbox: root in it holds the pod's network namespace, as root in a real sidecar does."""

import http.server
import ipaddress
import json
import os
import random
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from vivibox import probe
from vivibox.config import Config, ConfigError, HostService, load_config
from vivibox.egress import hostd, resolver
from vivibox.pod import HELPER, Pod
from vivibox.pod_egress import helper_missing, prepare_host

pytestmark = pytest.mark.docker

TASK = f"fwtest-{random.randint(1000, 9999)}"
SIDECAR = f"vivibox-{TASK}-dind"


def sh(*cmd: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def inside(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return sh("docker", "exec", SIDECAR, "sh", "-c", script, check=False, timeout=timeout)


def config() -> Config:
    try:
        return load_config()
    except ConfigError:
        return Config(tasks_dir=Path("/srv/vivibox"), max_rounds=3, roles={})


class Empty(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def serve() -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Empty)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def lan_ip() -> str:
    out = sh("ip", "-4", "route", "get", "1.1.1.1").stdout.split()
    return out[out.index("src") + 1]


@pytest.fixture(scope="module")
def pod(tmp_path_factory):
    if why := helper_missing():
        pytest.skip(why)
    cfg = config()
    allowed, denied = serve(), serve()
    repo = tmp_path_factory.mktemp("fw") / "repo"
    repo.mkdir()
    p = Pod(
        TASK, repo, "alpine:3", [HostService("host.docker.internal", allowed.server_address[1])],
        network_pool=cfg.network_pool, host_address=cfg.host_address, allocation=repo.parent / "net.json",
    )  # fmt: skip
    prepare_host(cfg.host_address, cfg.network_pool)
    net = p.ensure_network()
    p.apply_firewall()
    sh("docker", "pull", "-q", "alpine:3")
    sh("docker", "rm", "-f", SIDECAR, check=False)
    sh("docker", "run", "-d", "--runtime=sysbox-runc", "--name", SIDECAR, "--network", p.network,
       "--ip", net.address, "--dns", p.host_ip, "--dns-search", ".",
       "--add-host", f"host.docker.internal:{net.gateway}", "alpine:3", "sleep", "3600")  # fmt: skip
    try:
        yield {"pod": p, "net": net, "allowed": allowed.server_address[1], "denied": denied.server_address[1]}
    finally:
        sh("docker", "rm", "-f", SIDECAR, check=False)
        p.remove_firewall()
        sh("docker", "network", "rm", p.network, check=False)
        allowed.shutdown()
        denied.shutdown()
        hostd.ensure(p.host_ip)  # whatever a test did to it


def reachable(address: str, port: int) -> bool:
    return inside(f"wget -q -T 3 -O /dev/null http://{address}:{port}/").returncode == 0


def test_only_the_allowed_host_service_is_reachable(pod):
    gw = pod["net"].gateway
    assert reachable(gw, pod["allowed"])
    assert not reachable(gw, pod["denied"])


def test_the_host_and_the_lan_are_refused_at_once(pod):
    for address in (pod["net"].gateway, lan_ip(), pod["pod"].host_ip):
        started = time.monotonic()
        assert inside(f"nc -z -w 5 {address} 22").returncode != 0, address
        assert time.monotonic() - started < 4, f"{address}: refused, not left to time out"
    assert not reachable(lan_ip(), pod["allowed"]), "a host service only on its own address"


def test_the_internet_is_open(pod):
    assert inside("wget -q -T 10 -O /dev/null https://repo.maven.apache.org/maven2/").returncode == 0


def test_ipv6_is_refused(pod):
    assert inside("ping -6 -c 1 -W 3 2606:4700:4700::1111").returncode != 0


STUN = "\\000\\001\\000\\000\\041\\022\\244\\102abcdefghijkl"


def stun_from_host() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        s.sendto(b"\x00\x01\x00\x00\x21\x12\xa4\x42abcdefghijkl", ("stun.l.google.com", 19302))
        try:
            return bool(s.recvfrom(2048)[0])
        except TimeoutError:
            return False


def test_udp_other_than_dns_is_refused(pod):
    """A STUN request answers from the host; from the pod, UDP to a port other than 53 is refused."""
    if not stun_from_host():
        pytest.skip("stun.l.google.com:19302 does not answer this machine either")
    got = inside(f"printf '{STUN}' | nc -u -w 3 stun.l.google.com 19302 | wc -c")
    assert got.stdout.strip() == "0", got


def test_dns_to_any_server_reaches_the_forwarder_from_the_sidecars_address(pod):
    """D13: the pod's own resolver and a query to 8.8.8.8 alike arrive at the host's forwarder,
    from the sidecar's address. A forwarder of the test's own stands in for hostd, to see them."""
    p = pod["pod"]
    hostd.stop()
    seen = []
    f = resolver.Forwarder(
        (p.host_ip, resolver.PORT), resolver.stub(), on_query=lambda peer, how: seen.append(peer[0])
    )
    threading.Thread(target=f.serve, daemon=True).start()
    try:
        assert inside("nslookup example.com").returncode == 0
        assert pod["net"].address in seen, seen
        seen.clear()
        assert inside("nslookup example.com 8.8.8.8").returncode == 0, "DNAT to the forwarder"
        assert seen and set(seen) == {pod["net"].address}, seen
    finally:
        f.stop()
        time.sleep(0.5)
        hostd.ensure(p.host_ip)


def test_dns_fails_while_the_forwarder_is_down_and_the_header_says_why(pod):
    p = pod["pod"]
    hostd.ensure(p.host_ip)
    os.kill(hostd.running(), signal.SIGKILL)
    time.sleep(0.5)
    try:
        assert inside("nslookup -timeout=2 example.com 1.1.1.1", timeout=30).returncode != 0, "fail-closed"
        cfg = Config(tasks_dir=p.repo, max_rounds=3, roles={}, host_address=p.host_address)
        assert probe.forwarder_problem(cfg).startswith("DNS forwarder not running: ")
    finally:
        hostd.ensure(p.host_ip)
    assert inside("nslookup example.com").returncode == 0


def test_applying_the_policy_again_mid_download_drops_nothing_and_opens_nothing(pod):
    """One transaction each time: a long download goes on, and the denied port stays denied in
    between, whatever the moment."""
    p, gw, denied = pod["pod"], pod["net"].gateway, pod["denied"]
    download = subprocess.Popen(
        ["docker", "exec", SIDECAR, "wget", "-q", "-O", "/dev/null", "https://speed.cloudflare.com/__down?bytes=60000000"],
    )  # fmt: skip
    probes = subprocess.Popen(
        ["docker", "exec", SIDECAR, "sh", "-c",
         f"n=0; while [ $n -lt 100 ]; do wget -q -T 1 -O /dev/null http://{gw}:{denied}/ && echo OPEN; "
         "n=$((n+1)); done"],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    for _ in range(20):
        p.apply_firewall()
    assert download.wait(timeout=600) == 0, "the download survived every reload"
    out, _ = probes.communicate(timeout=600)
    assert "OPEN" not in out, "no window while the rules were replaced"


def test_a_policy_refused_half_way_leaves_the_rules_before_it(pod):
    """A second network the helper refuses (it overlaps the first): nothing is loaded, and the
    rules before stay whole. A file nft itself refuses is all or nothing as well (one nft -f)."""
    p, net = pod["pod"], pod["net"]
    bad = p.policy()
    bad["networks"].append(
        {"role": "r", "bridge": f"vbx-{TASK}-r", "subnet": str(net.subnet), "policy": "open"}
    )
    refused = sh("sudo", "-n", HELPER, "apply-policy", TASK, json.dumps(bad), check=False)
    assert refused.returncode == 1 and "overlaps" in refused.stderr
    assert reachable(net.gateway, pod["allowed"]) and not reachable(net.gateway, pod["denied"])


def test_root_in_the_pod_cannot_remove_the_rules(pod):
    """H10: root in the sidecar flushes every table it can see; the rules are not among them."""
    gw = pod["net"].gateway
    flushed = inside(
        "apk add -q --no-cache nftables iptables && nft flush ruleset; iptables -F; echo done", timeout=180
    )
    assert "done" in flushed.stdout, flushed
    assert not reachable(gw, pod["denied"])
    assert inside(f"nc -z -w 5 {lan_ip()} 22").returncode != 0


def test_a_bridge_without_a_policy_is_refused(pod):
    """Fail-closed: the rules gone, the bridge's packets meet the table's catch-all."""
    p = pod["pod"]
    p.remove_firewall()
    try:
        assert inside("wget -q -T 5 -O /dev/null https://repo.maven.apache.org/maven2/").returncode != 0
    finally:
        p.apply_firewall()
    assert reachable(pod["net"].gateway, pod["allowed"])


def test_the_allocation_is_in_the_pool(pod):
    assert pod["net"].subnet.subnet_of(ipaddress.IPv4Network(pod["pod"].network_pool))
