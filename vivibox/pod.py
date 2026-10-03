"""Task pod: a Docker daemon in a Sysbox sidecar and an unprivileged agent container in its network.

The agent reaches the daemon only through a unix socket on a volume shared by the pair, and sees
ports published by compose or Testcontainers on localhost. The sidecar mounts the task repo
read-only: under Sysbox, root in a nested container would otherwise write to it as host root.
What the pod writes outside the clone (the daemon's data, the agent's home, what a build
installs, the gate's and the reviewer's clones) is on the task's disk image (pod_disk).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import display, handoff, podspec, repo
from .config import DEFAULT_HOST_ADDRESS, DEFAULT_NETWORK_POOL, HostService
from .pod_demo import PREPARE_PID as PREPARE_PID
from .pod_demo import PodDemo
from .pod_disk import DISK_DIRS as DISK_DIRS
from .pod_disk import DISK_IMAGE as DISK_IMAGE
from .pod_disk import DISK_MOUNT as DISK_MOUNT
from .pod_disk import PodDisk
from .pod_egress import ETHERNET_MTU as ETHERNET_MTU
from .pod_egress import HELPER as HELPER
from .pod_egress import LINK as LINK
from .pod_egress import PodEgress
from .pod_egress import RulesLeft as RulesLeft
from .pod_egress import prepare_host as prepare_host
from .pod_egress import uplink_mtu as uplink_mtu
from .pod_network import PodError as PodError
from .pod_network import PodNetwork, RoleNetwork
from .podspec import AGENT_MAVEN_OPTS as AGENT_MAVEN_OPTS
from .podspec import CA_LABEL as CA_LABEL
from .podspec import CACHES as CACHES
from .podspec import DIND_IMAGE as DIND_IMAGE
from .podspec import HOST_GATEWAY as HOST_GATEWAY
from .podspec import INSTALLED as INSTALLED
from .podspec import LIMITS_LABEL as LIMITS_LABEL
from .podspec import RYUK_IMAGE as RYUK_IMAGE
from .podspec import SOCKET as SOCKET
from .podspec import SOCKET_DIR as SOCKET_DIR
from .podspec import Limits as Limits
from .podspec import Mount as Mount
from .podspec import Runner as Runner
from .podspec import binds as binds
from .podspec import run as run

# The host's trust store, shared with the pod: a registry or a proxy the host trusts through an
# authority of its own (corporate TLS inspection) is trusted in the pod the same way. The daemon
# (Go), OpenSSL and the system's tools read this file; tools with a store of their own do not.
HOST_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def addresses(sidecars: Sequence[str], runner: Runner = run, timeout: float | None = None) -> dict[str, str]:
    """Where each pod answers, for many pods at once: asking one at a time is what makes a list slow.
    timeout: seconds before subprocess.TimeoutExpired, for a daemon that does not answer."""
    if not sidecars:
        return {}
    template = "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    asked = ["docker", "inspect", "-f", template, *sidecars]
    found = runner(asked, timeout=timeout) if timeout else runner(asked)
    out = {}
    for line in found.stdout.splitlines():
        name, _, address = line.strip().partition(" ")
        if address:
            out[name.lstrip("/")] = address
    return out


CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# The volumes of a task from before its data was on its disk (layout 3), by the ending of their
# names; the socket's stays a volume, and is not one of them.
LEGACY_VOLUMES = ("docker", "installed", "gate-installed", "gate-build-cache", "config")


def container_ids(names: Sequence[str], runner: Runner = run) -> dict[str, str]:
    """The id of each container that exists, by name, in one question. An id is 64 hex digits
    and nothing else is taken: a label for a cgroup scope (limits.py), never a path or an argument."""
    if not names:
        return {}
    found = runner(["docker", "inspect", "-f", "{{.Name}} {{.Id}}", *names])
    out = {}
    for line in found.stdout.splitlines():
        name, _, cid = line.strip().partition(" ")
        if CONTAINER_ID.fullmatch(cid):
            out[name.lstrip("/")] = cid
    return out


def ca_mounts() -> list[Mount]:
    """The host's CA bundle over the container's own, when the host has one."""
    return [Mount(str(HOST_CA_BUNDLE), CA_BUNDLE, read_only=True)] if HOST_CA_BUNDLE.exists() else []


def ca_digest() -> str:
    """What the host's CA bundle holds, in a word; "" when the host has none."""
    try:
        return hashlib.sha256(HOST_CA_BUNDLE.read_bytes()).hexdigest()
    except OSError:
        return ""


