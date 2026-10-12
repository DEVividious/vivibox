"""What a pod's containers are made of: the images, the mounts, the caches, and the commands that
start the sidecar, the agent, the gate, the reviewer and the planner's control container. Plain
functions of the pod they are for; the host's CA bundle comes in as an argument, read by pod.py.
`run` and `Runner`, which run a process on the host, are here because pod_egress and pod both
need them and pod imports pod_egress."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pod import Pod
    from .pod_network import RoleNetwork

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
# Where Maven's split local repository puts what the agent's build installs: a directory of the
# task's disk, over the shared /cache/m2, so one task's unfinished modules never reach another's
# build. The gate's repository is its own (gate_tool_env), installed modules included; in the gate
# this path is the read-only volume's own empty directory.
INSTALLED = "/cache/m2/installed"
# The gate's own caches, under its directory on the task's disk, made fresh by gate_up for every
# verification and gone with the directory. The shared caches are mounted read-only in the gate:
# a writer can put a program into any of them (a git under mise's shims, an init script under
# Gradle's home), so the gate runs nothing from them and writes nothing there. What a tool can
# read from the shared cache without writing it reads (Maven through a file repository of the
# shared downloads, Gradle through its read-only dependency cache); the rest is fetched cold.
GATE_CACHES = "caches"
GATE_CACHE_DIRS = (
    "m2", "gradle", "npm", "yarn/classic", "yarn/berry", "corepack", "uv", "go", "cargo", "rustup", "mise",
)  # fmt: skip
# Maven's settings for the gate, written from the template into the gate's directory and mounted
# under its home: the shared downloads as a repository before the project's own.
MAVEN_SETTINGS = "settings.xml"
MAVEN_SETTINGS_TEMPLATE = "maven-settings.xml"
MAVEN_SETTINGS_HOME = "/config/.m2/settings.xml"
# Yarn Classic's cache for the gate, as a user-level .yarnrc under its home (gate_up copies it
# there): a project's own cache-folder outranks it, as it outranks the image's installation-level
# yarnrc for the agent, where YARN_CACHE_FOLDER would outrank the project's choice (and Berry
# reads that variable as its cacheFolder). Berry's global cache is YARN_GLOBAL_FOLDER, as in the image.
YARNRC = "yarnrc"
YARNRC_HOME = "/config/.yarnrc"
# The image's Maven options (vivibox/images/agent/Dockerfile: the same words), for the gate with
# its own repository in place of the shared one: the split (downloads under cached/, installs
# under installed/), the file locks, and the build cache restored whole.
IMAGE_MAVEN_ARGS = (
    "-Dmaven.repo.local=/cache/m2 -Daether.syncContext.named.factory=file-lock"
    " -Daether.syncContext.named.nameMapper=file-gav -Daether.enhancedLocalRepository.split=true"
    " -Dmaven.build.cache.lazyRestore=false"
)
# The agent's Maven: the image's options, and no build cache. A hit there restores a module up to
# the phase an earlier command reached (the agent builds with `test`, `package`, `-DskipTests` by
# turns) and runs only the later phases, without `initialize`: what that phase sets for the tests
# (Mockito's agent path from dependency:properties, jacoco's argLine) is missing, and the forked
# JVM dies. The gate keeps its cache: it holds whole verifications only (ADR-0023). Every Maven a
# wrapper may fetch reads MAVEN_OPTS, and the extension reads system properties.
AGENT_MAVEN_OPTS = "-Dmaven.repo.local=/cache/m2 -Dmaven.build.cache.enabled=false"
HOST_GATEWAY = "host.docker.internal"
# The sidecar carries the digest of the bundle it started with: its daemon reads the authorities
# once, at its start, so a bundle changed since (a VPN client installing a new one) is not seen
# until the sidecar is made again.
CA_LABEL = "vivibox.ca"
# And the digest of the limits it was made with: docker start keeps a container's flags, so a
# limit raised since arrives only when the sidecar is made again.
LIMITS_LABEL = "vivibox.limits"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

# --- limits ------------------------------------------------------------------------------------

GIB = 2**30
UNITS = {"k": 2**10, "m": 2**20, "g": 2**30, "t": 2**40}
SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt])(?:i?b)?$", re.IGNORECASE)
# The containers a task's limits name, by the key of config.toml's [limits] (<role>_memory,
# <role>_pids): the agent's, the gate's, the sidecar's (the pod's own Docker), the reviewer's and
# the planner's control container.
ROLES = ("agent", "gate", "sidecar", "review", "control")
DEFAULT_MEMORY = {
    "agent": 4 * GIB, "gate": 4 * GIB, "sidecar": 2 * GIB, "review": 3 * GIB // 2, "control": 3 * GIB // 2,
}  # fmt: skip
DEFAULT_PIDS = {"agent": 2048, "gate": 4096, "sidecar": 4096, "review": 1024, "control": 1024}
DEFAULT_POD_MEMORY, DEFAULT_POD_PIDS, DEFAULT_DISK = 6 * GIB, 8192, 20 * GIB
# The CPUs each container may use: six, or the machine's when it has fewer (Docker refuses
# --cpus past the machine's count, and the default must start a pod on a four-core laptop).
MOST_DEFAULT_CPUS = 6


def machine_cpus() -> int:
    """The CPUs this process may run on, which is what Docker counts; the machine's where the
    kernel does not say."""
    try:
        return len(os.sched_getaffinity(0)) or os.cpu_count() or MOST_DEFAULT_CPUS
    except (AttributeError, OSError):
        return os.cpu_count() or MOST_DEFAULT_CPUS


def default_cpus() -> float:
    return float(min(MOST_DEFAULT_CPUS, machine_cpus()))


# The smallest and the largest disk image the root helper makes (host/vivibox-host: the same).
DISK_SMALLEST, DISK_LARGEST = GIB, 2 * 2**40
# Every key of [limits] that is one of these, in the order the panel and `vivibox limits` say them.
LIMIT_KEYS = (
    "pod_memory", "pod_cpus", "pod_pids",
    *(f"{role}_{kind}" for role in ROLES for kind in ("memory", "pids")),
    "disk",
)  # fmt: skip


def parse_size(text) -> int:
    """Bytes of a size as a person writes it: 4g, 1536m, 2T, 512MiB; a whole number of them."""
    if not isinstance(text, str) or not (m := SIZE.match(text.strip())):
        raise ValueError(f'not a size, e.g. "4g" or "1536m": {text!r}')
    found = int(float(m.group(1)) * UNITS[m.group(2).lower()])
    if found <= 0:
        raise ValueError(f"not a size above zero: {text!r}")
    return found


def size_text(n: int) -> str:
    """A size in the units a person writes them in: 4 GiB, 1.5 GiB, 512 MiB."""
    for unit, scale in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("kiB", 2**10)):
        if n >= scale:
            value = n / scale
            return f"{value:.1f} {unit}".replace(".0 ", " ")
    return f"{n} B"


def size_option(n: int) -> str:
    """A size as an option of `vivibox limits` takes it, whole units where there are: 6g, 1536m."""
    for unit, scale in (("t", 2**40), ("g", 2**30), ("m", 2**20), ("k", 2**10)):
        if n >= scale and n % scale == 0:
            return f"{n // scale}{unit}"
    return str(n)


def slice_of(task_id: str) -> str:
    """The cgroup slice every container of the task runs under: a dash in a slice's name nests it
    in systemd (a-b.slice under a.slice), so the id's dashes are underscores there."""
    return f"vivibox-{task_id.replace('-', '_')}.slice"


