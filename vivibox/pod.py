"""Task pod: a Docker daemon in a Sysbox sidecar and an unprivileged agent container in its network.

The agent reaches the daemon only through a unix socket on a volume shared by the pair, and sees
ports published by compose or Testcontainers on localhost. The sidecar mounts the task repo
read-only: under Sysbox, root in a nested container would otherwise write to it as host root.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import HostService

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

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class PodError(Exception):
    pass


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True)


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
    # Where the gate builds a fresh clone of the committed work; outside anything the agent can write.
    gate_dir: Path | None = None
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

    def sidecar_command(self) -> list[str]:
        # The daemon listens only on the unix socket. 666: the socket is owned by root of the
        # Sysbox user namespace, the agent runs as your UID, and only this pair mounts the volume.
        daemon = (
            f"dind dockerd --host=unix://{SOCKET} >/var/log/dockerd.log 2>&1 & "
            f"while [ ! -S {SOCKET} ]; do sleep 0.2; done; chmod 666 {SOCKET}; wait"
        )
        # Read-only for Docker in the pod: compose and Testcontainers bind-mount files of the build.
        shared = [Mount(str(d), str(d), read_only=True) for d in (self.repo, self.gate_dir) if d]
        return [
            "docker", "run", "-d", "--name", self.sidecar, "--runtime=sysbox-runc",
            "--label", f"vivibox.task={self.task_id}",
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
            *(arg for m in mounts for arg in ("-v", m.arg())),
            "-w", str(self.gate_dir),
            self.image, "sleep", "infinity",
        ]  # fmt: skip

    # --- lifecycle --------------------------------------------------------------------------

    def up(self, timeout: float = 90) -> None:
        """Starts whatever is not running. Idempotent."""
        if not self.repo.is_dir():
            raise PodError(f"task repo {self.repo} does not exist")
        if self._state(self.sidecar) != "running":
            # The agent joins the sidecar's network namespace, which a sidecar restart replaces.
            self._run("docker", "rm", "-f", self.agent, check=False)
            if self._state(self.sidecar) is None:
                self._run(*self.sidecar_command())
            else:
                self._run("docker", "start", self.sidecar)
            self._wait_for_daemon(timeout)
        # Rules live in the sidecar's network namespace and are gone after any restart: always apply,
        # and before the agent exists.
        self.apply_firewall()
        if self._state(self.agent) != "running":
            self._run("docker", "rm", "-f", self.agent, check=False)
            self._run(*self.agent_command())

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
        targets = []
        for s in self.host_services:
            if s.host == HOST_GATEWAY:
                ip = self.gateway()
            else:
                try:
                    ip = socket.getaddrinfo(s.host, s.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
                except OSError as e:
                    raise PodError(f"cannot resolve host service {s.host}: {e}") from None
            targets.append(f"{ip}:{s.port}")
        return targets

    def apply_firewall(self) -> None:
        self._run("sudo", "-n", FIREWALL, "apply", self.sidecar, *self.firewall_targets())
