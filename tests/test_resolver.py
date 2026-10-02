"""The DNS forwarder every pod asks (vivibox/egress/resolver.py) and the host process that runs it
(vivibox hostd). Without root: the forwarder listens on localhost here, and a stub resolver of the
test's own stands for the host's."""

import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from vivibox.config import ConfigError, load_config
from vivibox.egress import hostd, resolver

pytestmark = pytest.mark.real_hostd

ROLES = (
    '[roles.planner]\nharness = "opencode"\nmodel = "m"\n[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
)


class Stub:
    """A resolver that answers every query with bytes of its own, and remembers what it got."""

    def __init__(self, tcp: bool = False):
        self.got: list[bytes] = []
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.port = self.udp.getsockname()[1]
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", self.port))
        self.tcp.listen(8)
        threading.Thread(target=self.serve_udp, daemon=True).start()
        threading.Thread(target=self.serve_tcp, daemon=True).start()

    @staticmethod
    def answer(query: bytes) -> bytes:
        # The query's id, the QR bit set: what any resolver's answer starts with.
        return query[:2] + bytes([query[2] | 0x80]) + query[3:] + b"\x00answer"

    def serve_udp(self):
        while True:
            try:
                data, peer = self.udp.recvfrom(65535)
            except OSError:
                return
            self.got.append(data)
            self.udp.sendto(self.answer(data), peer)

    def serve_tcp(self):
        while True:
            try:
                conn, _ = self.tcp.accept()
            except OSError:
                return
            with conn:
                length = struct.unpack("!H", conn.recv(2))[0]
                data = conn.recv(length)
                self.got.append(data)
                reply = self.answer(data)
                conn.sendall(struct.pack("!H", len(reply)) + reply)

    def close(self):
        self.udp.close()
        self.tcp.close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def forwarding(upstream, **kw):
    f = resolver.Forwarder(("127.0.0.1", 0), upstream, **kw)
    threading.Thread(target=f.serve, daemon=True).start()
    return f


QUERY = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"


def ask(address, data=QUERY, timeout=2.0) -> bytes | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(data, address)
        try:
            return s.recvfrom(65535)[0]
        except TimeoutError:
            return None


def test_a_query_is_relayed_to_the_stub_and_its_answer_back_unchanged(stub):
    f = forwarding(("127.0.0.1", stub.port))
    try:
        answer = ask(f.address)
        assert stub.got == [QUERY], "the query as the pod sent it, byte for byte"
        assert answer == Stub.answer(QUERY), "the answer as the stub gave it"
        odd = b"\xff" * 700  # not DNS at all: forwarded as it is, nothing parsed
        assert ask(f.address, odd) == Stub.answer(odd)
    finally:
        f.stop()


def test_a_query_over_tcp_is_relayed_too(stub):
    f = forwarding(("127.0.0.1", stub.port))
    try:
        with socket.create_connection(f.address, timeout=2) as s:
            s.sendall(struct.pack("!H", len(QUERY)) + QUERY)
            length = struct.unpack("!H", s.recv(2))[0]
            data = b""
            while len(data) < length:
                data += s.recv(length - len(data))
        assert data == Stub.answer(QUERY) and stub.got == [QUERY]
    finally:
        f.stop()


def test_a_dead_stub_means_no_answer_and_the_forwarder_goes_on(stub):
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    nobody = dead.getsockname()[1]
    dead.close()
    f = forwarding(("127.0.0.1", nobody), timeout=0.5)
    try:
        assert ask(f.address, timeout=1.0) is None
        assert ask(f.address, timeout=1.0) is None, "still serving"
        f.upstream = ("127.0.0.1", stub.port)
        assert ask(f.address) == Stub.answer(QUERY)
    finally:
        f.stop()


def test_it_tells_who_asked(stub):
    seen = []
    f = forwarding(("127.0.0.1", stub.port), on_query=lambda peer, how: seen.append((peer[0], how)))
    try:
        ask(f.address)
        assert seen == [("127.0.0.1", "udp")]
    finally:
        f.stop()