def _count(value, key: str, most: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= most:
        raise ValueError(f"{key} must be a whole number from 1 to {most}")
    return value


@dataclass(frozen=True)
class Limits:
    """What a task's containers may use: memory and processes per container, enforced by their
    cgroup; the pod's figures, a budget the per-container ones are checked against (and the CPU
    each container gets) until the slice itself is limited; and the size of the task's disk."""

    pod_memory: int = DEFAULT_POD_MEMORY
    pod_cpus: float = field(default_factory=default_cpus)
    pod_pids: int = DEFAULT_POD_PIDS
    memory: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MEMORY))
    pids: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PIDS))
    disk: int = DEFAULT_DISK

    def memory_of(self, role: str) -> int:
        return self.memory[role]

    def pids_of(self, role: str) -> int:
        return self.pids[role]

    def tmpfs_size(self, role: str) -> int:
        """Half of the container's memory: its scratch tmpfs and its processes never pass the limit."""
        return self.memory_of(role) // 2

    def flags(self, role: str, task_id: str) -> list[str]:
        """The docker run flags of a container of the task in this role, never --oom-kill-disable:
        memory with swap equal to it (a container may not swap past its limit), the pod's CPUs,
        its processes, under the task's slice."""
        memory = str(self.memory_of(role))
        return [
            "--memory", memory, "--memory-swap", memory, "--cpus", f"{self.pod_cpus:g}",
            "--pids-limit", str(self.pids_of(role)), "--cgroup-parent", slice_of(task_id),
        ]  # fmt: skip

    def values(self) -> dict:
        """Every limit by its key in [limits], sizes as a person writes them."""
        found = {"pod_memory": size_option(self.pod_memory), "pod_cpus": self.pod_cpus}
        found["pod_pids"] = self.pod_pids
        for role in ROLES:
            found[f"{role}_memory"] = size_option(self.memory[role])
            found[f"{role}_pids"] = self.pids[role]
        found["disk"] = size_option(self.disk)
        return found

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.values(), sort_keys=True).encode()).hexdigest()[:16]

    def with_values(self, values: dict) -> Limits:
        """These limits with the given keys of [limits] over them, each checked: a size as a
        person writes it, a count, the pod's CPUs; a container's limit at most the pod's. ValueError
        names the key."""
        memory, pids = dict(self.memory), dict(self.pids)
        pod_memory, pod_cpus, pod_pids, disk = self.pod_memory, self.pod_cpus, self.pod_pids, self.disk
        for key, value in values.items():
            if key not in LIMIT_KEYS:
                raise ValueError(f"limits.{key} is not a limit; the limits are {', '.join(LIMIT_KEYS)}")
            try:
                if key == "pod_cpus":
                    a_number = isinstance(value, (int, float)) and not isinstance(value, bool)
                    if not a_number or not 0 < value <= 1024:
                        raise ValueError("limits.pod_cpus must be a number of CPUs above 0, e.g. 6 or 2.5")
                    if value > (available := machine_cpus()):
                        raise ValueError(
                            f"limits.pod_cpus ({value:g}) is past this machine's {available} CPUs, "
                            "which Docker refuses"
                        )
                    pod_cpus = float(value)
                elif key == "pod_pids":
                    pod_pids = _count(value, "limits.pod_pids", 4_194_304)
                elif key == "pod_memory":
                    pod_memory = parse_size(value)
                elif key == "disk":
                    disk = parse_size(value)
                    if not DISK_SMALLEST <= disk <= DISK_LARGEST:
                        smallest, largest = size_option(DISK_SMALLEST), size_option(DISK_LARGEST)
                        raise ValueError(f"limits.disk must be from {smallest} to {largest}")
                elif key.endswith("_memory"):
                    memory[key[: -len("_memory")]] = parse_size(value)
                else:
                    pids[key[: -len("_pids")]] = _count(value, f"limits.{key}", 4_194_304)
            except ValueError as e:
                said = str(e) if str(e).startswith("limits.") else f"limits.{key}: {e}"
                raise ValueError(said) from None
        for role in ROLES:
            if memory[role] > pod_memory:
                raise ValueError(
                    f"limits.{role}_memory ({size_text(memory[role])}) is past the pod's limits.pod_memory "
                    f"({size_text(pod_memory)})"
                )
            if pids[role] > pod_pids:
                raise ValueError(
                    f"limits.{role}_pids ({pids[role]}) is past the pod's limits.pod_pids ({pod_pids})"
                )
        return Limits(pod_memory, pod_cpus, pod_pids, memory, pids, disk)


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


