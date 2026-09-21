"""Task pod: a Docker daemon in a Sysbox sidecar and an unprivileged agent container in its network.

The agent reaches the daemon only through a unix socket on a volume shared by the pair, and sees
ports published by compose or Testcontainers on localhost. The sidecar mounts the task repo
read-only: under Sysbox, root in a nested container would otherwise write to it as host root.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_NETWORK_POOL, TASK_NETWORK_BITS, HostService

DIND_IMAGE = "docker:29.8.1-dind"
SOCKET_DIR = "/run/vivibox-docker"
SOCKET = f"{SOCKET_DIR}/docker.sock"
FIREWALL = "/usr/local/libexec/vivibox-netns"
# Shared between tasks (N2): the dependency caches are safe to share, the task's Docker data is not.
CACHES = {
    "m2": "/cache/m2",
    "gradle": "/cache/gradle",
    "npm": "/cache/npm",
    "mise": "/cache/mise",
    "corepack": "/cache/corepack",
}
HOST_GATEWAY = "host.docker.internal"
# Running the project for you to look at: its process group, its output, both inside the pod.
DEMO_PID = "/tmp/vivibox-demo.pid"
DEMO_LOG = "/tmp/vivibox-demo.log"
DEMO_ALIVE = f'test -f {DEMO_PID} && kill -0 "$(cat {DEMO_PID})" 2>/dev/null'
DEMO_KILL = f'test -f {DEMO_PID} && kill -TERM -"$(cat {DEMO_PID})" 2>/dev/null; rm -f {DEMO_PID}'

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class PodError(Exception):
    pass


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


@dataclass(frozen=True)
class Listener:
    """A port something in the pod listens on. Bound to localhost it answers inside the pod only,
    which is the one mistake that makes a running app look like a broken one."""

    port: int
    reachable: bool

    @property
    def why_not(self) -> str:
        return "" if self.reachable else "bound to localhost inside the pod, so nothing outside can reach it"


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
    # Addresses task networks are cut from; see config.Config.network_pool.
    network_pool: str = DEFAULT_NETWORK_POOL
    runner: Runner = run

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
    def network(self) -> str:
        return f"vivibox-{self.task_id}-net"

    @property
    def volumes(self) -> dict[str, str]:
        return {"docker": f"vivibox-{self.task_id}-docker", "socket": f"vivibox-{self.task_id}-socket"}

    def _run(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        p = self.runner(cmd)
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
        daemon = (
            f"rm -f {SOCKET}; dind dockerd --host=unix://{SOCKET} >/var/log/dockerd.log 2>&1 & "
            'pid=$!; trap \'kill -TERM "$pid"; wait "$pid"\' TERM; '
            f"while [ ! -S {SOCKET} ]; do sleep 0.2; done; chmod 666 {SOCKET}; wait"
        )
        # Read-only for Docker in the pod: compose and Testcontainers bind-mount files of the build.
        shared = [Mount(str(d), str(d), read_only=True) for d in (self.repo, self.gate_dir) if d]
        return [
            "docker", "run", "-d", "--name", self.sidecar, "--runtime=sysbox-runc",
            "--label", f"vivibox.task={self.task_id}",
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

    def _outdated_sidecar(self) -> bool:
        p = self._run("docker", "inspect", "-f", "{{json .Config.Cmd}}", self.sidecar, check=False)
        try:
            return p.returncode == 0 and json.loads(p.stdout)[-1] != self.sidecar_command()[-1]
        except (ValueError, IndexError, TypeError):
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

    def gate_exec(self, *cmd: str, check: bool = True, workdir: str = "") -> subprocess.CompletedProcess:
        return self._run("docker", "exec", "-w", workdir or self.gate_src, self.gate, *cmd, check=check)

    def gate_down(self) -> None:
        self._run("docker", "rm", "-f", self.gate, check=False)

    def remove(self) -> None:
        """Removes containers and the task's volumes. Shared caches stay."""
        self._run("docker", "rm", "-f", self.agent, self.sidecar, self.gate, check=False)
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
        # without that, one task could reach another task's ports.
        self._run(
            "sudo", "-n", FIREWALL, "apply", self.sidecar,
            "--pool", self.network_pool, *self.firewall_targets(),
        )  # fmt: skip
