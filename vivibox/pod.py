"""Task pod: a Docker daemon in a Sysbox sidecar and an unprivileged agent container in its network.

The agent reaches the daemon only through a unix socket on a volume shared by the pair, and sees
ports published by compose or Testcontainers on localhost. The sidecar mounts the task repo
read-only: under Sysbox, root in a nested container would otherwise write to it as host root.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_NETWORK_POOL, TASK_NETWORK_BITS, HostService

DIND_IMAGE = "docker:29.8.1-dind"
SOCKET_DIR = "/run/vivibox-docker"
SOCKET = f"{SOCKET_DIR}/docker.sock"
FIREWALL = "/usr/local/libexec/vivibox-netns"
ETHERNET_MTU = 1500
# Shared between tasks (N2): the dependency caches are safe to share, the task's Docker data is not.
CACHES = {
    "m2": "/cache/m2",
    "gradle": "/cache/gradle",
    "npm": "/cache/npm",
    "mise": "/cache/mise",
    "corepack": "/cache/corepack",
    # Maven's build cache extension puts its cache beside the local repository, /cache/m2.
    "build-cache": "/cache/build-cache",
}
HOST_GATEWAY = "host.docker.internal"
# The host's trust store, shared with the pod: a registry or a proxy the host trusts through an
# authority of its own (corporate TLS inspection) is trusted in the pod the same way. The daemon
# (Go), OpenSSL and the system's tools read this file; tools with a store of their own do not.
HOST_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
# The sidecar carries the digest of the bundle it started with: its daemon reads the authorities
# once, at its start, so a bundle changed since (a VPN client installing a new one) is not seen
# until the sidecar is made again.
CA_LABEL = "vivibox.ca"
# Running the project for you to look at: its process group, its output, both inside the pod.
DEMO_PID = "/tmp/vivibox-demo.pid"
DEMO_LOG = "/tmp/vivibox-demo.log"
DEMO_ALIVE = f'test -f {DEMO_PID} && kill -0 "$(cat {DEMO_PID})" 2>/dev/null'
DEMO_KILL = f'test -f {DEMO_PID} && kill -TERM -"$(cat {DEMO_PID})" 2>/dev/null; rm -f {DEMO_PID}'

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class PodError(Exception):
    pass


def run(cmd: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """timeout: seconds, after which subprocess.TimeoutExpired is raised with the output so far."""
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


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


def addresses(sidecars: Sequence[str], runner: Runner = run) -> dict[str, str]:
    """Where each pod answers, for many pods at once: asking one at a time is what makes a list slow."""
    if not sidecars:
        return {}
    template = "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    found = runner(["docker", "inspect", "-f", template, *sidecars])
    out = {}
    for line in found.stdout.splitlines():
        name, _, address = line.strip().partition(" ")
        if address:
            out[name.lstrip("/")] = address
    return out


@dataclass(frozen=True)
class DemoState:
    """What became of the project you started. Nothing here is a health check: a process can be up
    and wedged, and only the app itself knows that. This is what the kernel and the log can say."""

    running: bool
    log: str = ""


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = False

    def arg(self) -> str:
        return f"{self.source}:{self.target}" + (":ro" if self.read_only else "")


LINK = re.compile(r"^\d+: ([^:@]+)(?:@\S+)?: <([^>]*)> mtu (\d+) .*?\blink/(\w+)")


def uplink_mtu(runner: Runner = run) -> int:
    """The MTU the host's traffic to the internet is bound by. A VPN tunnel has less than
    Ethernet's 1500 (Cloudflare WARP: 1280), and a pod, whose own interfaces have 1500, cannot
    tell: its large downloads through the tunnel stall. The route's interface counts, and so
    does every tunnel that is up: WARP steers traffic through rules and a table of its own, and
    `ip route get` still names the Wi-Fi. 1500 when there is no way to know."""
    links = {}
    p = runner(["ip", "-o", "link"])
    for line in p.stdout.splitlines() if p.returncode == 0 else []:
        if m := LINK.match(line):
            links[m.group(1)] = (set(m.group(2).split(",")), int(m.group(3)), m.group(4))
    mtus = [
        mtu for flags, mtu, kind in links.values() if kind in ("none", "ppp") and {"UP", "LOWER_UP"} <= flags
    ]
    p = runner(["ip", "-o", "route", "get", "1.1.1.1"])
    words = p.stdout.split()
    if p.returncode == 0 and "dev" in words[:-1] and words[words.index("dev") + 1] in links:
        mtus.append(links[words[words.index("dev") + 1]][1])
    return min(mtus, default=ETHERNET_MTU)


def binds(command: list[str]) -> list[str]:
    """The -v arguments of a docker run command, as Docker keeps them in HostConfig.Binds."""
    return [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]


def ca_mounts() -> list[Mount]:
    """The host's CA bundle over the container's own, when the host has one."""
    return [Mount(str(HOST_CA_BUNDLE), CA_BUNDLE, read_only=True)] if HOST_CA_BUNDLE.exists() else []


def ca_digest() -> str:
    """What the host's CA bundle holds, in a word; "" when the host has none."""
    try:
        return hashlib.sha256(HOST_CA_BUNDLE.read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclass
class Pod:
    task_id: str
    repo: Path
    image: str
    host_services: list[HostService] = field(default_factory=list)
    # Files the agent may read or write outside the repo (plan, handoff), and repo parts it must
    # not change (.git config and hooks); added by the layers that own them.
    agent_mounts: list[Mount] = field(default_factory=list)
    agent_env: dict[str, str] = field(default_factory=dict)
    # Names of variables passed from vivibox's own environment (Project.pass_env). Given to docker
    # by name only, so their values are never on a command line.
    passed_env: list[str] = field(default_factory=list)
    # Where the gate builds a fresh clone of the committed work; outside anything the agent can write.
    gate_dir: Path | None = None
    # Where the reviewer reads its own fresh clone, with its harness files and its review beside it.
    review_dir: Path | None = None
    # Addresses task networks are cut from; see config.Config.network_pool.
    network_pool: str = DEFAULT_NETWORK_POOL
    runner: Runner = run
    # What gate_stream starts a command with; a Popen-like whose stdout can be read line by line.
    popen: Callable[..., subprocess.Popen] = subprocess.Popen

    @property
    def sidecar(self) -> str:
        return f"vivibox-{self.task_id}-dind"

    @property
    def agent(self) -> str:
        return f"vivibox-{self.task_id}-agent"

    @property
    def gate(self) -> str:
        return f"vivibox-{self.task_id}-gate"

    @property
    def gate_src(self) -> str:
        return f"{self.gate_dir}/src"

    @property
    def review(self) -> str:
        return f"vivibox-{self.task_id}-review"

    @property
    def review_src(self) -> str:
        return f"{self.review_dir}/src"

    @property
    def network(self) -> str:
        return f"vivibox-{self.task_id}-net"

    @property
    def volumes(self) -> dict[str, str]:
        return {"docker": f"vivibox-{self.task_id}-docker", "socket": f"vivibox-{self.task_id}-socket"}

    def _run(
        self, *cmd: str, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        # The keyword only when there is one: runners that know no time limit (tests) keep working.
        p = self.runner(cmd, timeout=timeout) if timeout else self.runner(cmd)
        if check and p.returncode != 0:
            raise PodError(f"{' '.join(cmd[:4])}…: {(p.stderr or p.stdout).strip()}")
        return p

    def _state(self, name: str) -> str | None:
        p = self._run("docker", "inspect", "-f", "{{.State.Status}}", name, check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    # --- commands ---------------------------------------------------------------------------

    def taken_subnets(self) -> list[ipaddress.IPv4Network]:
        """Every subnet Docker has handed out, here or to anything else on this machine."""
        ids = self._run("docker", "network", "ls", "-q").stdout.split()
        if not ids:
            return []
        listed = self._run(
            "docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids
        ).stdout
        taken = []
        for word in listed.split():
            try:
                taken.append(ipaddress.IPv4Network(word, strict=False))
            except ValueError:
                continue  # an IPv6 subnet, or anything else that is not one of ours to avoid
        return taken

    def free_subnet(self) -> ipaddress.IPv4Network:
        pool = ipaddress.IPv4Network(self.network_pool)
        taken = self.taken_subnets()
        for candidate in pool.subnets(new_prefix=TASK_NETWORK_BITS):
            if not any(candidate.overlaps(other) for other in taken):
                return candidate
        raise PodError(
            f"no free address range left in {pool}: every /{TASK_NETWORK_BITS} is in use. "
            "Remove tasks you have finished with, or widen network.pool in config.toml"
        )

    def ensure_network(self) -> ipaddress.IPv4Network:
        """The task's own network. Its address is then its own, so its ports are nobody else's."""
        found = self._run(
            "docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}}{{end}}",
            self.network, check=False,
        )  # fmt: skip
        if found.returncode == 0 and found.stdout.strip():
            return ipaddress.IPv4Network(found.stdout.strip())
        subnet = self.free_subnet()
        self._run("docker", "network", "create", "--subnet", str(subnet), self.network)
        return subnet

    def address(self) -> str:
        """Where the pod answers, from the host and from inside itself alike. Empty when it is down."""
        template = f'{{{{(index .NetworkSettings.Networks "{self.network}").IPAddress}}}}'
        found = self._run("docker", "inspect", "-f", template, self.sidecar, check=False)
        return found.stdout.strip() if found.returncode == 0 else ""

    def listening(self) -> list[Listener]:
        """What is listening in the pod, read from the kernel so no tool has to be in the image.
        Ephemeral ports are left out: they belong to the pod's own Docker daemon."""
        found = self._run(
            "docker", "exec", self.sidecar, "sh", "-c",
            "cat /proc/sys/net/ipv4/ip_local_port_range; cat /proc/net/tcp /proc/net/tcp6 2>/dev/null",
            check=False,
        )  # fmt: skip
        if found.returncode != 0:
            return []
        return parse_listening(found.stdout, from_table=True)

    def demo_running(self) -> bool:
        return self.demo_state().running

    def probe(self) -> PodProbe:
        """One question of the pod instead of two, so a list of tasks stays cheap to keep current."""
        found = self._run("docker", "exec", self.agent, "sh", "-c", PROBE, check=False)
        if found.returncode != 0:
            return PodProbe()
        head, _, log = found.stdout.partition("\n--\n")
        state, _, rows = head.partition("\n")
        return PodProbe(parse_listening(rows), state.strip() == "RUNNING", log.strip())

    def demo_state(self) -> DemoState:
        """Whether what you started is still alive, and its last output, in one question: a crash
        is only useful if it comes with the reason, and the reason is in the log."""
        script = f"({DEMO_ALIVE}) && echo RUNNING || echo STOPPED; tail -n 25 {DEMO_LOG} 2>/dev/null"
        found = self._run("docker", "exec", self.agent, "sh", "-c", script, check=False)
        if found.returncode != 0:
            return DemoState(False, "")
        first, _, rest = found.stdout.partition("\n")
        return DemoState(first.strip() == "RUNNING", rest.strip())

    def leftovers(self) -> list[Leftover]:
        """What the agent's container has listening, one entry per process. opencode's own server
        is left out: it is the agent, behind the window you watch it in."""
        found = self._run("docker", "exec", self.agent, "sh", "-c", LEFTOVERS, check=False)
        if found.returncode != 0:
            return []
        seen: dict[int, Leftover] = {}
        for line in found.stdout.splitlines():
            pid, _, rest = line.partition("\t")
            port, _, command = rest.partition("\t")
            try:
                pid, port = int(pid), int(port, 16)
            except ValueError:
                continue
            command = " ".join(command.split())
            if "opencode serve" in command or pid in seen:
                continue
            seen[pid] = Leftover(pid, port, command[:MAX_COMMAND])
        return list(seen.values())

    def stop_leftovers(self) -> list[str]:
        """Stops what the agent left listening in its container and says what went. The app is
        run once the agent rests, so a server still up in there is a leftover of its turn, and
        one that holds the port the app wants makes the app die on it."""
        left = self.leftovers()
        for process in left:
            self._run(
                "docker", "exec", self.agent, "sh", "-c", f"kill -TERM {process.pid} 2>/dev/null", check=False
            )
        return [str(process) for process in left]

    def demo_stop(self) -> None:
        """Kills the whole process group: a build tool starting a server leaves children behind."""
        self._run("docker", "exec", self.agent, "sh", "-c", DEMO_KILL, check=False)

    def demo_log(self, lines: int = 20) -> str:
        found = self._run("docker", "exec", self.agent, "tail", "-n", str(lines), DEMO_LOG, check=False)
        return found.stdout.strip() if found.returncode == 0 else ""

    def demo_start(self, commands: Sequence[str], workdir: str = "", wait: float = 40) -> list[Listener]:
        """Runs the project the way the project is run, in the agent's container, and waits for it
        to listen. Returns the ports that were not there before; empty means nothing came up.

        setsid: the commands outlive the exec that started them, so the app stays up while you look
        at it, and its whole process group can be stopped later in one go.
        """
        self.demo_stop()
        # A port the last run held is not free the moment its process is killed, and starting again
        # on the same port would then look like nothing came up. Wait for the picture to settle.
        before = {listener.port for listener in self.listening()}
        settle = time.monotonic() + 10
        while time.monotonic() < settle:
            time.sleep(0.5)
            now_ports = {listener.port for listener in self.listening()}
            if now_ports == before:
                break
            before = now_ports
        script = " && ".join(commands)
        started = f"setsid sh -c {shlex.quote(script)} > {DEMO_LOG} 2>&1 < /dev/null & echo $! > {DEMO_PID}"
        self._run("docker", "exec", "-d", "-w", workdir or str(self.repo), self.agent, "sh", "-c", started)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            fresh = [listener for listener in self.listening() if listener.port not in before]
            if fresh:
                return fresh
            if not self.demo_running():
                return []  # it exited; the log says why
            time.sleep(0.5)
        return []

    def sidecar_command(self, address: str = "") -> list[str]:
        # The daemon listens only on the unix socket. 666: the socket is owned by root of the
        # Sysbox user namespace, the agent runs as your UID, and only this pair mounts the volume.
        # The socket left by the last run goes first: the wait would find it at once, chmod it, and
        # the daemon would then replace it with one the agent cannot use ("permission denied").
        # The trap passes docker stop on to the daemon, which then shuts down cleanly.
        # Docker 29 refuses API versions below 1.40, and Testcontainers before 1.21 speaks 1.32: it
        # would find no Docker at all while `docker version` works. 1.24 is the oldest it still knows.
        daemon = (
            f"rm -f {SOCKET}; DOCKER_MIN_API_VERSION=1.24 dind dockerd --host=unix://{SOCKET} "
            ">/var/log/dockerd.log 2>&1 & "
            'pid=$!; trap \'kill -TERM "$pid"; wait "$pid"\' TERM; '
            f"while [ ! -S {SOCKET} ]; do sleep 0.2; done; chmod 666 {SOCKET}; wait"
        )
        # Read-only for Docker in the pod: compose and Testcontainers bind-mount files of the build.
        shared = [Mount(str(d), str(d), read_only=True) for d in (self.repo, self.gate_dir) if d]
        shared += ca_mounts()
        digest = ca_digest()
        return [
            "docker", "run", "-d", "--name", self.sidecar, "--runtime=sysbox-runc",
            "--label", f"vivibox.task={self.task_id}",
            *(("--label", f"{CA_LABEL}={digest}") if digest else ()),
            "--network", self.network, *(("--ip", address) if address else ()),
            "--add-host", f"{HOST_GATEWAY}:host-gateway",
            "-e", "DOCKER_TLS_CERTDIR=",
            "-v", f"{self.volumes['docker']}:/var/lib/docker",
            "-v", f"{self.volumes['socket']}:{SOCKET_DIR}",
            *(arg for m in shared for arg in ("-v", m.arg())),
            "--entrypoint", "sh", DIND_IMAGE, "-c", daemon,
        ]  # fmt: skip

    def agent_command(self) -> list[str]:
        mounts = [
            Mount(self.volumes["socket"], SOCKET_DIR),
            Mount(str(self.repo), str(self.repo)),
            Mount(f"vivibox-{self.task_id}-config", "/config"),
            *(Mount(f"vivibox-cache-{name}", path) for name, path in CACHES.items()),
            *ca_mounts(),
            *self.agent_mounts,
        ]
        return [
            "docker", "run", "-d", "--name", self.agent,
            "--label", f"vivibox.task={self.task_id}",
            "--network", f"container:{self.sidecar}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:exec,mode=1777",
            "-e", f"DOCKER_HOST=unix://{SOCKET}",
            "-e", f"TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE={SOCKET}",
            "-e", "TESTCONTAINERS_HOST_OVERRIDE=localhost",
            # Hook installers (husky in npm "prepare") would try to change the read-only .git/config.
            "-e", "HUSKY=0",
            *(arg for k, v in self.agent_env.items() for arg in ("-e", f"{k}={v}")),
            *(arg for name in self.passed_env for arg in ("-e", name)),
            *(arg for m in mounts for arg in ("-v", m.arg())),
            "-w", str(self.repo),
            self.image, "sleep", "infinity",
        ]  # fmt: skip

    def gate_command(self) -> list[str]:
        """Like the agent container, but without anything the agent could have left behind: its
        working tree and build outputs, its home directory, its background processes."""
        mounts = [
            Mount(self.volumes["socket"], SOCKET_DIR),
            Mount(str(self.repo), str(self.repo), read_only=True),
            Mount(str(self.gate_dir), str(self.gate_dir)),
            *(Mount(f"vivibox-cache-{name}", path) for name, path in CACHES.items()),
            *ca_mounts(),
        ]
        return [
            "docker", "run", "-d", "--name", self.gate,
            "--label", f"vivibox.task={self.task_id}",
            "--network", f"container:{self.sidecar}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:exec,mode=1777", "--tmpfs", "/config:exec,mode=1777",
            "-e", f"DOCKER_HOST=unix://{SOCKET}",
            "-e", f"TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE={SOCKET}",
            "-e", "TESTCONTAINERS_HOST_OVERRIDE=localhost",
            "-e", "HUSKY=0",
            *(arg for k, v in self.agent_env.items() for arg in ("-e", f"{k}={v}")),
            *(arg for name in self.passed_env for arg in ("-e", name)),
            *(arg for m in mounts for arg in ("-v", m.arg())),
            "-w", str(self.gate_dir),
            self.image, "sleep", "infinity",
        ]  # fmt: skip

    def review_command(self, mounts: list[Mount], env: dict[str, str]) -> list[str]:
        """The reviewer's container: a fresh clone like the gate's, with the writer's tree read-only
        and without the build's means, since it reads and runs nothing: no Docker socket, no build
        caches, no variables of the build. Its own secrets and harness files come as mounts."""
        all_mounts = [
            Mount(str(self.repo), str(self.repo), read_only=True),
            Mount(str(self.review_dir), str(self.review_dir)),
            *ca_mounts(),
            *mounts,
        ]
        return [
            "docker", "run", "-d", "--name", self.review,
            "--label", f"vivibox.task={self.task_id}",
            "--network", f"container:{self.sidecar}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:exec,mode=1777", "--tmpfs", "/config:exec,mode=1777",
            *(arg for k, v in env.items() for arg in ("-e", f"{k}={v}")),
            *(arg for m in all_mounts for arg in ("-v", m.arg())),
            "-w", self.review_src,
            self.image, "sleep", "infinity",
        ]  # fmt: skip

    def passed_values(self) -> list[str]:
        """The values of the passed variables, for keeping them out of logs."""
        return [v for name in self.passed_env if len(v := os.environ.get(name, "")) >= 4]

    def missing_env(self) -> list[str]:
        return [name for name in self.passed_env if not os.environ.get(name)]

    # --- lifecycle --------------------------------------------------------------------------

    def up(self, timeout: float = 90) -> None:
        """Starts whatever is not running. Idempotent."""
        if not self.repo.is_dir():
            raise PodError(f"task repo {self.repo} does not exist")
        if self._state(self.sidecar) == "running" and self._stale_ca():
            # Its daemon read the host's authorities at its start and would go on failing TLS
            # against a new one ("x509: certificate signed by unknown authority"); up() runs
            # before any turn, so this is the moment to make it again.
            self._run("docker", "rm", "-f", self.agent, check=False)
            self._run("docker", "rm", "-f", self.sidecar, check=False)
        if self._state(self.sidecar) != "running":
            # The agent joins the sidecar's network namespace, which a sidecar restart replaces.
            self._run("docker", "rm", "-f", self.agent, check=False)
            if self._state(self.sidecar) is not None and self._outdated_sidecar():
                # docker start would run the script it was made with; its data is in volumes.
                self._run("docker", "rm", "-f", self.sidecar, check=False)
            if self._state(self.sidecar) is None:
                # Pinned rather than left to Docker: this address goes into your configuration files,
                # so it has to be the same one after every restart.
                subnet = self.ensure_network()
                self._run(*self.sidecar_command(str(subnet[2])))
            else:
                self._run("docker", "start", self.sidecar)
            self._wait_for_daemon(timeout)
        # Rules live in the sidecar's network namespace and are gone after any restart: always apply,
        # and before the agent exists.
        self.apply_firewall()
        if self._state(self.agent) != "running":
            self._run("docker", "rm", "-f", self.agent, check=False)
            self._run(*self.agent_command())

    def _made_with(self) -> dict | None:
        """The sidecar's script, mounts and labels as it was made; None when it cannot be asked."""
        template = (
            '{"cmd":{{json .Config.Cmd}},"binds":{{json .HostConfig.Binds}},"labels":{{json .Config.Labels}}}'
        )
        p = self._run("docker", "inspect", "-f", template, self.sidecar, check=False)
        try:
            return json.loads(p.stdout) if p.returncode == 0 else None
        except ValueError:
            return None

    def _stale_ca(self) -> bool:
        """Whether the host's CA bundle changed since the sidecar was made."""
        made = self._made_with()
        return made is not None and (made.get("labels") or {}).get(CA_LABEL, "") != ca_digest()

    def _outdated_sidecar(self) -> bool:
        """Whether the sidecar was made with another script, other mounts or another CA bundle
        than this vivibox would give it: docker start keeps all three, so a change would never
        arrive."""
        made = self._made_with()
        wanted = self.sidecar_command()
        try:
            return made is not None and (
                made["cmd"][-1] != wanted[-1]
                or set(made["binds"] or []) != set(binds(wanted))
                or (made.get("labels") or {}).get(CA_LABEL, "") != ca_digest()
            )
        except (KeyError, IndexError, TypeError):
            return True

    def _wait_for_daemon(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sidecar_docker("info", check=False).returncode == 0:
                return
            time.sleep(0.5)
        log = self._run("docker", "exec", self.sidecar, "tail", "-20", "/var/log/dockerd.log", check=False)
        raise PodError(f"Docker in {self.sidecar} did not start:\n{log.stdout}")

    def down(self) -> None:
        """Stops the pod and keeps its data."""
        for name in (self.agent, self.sidecar):
            self._run("docker", "stop", "-t", "10", name, check=False)

    def kill(self) -> None:
        """Kills the pod's containers at once, keeping its data: for a pod that ignores a stop."""
        self._run("docker", "kill", self.agent, self.sidecar, check=False)

    def gate_up(self) -> None:
        """A fresh container with a fresh clone of the task branch: only committed work is verified."""
        if self.gate_dir is None:
            raise PodError("the pod has no gate directory")
        self.gate_down()
        shutil.rmtree(self.gate_dir, ignore_errors=True)
        self.gate_dir.mkdir(parents=True)
        self._run(*self.gate_command())
        # Gradle runs init scripts and reads JVM options from its home, a cache the agent can write.
        self.gate_exec("rm", "-rf", f"{CACHES['gradle']}/init.d", f"{CACHES['gradle']}/gradle.properties",
                       workdir=str(self.gate_dir))  # fmt: skip
        self.gate_exec("git", "clone", "--quiet", "--no-hardlinks", str(self.repo), self.gate_src,
                       workdir=str(self.gate_dir))  # fmt: skip

    def gate_exec(
        self, *cmd: str, check: bool = True, workdir: str = "", timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        """timeout: raises subprocess.TimeoutExpired; the command inside goes down with the container."""
        return self._run(
            "docker", "exec", "-w", workdir or self.gate_src, self.gate, *cmd, check=check, timeout=timeout
        )

    def gate_stream(
        self, *cmd: str, sink: Callable[[str], None], workdir: str = "", timeout: float | None = None
    ) -> int:
        """gate_exec with the output handed to sink line by line as the command writes it, errors
        among the rest in their order, so a log written from it moves while the command runs.
        Returns the exit code. timeout: raises subprocess.TimeoutExpired; the command inside goes
        down with the container."""
        p = self.popen(
            ["docker", "exec", "-w", workdir or self.gate_src, self.gate, *cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        # Read on a thread of its own: a command silent past its time limit would otherwise hold
        # the reader, and a waiter that did not read would let a full pipe hold the command.
        reader = threading.Thread(target=lambda: [sink(line) for line in p.stdout], daemon=True)
        reader.start()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            reader.join()
            raise
        reader.join()
        return p.returncode

    def gate_down(self) -> None:
        self._run("docker", "rm", "-f", self.gate, check=False)

    def review_fresh(self) -> None:
        """An empty review directory with the clone's place in it, before the caller puts the
        reviewer's files beside it; the container from the last round, if any, goes first."""
        if self.review_dir is None:
            raise PodError("the pod has no review directory")
        self.review_down()
        shutil.rmtree(self.review_dir, ignore_errors=True)
        (self.review_dir / "src").mkdir(parents=True)

    def review_start(self, mounts: list[Mount], env: dict[str, str]) -> None:
        """The reviewer's container, with a fresh clone of the task branch in it."""
        self._run(*self.review_command(mounts, env))
        self._run("docker", "exec", "-w", str(self.review_dir), self.review, "git", "clone", "--quiet",
                  "--no-hardlinks", str(self.repo), self.review_src)  # fmt: skip

    def review_down(self) -> None:
        self._run("docker", "rm", "-f", self.review, check=False)

    def review_side(self) -> ContainerSide:
        """The review container as a harness talks to it."""
        return ContainerSide(self, self.review)

    def remove(self) -> None:
        """Removes containers and the task's volumes. Shared caches stay."""
        self._run("docker", "rm", "-f", self.agent, self.sidecar, self.gate, self.review, check=False)
        self._run(
            "docker", "volume", "rm", *self.volumes.values(), f"vivibox-{self.task_id}-config", check=False
        )
        self._run("docker", "network", "rm", self.network, check=False)

    # --- inside the pod ---------------------------------------------------------------------

    def sidecar_docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Runs the docker client against the task's daemon, from inside the sidecar."""
        return self._run(
            "docker", "exec", "-e", f"DOCKER_HOST=unix://{SOCKET}", self.sidecar, "docker", *args, check=check
        )

    def exec(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run("docker", "exec", self.agent, *cmd, check=check)

    def exec_detached(self, *cmd: str) -> None:
        self._run("docker", "exec", "-d", self.agent, *cmd)

    def stream(self, *cmd: str) -> subprocess.Popen:
        """A command in the agent container whose output is read as it comes, errors in the same
        stream; the caller reads stdout to the end and waits."""
        return self.popen(
            ["docker", "exec", self.agent, *cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    # --- network ----------------------------------------------------------------------------

    def gateway(self) -> str:
        hosts = self._run("docker", "exec", self.sidecar, "cat", "/etc/hosts").stdout
        for line in hosts.splitlines():
            parts = line.split()
            if len(parts) >= 2 and HOST_GATEWAY in parts[1:]:
                return parts[0]
        raise PodError(f"{HOST_GATEWAY} missing in {self.sidecar}")

    def firewall_targets(self) -> list[str]:
        """Every address a name answers with, not just the first: a service behind a load balancer
        or round-robin DNS reaches you on several, and allowing one of them fails at random."""
        targets: list[str] = []
        for s in self.host_services:
            if s.host == HOST_GATEWAY:
                found = [self.gateway()]
            else:
                try:
                    where = socket.getaddrinfo(s.host, s.port, socket.AF_INET, socket.SOCK_STREAM)
                except OSError as e:
                    raise PodError(f"cannot resolve host service {s.host}: {e}") from None
                found = sorted({r[4][0] for r in where})
            targets += [f"{ip}:{s.port}" for ip in found if f"{ip}:{s.port}" not in targets]
        return targets

    def apply_firewall(self) -> None:
        # The pool is not a private range, so the helper has to be told to reject it by name:
        # without that, one task could reach another task's ports. Applied on every start, so the
        # MSS clamp follows the uplink of the moment: a VPN on or off since the last start.
        mtu = uplink_mtu(self.runner)
        self._run(
            "sudo", "-n", FIREWALL, "apply", self.sidecar,
            "--pool", self.network_pool, *(("--mtu", str(mtu)) if mtu < ETHERNET_MTU else ()),
            *self.firewall_targets(),
        )  # fmt: skip


@dataclass
class ContainerSide:
    """One container of the pod, as a harness talks to it: the same calls as on the agent
    container, aimed at another one, under the name the harness knows the container by."""

    pod: Pod
    agent: str

    @property
    def task_id(self) -> str:
        return self.pod.task_id

    def exec(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        return self.pod._run("docker", "exec", self.agent, *cmd, check=check)

    def exec_detached(self, *cmd: str) -> None:
        self.pod._run("docker", "exec", "-d", self.agent, *cmd)

    def stream(self, *cmd: str) -> subprocess.Popen:
        return self.pod.popen(
            ["docker", "exec", self.agent, *cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
