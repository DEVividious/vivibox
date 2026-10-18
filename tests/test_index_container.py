"""The planner's code index container I: what it mounts, what it is given, what it is not, and
what of it the planner's container mounts. I is taken to be compromised by the repository from
its start, and every role runs as the same user, so what keeps it from the others is that
nothing of theirs is mounted in it and it has no network: the mount matrix below is that
boundary, written down."""

import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hostile_index import MARK, HostileIndex
from index_harness import INITIALIZE, INITIALIZED, VALID, call, listing
from test_pod import FakeDocker, flag, starts

import vivibox
from vivibox import index, pod_index, pod_network, podspec
from vivibox.index import channel, tools
from vivibox.index.session import Session
from vivibox.pod import Mount, Pod, PodError
from vivibox.podspec import IndexServer, binds

SERVER = IndexServer(
    mounts=(Mount("/srv/index-server", "/opt/index", read_only=True),),
    command=("python3", "/opt/index/server.py"),
)
COMMIT = "a" * 40


def git(where: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(clone: Path, files: dict[str, str | None], message: str = "work") -> str:
    """A commit of the writer's on the task's branch: each file written, or deleted for None."""
    for name, text in files.items():
        path = clone / name
        if text is None:
            git(clone, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(clone, "add", name)
    git(
        clone,
        "-c",
        "user.name=W",
        "-c",
        "user.email=w@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
    )
    return git(clone, "rev-parse", "HEAD")


def a_clone(repo: Path) -> str:
    """The task's clone on its branch, with one commit; the commit."""
    if (repo / ".git").exists():
        return git(repo, "rev-parse", "HEAD")
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "vivibox/shop-1", str(repo)], check=True)
    return commit(repo, {"README.md": "shop\n"}, "start")


def a_pod(tmp_path: Path, control: bool = True, server: IndexServer | None = SERVER) -> Pod:
    repo = tmp_path / "repo"
    a_clone(repo)
    docker = FakeDocker()
    docker.sys_net = pod_network.SYS_NET
    return Pod(
        "shop-1", repo, "vivibox-agent:abc", [], runner=docker, folder=tmp_path,
        gate_dir=tmp_path / "gate", review_dir=tmp_path / "disk" / "review",
        control_mounts=[Mount(str(tmp_path / ".task"), "/task", read_only=True)] if control else None,
        index_server=server,
    )  # fmt: skip


def commands(pod: Pod) -> dict[str, list[str]]:
    pod.ensure_networks()
    return {
        "sidecar": pod.sidecar_command(),
        "agent": pod.agent_command(),
        "gate": pod.gate_command(),
        "review": pod.review_command([], {}),
        "control": pod.control_command(),
        "index": pod.index_command(COMMIT),
        "provision": pod.provision_command(COMMIT),
    }


def sources(cmd: list[str]) -> dict[str, str]:
    """Each bind of a docker run command, source to its target with its mode."""
    found = {}
    for bind in binds(cmd):
        source, _, rest = bind.partition(":")
        found[source] = rest
    return found


def test_only_the_index_mounts_its_folders_and_only_it_and_the_planner_its_sockets(tmp_path):
    """The mount matrix: I's store, its copy of the commits and the host's view for it in I and
    its provisioning run alone; the socket directory in I read-write and in the planner's
    container read-only, in no other container (not the provisioning run); nothing in the
    writer's, the gate's, the sidecar's (whose nested containers see only what it mounts) or the
    reviewer's."""
    pod = a_pod(tmp_path)
    store, sockets = str(pod.index_store), str(pod.index_sockets)
    source, copy = str(pod.index_source), str(pod.index_copy)
    index_only = (store, source, copy)
    made = commands(pod)
    for role, cmd in made.items():
        found = sources(cmd)
        mine = role in ("index", "provision")
        assert not [f for f in index_only if f in found] or mine, role
        assert sockets not in found or role in ("index", "control"), role
        assert not [s for s in found if any(s.startswith(f + "/") for f in (*index_only, sockets))], role
        # Nor a folder above any: the task's disk, its folder, the tasks' folder, the root.
        parents = {p for f in (*index_only, sockets) for p in Path(f).parents}
        above = [s for s in found if s.startswith("/") and Path(s) in parents]
        assert above == [], (role, above)
    for role in ("index", "provision"):
        found = sources(made[role])
        assert found[store] == "/config"
        assert found[copy] == str(pod.repo), "at the path the planner reads its own copy"
        assert found[source] == f"{podspec.INDEX_SOURCE_MOUNT}:ro"
    assert sources(made["index"])[sockets] == podspec.INDEX_SOCKET_MOUNT
    assert sockets not in sources(made["provision"]), "nothing reads the socket while it runs"
    assert sources(made["control"])[sockets] == f"{podspec.INDEX_SOCKET_MOUNT}:ro"
    assert source not in sources(made["control"]) and str(pod.control_src) not in sources(made["index"])


