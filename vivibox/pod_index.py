"""The planner's code index container, I, in a task's pod (podspec.index_command): where its
store, its socket's folder, its source and its copy of the commits are on the task's disk, its
lifecycle with the pod's, its provisioning run, and the wrapper as the planner's harness starts it.
Mixed into Pod, whose task_id, repo, disk, runner, silent, index_server, has_control and network
methods it uses.

I is the task's for its whole life, like the planner's container, and only with it: the wrapper
that reads I runs in the planner's container, so a pod without one has no index.

I is taken to be compromised by the repository from its start, and everything it writes with
it: its store, its copy of the commits and that copy's .git. The host makes I's source, a
read-only view of the commits (reading.view, as for the planner), and runs git there only; it
never runs git in, nor reads, what I writes. What it asks of I's copy is one fixed operation that
runs in I (index_sync), whose exit status is all the host learns; when it fails, I and all it
wrote are discarded, and made again from the view at the pod's next start."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import index, podspec, reading, repo
from .config import DEFAULT_HISTORY_DEPTH
from .pod_network import PodError

# The MCP server the wrapper is in the planner's harness configuration.
MCP_NAME = "index"
# How long past the wrapper's own limits the harness waits for its answer: the wrapper always
# answers first, in its own words, and the harness never gives up on a call the wrapper is still
# bounding (opencode then sends the next call, which waits behind it).
MCP_MARGIN_SECONDS = 30
# The wrapper's package under root, imported in isolated mode: no variable, no user site, no
# folder of the harness's on the path, so nothing of the planner's home is imported in its place.
LAUNCH = (
    "import sys; sys.path.insert(0, {root!r}); from vivibox.index.__main__ import main; "
    "sys.exit(main(sys.argv[1:]))"
)
# The network role of the index's provisioning run (names.ROLES): a range of the task's block,
# with a network and the host's rules only while the run lasts.
INDEX_ROLE = "i"
# The host's own record that I's store was provisioned, with which command: a file of the task's
# disk beside I's folders, never in them, since nothing I writes tells the host anything.
PROVISIONED = "index-provisioned"
INDEX_OWN = "the code index's"


def wrapper_command(
    root: str = podspec.INDEX_WRAPPER_MOUNT, sockets: str = podspec.INDEX_SOCKET_MOUNT
) -> list[str]:
    """The wrapper as the planner's harness starts it, on stdio, in front of the index's socket."""
    return ["python3", "-I", "-c", LAUNCH.format(root=root), sockets]


def mcp_server(limits: index.Limits | None = None) -> dict:
    """The wrapper as an MCP server of the planner's opencode. opencode ends a call at the
    server's `timeout` (milliseconds), or at its MCP library's 60 seconds without one, and also
    waits that long for the server to start; the wrapper may take its connection's and its
    call's limits for one call, so the harness waits past both."""
    limits = limits or index.Limits()
    seconds = limits.connect_seconds + limits.call_seconds + MCP_MARGIN_SECONDS
    return {"type": "local", "command": wrapper_command(), "timeout": int(seconds * 1000)}


def _empty(folder: Path) -> None:
    """folder deleted with all in it and made again, empty, or PodError. What it holds is I's, made
    to be deleted: rmtree walks it without following a link, and I, stopped, changes nothing."""
    if folder.is_symlink():
        raise PodError(f"the code index's folder {folder} is a link; it is not deleted through it")
    try:
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(mode=0o700)
    except (OSError, RecursionError) as e:
        raise PodError(f"the code index's folder {folder} could not be emptied: {e}") from None


