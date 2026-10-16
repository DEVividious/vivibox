"""python -m vivibox.index <directory> [<socket name>]: the wrapper as its harness starts it, on
stdio, in front of the index's socket in that directory."""

from __future__ import annotations

import ctypes
import os
import sys

from vivibox.index import channel, server
from vivibox.index.session import Session

PR_SET_DUMPABLE = 4


def undumpable() -> None:
    """The process made undumpable, before any connection: the index, at the other end of a
    unix socket, can learn this process (SO_PEERPIDFD) and, while it is dumpable, take handles
    to its namespaces, as one process of a user may of another of the same user. Undumpable,
    its /proc entries are root's and the kernel refuses it."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "the wrapper could not make itself undumpable")


def write(data: bytes) -> None:
    while data:
        data = data[os.write(1, data) :]


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: python -m vivibox.index <directory> [<socket name>]", file=sys.stderr)
        return 2
    undumpable()
    connect = channel.unix_entry(argv[0], argv[1] if len(argv) == 2 else "index.sock")
    server.serve(lambda n: os.read(0, n), write, Session(connect))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