# What makes a copy of the task's clone slow with nothing wrong in it.
COPYING_IS_SLOW = "a very large repository, or a slow fetch of its submodules over the network,"


@dataclass
class Pod(PodNetwork, PodDemo, PodEgress, PodDisk):
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
    # The host's address for the pods, where the DNS forwarder listens; see config.Config.
    host_address: str = DEFAULT_HOST_ADDRESS
    # Where the task's network ranges are kept once allocated (pod_network); None: in memory.
    allocation: Path | None = None
    # Where the daemon pulls Docker Hub's images through, "" for Docker Hub itself (mirror.py).
    hub_mirror: str = ""
    # What its containers may use (config.toml's [limits], with the task's own over them).
    limits: Limits = field(default_factory=Limits)
    # The task's folder, <tasks_dir>/<id>, where its disk image lives and its clone beside it:
    # the clone's parent unless given (task.py: the clone is <folder>/repo).
    folder: Path | None = None
    runner: Runner = run
    # What gate_stream starts a command with; a Popen-like whose stdout can be read line by line.
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    _in_memory: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.folder is None:
            self.folder = self.repo.parent

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
        """What the pod binds of the task's own, by name: directories of its disk (pod_disk),
        bounded by the image's size, but for the Docker socket, in a volume the sidecar and the
        agent share and never on the host."""
        disk = self.disk
        return {
            "docker": str(disk / "docker"),
            "socket": f"vivibox-{self.task_id}-socket",
            # What Maven installs, the task's own modules; kept apart from the shared downloads.
            "installed": str(disk / "installed"),
            "gate-installed": str(disk / "gate-installed"),
            # The gate's own build cache: what it built from committed work, for its next attempt.
            "gate-build-cache": str(disk / "gate-build-cache"),
            # The agent's home: its tools' configuration and state.
            "config": str(disk / "config"),
        }

    def legacy_volumes(self) -> list[str]:
        """The names of the volumes a task had before its data was on its disk."""
        return [f"vivibox-{self.task_id}-{v}" for v in LEGACY_VOLUMES]

    def _run(
        self, *cmd: str, check: bool = True, timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        # The keyword only when there is one: runners that know no time limit (tests) keep working.
        p = self.runner(cmd, timeout=timeout) if timeout else self.runner(cmd)
        if check and p.returncode != 0:
            said = display.name((p.stderr or p.stdout).strip())
            raise PodError(f"{display.name(' '.join(cmd[:4]))}…: {said}")
        return p

    def _copying_clone(
        self, down: Callable[[], None], *cmd: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        """A docker exec of git that reads the task's clone, which the agent writes (a named pipe
        there makes git wait forever): past repo.COPY_TIMEOUT the container is taken down, and
        git in it with it, and GitTimeout says why. Any runner: nothing waits on its time limit.
        Only once the clone is as recorded: commondir or alternates would aim the copy elsewhere."""
        repo.guard(self.repo)
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            down()

        timer = threading.Timer(repo.COPY_TIMEOUT, expire)
        timer.daemon = True
        timer.start()
        try:
            p = self._run(*cmd, check=False)
        finally:
            timer.cancel()
            timer.join()  # a container being taken down is gone before anyone is told
        if expired.is_set():  # even had git just finished: the container is gone
            raise repo.timed_out(cmd[cmd.index("git") + 1 :], repo.COPY_TIMEOUT, self.repo, COPYING_IS_SLOW)
        if check and p.returncode != 0:
            said = display.name((p.stderr or p.stdout).strip())
            raise PodError(f"{display.name(' '.join(cmd[:4]))}…: {said}")
        return p

    def _state(self, name: str) -> str | None:
        p = self._run("docker", "inspect", "-f", "{{.State.Status}}", name, check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    # --- commands ---------------------------------------------------------------------------

    def address(self) -> str:
        """Where the pod answers, from the host and from inside itself alike. Empty when it is down."""
        template = f'{{{{(index .NetworkSettings.Networks "{self.network}").IPAddress}}}}'
        found = self._run("docker", "inspect", "-f", template, self.sidecar, check=False)
        return found.stdout.strip() if found.returncode == 0 else ""

    def sidecar_command(self, net: RoleNetwork | None = None) -> list[str]:
        return podspec.sidecar_command(self, ca_mounts(), ca_digest(), net or self.recorded_network())

    def docker_env(self) -> list[str]:
        return podspec.docker_env(self)

    def agent_command(self) -> list[str]:
        return podspec.agent_command(self, ca_mounts())

    def gate_command(self) -> list[str]:
        return podspec.gate_command(self, ca_mounts())

    def review_command(self, mounts: list[Mount], env: dict[str, str]) -> list[str]:
        return podspec.review_command(self, mounts, env, ca_mounts())

    def scopes(self) -> dict[str, str]:
        """The id of each container of the pod that is there, by its limits role (config.toml's
        [limits]): for limits.py, a label for the container's cgroup scope under the task's slice."""
        names = {"agent": self.agent, "sidecar": self.sidecar, "gate": self.gate, "review": self.review}
        found = container_ids(list(names.values()), self.runner)
        return {role: found[name] for role, name in names.items() if name in found}

    def passed_values(self) -> list[str]:
        """The values of the passed variables, for keeping them out of logs."""
        return [v for name in self.passed_env if len(v := os.environ.get(name, "")) >= 4]

    def missing_env(self) -> list[str]:
        return [name for name in self.passed_env if not os.environ.get(name)]

    # --- lifecycle --------------------------------------------------------------------------

    def running(self) -> bool:
        """Whether the agent's container runs: Docker may have taken it down under a task."""
        return self._state(self.agent) == "running"

    def up(self, timeout: float = 90) -> None:
        """Starts whatever is not running. Idempotent."""
        if not self.repo.is_dir():
            raise PodError(f"task repo {self.repo} does not exist")
        prepare_host(self.host_address, self.network_pool, self.runner)
        # The disk before anything of the pod: the daemon's data and the agent's home are on it.
        self.attach_disk()
        state = self._state(self.sidecar)
        if state == "running" and self._stale_ca():
            # Its daemon read the host's authorities at its start and would go on failing TLS
            # against a new one ("x509: certificate signed by unknown authority"); up() runs
            # before any turn, so this is the moment to make it again.
            state = self._sidecar_gone()
        if state is not None and self.recorded_network() is None:
            # Its address is not one vivibox allocated in today's pool: made again, data in volumes.
            state = self._sidecar_gone()
        if state != "running":
            # The agent joins the sidecar's network namespace, which a sidecar restart replaces.
            self._run("docker", "rm", "-f", self.agent, check=False)
            if state is not None and self._outdated_sidecar():
                # docker start would run the script it was made with; its data is in volumes.
                state = self._sidecar_gone()
        net = self.ensure_network() if state is None else self.recorded_network()
        self.check_bridge(net)
        # The host's rules for the task's bridges, before anything of the pod sends a packet, and
        # again at every start: they follow the uplink of the moment (a VPN on or off).
        self.apply_firewall()
        if state is None:
            self._run(*self.sidecar_command(net))
        elif state != "running":
            self._run("docker", "start", self.sidecar)
        if state != "running":
            self._wait_for_daemon(timeout)
        if self._state(self.agent) != "running":
            self._run("docker", "rm", "-f", self.agent, check=False)
            self._placeholders(self.agent_mounts)
            self._run(*self.agent_command())

    def _sidecar_gone(self) -> None:
        self._run("docker", "rm", "-f", self.agent, check=False)
        self._run("docker", "rm", "-f", self.sidecar, check=False)

    def _placeholders(self, mounts: list[Mount]) -> None:
        """Each mount point in a folder the agent writes made a plain empty file (Mount.placeholder),
        before a container is made with it, or no container is made."""
        for m in mounts:
            if m.placeholder:
                try:
                    handoff.placeholder(Path(m.placeholder))
                except handoff.HandoffError as e:
                    raise PodError(str(e)) from None

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
        """Whether the sidecar was made with another script, other mounts, another CA bundle or
        other limits than this vivibox would give it: docker start keeps all four, so a change
        would never arrive."""
        made = self._made_with()
        wanted = self.sidecar_command()
        try:
            return made is not None and (
                made["cmd"][-1] != wanted[-1]
                or set(made["binds"] or []) != set(binds(wanted))
                or (made.get("labels") or {}).get(CA_LABEL, "") != ca_digest()
                or (made.get("labels") or {}).get(LIMITS_LABEL, "") != self.limits.digest()
            )
        except (KeyError, IndexError, TypeError):
            return True

    def _wait_for_daemon(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sidecar_docker("info", check=False).returncode == 0:
                return
            time.sleep(0.5)
        # The daemon's output is the container's, which Docker keeps (stderr apart, as it wrote it).
        log = self._run("docker", "logs", "--tail", "20", self.sidecar, check=False)
        raise PodError(f"Docker in {self.sidecar} did not start:\n{log.stdout}{log.stderr}")

    def down(self) -> None:
        """Stops the pod and keeps its data. A review or a verification under way is lost with it:
        their containers are made anew for every round. The gate shares the sidecar's network
        namespace; left behind, it would hang without a network once the sidecar stops."""
        self.review_down()
        self.gate_down()
        for name in (self.agent, self.sidecar):
            self._run("docker", "stop", "-t", "10", name, check=False)

    def kill(self) -> None:
        """Kills the pod's containers at once, keeping its data: for a pod that ignores a stop."""
        self._run("docker", "kill", self.agent, self.sidecar, check=False)
        self.review_down()
        self.gate_down()

    def _rules_before(self, what: str) -> None:
        """The host's rules for the task, applied again before a container of the pod other than
        at up: every operation that starts one reconciles them first, fail-closed, so none runs
        on a bridge without them (the host restarted, its table gone with it). The policy is
        vivibox's own allocation and configuration, as at up; nothing of Docker's."""
        try:
            self.apply_firewall()
        except PodError as e:
            raise PodError(f"{what} could not start: {e}") from None

    def gate_up(self) -> None:
        """A fresh container with a fresh clone of the task branch: only committed work is verified."""
        if self.gate_dir is None:
            raise PodError("the pod has no gate directory")
        self.gate_down()
        # Nothing a gate before it installed: the gate builds the whole reactor from its clone.
        installed = Path(self.volumes["gate-installed"])
        shutil.rmtree(installed, ignore_errors=True)
        installed.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.gate_dir, ignore_errors=True)
        self.gate_dir.mkdir(parents=True)
        self._rules_before("the verification")
        self._run(*self.gate_command())
        # Gradle runs init scripts and reads JVM options from its home, a cache the agent can write.
        self.gate_exec("rm", "-rf", f"{CACHES['gradle']}/init.d", f"{CACHES['gradle']}/gradle.properties",
                       workdir=str(self.gate_dir))  # fmt: skip
        self.gate_exec("git", "clone", "--quiet", "--no-hardlinks", str(self.repo), self.gate_src,
                       workdir=str(self.gate_dir))  # fmt: skip

    def gate_exec(
        self, *cmd: str, check: bool = True, workdir: str = "", timeout: float | None = None
    ) -> subprocess.CompletedProcess:
        """timeout: raises subprocess.TimeoutExpired; the command inside goes down with the container.
        git, which here reads the task's clone (the clone of it, its submodules), is bounded
        without one: past repo.COPY_TIMEOUT the gate goes down and GitTimeout says why."""
        exec_ = ("docker", "exec", "-w", workdir or self.gate_src, self.gate, *cmd)
        if timeout is None and cmd[:1] == ("git",):
            return self._copying_clone(self.gate_down, *exec_, check=check)
        return self._run(*exec_, check=check, timeout=timeout)

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
        self._placeholders(mounts)
        self._rules_before("the review")
        self._run(*self.review_command(mounts, env))
        where = ("docker", "exec", "-w", str(self.review_dir), self.review)
        clone = ("git", "clone", "--quiet", "--no-hardlinks", str(self.repo), self.review_src)
        try:
            self._copying_clone(self.review_down, *where, *clone)
        except repo.GitTimeout as e:
            raise repo.GitTimeout(
                f"the review could not start: {e}; then start the task again: vivibox start {self.task_id}"
            ) from None

    def review_down(self) -> None:
        self._run("docker", "rm", "-f", self.review, check=False)

    def review_side(self) -> ContainerSide:
        """The review container as a harness talks to it."""
        return ContainerSide(self, self.review)

    def remove(self) -> str:
        """Removes containers, the task's disk (detached, then its image deleted) and socket
        volume, the host's rules for it and its network. Shared caches stay. Returns what the
        host kept (a disk the helper did not detach, its rules), "" else."""
        self._run("docker", "rm", "-f", self.agent, self.sidecar, self.gate, self.review, check=False)
        kept = [self.remove_disk()]
        self._run("docker", "volume", "rm", self.volumes["socket"], check=False)
        # The volumes a task had before its data was on its disk (layout 3), by their old names:
        # a task from an earlier vivibox is deleted here, and nothing else would list them.
        self._run("docker", "volume", "rm", "-f", *self.legacy_volumes(), check=False)
        # The host's rules go before the bridge: one left without them is refused outright.
        kept.append(self.remove_firewall())
        self._run("docker", "network", "rm", self.network, check=False)
        return "; ".join(filter(None, kept))

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
