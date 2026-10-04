"""vivibox hostd: the process on the host that runs the DNS forwarder (resolver.py), started like a
task's supervisor, in the background, at the first task's start. Its pid is in
$XDG_RUNTIME_DIR/vivibox/hostd.pid and its output in hostd.log beside it, so both go with the
session.

While it is down a pod resolves no names: the host's rules send every query to it and nowhere else,
so nothing goes past it. That is on purpose; a pod does not start then, and the view's header says
why (problem)."""

from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from . import resolver
from .resolver import PORT

# A query any stub answers on its own, without the internet: localhost, type A.
PROBE_NAME = b"\x09localhost\x00\x00\x01\x00\x01"
NEXT_STEP = "start it again: vivibox hostd start"


class HostdError(Exception):
    pass


def folder() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "vivibox"


def prepared_folder() -> Path:
    """The folder, made when it is not there; HostdError says why it cannot be."""
    where = folder()
    try:
        where.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise HostdError(
            f"no folder of yours for its pid file, {where}: {e.strerror or e}; a login session has "
            f"one in /run/user/{os.getuid()}, or set XDG_RUNTIME_DIR to a folder of yours"
        ) from None
    return where


@contextlib.contextmanager
def locked(name: str) -> Iterator[None]:
    """One process at a time through what follows, on this machine for this user: several tasks
    started at once each prepare the host and ensure the forwarder."""
    try:
        lock = (prepared_folder() / name).open("a")
    except OSError as e:
        raise HostdError(f"cannot take the lock {folder() / name}: {e.strerror or e}") from None
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def pid_file() -> Path:
    return folder() / "hostd.pid"


def log_file() -> Path:
    return folder() / "hostd.log"


def record(pid: int) -> None:
    """The pid file naming pid, put in place whole. Written in place it is empty for a moment,
    and the forwarder writes its own pid as it comes up while its start asks ten times a second
    whether it still runs (ensure): a start that read the file just then took the forwarder
    for ended, and left it running with no pid file."""
    path = pid_file()
    whole = path.with_name(f"{path.name}.{os.getpid()}")
    whole.write_text(str(pid))
    os.replace(whole, path)


def recorded() -> int | None:
    try:
        return int(pid_file().read_text())
    except (OSError, ValueError):
        return None


def running() -> int | None:
    """Its pid while the process the pid file names is a hostd; None otherwise."""
    pid = recorded()
    if pid is None:
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    return pid if b"hostd" in cmdline else None


def check(address: str, port: int = PORT, timeout: float = 2.0) -> str:
    """ "" when the forwarder answers a query (through to the stub resolver it asks); else why not."""
    query_id = secrets.token_bytes(2)
    query = query_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + PROBE_NAME
    where = f"{address}:{port}"
    try:
        with socket.socket(resolver.family(address), socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.connect((address, port))
            s.send(query)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                answer = s.recv(resolver.DATAGRAM)
                if answer[:2] == query_id and len(answer) > 2 and answer[2] & 0x80:
                    return ""
    except ConnectionRefusedError:
        return f"nothing listens on {where}"
    except TimeoutError:
        pass
    except OSError as e:
        return f"{where}: {e.strerror or e}"
    return f"{where} does not answer, or the resolver it asks does not"


def problem(
    address: str, port: int = PORT, timeout: float = 1.0, pods_up: Callable[[], bool] = lambda: False
) -> str:
    """What the view's header says while the forwarder that should run does not; "" when it runs.
    Without a pid file, never started or stopped on purpose (the next task's start starts it),
    it says so only while a pod runs (pods_up), whose DNS then goes nowhere: a logout without
    linger empties the runtime folder, pid file and all."""
    pid = recorded()
    if pid is None:
        if pods_up() and (why := check(address, port, timeout)):
            return f"DNS forwarder not running: {why}, so pods resolve no names; {NEXT_STEP}"
        return ""
    if running() is None:
        return (
            f"DNS forwarder not running: its process {pid} has ended, so pods resolve no names; {NEXT_STEP}"
        )
    if why := check(address, port, timeout):
        return f"DNS forwarder not running: {why}, so pods resolve no names; {NEXT_STEP}"
    return ""


def start(address: str, port: int = PORT, upstream: str = "") -> int:
    prepared_folder()
    command = [sys.executable, "-m", "vivibox", "hostd", "run", "--address", address, "--port", str(port)]
    with log_file().open("a") as out:
        p = subprocess.Popen(
            [*command, *(("--upstream", upstream) if upstream else ())],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip
    record(p.pid)
    return p.pid


def stop() -> bool:
    """Ends the forwarder; False when none ran."""
    pid = running()
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(60):
            if running() != pid:
                break
            time.sleep(0.05)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    pid_file().unlink(missing_ok=True)
    return pid is not None


def ensure(address: str, port: int = PORT, upstream: str = "", timeout: float = 5.0) -> None:
    """The forwarder answering on address:port, started when it does not. HostdError says why
    it does not answer after timeout seconds. One at a time (locked): two at once would
    stop each other's."""
    with locked("hostd.lock"):
        if not check(address, port, timeout=1.0):
            return
        # One that runs and does not answer listens elsewhere (another address in config.toml) or is
        # stuck: it goes, and one for here starts.
        stop()
        pid = start(address, port, upstream)
        deadline = time.monotonic() + timeout
        why = "it did not answer"
        while time.monotonic() < deadline:
            if running() != pid:
                why = "it ended"
                break
            if not (why := check(address, port, timeout=0.5)):
                return
            time.sleep(0.1)
        stop()
        raise HostdError(f"the DNS forwarder on {address}:{port} did not start: {why}; its log: {tail()}")


def tail(lines: int = 5) -> str:
    try:
        said = log_file().read_text().strip().splitlines()[-lines:]
    except OSError:
        return "(none)"
    return " / ".join(said) or "(empty)"


def serve(address: str, port: int = PORT, upstream: str = "") -> int:
    """vivibox hostd run: the forwarder in this process, until SIGTERM."""
    host, sep, upstream_port = upstream.rpartition(":")
    try:
        stub = (host, int(upstream_port)) if sep else resolver.stub()
        forwarder = resolver.Forwarder((address, port), stub)
    except (resolver.ResolverError, OSError, ValueError) as e:
        print(f"vivibox hostd: cannot listen on {address}:{port}: {e}", file=sys.stderr, flush=True)
        return 1
    try:
        prepared_folder()
    except HostdError as e:
        print(f"vivibox hostd: {e}", file=sys.stderr, flush=True)
        return 1
    signal.signal(signal.SIGTERM, lambda *_: forwarder.stop())
    signal.signal(signal.SIGINT, lambda *_: forwarder.stop())
    record(os.getpid())
    print(f"vivibox hostd: forwarding {address}:{port} to {stub[0]}:{stub[1]}", flush=True)
    try:
        forwarder.serve()
    finally:
        if recorded() == os.getpid():
            pid_file().unlink(missing_ok=True)
    return 0
