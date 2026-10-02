"""The DNS forwarder every pod asks: forward-only. The host's rules send each query a pod makes,
to whatever address it names, here (DNAT of port 53 to PORT on the host's services address);
this passes it unchanged to the host's own stub resolver and passes the answer back. It parses
nothing and decides nothing: the policy on names is phase 4a's, in this module.

It listens on an unprivileged port, so it runs as you: no root process holds a socket a pod talks
to. UDP and TCP both: an answer too large for a datagram comes truncated and the client asks again
over TCP, which goes to the same stub and nowhere else."""

from __future__ import annotations

import contextlib
import ipaddress
import selectors
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Where it listens on the host's services address; the host's rules turn port 53 into it.
PORT = 5053
RESOLV_CONF = Path("/etc/resolv.conf")
# What one forwarder holds at once: queries waiting on the stub, and TCP connections.
PENDING_LIMIT = 512
TCP_LIMIT = 64
DATAGRAM = 65535


class ResolverError(Exception):
    pass


def stub(path: Path = RESOLV_CONF) -> tuple[str, int]:
    """The host's own resolver: the first nameserver of resolv.conf (Ubuntu's is systemd-resolved
    on 127.0.0.53)."""
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        raise ResolverError(f"cannot read {path}: {e}") from None
    for line in lines:
        words = line.split()
        if len(words) >= 2 and words[0] == "nameserver":
            found = words[1].split("%")[0]
            try:
                return str(ipaddress.ip_address(found)), 53
            except ValueError:
                continue
    raise ResolverError(f"no nameserver in {path}")


def family(host: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET


class Forwarder:
    """Listens on listen (UDP and TCP, one port) and relays to upstream. on_query, when given, is
    told of every query: who sent it and over what ("udp", "tcp")."""

    def __init__(
        self,
        listen: tuple[str, int],
        upstream: tuple[str, int],
        timeout: float = 5.0,
        on_query: Callable[[tuple, str], None] | None = None,
    ):
        self.upstream = upstream
        self.timeout = timeout
        self.on_query = on_query
        self.stopped = threading.Event()
        self.udp = socket.socket(family(listen[0]), socket.SOCK_DGRAM)
        self.tcp = socket.socket(family(listen[0]), socket.SOCK_STREAM)
        try:
            self.udp.bind(listen)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind((listen[0], self.udp.getsockname()[1]))
            self.tcp.listen(TCP_LIMIT)
        except OSError:
            self.udp.close()
            self.tcp.close()
            raise
        self.connections = threading.BoundedSemaphore(TCP_LIMIT)
        # Where it listens, the port the system picked for port 0 included.
        self.address: tuple[str, int] = self.udp.getsockname()[:2]

    def stop(self) -> None:
        self.stopped.set()

    def serve(self) -> None:
        """Until stop(): datagrams in this thread, each TCP connection in a thread of its own."""
        sel = selectors.DefaultSelector()
        sel.register(self.udp, selectors.EVENT_READ, "udp")
        sel.register(self.tcp, selectors.EVENT_READ, "tcp")
        pending: dict[socket.socket, tuple[tuple, float]] = {}
        try:
            while not self.stopped.is_set():
                for key, _ in sel.select(timeout=0.2):
                    if key.data == "udp":
                        self._query(sel, pending)
                    elif key.data == "tcp":
                        self._connection()
                    else:
                        self._answer(sel, pending, key.fileobj)
                now = time.monotonic()
                for up in [s for s, (_, deadline) in pending.items() if deadline < now]:
                    self._drop(sel, pending, up)  # the stub said nothing: neither do we
        finally:
            for up in list(pending):
                self._drop(sel, pending, up)
            sel.close()
            self.udp.close()
            self.tcp.close()

    def _query(self, sel: selectors.BaseSelector, pending: dict) -> None:
        try:
            data, peer = self.udp.recvfrom(DATAGRAM)
        except OSError:
            return
        if self.on_query:
            self.on_query(peer, "udp")
        if len(pending) >= PENDING_LIMIT:
            return
        up = socket.socket(family(self.upstream[0]), socket.SOCK_DGRAM)
        try:
            up.setblocking(False)
            up.connect(self.upstream)
            up.send(data)
        except OSError:
            up.close()
            return
        sel.register(up, selectors.EVENT_READ, peer)
        pending[up] = (peer, time.monotonic() + self.timeout)

    def _answer(self, sel: selectors.BaseSelector, pending: dict, up) -> None:
        peer, _ = pending.get(up, (None, 0))
        try:
            data = up.recv(DATAGRAM)
            if peer is not None:
                self.udp.sendto(data, peer)
        except OSError:
            pass  # the stub refused (nothing listens there): no answer
        self._drop(sel, pending, up)

    @staticmethod
    def _drop(sel: selectors.BaseSelector, pending: dict, up) -> None:
        pending.pop(up, None)
        with contextlib.suppress(KeyError, ValueError):
            sel.unregister(up)
        up.close()

    def _connection(self) -> None:
        try:
            conn, peer = self.tcp.accept()
        except OSError:
            return
        if self.on_query:
            self.on_query(peer, "tcp")
        if not self.connections.acquire(blocking=False):
            conn.close()
            return
        threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        """Bytes both ways between the pod's connection and one to the stub, until both ends
        close or nothing moves for a while."""
        try:
            with conn, socket.create_connection(self.upstream, timeout=self.timeout) as up:
                conn.settimeout(self.timeout)
                sel = selectors.DefaultSelector()
                other = {conn: up, up: conn}
                for s in other:
                    sel.register(s, selectors.EVENT_READ)
                open_ends = 2
                while open_ends and not self.stopped.is_set():
                    ready = sel.select(timeout=self.timeout * 2)
                    if not ready:
                        return
                    for key, _ in ready:
                        data = key.fileobj.recv(DATAGRAM)
                        if data:
                            other[key.fileobj].sendall(data)
                            continue
                        sel.unregister(key.fileobj)
                        open_ends -= 1
                        with contextlib.suppress(OSError):
                            other[key.fileobj].shutdown(socket.SHUT_WR)
                sel.close()
        except OSError:
            pass
        finally:
            self.connections.release()
