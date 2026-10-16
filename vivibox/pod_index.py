"""The planner's code index container, I, in a task's pod (podspec.index_command): where its
store and its socket's folder are on the task's disk, its lifecycle with the pod's, and the
wrapper as the planner's harness starts it. Mixed into Pod, whose task_id, disk, runner,
index_server and has_control it uses.

I is the task's for its whole life, like the planner's container, and only with it: the wrapper
that reads I runs in the planner's container, so a pod without one has no index."""

from __future__ import annotations

from pathlib import Path

from . import index, podspec
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


class PodIndex:
    @property
    def index(self) -> str:
        return f"vivibox-{self.task_id}-index"

    @property
    def index_store(self) -> Path:
        """I's own store, its home: a folder of the task's disk that no other container mounts."""
        return self.disk / podspec.INDEX_STORE

    @property
    def index_sockets(self) -> Path:
        """Where I makes its socket: mounted in I and, read-only, in the planner's container."""
        return self.disk / podspec.INDEX_SOCKETS

    @property
    def has_index(self) -> bool:
        return self.index_server is not None

    def index_command(self) -> list[str]:
        if not self.has_index:
            raise PodError(f"{self.task_id} has no code index")
        try:
            return podspec.index_command(self, self.index_server)
        except ValueError as e:
            raise PodError(f"the code index could not start: {e}") from None

    def _index_folders(self) -> None:
        """I's store and its socket's folder, made before the planner's container, which mounts
        the second; a link in either place is never mounted."""
        if not self.has_index:
            return
        if not self.has_control:
            raise PodError(
                f"{self.task_id} has a code index and no planner's container: only the planner reads it"
            )
        for folder in (self.index_store, self.index_sockets):
            if folder.is_symlink():
                raise PodError(f"the code index's folder {folder} is a link; it is not mounted")
            try:
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as e:
                raise PodError(f"the code index's folder {folder} could not be made: {e}") from None

    def _index_up(self) -> None:
        """I, after the planner's container: running, it is left as it is."""
        if not self.has_index or self._state(self.index) == "running":
            return
        self._run("docker", "rm", "-f", self.index, check=False)
        self._run(*self.index_command())
