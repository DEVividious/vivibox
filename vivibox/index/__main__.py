"""python -m vivibox.index <directory> [<socket name>]: the wrapper as its harness starts it, on
stdio, in front of the index's socket in that directory."""

from __future__ import annotations

import os
import sys

from vivibox.index import channel, server
from vivibox.index.session import Session


def write(data: bytes) -> None:
    while data:
        data = data[os.write(1, data) :]


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: python -m vivibox.index <directory> [<socket name>]", file=sys.stderr)
        return 2
    connect = channel.unix_entry(argv[0], argv[1] if len(argv) == 2 else "index.sock")
    server.serve(lambda n: os.read(0, n), write, Session(connect))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
