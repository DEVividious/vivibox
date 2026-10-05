"""A program whose output is read within a limit: of bytes, counted as they come, and of time.

For git on the host where what it lists is as long as an agent made it (repo.run_git with a
limit): a few objects make a listing of gigabytes, and one read whole into memory, or read
after the program ended, is the size the agent chose.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time

# Bytes taken at once from what the program prints: what a pipe holds.
CHUNK = 65536


class TooMuch(Exception):
    """The program printed more than it may and was ended: none of it is an answer. read: the
    bytes taken from it by then."""

    def __init__(self, said: str, read: int) -> None:
        super().__init__(said)
        self.read = read


def run(
    command: list[str], env: dict[str, str], limit: int, timeout: float, **how
) -> subprocess.CompletedProcess:
    """command in a session of its own, what it prints read here as it comes, as bytes, and
    counted, stdout and stderr together. Past limit bytes (TooMuch) or timeout seconds
    (subprocess.TimeoutExpired), whichever comes first, its session is ended: the program
    and every process it started that did not leave the session. Nothing it printed is handed
    on: a listing cut short is no listing. Both pipes are read to their end or closed here, so
    a process that holds one after the program ended is waited on no longer than the time; one
    that left the session is not ended, and not waited on either. All of it is this process's
    doing, on its way out of an interrupt too: where vivibox itself is ended by another signal,
    the program runs to its own end. how: stdin and cwd, as subprocess.Popen takes them."""
    until = time.monotonic() + timeout
    (out, out_w), (err, err_w) = os.pipe(), os.pipe()
    read: dict[int, list[bytes]] = {out: [], err: []}
    waiting, total = selectors.DefaultSelector(), 0
    try:
        try:
            p = subprocess.Popen(command, env=env, stdout=out_w, stderr=err_w, start_new_session=True, **how)
        finally:
            os.close(out_w)
            os.close(err_w)
        try:
            for pipe in read:
                waiting.register(pipe, selectors.EVENT_READ)
            while waiting.get_map():
                if (left := until - time.monotonic()) <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in waiting.select(left):
                    if not (chunk := os.read(key.fd, CHUNK)):
                        waiting.unregister(key.fd)
                    elif (total := total + len(chunk)) > limit:
                        raise TooMuch(f"{command[0]} printed more than {limit} bytes", total)
                    else:
                        read[key.fd].append(chunk)
            p.wait(timeout=max(until - time.monotonic(), 0))
        except BaseException:  # past the limit or the time, or an interrupt: nothing is left behind
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            raise
    finally:
        waiting.close()
        os.close(out)
        os.close(err)
    return subprocess.CompletedProcess(command, p.returncode, b"".join(read[out]), b"".join(read[err]))
