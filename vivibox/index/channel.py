"""How the bytes between the wrapper and the index travel: a Channel sends, receives at most
so many bytes within so many seconds, and closes. The session needs no more of it, so the
socket or channel that the index's container is given is a function that opens one.

`unix_entry` is the shape the architecture names: a unix socket the index made in a directory
the planner's container mounts read-only. The entry itself is opened, without following a link
and without opening what it is (O_PATH), and checked on the descriptor held: only a socket is
connected to; a link, a regular file, a FIFO or a directory in its place is refused. The connect
goes through that descriptor, so it reaches the socket that was checked whatever the index does
to the name meanwhile: a name swapped for a link to a socket of the planner's side is never
followed. The wrapper reads bytes and no ancillary data: a descriptor the index sends is never
received."""

from __future__ import annotations

import os
import socket
import stat
from collections.abc import Callable
from typing import Protocol


class Channel(Protocol):
    def send(self, data: bytes, timeout: float) -> None: ...

    def recv(self, limit: int, timeout: float) -> bytes:
        """At most limit bytes, b"" once the other side has closed; TimeoutError past timeout."""
        ...

    def close(self) -> None: ...


class NotASocket(OSError):
    """The entry where the index's socket should be is something else."""


class SocketChannel:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def send(self, data: bytes, timeout: float) -> None:
        if timeout <= 0:
            raise TimeoutError("no time left to send")
        # Since Python 3.5 the timeout bounds the whole of sendall, not each piece of it.
        self._sock.settimeout(timeout)
        self._sock.sendall(data)

    def recv(self, limit: int, timeout: float) -> bytes:
        if timeout <= 0:
            raise TimeoutError("no time left to receive")
        self._sock.settimeout(timeout)
        return self._sock.recv(limit)

    def close(self) -> None:
        self._sock.close()


def unix_entry(directory: str, name: str) -> Callable[[float], Channel]:
    """A connector to the socket called name in directory: the directory opened without
    following a link, the entry opened relative to it without following one, checked on that
    descriptor, and the connect made through it, so neither can be swapped under the connect."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("the socket's name is one entry of its directory")

    def connect(timeout: float) -> Channel:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            # O_PATH opens no file: a FIFO does not block, a device's driver is not asked; a link
            # is opened as the link, which the check refuses.
            entry = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder)
        finally:
            os.close(folder)
        try:
            if not stat.S_ISSOCK(os.fstat(entry).st_mode):
                raise NotASocket(f"{name} is not a socket")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            try:
                sock.settimeout(timeout)
                # The kernel resolves the descriptor's own inode: the socket checked above.
                sock.connect(f"/proc/self/fd/{entry}")
            except BaseException:
                sock.close()
                raise
            return SocketChannel(sock)
        finally:
            os.close(entry)

    return connect
