"""The DNS forwarder every pod asks (vivibox/egress/resolver.py) and the host process that runs it
(vivibox hostd). Without root: the forwarder listens on localhost here, and a stub resolver of the
test's own stands for the host's."""

import contextlib
import itertools
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vivibox import probe
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
        # One number for UDP and TCP: the kernel's choice for the one may be taken for the other.
        self.port = free_port()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((HERE, self.port))
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind((HERE, self.port))
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
    f = resolver.Forwarder((HERE, free_port()), upstream, **kw)
    threading.Thread(target=f.serve, daemon=True).start()
    return f


QUERY = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"


def ask(address, data=QUERY, timeout=2.0) -> bytes | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))  # who asks, said: not left to the route the machine has to HERE
        s.settimeout(timeout)
        s.sendto(data, address)
        try:
            return s.recvfrom(65535)[0]
        except TimeoutError:
            return None


def test_a_query_is_relayed_to_the_stub_and_its_answer_back_unchanged(stub):
    f = forwarding((HERE, stub.port))
    try:
        answer = ask(f.address)
        assert stub.got == [QUERY], "the query as the pod sent it, byte for byte"
        assert answer == Stub.answer(QUERY), "the answer as the stub gave it"
        odd = b"\xff" * 700  # not DNS at all: forwarded as it is, nothing parsed
        assert ask(f.address, odd) == Stub.answer(odd)
    finally:
        f.stop()


def test_a_query_over_tcp_is_relayed_too(stub):
    f = forwarding((HERE, stub.port))
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
    f = forwarding((HERE, free_port()), timeout=0.5)  # nobody listens there, and nobody will
    try:
        assert ask(f.address, timeout=1.0) is None
        assert ask(f.address, timeout=1.0) is None, "still serving"
        f.upstream = (HERE, stub.port)
        assert ask(f.address) == Stub.answer(QUERY)
    finally:
        f.stop()


def test_it_tells_who_asked(stub):
    seen = []
    f = forwarding((HERE, stub.port), on_query=lambda peer, how: seen.append((peer[0], how)))
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
    f = forwarding((HERE, stub.port))
    try:
        assert hostd.check(f.address[0], f.address[1]) == ""
        assert stub.got and stub.got[-1][12:] == b"\x09localhost\x00\x00\x01\x00\x01"
    finally:
        f.stop()
    said = hostd.check(f.address[0], f.address[1], timeout=0.5)
    assert said and f"{f.address[0]}:{f.address[1]}" in said


# --- vivibox hostd ------------------------------------------------------------------------------------


# The pids of every forwarder the test under way started, for the teardown of runtime.
STARTED: list[int] = []


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    STARTED.clear()
    start = hostd.start
    monkeypatch.setattr(hostd, "start", lambda *a, **kw: STARTED.append(start(*a, **kw)) or STARTED[-1])
    yield tmp_path / "run" / "vivibox"
    reap(STARTED)


# What hostd.start runs: a forwarder's argv holds these three words in a row, and nothing else's.
HOSTD_ARGV = (b"-m", b"vivibox", b"hostd")
# How long the teardown waits for a killed forwarder to go before it fails, never hanging the run.
REAP_TIMEOUT = 5.0


