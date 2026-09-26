"""What a pod is doing, read from inside it: the ports something listens on, whether the app you
started (v) still runs and what it printed, and what a turn left listening in the agent's own
container. Text and shell scripts only; pod.py runs them.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

DEMO_PID = "/tmp/vivibox-demo.pid"
DEMO_LOG = "/tmp/vivibox-demo.log"
DEMO_ALIVE = f'test -f {DEMO_PID} && kill -0 "$(cat {DEMO_PID})" 2>/dev/null'
DEMO_KILL = f'test -f {DEMO_PID} && kill -TERM -"$(cat {DEMO_PID})" 2>/dev/null; rm -f {DEMO_PID}'


@dataclass(frozen=True)
class Listener:
    """A port something in the pod listens on. Bound to localhost it answers inside the pod only,
    which is the one mistake that makes a running app look like a broken one."""

    port: int
    reachable: bool

    @property
    def why_not(self) -> str:
        return "" if self.reachable else "bound to localhost inside the pod, so nothing outside can reach it"


@dataclass(frozen=True)
class Leftover:
    """A process of the agent's own container that listens on a port: what a turn started in the
    background and never stopped."""

    pid: int
    port: int
    command: str

    def __str__(self) -> str:
        return f"{self.command} (port {self.port})"


# The agent container's listeners by process: the kernel's TCP table gives each listening
# socket's inode and port (in hex); the process holding that socket has it among its fds. The
# container's /proc has its own processes only, so a service in the pod's Docker, which lives in
# the sidecar, is never among them.
# A raw string: tr '\0' in a plain one was a real NUL, which subprocess refuses.
LEFTOVERS = r"""awk '$4=="0A"{print $10, $2}' /proc/net/tcp /proc/net/tcp6 2>/dev/null |
while read inode addr; do
  for fd in /proc/[0-9]*/fd/*; do
    [ "$(readlink "$fd" 2>/dev/null)" = "socket:[$inode]" ] || continue
    pid=${fd#/proc/}; pid=${pid%%/*}
    printf '%s\t%s\t' "$pid" "${addr##*:}"; tr '\0' ' ' < "/proc/$pid/cmdline"; echo
  done
done"""
MAX_COMMAND = 60


# Both questions in one exec: the agent container shares the sidecar's network namespace, so its
# /proc/net/tcp is the pod's, and its /tmp holds what the demo left behind.
PROBE = f"""({DEMO_ALIVE}) && echo RUNNING || echo STOPPED
cat /proc/sys/net/ipv4/ip_local_port_range
awk '$4=="0A"{{print $2}}' /proc/net/tcp /proc/net/tcp6 2>/dev/null
echo --
tail -n 25 {DEMO_LOG} 2>/dev/null"""


@dataclass(frozen=True)
class PodProbe:
    """What the pod is doing, asked once."""

    listening: list[Listener] = field(default_factory=list)
    demo: bool = False
    log: str = ""


def parse_listening(text: str, from_table: bool = False) -> list[Listener]:
    """The kernel's listening sockets. First line is the ephemeral range, whose ports belong to the
    pod's own Docker daemon rather than to anything you started."""
    first, *rest = text.splitlines() or [""]
    low = first.split()[0] if first.split() else ""
    ephemeral = int(low) if low.isdigit() else 32768
    found: dict[int, bool] = {}
    for line in rest:
        fields = line.split()
        # In the raw table 0A is TCP_LISTEN and the address is field 2; awk has already picked it.
        local = fields[1] if from_table and len(fields) > 3 and fields[3] == "0A" else ""
        local = local or (fields[0] if not from_table and fields else "")
        if not local or ":" not in local:
            continue
        address, _, port_hex = local.rpartition(":")
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if port >= ephemeral:
            continue
        # All zeros is 0.0.0.0 or ::; a port bound both ways is reachable, so the widest wins.
        found[port] = found.get(port, False) or set(address) == {"0"}
    return [Listener(port, reachable) for port, reachable in sorted(found.items())]


@dataclass(frozen=True)
class DemoState:
    """What became of the project you started. Nothing here is a health check: a process can be up
    and wedged, and only the app itself knows that. This is what the kernel and the log can say."""

    running: bool
    log: str = ""


def docker_running() -> bool:
    """Whether the host's Docker daemon answers: without it no pod starts, and the view says so
    before you make a task, not from a message gone in seconds after one fails to start."""
    try:
        return (
            subprocess.run(
                ["docker", "info", "-f", "{{.ServerVersion}}"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