def sidecar_command(
    pod: Pod, ca_mounts: list[Mount], ca_digest: str, net: RoleNetwork | None = None
) -> list[str]:
    # The daemon listens only on the unix socket. 666: the socket is owned by root of the
    # Sysbox user namespace, the agent runs as your UID, and only this pair mounts the volume.
    # The socket left by the last run goes first: the wait would find it at once, chmod it, and
    # the daemon would then replace it with one the agent cannot use ("permission denied").
    # The trap passes docker stop on to the daemon, which then shuts down cleanly.
    # Docker 29 refuses API versions below 1.40, and Testcontainers before 1.21 speaks 1.32: it
    # would find no Docker at all while `docker version` works. 1.24 is the oldest it still knows.
    mirror = f"--registry-mirror={pod.hub_mirror} " if pod.hub_mirror else ""
    # The daemon writes to the container's output, which Docker keeps in three files of 10 MB.
    daemon = (
        f"rm -f {SOCKET}; DOCKER_MIN_API_VERSION=1.24 dind dockerd --host=unix://{SOCKET} "
        f"{mirror}& "
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
        "--label", f"{LIMITS_LABEL}={pod.limits.digest()}",
        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        *pod.limits.flags("sidecar", pod.task_id),
        # Pinned rather than left to Docker: this address goes into your configuration files, so
        # it has to be the same one after every restart. The host is the bridge's gateway.
        "--network", pod.network,
        *(("--ip", net.address, "--add-host", f"{HOST_GATEWAY}:{net.gateway}") if net else ()),
        # Every DNS client in the pod asks the host's forwarder: Docker's own resolver in the
        # sidecar, which the agent shares, and the daemon in it, which its containers inherit.
        # No search domain of the host's ("." is Docker's none).
        "--dns", pod.host_ip, "--dns-search", ".",
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


def agent_repo(repo) -> str:
    """Where the agent's container has the task's clone: at the path the clone has on the host.
    The one place that says so for three things, which a test holds to one value: the
    container's mount of the clone and its working directory (agent_command), and what is told
    there where the project is (vivibox's own Serena, providers.task_mcp).

    Not a way to move the clone: everything else that names it in a container says the host's
    path itself. The parts of the clone mounted over it (repo.agent_mounts: .git, and its
    config and hooks read-only), the folder the agent's brief names, the working directory of
    what is started there besides the harness (the project's preparation, its app, a box's
    shell), the sidecar's and the gate's mounts of the clone, and the readers' copies of the
    commits at the path the writer has its clone. Changed alone, this leaves the clone at a new
    path with none of its protected parts."""
    return str(repo)


def agent_command(pod: Pod, ca_mounts: list[Mount]) -> list[str]:
    mounts = [
        Mount(pod.volumes["socket"], SOCKET_DIR),
        Mount(str(pod.repo), agent_repo(pod.repo)),
        Mount(pod.volumes["config"], "/config"),
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
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={pod.limits.tmpfs_size('agent')}",
        *pod.limits.flags("agent", pod.task_id),
        *pod.docker_env(),
        # Hook installers (husky in npm "prepare") would try to change the read-only .git/config.
        "-e", "HUSKY=0",
        "-e", f"MAVEN_OPTS={AGENT_MAVEN_OPTS}",
        *(arg for k, v in pod.agent_env.items() for arg in ("-e", f"{k}={v}")),
        *(arg for name in pod.passed_env for arg in ("-e", name)),
        *(arg for m in mounts for arg in ("-v", m.arg())),
        "-w", agent_repo(pod.repo),
        pod.image, "sleep", "infinity",
    ]  # fmt: skip


def gate_caches(gate_dir) -> str:
    return f"{gate_dir}/{GATE_CACHES}"


def gate_tool_env(gate_dir) -> dict[str, str]:
    """Every variable of the image's that aims a build tool at a shared cache, aimed at the gate's
    own under its directory instead; Maven's build cache stays where the image had it, beside the
    shared repository, which is the gate's own volume there. Gradle reads the shared dependency
    cache through its read-only cache, the directory holding modules-2."""
    caches = gate_caches(gate_dir)
    build_cache = f"-Dmaven.build.cache.location={CACHES['build-cache']}"
    return {
        "MAVEN_ARGS": f"{IMAGE_MAVEN_ARGS.replace(CACHES['m2'], f'{caches}/m2')} {build_cache}",
        "MAVEN_OPTS": f"-Dmaven.repo.local={caches}/m2 {build_cache}",
        # The wrapper's own home: $HOME/.m2 is a folder Docker made as root for the settings
        # file mounted there, so the wrapper could not fetch a distribution into it.
        "MAVEN_USER_HOME": f"{caches}/m2-home",
        "GRADLE_USER_HOME": f"{caches}/gradle",
        "GRADLE_RO_DEP_CACHE": f"{CACHES['gradle']}/caches",
        "npm_config_cache": f"{caches}/npm",
        "YARN_GLOBAL_FOLDER": f"{caches}/yarn/berry",
        "COREPACK_HOME": f"{caches}/corepack",
        "UV_CACHE_DIR": f"{caches}/uv",
        "UV_PYTHON_INSTALL_DIR": f"{caches}/uv/python",
        "GOMODCACHE": f"{caches}/go/mod",
        "GOCACHE": f"{caches}/go/build",
        "CARGO_HOME": f"{caches}/cargo",
        "RUSTUP_HOME": f"{caches}/rustup",
    }


def yarnrc(gate_dir) -> str:
    return f'cache-folder "{gate_caches(gate_dir)}/yarn/classic"\n'


def gate_command(pod: Pod, ca_mounts: list[Mount], env: dict[str, str]) -> list[str]:
    """Like the agent container, but without anything the agent could have left behind: its
    working tree and build outputs, its home directory, its background processes, and what it
    put into the shared caches, read-only here. env: the gate's PATH, JDK and mise
    (toolchain.gate_env); the build tools' caches are gate_tool_env's."""
    mounts = [
        Mount(pod.volumes["socket"], SOCKET_DIR),
        Mount(str(pod.repo), str(pod.repo), read_only=True),
        Mount(str(pod.gate_dir), str(pod.gate_dir)),
        Mount(f"{pod.gate_dir}/{MAVEN_SETTINGS}", MAVEN_SETTINGS_HOME, read_only=True),
        # Not the build cache the writer fills: a hit there would skip a module's build and
        # tests on the writer's word.
        *(
            Mount(f"vivibox-cache-{name}", path, read_only=True)
            for name, path in CACHES.items()
            if name != "build-cache"
        ),
        Mount(pod.volumes["gate-build-cache"], CACHES["build-cache"]),
        *ca_mounts,
    ]
    return [
        "docker", "run", "-d", "--name", pod.gate,
        "--label", f"vivibox.task={pod.task_id}",
        "--network", f"container:{pod.sidecar}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={pod.limits.tmpfs_size('gate')}",
        "--tmpfs", f"/config:exec,mode=1777,size={pod.limits.tmpfs_size('gate')}",
        *pod.limits.flags("gate", pod.task_id),
        *pod.docker_env(),
        "-e", "HUSKY=0",
        *(arg for k, v in {**gate_tool_env(pod.gate_dir), **env}.items() for arg in ("-e", f"{k}={v}")),
        *(arg for name in pod.passed_env for arg in ("-e", name)),
        *(arg for m in mounts for arg in ("-v", m.arg())),
        "-w", str(pod.gate_dir),
        pod.image, "sleep", "infinity",
    ]  # fmt: skip


def review_command(
    pod: Pod, mounts: list[Mount], env: dict[str, str], ca_mounts: list[Mount], net: RoleNetwork
) -> list[str]:
    """The reviewer's container, made anew for every round, without the build's means, since
    it reads and runs nothing: no Docker socket, no build caches, no variables of the build. It
    reads the work in a view of the commits that the host makes (reviewing.up), read-only, at
    the path the clone has for the writer: the writer's clone itself is not mounted, so git
    here never follows what the writer left in its .git, and nothing the reviewer does reaches
    the writer's tree. The view is data, never the harness's working directory, which is
    READER_WORK, a folder of the host's beside it: harness configuration the writer committed
    is read, not run. Its own secrets and harness files come as mounts. It is in the reviewer's
    own network (net), not the sidecar's namespace: what the writer runs, nested containers
    with any capability included, is not on its wire, and the host's rules for its bridge
    refuse the writer's range and the writer's refuse its."""
    all_mounts = [
        Mount(pod.review_src, str(pod.repo), read_only=True),
        Mount(str(pod.review_work), READER_WORK, read_only=True),
        *ca_mounts,
        *mounts,
    ]
    # git reads a tree it cannot write: no index refresh, no lock.
    env = {"GIT_OPTIONAL_LOCKS": "0", **env}
    return [
        "docker", "run", "-d", "--name", pod.review,
        "--label", f"vivibox.task={pod.task_id}",
        # Pinned to the address allocated for it; its gateway is the host on its own bridge, so
        # host.docker.internal is never the writer's. Every DNS query to the host's forwarder,
        # no search domain of the host's, as for the sidecar.
        "--network", pod.network_name(net.role), "--ip", net.address,
        "--add-host", f"{HOST_GATEWAY}:{net.gateway}",
        "--dns", pod.host_ip, "--dns-search", ".",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={pod.limits.tmpfs_size('review')}",
        "--tmpfs", f"/config:exec,mode=1777,size={pod.limits.tmpfs_size('review')}",
        *pod.limits.flags("review", pod.task_id),
        *(arg for k, v in env.items() for arg in ("-e", f"{k}={v}")),
        *(arg for m in all_mounts for arg in ("-v", m.arg())),
        "-w", READER_WORK,
        pod.image, "sleep", "infinity",
    ]  # fmt: skip


# On the task's disk, made by the pod itself and mounted in no container but the planner's: its
# home, and its copy of the committed work, which the host writes and it only reads.
CONTROL_HOME = "config-control"
CONTROL_SRC = "control-src"
# The working directory of a harness that only reads the work, in its container (the planner's
# control container, the reviewer's), where a folder of the task's disk is mounted read-only: an
# empty git repository of the host's making (reading.workdir). A harness takes its working
# directory for the project and loads the project's own configuration from it, so that directory
# holds nothing anyone but the host wrote; the copy of the writer's commits is data beside it,
# read by its path. A git repository, because opencode's attached run prints no events for a
# turn in a directory that is not one (seen in the image, opencode 1.18.31).
READER_WORK = "/work"
CONTROL_WORK_DIR = "control-work"


def control_command(pod: Pod, ca_mounts: list[Mount], net: RoleNetwork) -> list[str]:
    """The planner's control container, for the whole life of the task: the reviewer's recipe (no
    Docker socket, no build caches, nothing a build installed, no variables of the build, a
    network of its own), with a home of its own on the task's disk in place of a tmpfs, since
    the planner's conversation is carried on from turn to turn. It reads the work in a copy of
    the commits that the host makes (control.refresh), read-only, at the path the clone has
    for the writer: the writer's clone itself is not mounted, so git here never follows what
    the writer left in its .git (a commondir, a config of its choosing), and nothing the planner
    does reaches the writer's tree. The copy is data, never the harness's working directory,
    which is READER_WORK: harness configuration the writer committed is read, not run. Nothing
    the writer's container mounts is that home, that copy or its secrets, and the host's rules
    refuse the writer's range and the reviewer's on its bridge, and its range on theirs."""
    all_mounts = [
        Mount(str(pod.control_src), str(pod.repo), read_only=True),
        Mount(str(pod.control_work), READER_WORK, read_only=True),
        Mount(str(pod.control_home), "/config"),
        *ca_mounts,
        *(pod.control_mounts or []),
    ]
    # git reads a tree it cannot write: no index refresh, no lock.
    env = {"GIT_OPTIONAL_LOCKS": "0", **pod.control_env}
    return [
        "docker", "run", "-d", "--name", pod.control,
        "--label", f"vivibox.task={pod.task_id}",
        # Pinned to the address allocated for it, on its own bridge, as the reviewer's.
        "--network", pod.network_name(net.role), "--ip", net.address,
        "--add-host", f"{HOST_GATEWAY}:{net.gateway}",
        "--dns", pod.host_ip, "--dns-search", ".",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", f"/tmp:exec,mode=1777,size={pod.limits.tmpfs_size('control')}",
        *pod.limits.flags("control", pod.task_id),
        *(arg for k, v in env.items() for arg in ("-e", f"{k}={v}")),
        *(arg for m in all_mounts for arg in ("-v", m.arg())),
        "-w", READER_WORK,
        pod.image, "sleep", "infinity",
    ]  # fmt: skip
