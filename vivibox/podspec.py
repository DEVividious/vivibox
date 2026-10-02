"""What a pod's containers are made of: the images, the mounts, the caches, and the commands that
start the sidecar, the agent, the gate and the reviewer. Plain functions of the pod they are for;
the host's CA bundle comes in as an argument, read by pod.py. `run` and `Runner`, which run a process
on the host, are here because pod_egress and pod both need them and pod imports pod_egress."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pod import Pod

DIND_IMAGE = "docker:29.8.1-dind"
# The Ryuk Testcontainers runs in the pod, over the one a project's library would pick. Ryuk before
# 0.10 prunes images when it cleans up after a test, and the daemon in the sidecar (Docker 29,
# containerd image store) then loses the image another test is still pulling: "failed to extract
# layer", at random, on the work laptop's case (moby/moby#53321). This one removes what it made, by ID.
RYUK_IMAGE = "testcontainers/ryuk:0.12.0"
SOCKET_DIR = "/run/vivibox-docker"
SOCKET = f"{SOCKET_DIR}/docker.sock"
# Shared between tasks (N2): the dependency caches are safe to share, the task's Docker data is not.
CACHES = {
    "m2": "/cache/m2",
    "gradle": "/cache/gradle",
    "npm": "/cache/npm",
    "yarn": "/cache/yarn",
    "mise": "/cache/mise",
    "corepack": "/cache/corepack",
    "uv": "/cache/uv",
    "go": "/cache/go",
    "cargo": "/cache/cargo",
    "rustup": "/cache/rustup",
    # Maven's build cache extension puts its cache beside the local repository, /cache/m2.
    "build-cache": "/cache/build-cache",
}
# Where Maven's split local repository puts what a build installs: a volume per task, over the
# shared /cache/m2, so one task's unfinished modules never reach another's build.
INSTALLED = "/cache/m2/installed"
# The agent's Maven: the image's options, and no build cache. A hit there restores a module up to
# the phase an earlier command reached (the agent builds with `test`, `package`, `-DskipTests` by
# turns) and runs only the later phases, without `initialize`: what that phase sets for the tests
# (Mockito's agent path from dependency:properties, jacoco's argLine) is missing, and the forked
# JVM dies. The gate keeps its cache: it holds whole verifications only (ADR-0023). Every Maven a
# wrapper may fetch reads MAVEN_OPTS, and the extension reads system properties.
AGENT_MAVEN_OPTS = "-Dmaven.repo.local=/cache/m2 -Dmaven.build.cache.enabled=false"
# The most a scratch tmpfs (/tmp, and the gate's and reviewer's fresh home) may take of the host's
# memory. Without a size it grows to half of it: an agent that cloned the project twice into /tmp
# to try an install held 5 GB. Real work lives in the clone and the caches, which are on disk.
TMP_SIZE = "4g"
HOST_GATEWAY = "host.docker.internal"
# The sidecar carries the digest of the bundle it started with: its daemon reads the authorities
# once, at its start, so a bundle changed since (a VPN client installing a new one) is not seen
# until the sidecar is made again.
CA_LABEL = "vivibox.ca"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def run(cmd: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """timeout: seconds, after which subprocess.TimeoutExpired is raised with the output so far."""
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = False
    # The host's path of the target, for a file mounted at a place the agent writes: made a plain
    # empty file before a container is made with it (pod.Pod._placeholders), never followed.
    placeholder: str = ""

    def arg(self) -> str:
        return f"{self.source}:{self.target}" + (":ro" if self.read_only else "")


def binds(command: list[str]) -> list[str]:
    """The -v arguments of a docker run command, as Docker keeps them in HostConfig.Binds."""
    return [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]


def sidecar_command(pod: Pod, ca_mounts: list[Mount], ca_digest: str, address: str = "") -> list[str]:
    # The daemon listens only on the unix socket. 666: the socket is owned by root of the
    # Sysbox user namespace, the agent runs as your UID, and only this pair mounts the volume.
    # The socket left by the last run goes first: the wait would find it at once, chmod it, and
    # the daemon would then replace it with one the agent cannot use ("permission denied").
    # The trap passes docker stop on to the daemon, which then shuts down cleanly.
    # Docker 29 refuses API versions below 1.40, and Testcontainers before 1.21 speaks 1.32: it
    # would find no Docker at all while `docker version` works. 1.24 is the oldest it still knows.
    mirror = f"--registry-mirror={pod.hub_mirror} " if pod.hub_mirror else ""
    daemon = (
        f"rm -f {SOCKET}; DOCKER_MIN_API_VERSION=1.24 dind dockerd --host=unix://{SOCKET} "
        f"{mirror}>/var/log/dockerd.log 2>&1 & "
        'pid=$!; trap \'kill -TERM "$pid"; wait "$pid"\' TERM; '
        f"while [ ! -S {SOCKET} ]; do sleep 0.2; done; chmod 666 {SOCKET}; wait"
    )
    # Read-only for Docker in the pod: compose and Testcontainers bind-mount files of the build.
    shared = [Mount(str(d), str(d), read_only=True) for d in (pod.repo, pod.gate_dir) if d]
    shared += ca_mounts
    digest = ca_digest
    return [
        "docker", "run", "-d", "--name", pod.sidecar, "--runtime=sysbox-runc",
        "--label", f"vivibox.task={pod.task_id}",
        *(("--label", f"{CA_LABEL}={digest}") if digest else ()),
        "--network", pod.network, *(("--ip", address) if address else ()),
        "--add-host", f"{HOST_GATEWAY}:host-gateway",
        "-e", "DOCKER_TLS_CERTDIR=",
        "-v", f"{pod.volumes['docker']}:/var/lib/docker",
        "-v", f"{pod.volumes['socket']}:{SOCKET_DIR}",
        *(arg for m in shared for arg in ("-v", m.arg())),
        "--entrypoint", "sh", DIND_IMAGE, "-c", daemon,
    ]  # fmt: skip


def docker_env(pod: Pod) -> list[str]:
    """How the agent and the gate reach the sidecar's daemon, and what Testcontainers runs on
    it. A project that passes a variable of these itself (`pass_env`) keeps its own value."""
    fixed = {
        "DOCKER_HOST": f"unix://{SOCKET}",
        "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE": SOCKET,
        "TESTCONTAINERS_HOST_OVERRIDE": "localhost",
        "TESTCONTAINERS_RYUK_CONTAINER_IMAGE": RYUK_IMAGE,
    }
    return [arg for k, v in fixed.items() if k not in pod.passed_env for arg in ("-e", f"{k}={v}")]


def agent_command(pod: Pod, ca_mounts: list[Mount]) -> list[str]:
    mounts = [
        Mount(pod.volumes["socket"], SOCKET_DIR),
        Mount(str(pod.repo), str(pod.repo)),
        Mount(f"vivibox-{pod.task_id}-config", "/config"),
        *(Mount(f"vivibox-cache-{name}", path) for name, path in CACHES.items()),
        Mount(pod.volumes["installed"], INSTALLED),
        *ca_mounts,
        *pod.agent_mounts,
    ]
    return [
        "docker", "run", "-d", "--name", pod.agent,
        "--label", f"vivibox.task={pod.task_id}",
        "--network", f"container:{pod.sidecar}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={TMP_SIZE}",
        *pod.docker_env(),
        # Hook installers (husky in npm "prepare") would try to change the read-only .git/config.
        "-e", "HUSKY=0",
        "-e", f"MAVEN_OPTS={AGENT_MAVEN_OPTS}",
        *(arg for k, v in pod.agent_env.items() for arg in ("-e", f"{k}={v}")),
        *(arg for name in pod.passed_env for arg in ("-e", name)),
        *(arg for m in mounts for arg in ("-v", m.arg())),
        "-w", str(pod.repo),
        pod.image, "sleep", "infinity",
    ]  # fmt: skip


def gate_command(pod: Pod, ca_mounts: list[Mount]) -> list[str]:
    """Like the agent container, but without anything the agent could have left behind: its
    working tree and build outputs, its home directory, its background processes."""
    mounts = [
        Mount(pod.volumes["socket"], SOCKET_DIR),
        Mount(str(pod.repo), str(pod.repo), read_only=True),
        Mount(str(pod.gate_dir), str(pod.gate_dir)),
        # Not the build cache the writer fills: a hit there would skip a module's build and
        # tests on the writer's word.
        *(Mount(f"vivibox-cache-{name}", path) for name, path in CACHES.items() if name != "build-cache"),
        Mount(pod.volumes["gate-build-cache"], CACHES["build-cache"]),
        Mount(pod.volumes["gate-installed"], INSTALLED),
        *ca_mounts,
    ]
    return [
        "docker", "run", "-d", "--name", pod.gate,
        "--label", f"vivibox.task={pod.task_id}",
        "--network", f"container:{pod.sidecar}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={TMP_SIZE}",
        "--tmpfs", f"/config:exec,mode=1777,size={TMP_SIZE}",
        *pod.docker_env(),
        "-e", "HUSKY=0",
        *(arg for k, v in pod.agent_env.items() for arg in ("-e", f"{k}={v}")),
        *(arg for name in pod.passed_env for arg in ("-e", name)),
        *(arg for m in mounts for arg in ("-v", m.arg())),
        "-w", str(pod.gate_dir),
        pod.image, "sleep", "infinity",
    ]  # fmt: skip


def review_command(pod: Pod, mounts: list[Mount], env: dict[str, str], ca_mounts: list[Mount]) -> list[str]:
    """The reviewer's container: a fresh clone like the gate's, with the writer's tree read-only
    and without the build's means, since it reads and runs nothing: no Docker socket, no build
    caches, no variables of the build. Its own secrets and harness files come as mounts."""
    all_mounts = [
        Mount(str(pod.repo), str(pod.repo), read_only=True),
        Mount(str(pod.review_dir), str(pod.review_dir)),
        *ca_mounts,
        *mounts,
    ]
    return [
        "docker", "run", "-d", "--name", pod.review,
        "--label", f"vivibox.task={pod.task_id}",
        "--network", f"container:{pod.sidecar}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={TMP_SIZE}",
        "--tmpfs", f"/config:exec,mode=1777,size={TMP_SIZE}",
        *(arg for k, v in env.items() for arg in ("-e", f"{k}={v}")),
        *(arg for m in all_mounts for arg in ("-v", m.arg())),
        "-w", pod.review_src,
        pod.image, "sleep", "infinity",
    ]  # fmt: skip