def test_the_index_mounts_its_folders_and_its_program_read_only_and_nothing_else(tmp_path):
    """Its store, its copy, the host's view read-only and its socket's folder; nothing of the
    planner's (its home, its copy, its working directory, the task's files, the handoff, its
    secrets), nothing of the writer's (the clone, its home, the Docker socket, the caches, what a
    build installed), nothing of the reviewer's or the gate's, no CA bundle (it has no network to
    use one on)."""
    pod = a_pod(tmp_path)
    cmd = pod.index_command(COMMIT)
    assert binds(cmd) == [
        f"{pod.index_store}:/config",
        f"{pod.index_copy}:{pod.repo}",
        f"{pod.index_source}:/source:ro",
        "/srv/index-server:/opt/index:ro",
        f"{pod.index_sockets}:{podspec.INDEX_SOCKET_MOUNT}",
    ]
    assert "--mount" not in cmd and "--volumes-from" not in cmd


def test_a_program_of_the_index_mounted_writable_is_refused(tmp_path):
    writable = IndexServer(mounts=(Mount("/srv/x", "/opt/index"),), command=("x",))
    with pytest.raises(PodError, match="read-only"):
        a_pod(tmp_path, server=writable).index_command(COMMIT)


def test_the_index_has_no_network_no_capability_no_variable_and_the_tasks_limits(tmp_path):
    pod = a_pod(tmp_path)
    cmd = pod.index_command(COMMIT)
    assert cmd[:5] == ["docker", "run", "-d", "--name", "vivibox-shop-1-index"]
    assert flag(cmd, "--network") == "none"
    for absent in ("--add-host", "--ip", "-e", "--env", "--env-file", "-p", "--publish",
                   "--privileged", "--cap-add", "--device", "--pid", "--userns", "--runtime"):  # fmt: skip
        assert absent not in cmd, absent
    assert not [a for a in cmd if a.startswith("--restart") or a.startswith("--network=")]
    assert flag(cmd, "--cap-drop") == "ALL"
    assert flag(cmd, "--security-opt") == "no-new-privileges"
    assert flag(cmd, "--ipc") == "private"
    assert "--read-only" in cmd and "--entrypoint" not in cmd, "the image's tini reaps and passes a stop on"
    half = pod.limits.memory_of("index") // 2
    assert flag(cmd, "--tmpfs") == f"/tmp:exec,mode=1777,size={half}"
    assert flag(cmd, "--cgroup-parent") == "vivibox-shop_1.slice"
    assert flag(cmd, "--memory") == flag(cmd, "--memory-swap") == str(pod.limits.memory_of("index"))
    assert flag(cmd, "--pids-limit") == str(pod.limits.pids_of("index"))
    assert flag(cmd, "--label") == "vivibox.task=shop-1"
    # No server of the host's in its resolv.conf, nor the host's search domains (a VPN's, an
    # employer's): it could put them in an answer the planner reads.
    assert flag(cmd, "--dns") == "127.0.0.1" and flag(cmd, "--dns-search") == "."
    # What its program prints is Docker's log on the host's disk: bounded, outside the task's disk.
    logs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--log-opt"]
    assert flag(cmd, "--log-driver") == "json-file" and logs == ["max-size=1m", "max-file=1"]
    # Its copy synced to the commit by the host's fixed script, then its program.
    script = (Path(vivibox.__file__).parent / "index_sync.py").read_text()
    at = cmd.index(pod.image)
    assert cmd[at:] == [
        pod.image, "python3", "-I", "-c", script, str(pod.repo), COMMIT,
        "--", "python3", "/opt/index/server.py",
    ]  # fmt: skip