class PodIndex:
    # How long the index's operation on its copy (index_sync, by docker exec) and its
    # provisioning run may take; past them, the container is removed.
    SYNC_SECONDS = repo.COPY_TIMEOUT
    PROVISION_SECONDS = 1800.0

    @property
    def index(self) -> str:
        return f"vivibox-{self.task_id}-index"

    @property
    def index_provision(self) -> str:
        return f"vivibox-{self.task_id}-index-provision"

    @property
    def index_store(self) -> Path:
        """I's own store, its home: a folder of the task's disk that no other container mounts."""
        return self.disk / podspec.INDEX_STORE

    @property
    def index_sockets(self) -> Path:
        """Where I makes its socket: mounted in I and, read-only, in the planner's container."""
        return self.disk / podspec.INDEX_SOCKETS

    @property
    def index_source(self) -> Path:
        """The host's view of the commits for I, mounted read-only in I alone."""
        return self.disk / podspec.INDEX_SOURCE

    @property
    def index_copy(self) -> Path:
        """I's private copy of the commits, which it writes: mounted in I alone."""
        return self.disk / podspec.INDEX_COPY

    @property
    def index_provisioned(self) -> Path:
        return self.disk / PROVISIONED

    @property
    def has_index(self) -> bool:
        return self.index_server is not None

    def index_command(self, commit: str) -> list[str]:
        if not self.has_index:
            raise PodError(f"{self.task_id} has no code index")
        try:
            return podspec.index_command(self, self.index_server, commit)
        except ValueError as e:
            raise PodError(f"the code index could not start: {e}") from None

    def provision_command(self, commit: str) -> list[str]:
        net = self.recorded_network(INDEX_ROLE)
        if net is None:
            raise PodError(
                f"{self.task_id}'s addresses were allocated before the code index had a range of its "
                f"own, so its index cannot be provisioned; delete the task and create it again: "
                f"vivibox delete {self.task_id}"
            )
        try:
            return podspec.provision_command(self, self.index_server, commit, net, self.ca_mounts())
        except ValueError as e:
            raise PodError(f"the code index could not be provisioned: {e}") from None

    def _index_folders(self) -> None:
        """I's folders, made before the planner's container, which mounts the socket's: a link in
        the place of any of them is never mounted, nor made a view in."""
        if not self.has_index:
            return
        if not self.has_control:
            raise PodError(
                f"{self.task_id} has a code index and no planner's container: only the planner reads it"
            )
        for folder in (self.index_store, self.index_sockets, self.index_source, self.index_copy):
            if folder.is_symlink():
                raise PodError(f"the code index's folder {folder} is a link; it is not mounted")
            try:
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as e:
                raise PodError(f"the code index's folder {folder} could not be made: {e}") from None

    def _index_view(self, commit: str, depth: int) -> str:
        """The host's view for I on commit, else on the task's branch as it is (reading.view: the
        planner's machinery, in a folder of its own); the commit it stands on."""
        branch = repo.branch_name(self.task_id)
        reading.view(self.repo, branch, self.index_source, commit, INDEX_OWN, depth)
        own = reading.own_git(self.index_source)
        head = repo.git("rev-parse", "--verify", "HEAD^{commit}", cwd=self.index_source, env=own)
        return head.stdout.strip()

    def _index_up(self) -> None:
        """I, after the planner's container: running, it is left as it is. Else made again from
        the host's view of the branch, provisioned first when its program needs it and its store
        is not. A start that fails leaves I down, with why in index_failure; the pod goes on: the
        wrapper says the index is not available."""
        if not self.has_index or self._state(self.index) == "running":
            return
        self._run("docker", "rm", "-f", self.index, check=False)
        try:
            # A run a vivibox left when it stopped in it: never taken for done.
            leftover = self._state(self.index_provision) is not None or self._network_there(INDEX_ROLE)
            if leftover and (left := self._provision_gone()):
                raise PodError(left)
            head = self._index_view("", DEFAULT_HISTORY_DEPTH)
            if self.index_server.provision and not self._provisioned():
                self._provision(head)
            self._run(*self.index_command(head))
        except (PodError, repo.RepoError) as e:
            self._run("docker", "rm", "-f", self.index, check=False)
            self.index_failure = str(e)
            return
        self.index_failure = ""

    def index_refresh(self, commit: str = "", depth: int = DEFAULT_HISTORY_DEPTH) -> bool:
        """I's copy brought to the planner's commit before a turn: the host's view first, then
        index_sync in I. True once I says its copy is there; else I is down (its copy behind the
        planner's is never answered from), and, when its own operation failed, all it wrote is
        discarded. I not running is left so: the pod's next start makes it."""
        if not self.has_index or self._state(self.index) != "running":
            return False
        try:
            head = self._index_view(commit, depth)
        except (PodError, repo.RepoError) as e:
            self._run("docker", "rm", "-f", self.index, check=False)
            self.index_failure = f"the code index's view of the commits could not be made: {e}"
            return False
        cmd = ["docker", "exec", self.index, *podspec.index_sync_command(self, head)]
        try:
            synced = self.silent(cmd, self.SYNC_SECONDS) == 0
        except subprocess.TimeoutExpired:
            synced = False
        if synced:
            return True
        try:
            self.index_discard()
        except PodError as e:
            self.index_failure = str(e)
            return False
        self.index_failure = (
            "the code index could not bring its copy of the commits up to date; it starts again from scratch"
        )
        return False

    def index_discard(self) -> None:
        """I and its provisioning run removed, its copy and its store deleted and made empty, the
        host's record of its provisioning with them: nothing I wrote is used again. The host's
        view stays, the host's own."""
        self._run("docker", "rm", "-f", self.index, self.index_provision, check=False)
        self.index_provisioned.unlink(missing_ok=True)
        for folder in (self.index_store, self.index_copy):
            _empty(folder)

    # --- the provisioning run ---------------------------------------------------------------

    def _provisioned(self) -> bool:
        try:
            return self.index_provisioned.read_text() == json.dumps(list(self.index_server.provision))
        except OSError:
            return False

    def _provision(self, commit: str) -> None:
        """The provisioning run, I stopped: its own container on the code index's network, made
        with the host's rules for it before it starts, and removed with them after it ends, done
        or not. Recorded as done once it exited 0 and nothing of it is left."""
        command = self.provision_command(commit)
        net = self.recorded_network(INDEX_ROLE)
        self.index_provisioned.unlink(missing_ok=True)
        code = None
        try:
            if not self._network_there(INDEX_ROLE) and (said := self._make(INDEX_ROLE, net.subnet)):
                raise PodError(f"docker network create {self.network_name(INDEX_ROLE)}: {said}")
            self.check_bridge(net)
            try:
                self.apply_firewall(extra=(INDEX_ROLE,))
            except PodError as e:
                raise PodError(f"the code index's provisioning could not start: {e}") from None
            self._run(*command)
            code = self._provision_wait()
        finally:
            left = self._provision_gone()
        if left:
            raise PodError(left)
        if code != 0:
            ended = "ran out of time" if code is None else f"ended with exit status {code}"
            raise PodError(f"the code index's provisioning run {ended}")
        self.index_provisioned.write_text(json.dumps(list(self.index_server.provision)))

    def _provision_wait(self) -> int | None:
        """The run's exit status, as Docker gives it; None past PROVISION_SECONDS or unknown."""
        try:
            done = self._run(
                "docker", "wait", self.index_provision, check=False, timeout=self.PROVISION_SECONDS
            )
        except subprocess.TimeoutExpired:
            return None
        word = done.stdout.strip()
        return int(word) if done.returncode == 0 and word.isdigit() else None

    def _provision_stop(self) -> None:
        """The run's container and its network gone, at a stop of the pod: the rules for a bridge
        that is not there hold nothing, and the next run loads them again."""
        self._run("docker", "rm", "-f", self.index_provision, check=False)
        self._run("docker", "network", "rm", self.network_name(INDEX_ROLE), check=False)

    def _provision_gone(self) -> str:
        """The run's container, its network and the host's rules for it gone: "" once they are,
        else what is left."""
        self._provision_stop()
        left = []
        if self._state(self.index_provision) is not None:
            left.append(f"the container {self.index_provision} is still there")
        if self._network_there(INDEX_ROLE):
            left.append(f"the network {self.network_name(INDEX_ROLE)} is still there")
        if why := self.remove_firewall(roles=(INDEX_ROLE,)):
            left.append(why)
        if left:
            return "the code index's provisioning run was not cleaned up: " + "; ".join(left)
        return ""
