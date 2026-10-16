"""How the bytes between the wrapper and the index travel: a Channel sends, receives at most
so many bytes within so many seconds, and closes. The session needs no more of it, so the
socket or channel that the index's container is given is a function that opens one.

`unix_entry` is the shape the architecture names: a unix socket the index made in a directory
the planner's container mounts read-only. The entry is checked before the connect, without
following a link, and only a socket is connected to; a link, a regular file, a FIFO or a
directory in its place is refused. The check and the connect are two steps, and the index can
change the entry between them: that residual is the channel's, not closed here."""

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
    following a link, the entry looked at relative to it without following one, and the
    connect made through the descriptor held, so the directory cannot be swapped under it."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("the socket's name is one entry of its directory")

    def connect(timeout: float) -> Channel:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            found = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if not stat.S_ISSOCK(found.st_mode):
                raise NotASocket(f"{name} is not a socket")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            try:
                sock.settimeout(timeout)
                sock.connect(f"/proc/self/fd/{folder}/{name}")
            except BaseException:
                sock.close()
                raise
            return SocketChannel(sock)
        finally:
            os.close(folder)

    return connect