def test_the_planner_mounts_the_sockets_and_the_wrapper_read_only_only_with_an_index(tmp_path):
    pod = a_pod(tmp_path)
    pod.ensure_networks()
    with_index = sources(pod.control_command())
    assert with_index[str(pod.index_sockets)] == f"{podspec.INDEX_SOCKET_MOUNT}:ro"
    wrapper = str(Path(index.__file__).parent)
    assert with_index[wrapper] == f"{podspec.INDEX_WRAPPER_MOUNT}/vivibox/index:ro"
    pod = a_pod(tmp_path, server=None)
    pod.ensure_networks()
    without = sources(pod.control_command())
    assert wrapper not in without and str(pod.index_sockets) not in without


def test_the_index_starts_after_the_planner_and_goes_with_the_pod(tmp_path):
    pod = a_pod(tmp_path)
    pod.up()
    started = starts(pod.runner.calls)
    assert started.index("vivibox-shop-1-control") < started.index("vivibox-shop-1-index")
    assert pod.index_store.is_dir() and pod.index_sockets.is_dir()
    pod.runner.calls.clear()
    pod.up()
    assert "vivibox-shop-1-index" not in starts(pod.runner.calls), "running: left as it is"
    pod.down()
    assert ["docker", "stop", "-t", "10", "vivibox-shop-1-index"] in pod.runner.calls
    pod.kill()
    assert any("vivibox-shop-1-index" in c for c in pod.runner.find("docker", "kill"))
    pod.runner.calls.clear()
    pod.remove()
    assert any("vivibox-shop-1-index" in c for c in pod.runner.find("docker", "rm", "-f"))


def test_a_stop_and_a_forced_stop_take_the_index_whatever_the_pod_was_made_with(tmp_path):
    """A pod made without the index's program (a command that names none, an index switched off
    since) still stops and kills one the task has running, as remove removes it."""
    pod = a_pod(tmp_path, server=None)
    pod.down()
    assert ["docker", "stop", "-t", "10", "vivibox-shop-1-index"] in pod.runner.calls
    pod.kill()
    assert any("vivibox-shop-1-index" in c for c in pod.runner.find("docker", "kill"))


def test_a_pod_without_an_index_makes_none_and_an_index_needs_a_planner(tmp_path):
    pod = a_pod(tmp_path, server=None)
    pod.up()
    assert "vivibox-shop-1-index" not in starts(pod.runner.calls)
    assert not pod.index_store.exists()
    with pytest.raises(PodError, match="no code index"):
        pod.index_command(COMMIT)
    alone = a_pod(tmp_path, control=False)
    with pytest.raises(PodError, match="planner"):
        alone.up()


# The modules of the package that may name what gives a pod a code index: the index's own.
INDEX_OWN = ("pod_index.py", "serena_index.py")


def index_switches(source: str) -> list[str]:
    """What in source would give a pod a code index: an index_server set or passed, Serena's
    index named, or the wrapper put in a harness's configuration."""
    found = []
    for node in ast.walk(ast.parse(source)):
        attribute = isinstance(node, ast.Attribute)
        if attribute and node.attr == "index_server" and isinstance(node.ctx, ast.Store):
            found.append("index_server set")
        elif isinstance(node, ast.keyword) and node.arg == "index_server":
            found.append("index_server passed")
        elif attribute and node.attr in ("serena_index", "mcp_server"):
            found.append(f"{node.attr} named")
        elif isinstance(node, ast.Name) and node.id == "serena_index":
            found.append("serena_index named")
        elif isinstance(node, ast.alias) and node.name.endswith("serena_index"):
            found.append("serena_index imported")
    return found


def test_nothing_in_the_package_gives_a_pod_a_code_index():
    """The code index is off in this release, with no setting that turns it on: a pod the
    product makes has no index container, no provisioning run, no network or rules for one and
    none of its memory, and the planner's harness has no index server. Turning it on is a
    decision of its own, and changes this test with it."""
    package = Path(vivibox.__file__).parent
    found = {}
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package)
        if rel.parts[0] in ("index", "index_program") or rel.name in INDEX_OWN:
            continue
        if switches := index_switches(path.read_text()):
            found[str(rel)] = switches
    assert found == {}