def forwarder_argv(pid: int) -> list[bytes] | None:
    """The process's argv while it is a forwarder (hostd.start's command, word for word in a
    row); None when it is not, or is gone: a pid a test's forwarder had may be another's by now."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    runs = any(tuple(argv[i : i + 3]) == HOSTD_ARGV for i in range(len(argv) - 2))
    return argv if runs else None


def gone(pid: int) -> bool:
    """Whether the process has ended, reaped here when it is a child of this one."""
    try:
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            return True
    except ChildProcessError:
        pass  # not a child of this process: /proc says whether it is still there
    return not Path(f"/proc/{pid}").exists()


def reap(started: list[int]) -> None:
    """No forwarder left on the machine by a test: the one the pid file names and every one the
    test started, each killed while its argv is still a forwarder's, and waited for within a
    bound; whether or not hostd.stop() works in the code under test (a mutant that broke it once
    left two behind, and a bound-less wait for a kill that did not land once hung the run)."""
    with contextlib.suppress(Exception):
        hostd.stop()
    if (pid := hostd.recorded()) is not None and pid not in started:
        started.append(pid)
    left = []
    for pid in started:
        if (argv := forwarder_argv(pid)) is None:
            continue
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + REAP_TIMEOUT
        while not gone(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not gone(pid):
            left.append(f"pid {pid}, {b' '.join(argv).decode(errors='replace').strip()}")
    with contextlib.suppress(OSError):
        hostd.pid_file().unlink()
    if left:
        pytest.fail("a forwarder is still running after the teardown: " + "; ".join(left))


def test_the_teardown_never_waits_forever_for_a_kill_that_did_not_land(runtime, stub, monkeypatch):
    """A reviewer's mutant once hung the suite ten minutes in this teardown: the kill had not
    landed, and waitpid without a bound waited for it. The teardown waits a few seconds, then
    fails, naming the pid and what it is, and the run goes on."""
    port = free_port()
    hostd.ensure(HERE, port, upstream=f"{HERE}:{stub.port}")
    pid = hostd.running()
    outcome: dict[str, str] = {}

    def teardown():
        try:
            reap([pid])
            outcome["said"] = "returned without a word"
        except BaseException as e:  # pytest.fail raises its own; what it says is the point
            outcome["said"] = str(e)

    real_kill = os.kill
    with monkeypatch.context() as m:
        m.setattr(hostd, "stop", lambda: False)
        m.setattr(os, "kill", lambda pid, sig: None)  # the kill does not land
        waiting = threading.Thread(target=teardown, daemon=True)
        waiting.start()
        waiting.join(10)
    try:
        assert not waiting.is_alive(), "the teardown waited past its bound"
        assert f"pid {pid}" in outcome["said"] and "hostd" in outcome["said"], outcome
    finally:
        real_kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def test_the_teardown_kills_only_a_forwarder_never_a_pid_something_else_has_now(runtime):
    """A pid a test's forwarder had may be another process's by the teardown: one whose argv is
    not `-m vivibox hostd` is left alone, whatever its name says."""
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        reap([other.pid])
        assert other.poll() is None, "not a forwarder: left alone"
    finally:
        other.kill()
        other.wait()


def test_the_fixture_leaves_no_forwarder_behind_even_when_stop_is_broken(runtime, stub, monkeypatch):
    """A reviewer's mutation run once left two forwarders on the machine: the mutant had broken
    hostd.stop(), and the fixture's teardown was that call. The teardown ends what the pid file
    names and every forwarder a test started, on its own, whatever the code under test does."""
    port = free_port()
    hostd.ensure(HERE, port, upstream=f"{HERE}:{stub.port}")
    pid = hostd.running()
    monkeypatch.setattr(hostd, "stop", lambda: False)
    reap(STARTED)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (runtime / "hostd.pid").exists()


def own_address() -> str:
    """A loopback address of this process's alone: 127.x.y.z from its pid, which no other live
    process has. All of 127.0.0.0/8 is this machine's, so nothing has to be set up."""
    pid = os.getpid()
    return f"127.{64 + (pid >> 16)}.{(pid >> 8) & 0xFF}.{pid & 0xFF}"


# Where a test's own forwarder listens, and what it is asked on.
HERE = own_address()
KERNELS_PORTS = Path("/proc/sys/net/ipv4/ip_local_port_range")
PORTS = itertools.count(int(KERNELS_PORTS.read_text().split()[0]) - 8000)


def free_port() -> int:
    """A port on HERE for a forwarder the test starts as a process, which binds it a second
    later; or for nothing at all, where a test needs a port nobody listens on. Still free then.

    This used to bind port 0 on 127.0.0.1 and let go. The kernel hands such a port to the next
    socket that asks for any, and under load another worker's stub or query got it before the
    forwarder did: "cannot listen on 127.0.0.1:45994: Address already in use". A port for
    nothing came to belong to a stub that answers. So: a port below those the kernel hands out
    by itself, on an address only this process uses, and never the same one twice."""
    for port in PORTS:
        assert port < int(KERNELS_PORTS.read_text().split()[0]), "no port left below the kernel's own"
        try:
            for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                with socket.socket(socket.AF_INET, kind) as s:
                    s.bind((HERE, port))
        except OSError:
            continue  # a service of this machine's listens there on every address
        return port


def test_a_port_for_a_forwarder_is_one_nobody_else_is_given():
    """Not one the kernel gives the next socket that asks for any port, and not on an address
    another process binds: both were so, and the port was taken while the forwarder started."""
    low, high = (int(n) for n in KERNELS_PORTS.read_text().split())
    ports = [free_port() for _ in range(3)]
    assert len(set(ports)) == 3 and not [p for p in ports if low <= p <= high]
    assert HERE.startswith("127.") and HERE != "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((HERE, ports[0]))  # the machine takes the address as its own