def test_the_stub_is_the_hosts_first_nameserver(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("# made by systemd-resolved\nnameserver 127.0.0.53\noptions edns0 trust-ad\nsearch .\n")
    assert resolver.stub(conf) == ("127.0.0.53", 53)
    conf.write_text("search corp\nnameserver fe80::1%eth0\nnameserver 10.0.0.2\n")
    assert resolver.stub(conf) == ("fe80::1", 53)
    conf.write_text("search corp\n")
    with pytest.raises(resolver.ResolverError, match="no nameserver"):
        resolver.stub(conf)


def test_a_probe_query_says_whether_it_is_answered(stub):
    f = forwarding(("127.0.0.1", stub.port))
    try:
        assert hostd.check(f.address[0], f.address[1]) == ""
        assert stub.got and stub.got[-1][12:] == b"\x09localhost\x00\x00\x01\x00\x01"
    finally:
        f.stop()
    said = hostd.check(f.address[0], f.address[1], timeout=0.5)
    assert said and f"{f.address[0]}:{f.address[1]}" in said


# --- vivibox hostd ------------------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    yield tmp_path / "run" / "vivibox"
    hostd.stop()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_hostd_starts_once_answers_and_stops(runtime, stub):
    port = free_port()
    upstream = f"127.0.0.1:{stub.port}"
    hostd.ensure("127.0.0.1", port, upstream=upstream)
    pid = int((runtime / "hostd.pid").read_text())
    assert hostd.running() == pid and hostd.check("127.0.0.1", port) == ""
    hostd.ensure("127.0.0.1", port, upstream=upstream)
    assert hostd.running() == pid, "idempotent: the one that answers stays"
    assert hostd.problem("127.0.0.1", port) == ""
    assert hostd.stop() is True
    assert not (runtime / "hostd.pid").exists() and hostd.running() is None
    assert hostd.problem("127.0.0.1", port) == "", "stopped on purpose, or never started: nothing to say"


def test_a_forwarder_that_died_is_named_in_the_header_and_started_again(runtime, stub):
    from vivibox import roles

    port = free_port()
    upstream = f"127.0.0.1:{stub.port}"
    hostd.ensure("127.0.0.1", port, upstream=upstream)
    pid = hostd.running()
    os.kill(pid, signal.SIGKILL)
    for _ in range(50):
        if hostd.running() is None:
            break
        time.sleep(0.05)
    said = hostd.problem("127.0.0.1", port)
    assert said.startswith("DNS forwarder not running: ")
    assert f"process {pid}" in said and "vivibox hostd start" in said
    base = tmp_config(runtime.parent.parent)
    assert roles.machine_problem(load_config(base), docker_ok=True, forwarder=said) == said
    assert roles.machine_problem(load_config(base), docker_ok=False, forwarder=said).startswith("Docker")
    hostd.ensure("127.0.0.1", port, upstream=upstream)
    assert hostd.running() not in (None, pid) and hostd.problem("127.0.0.1", port) == ""


def test_a_forwarder_that_runs_and_does_not_answer_is_named(runtime):
    port = free_port()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "vivibox", "hostd", "run"],
        start_new_session=True,
    )
    try:
        runtime.mkdir(parents=True)
        (runtime / "hostd.pid").write_text(str(sleeper.pid))
        assert hostd.running() == sleeper.pid
        said = hostd.problem("127.0.0.1", port, timeout=0.3)
        assert said.startswith("DNS forwarder not running: ") and f"127.0.0.1:{port}" in said
    finally:
        sleeper.kill()
        sleeper.wait()


def test_a_forwarder_that_cannot_start_says_why(runtime):
    with pytest.raises(hostd.HostdError, match="did not start"):
        hostd.ensure("192.0.2.77", free_port(), upstream="127.0.0.1:53", timeout=3)
    assert hostd.running() is None


def tmp_config(where):
    path = where / "cfg" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('tasks_dir = "/t"\n' + ROLES)
    return path.parent


# --- [network] host_address ------------------------------------------------------------------------


def test_the_host_address_is_test_net_3_unless_config_says_otherwise(tmp_path):
    base = tmp_config(tmp_path)
    config = load_config(base)
    assert config.host_address == "203.0.113.1/32" and config.host_ip == "203.0.113.1"
    (base / "config.toml").write_text('tasks_dir = "/t"\n[network]\nhost_address = "10.250.0.1"\n' + ROLES)
    assert load_config(base).host_address == "10.250.0.1/32"


@pytest.mark.parametrize(
    "value, said",
    [
        ('"203.0.113.0/24"', "one address"),
        ('"127.0.0.1"', "loopback"),
        ('"198.51.100.7"', "network.pool"),
        ('"host"', "an IPv4 address"),
        ("5", "an IPv4 address"),
    ],
)
def test_a_host_address_that_cannot_be_one_is_refused(tmp_path, value, said):
    base = tmp_config(tmp_path)
    (base / "config.toml").write_text(f'tasks_dir = "/t"\n[network]\nhost_address = {value}\n' + ROLES)
    with pytest.raises(ConfigError, match="network.host_address") as e:
        load_config(base)
    assert said in str(e.value)


def test_vivibox_hostd_says_whether_it_runs(env, monkeypatch, capsys):
    from vivibox.cli import main

    monkeypatch.setattr(hostd, "running", lambda: None)
    monkeypatch.setattr(hostd, "problem", lambda address, port=resolver.PORT, timeout=1.0: "")
    assert main(["hostd"]) == 0
    out = capsys.readouterr().out
    assert "not running" in out and "vivibox hostd start" in out
    stopped = []
    monkeypatch.setattr(hostd, "stop", lambda: stopped.append(True) or True)
    assert main(["hostd", "stop"]) == 0 and stopped
    assert "resolve no names" in capsys.readouterr().out


def test_vivibox_hostd_start_prepares_the_host_first(env, monkeypatch, capsys):
    """The forwarder listens on the host's address for the pods, which the root helper makes."""
    from vivibox import pod_egress
    from vivibox.cli import main

    asked = []
    monkeypatch.setattr(pod_egress, "prepare_host", lambda address, pool: asked.append((address, pool)))
    monkeypatch.setattr(hostd, "problem", lambda address, port=resolver.PORT, timeout=1.0: "")
    monkeypatch.setattr(hostd, "running", lambda: 4242)
    assert main(["hostd", "start"]) == 0
    assert asked == [("203.0.113.1/32", "198.51.100.0/24")]
    assert "runs on 203.0.113.1:5053 (process 4242)" in capsys.readouterr().out


def test_vivibox_hostd_start_says_why_it_could_not(env, monkeypatch, capsys):
    from vivibox import pod_egress
    from vivibox.cli import main
    from vivibox.pod import PodError

    def refused(address, pool):
        raise PodError("this machine's rules for pods could not be set: sudo: a password is required")

    monkeypatch.setattr(pod_egress, "prepare_host", refused)
    assert main(["hostd", "start"]) == 1
    assert "a password is required" in capsys.readouterr().err