@pytest.mark.parametrize(
    "source",
    [
        "pod.index_server = serena.server(repo)",
        "Pod(task_id, repo, image, index_server=server)",
        "from . import serena_index",
        "config = {'mcp': {'index': pod_index.mcp_server()}}",
    ],
)
def test_the_guard_on_the_index_sees_each_way_of_switching_it_on(source):
    assert index_switches(source)


@pytest.mark.parametrize("which", ["index_store", "index_sockets", "index_source", "index_copy"])
def test_a_folder_of_the_index_that_is_a_link_is_not_mounted(tmp_path, which):
    pod = a_pod(tmp_path)
    folder = getattr(pod, which)
    folder.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "elsewhere").mkdir()
    folder.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PodError, match="link"):
        pod.up()
    assert "vivibox-shop-1-index" not in starts(pod.runner.calls)
    assert "vivibox-shop-1-control" not in starts(pod.runner.calls)


def test_the_planners_harness_waits_for_a_call_as_long_as_the_wrapper_may_take(tmp_path):
    """opencode ends an MCP call at the server's `timeout`, or at the MCP SDK's 60 seconds
    without one, and sends the next call meanwhile; the wrapper answers one call at a time, for
    up to its connection's and its call's limits. The harness's limit is past both."""
    entry = pod_index.mcp_server()
    limits = index.Limits()
    assert entry["type"] == "local" and entry.get("enabled", True)
    assert entry["timeout"] >= (limits.connect_seconds + limits.call_seconds) * 1000 + 10_000
    assert entry["command"][:3] == ["python3", "-I", "-c"]
    assert entry["command"][-1] == podspec.INDEX_SOCKET_MOUNT
    assert "environment" not in entry


def test_the_wrappers_command_runs_the_wrapper_in_front_of_the_socket(tmp_path):
    """The command as the harness starts it, with the wrapper's folder where the planner's
    container mounts it: the harness's three messages and a call reach the index, and the
    answer comes back."""
    folder = tmp_path / "sockets"
    folder.mkdir()
    hostile = HostileIndex(folder)
    root = Path(index.__file__).parent.parent.parent
    command = pod_index.wrapper_command(str(root), str(folder))
    lines = [INITIALIZE, INITIALIZED, listing(2), call(3, "find_symbol", VALID["find_symbol"])]
    try:
        done = subprocess.run(
            [sys.executable if c == "python3" else c for c in command],
            input="".join(json.dumps(m) + "\n" for m in lines),
            capture_output=True, text=True, timeout=30, cwd=tmp_path,
        )  # fmt: skip
    finally:
        hostile.close()
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3], done.stderr
    assert replies[1]["result"]["tools"] == tools.listed()
    assert "answer of find_symbol" in replies[2]["result"]["content"][0]["text"]
    assert MARK not in done.stdout
    assert len(hostile.calls()) == 1


def test_a_slow_index_answers_after_its_delay(tmp_path):
    """The hostile index's slow mode, which the live test of the harness's waiting uses."""
    folder = tmp_path / "sockets"
    folder.mkdir()
    hostile = HostileIndex(folder, mode="slow:0.3")
    try:
        started = time.monotonic()
        parts, failed = Session(channel.unix_entry(str(folder), "index.sock")).call("find_symbol", {})
        assert time.monotonic() - started >= 0.3
        assert not failed and parts == ["answer of find_symbol: {}"]
    finally:
        hostile.close()


def test_the_wrapper_makes_itself_undumpable_before_it_connects(tmp_path):
    """A peer of a unix socket can ask the kernel for the other end's process (SO_PEERPIDFD)
    and, through it, for handles to that process's namespaces, which its user may take of its
    own processes. The wrapper's process is made undumpable at its start: its /proc entries
    become root's, and the index gets no handle of the planner's container through it."""
    folder = tmp_path / "sockets"
    folder.mkdir()
    root = Path(index.__file__).parent.parent.parent
    command = pod_index.wrapper_command(str(root), str(folder))
    wrapper = subprocess.Popen(
        [sys.executable if c == "python3" else c for c in command],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=tmp_path,
    )  # fmt: skip
    try:
        wrapper.stdin.write(json.dumps(INITIALIZE).encode() + b"\n")
        wrapper.stdin.flush()
        assert json.loads(wrapper.stdout.readline())["id"] == 1, "started and serving"
        assert Path(f"/proc/{wrapper.pid}/environ").stat().st_uid == 0
    finally:
        wrapper.kill()
        wrapper.wait()