def test_hostd_starts_once_answers_and_stops(runtime, stub):
    port = free_port()
    upstream = f"{HERE}:{stub.port}"
    hostd.ensure(HERE, port, upstream=upstream)
    pid = int((runtime / "hostd.pid").read_text())
    assert hostd.running() == pid and hostd.check(HERE, port) == ""
    hostd.ensure(HERE, port, upstream=upstream)
    assert hostd.running() == pid, "idempotent: the one that answers stays"
    assert hostd.problem(HERE, port) == ""
    assert hostd.stop() is True
    assert not (runtime / "hostd.pid").exists() and hostd.running() is None
    assert hostd.problem(HERE, port) == "", "stopped on purpose, or never started: nothing to say"


def test_a_forwarder_that_died_is_named_in_the_header_and_started_again(runtime, stub):
    from vivibox import roles

    port = free_port()
    upstream = f"{HERE}:{stub.port}"
    hostd.ensure(HERE, port, upstream=upstream)
    pid = hostd.running()
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)  # this process's child: until it has ended, however long the machine takes
    assert hostd.running() is None
    said = hostd.problem(HERE, port)
    assert said.startswith("DNS forwarder not running: ")
    assert f"process {pid}" in said and "vivibox hostd start" in said
    base = tmp_config(runtime.parent.parent)
    assert roles.machine_problem(load_config(base), docker_ok=True, forwarder=said) == said
    assert roles.machine_problem(load_config(base), docker_ok=False, forwarder=said).startswith("Docker")
    hostd.ensure(HERE, port, upstream=upstream)
    assert hostd.running() not in (None, pid) and hostd.problem(HERE, port) == ""


def test_the_pid_file_names_a_pid_at_every_moment_it_is_rewritten(runtime, monkeypatch):
    """The forwarder writes its own pid as it comes up, over the one its start wrote, while that
    start asks ten times a second whether it still runs. A file written in place is empty for a
    moment: a start that read it just then took its forwarder for ended, failed with "did not
    start: it ended", and left the forwarder running with no pid file (seen on a busy machine).
    Planted here: a reader at the moment a file being written has been emptied."""
    from types import SimpleNamespace

    nobody = 2**22 + 1  # past the kernel's highest pid: no process, a forwarder least of all
    runtime.mkdir(parents=True)
    hostd.pid_file().write_text(str(nobody))
    read = []
    write_text = Path.write_text

    def emptied_then_read(path, text, *args, **kwargs):
        path.open("w").close()  # as write_text leaves a file before its text is in it
        read.append(hostd.recorded())
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", emptied_then_read)
    monkeypatch.setattr(hostd.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=nobody + 1))
    assert hostd.start("127.0.0.1", 5353) == nobody + 1
    assert read == [nobody] and hostd.recorded() == nobody + 1, "the start's own write"

    class Served:
        def __init__(self, *args, **kwargs):
            pass

        def serve(self):
            read.append(hostd.recorded())

    monkeypatch.setattr(resolver, "Forwarder", Served)
    monkeypatch.setattr(hostd.signal, "signal", lambda *args: None)
    assert hostd.serve("127.0.0.1", 5353, upstream="127.0.0.1:53") == 0
    assert read == [nobody, nobody + 1, os.getpid()], "the forwarder's, over the start's"
    assert [p.name for p in runtime.iterdir() if p.name.startswith("hostd.pid")] == [], "nothing left"


def test_a_forwarder_that_runs_and_does_not_answer_is_named(runtime):
    port = free_port()
    program = "import time; print('up', flush=True); time.sleep(30)"
    sleeper = subprocess.Popen(
        [sys.executable, "-c", program, "vivibox", "hostd", "run"],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    try:
        # Its command line is there once its program runs; just started, a process has none yet.
        assert sleeper.stdout.readline() == b"up\n"
        runtime.mkdir(parents=True)
        (runtime / "hostd.pid").write_text(str(sleeper.pid))
        assert hostd.running() == sleeper.pid
        said = hostd.problem(HERE, port, timeout=0.3)
        assert said.startswith("DNS forwarder not running: ") and f"{HERE}:{port}" in said
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


def test_vivibox_hostd_run_listens_only_on_the_hosts_address_for_the_pods(env, monkeypatch):
    """Run by hand without --address, it takes network.host_address; never every interface, which
    would make it a resolver for your LAN."""
    from vivibox.cli import main

    asked = []
    monkeypatch.setattr(hostd, "serve", lambda address, port, upstream: asked.append(address) or 0)
    assert main(["hostd", "run"]) == 0
    assert asked == ["203.0.113.1"]


@pytest.mark.parametrize("address", ["", "0.0.0.0", "::"])
def test_the_forwarder_never_listens_on_every_interface(address):
    with pytest.raises(resolver.ResolverError, match="every interface"):
        resolver.Forwarder((address, 0), ("127.0.0.1", 53))


# --- what one client may hold of the forwarder ------------------------------------------------------


class Quiet:
    """A stub that answers only queries starting with b"answer-me", as one whose name servers
    are dead answers nothing: what a flood of random names leaves waiting. Over TCP it accepts and
    holds every connection."""

    def __init__(self):
        self.got: list[bytes] = []
        self.held: list[socket.socket] = []
        # One number for UDP and TCP: the kernel's choice for the one may be taken for the other.
        self.port = free_port()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((HERE, self.port))
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind((HERE, self.port))
        self.tcp.listen(64)
        threading.Thread(target=self.serve_udp, daemon=True).start()
        threading.Thread(target=self.serve_tcp, daemon=True).start()

    def serve_udp(self):
        while True:
            try:
                data, peer = self.udp.recvfrom(65535)
            except OSError:
                return
            self.got.append(data)
            if data.startswith(b"answer-me"):
                self.udp.sendto(b"answered", peer)

    def serve_tcp(self):
        while True:
            try:
                self.held.append(self.tcp.accept()[0])
            except OSError:
                return

    def close(self):
        for s in [self.udp, self.tcp, *self.held]:
            s.close()


@pytest.fixture
def quiet():
    q = Quiet()
    yield q
    q.close()


def send_from(source: str, address, data: bytes, wait: float = 0.0) -> bytes | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((source, 0))
        s.settimeout(wait or 0.01)
        s.sendto(data, address)
        if not wait:
            return None
        try:
            return s.recvfrom(65535)[0]
        except TimeoutError:
            return None


def settled(check, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not check():
        time.sleep(0.02)
    time.sleep(0.2)  # and nothing more arrives


def test_one_source_holds_at_most_its_share_of_the_waiting_queries(quiet):
    f = forwarding((HERE, quiet.port), pending_limit=8, per_source=2)
    try:
        for n in range(10):
            send_from("127.0.0.2", f.address, b"q%d" % n)
        settled(lambda: len(quiet.got) >= 2)
        assert len(quiet.got) == 2, "its share, not the forwarder's whole room"
    finally:
        f.stop()


def test_all_sources_together_hold_at_most_the_forwarders_room(quiet):
    f = forwarding((HERE, quiet.port), pending_limit=8, per_source=2)
    try:
        for source in range(2, 8):
            for n in range(2):
                send_from(f"127.0.0.{source}", f.address, b"q%d" % n)
        settled(lambda: len(quiet.got) >= 8)
        assert len(quiet.got) == 8
    finally:
        f.stop()


def test_a_flood_of_unanswered_queries_from_one_pod_leaves_room_for_another(quiet):
    """The review's case: one task asking random names whose servers never answer took every
    waiting slot, and another task's DNS went unanswered."""
    f = forwarding((HERE, quiet.port), pending_limit=8, per_source=2)
    try:
        for n in range(50):
            send_from("127.0.0.2", f.address, b"q%d" % n)
        assert send_from("127.0.0.3", f.address, b"answer-me", wait=2) == b"answered"
    finally:
        f.stop()


def test_the_default_share_is_a_quarter():
    assert resolver.PER_SOURCE == resolver.PENDING_LIMIT // 4
    assert resolver.TCP_PER_SOURCE == resolver.TCP_LIMIT // 4


def connect_from(source: str, address) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((source, 0))
    s.connect(address)
    return s


def test_tcp_connections_are_held_to_a_share_and_to_the_forwarders_room(quiet):
    f = forwarding((HERE, quiet.port), tcp_limit=4, tcp_per_source=1)
    mine = []
    try:
        mine += [connect_from("127.0.0.2", f.address) for _ in range(3)]
        settled(lambda: len(quiet.held) >= 1)
        assert len(quiet.held) == 1, "one source: its share"
        mine += [connect_from(f"127.0.0.{n}", f.address) for n in range(3, 9)]
        settled(lambda: len(quiet.held) >= 4)
        assert len(quiet.held) == 4, "all sources: the forwarder's room"
    finally:
        for s in mine:
            s.close()
        f.stop()


# --- a forwarder whose pid file is gone, two starts at once, no runtime folder -------------------


def test_a_forwarder_without_its_pid_file_is_named_while_a_pod_runs(runtime):
    """Logging out without linger empties $XDG_RUNTIME_DIR: the pid file goes, a pod still runs and
    resolves nothing. The header says so whenever a pod runs and the forwarder does not answer."""
    port = free_port()
    assert hostd.recorded() is None
    said = hostd.problem(HERE, port, timeout=0.3, pods_up=lambda: True)
    assert said.startswith("DNS forwarder not running: ") and "vivibox hostd start" in said
    assert hostd.problem(HERE, port, timeout=0.3, pods_up=lambda: False) == "", "no pod: nothing to say"


def test_the_header_asks_whether_a_pod_runs(monkeypatch):
    from vivibox.config import Config

    asked = {}

    def problem(address, port=resolver.PORT, timeout=1.0, pods_up=lambda: False):
        asked["pods_up"] = pods_up
        return ""

    monkeypatch.setattr(hostd, "problem", problem)
    probe.forwarder_problem(Config(tasks_dir=__import__("pathlib").Path("/t"), max_rounds=3, roles={}))
    assert asked["pods_up"] is probe.pods_running


def test_a_test_with_a_forwarder_of_its_own_asks_it_not_the_suites_stand_in(runtime):
    """conftest answers probe.forwarder_problem with "" for the suite, which has no forwarder to
    ask; a test marked real_hostd or docker has one, and the stand-in once hid from a live test
    that the forwarder it had killed was down. A pid file naming a process that has ended is
    reason enough: no forwarder has to run for the header to have something to say."""
    from vivibox.config import Config

    ended = subprocess.Popen(["true"])
    ended.wait()
    runtime.mkdir(parents=True)
    (runtime / "hostd.pid").write_text(str(ended.pid))
    said = probe.forwarder_problem(Config(tasks_dir=runtime, max_rounds=3, roles={}))
    assert said.startswith("DNS forwarder not running: ") and f"process {ended.pid}" in said


@pytest.mark.parametrize(("out", "code", "up"), [("3f2a\n", 0, True), ("", 0, False), ("3f2a\n", 1, False)])
def test_a_running_pod_is_one_docker_lists_with_vivibox_s_label(out, code, up):
    seen = []

    def runner(cmd, timeout=None):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, code, out, "")

    assert probe.pods_running(runner) is up
    assert seen[0][:2] == ["docker", "ps"] and "label=vivibox.task" in seen[0]


def test_two_starts_at_once_leave_one_forwarder_that_answers(runtime, stub):
    """Several tasks started after a reboot: each start ensures the forwarder, and neither may
    stop the other's."""
    port = free_port()
    failed = []

    def start():
        try:
            hostd.ensure(HERE, port, upstream=f"{HERE}:{stub.port}")
        except hostd.HostdError as e:
            failed.append(str(e))

    starts = [threading.Thread(target=start) for _ in range(3)]
    for t in starts:
        t.start()
    for t in starts:
        t.join()
    assert failed == []
    assert hostd.check(HERE, port) == "" and hostd.running() is not None


def test_a_forwarder_on_another_address_is_replaced_not_left_behind(runtime, stub):
    """network.host_address changed: the forwarder on the old one goes, it is not left answering."""
    old, new = free_port(), free_port()
    upstream = f"{HERE}:{stub.port}"
    hostd.ensure(HERE, old, upstream=upstream)
    hostd.ensure(HERE, new, upstream=upstream)
    assert hostd.check(HERE, new) == ""
    assert hostd.check(HERE, old, timeout=0.5) != "", "the old one is gone"


def test_no_runtime_folder_is_a_reason_not_a_crash(tmp_path, monkeypatch):
    shut = tmp_path / "shut"
    shut.mkdir(mode=0o500)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(shut))
    try:
        with pytest.raises(hostd.HostdError, match="XDG_RUNTIME_DIR"):
            hostd.ensure(HERE, free_port(), upstream="127.0.0.1:53", timeout=1)
    finally:
        shut.chmod(0o700)
