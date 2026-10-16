"""The planner's code index container I on a real pod, run by a fully compromised index (the
hostile index of tests/hostile_index.py as I's program): what it can see, reach and change from
inside, and that the planner's wrapper still answers through the socket it checked.

Canaries are planted in every place I must not reach: the planner's home and secrets, the task's
files and the handoff, the writer's clone and home, the reviewer's folder, another task's
folder, a unix socket of the host's in the abstract namespace. Needs host/setup.sh to have run,
Docker with runc and sysbox-runc, and internet access (images, opencode's provider package).
Run with: uv run pytest -m docker tests/test_index_live.py
"""

from __future__ import annotations

import json
import secrets as token
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import hostile_index
import index_probe
import pytest
from index_harness import INITIALIZE, INITIALIZED, VALID, call, listing
from test_control_live import MOCK_LOG, MOCK_MODEL, a_pod, a_task, asked, gone, provider_up
from test_pod_live import TASKS_ROOT, own_folder

from vivibox import control, image, opencode, pod_index, podspec, secrets
from vivibox.index import tools
from vivibox.pod import ContainerSide, Mount
from vivibox.pod_egress import helper_missing
from vivibox.podspec import IndexServer

pytestmark = pytest.mark.docker

PROGRAM = "/opt/index"
CANARY = {
    where: f"CANARY-{where}-{token.token_hex(6)}"
    for where in (
        "planner-home",
        "planner-secret",
        "task",
        "handoff",
        "clone",
        "writer-home",
        "review",
        "other-task",
    )
}


def index_side(pod) -> ContainerSide:
    return ContainerSide(pod, pod.index)


def wait_for_socket(pod, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    entry = f"{podspec.INDEX_SOCKET_MOUNT}/{podspec.INDEX_SOCKET}"
    while time.monotonic() < deadline:
        if index_side(pod).exec("test", "-S", entry, check=False).returncode == 0:
            return
        time.sleep(0.2)
    logs = subprocess.run(["docker", "logs", "--tail", "20", pod.index], capture_output=True, text=True)
    raise AssertionError(f"the index made no socket: {logs.stdout}{logs.stderr}")


def mode(pod, chosen: str) -> None:
    """The mode of the index's next calls, set from inside I, where anything may be written."""
    index_side(pod).exec("sh", "-c", f"printf %s '{chosen}' > /config/mode")
    time.sleep(0.3)


@pytest.fixture(scope="module")
def live():
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    ref, _ = image.build()
    task = a_task("indexcheck")
    pod = a_pod(task, ref)
    program = task.root / "index-program"
    program.mkdir()
    shutil.copy(hostile_index.__file__, program / "hostile_index.py")
    shutil.copy(index_probe.__file__, program / "index_probe.py")
    pod.index_server = IndexServer(
        mounts=(Mount(str(program), PROGRAM, read_only=True),),
        command=("python3", f"{PROGRAM}/hostile_index.py", podspec.INDEX_SOCKET_MOUNT),
    )
    harness = control.harness_dir(task) / "opencode.json"
    settings = json.loads(harness.read_text())
    harness.write_text(
        json.dumps({**settings, "mcp": {pod_index.MCP_NAME: pod_index.mcp_server()}}, indent=2)
    )
    other = own_folder(f"other{token.token_hex(3)}-1")
    abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abstract.bind(f"\0vivibox-canary-{token.token_hex(4)}")
    abstract.listen(1)
    try:
        pod.up()
        control.refresh(task, pod)
        plant(task, pod, other)
        wait_for_socket(pod)
        yield SimpleNamespace(
            task=task, pod=pod, planner=pod.control_side(), index=index_side(pod), other=other,
            abstract=abstract.getsockname(),
        )  # fmt: skip
    finally:
        abstract.close()
        shutil.rmtree(other, ignore_errors=True)
        gone(task, pod)


def plant(task, pod, other: Path) -> None:
    """A canary in every place of the task's, and of another task's, that the index must not reach."""
    (pod.control_home / "canary").write_text(CANARY["planner-home"])
    (secrets.runtime_dir(control.secrets_id(task.id)) / "canary").write_text(CANARY["planner-secret"])
    (task.meta / "canary").write_text(CANARY["task"])
    (task.meta / "handoff" / "canary").write_text(CANARY["handoff"])
    (task.repo / "canary.txt").write_text(CANARY["clone"])
    pod.exec("sh", "-c", f"printf %s {CANARY['writer-home']} > /config/canary")
    pod.review_dir.mkdir(parents=True, exist_ok=True)
    (pod.review_dir / "canary").write_text(CANARY["review"])
    (other / "canary").write_text(CANARY["other-task"])


def probe(live, attempts: list[dict] | None = None, side=None, canaries: list[str] = ()) -> dict:
    asked_for = json.dumps({"canaries": [*CANARY.values(), *canaries], "tries": attempts or []})
    done = (side or live.index).exec("python3", f"{PROGRAM}/index_probe.py", asked_for, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def bind_mounts(report: dict) -> dict[str, str]:
    """The index's mounts that come from a folder of the host's, mount point to its options."""
    return {m["point"]: m["options"] for m in report["mounts"] if not usual(m)}


# The kernel's own file systems a container has, at the places Docker puts them: one of these
# types anywhere else (a host's /proc mounted in) is a mount like any other.
USUAL = {
    "/proc": "proc", "/sys": "sysfs", "/sys/fs/cgroup": "cgroup2", "/dev/pts": "devpts",
    "/dev/mqueue": "mqueue",
}  # fmt: skip


def usual(m: dict) -> bool:
    """Docker's mounts of the kernel's file systems, and its masks over parts of /proc and /sys."""
    if USUAL.get(m["point"]) == m["type"]:
        return True
    masked = m["point"].startswith(("/proc/", "/sys/")) and m["type"] in ("tmpfs", "proc", "sysfs")
    return masked and m["point"] not in ("/proc", "/sys")


def wrapper(live, *lines: dict, side=None) -> list[dict]:
    """The wrapper in the planner's container as its harness starts it, given these lines."""
    done = subprocess.run(
        ["docker", "exec", "-i", (side or live.planner).agent, *pod_index.wrapper_command()],
        input="".join(json.dumps(m) + "\n" for m in lines), capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return [json.loads(one) for one in done.stdout.splitlines()]


def six_calls(live) -> list[dict]:
    lines = [INITIALIZE, INITIALIZED, listing(2)]
    lines += [call(10 + i, name, VALID[name]) for i, name in enumerate(VALID)]
    return wrapper(live, *lines)


# --- what the index holds and sees ----------------------------------------------------------


def test_the_index_mounts_its_store_its_sockets_and_its_program_and_nothing_else(live):
    report = probe(live)
    found = bind_mounts(report)
    assert set(found) == {
        "/", "/dev", "/dev/shm", "/tmp", "/config", podspec.INDEX_SOCKET_MOUNT, PROGRAM,
        "/etc/hosts", "/etc/hostname", "/etc/resolv.conf",
    }, found  # fmt: skip
    assert found["/"].startswith("ro") and found[PROGRAM].startswith("ro")
    assert found["/config"].startswith("rw") and found[podspec.INDEX_SOCKET_MOUNT].startswith("rw")
    roots = {m["point"]: m["root"] for m in report["mounts"]}
    assert roots["/config"] == f"/{podspec.INDEX_STORE}" and roots[podspec.INDEX_SOCKET_MOUNT] == (
        f"/{podspec.INDEX_SOCKETS}"
    ), "folders of the task's own disk"


def test_no_canary_of_the_planner_the_writer_the_reviewer_or_another_task_is_in_the_index(live):
    own = f"CANARY-index-own-{token.token_hex(6)}"
    live.index.exec("sh", "-c", f"printf %s {own} > /config/own-canary")
    report = probe(live, canaries=[own])
    assert report["canaries"] == [f"/config/own-canary: {own}"], "the probe finds one where there is one"
    for path in (
        "/task",
        "/task/handoff",
        "/work",
        str(live.task.repo),
        podspec.SOCKET_DIR,
        "/var/run/docker.sock",
    ):
        assert live.index.exec("test", "-e", path, check=False).returncode != 0, path


def test_the_index_has_no_network_but_loopback_and_reaches_nothing(live):
    pod = live.pod
    nets = {role: pod.recorded_network(role) for role in ("w", "r", "p")}
    attempts = [
        {"kind": "tcp", "host": nets["p"].address, "port": opencode.PORT},  # the planner's server
        {"kind": "tcp", "host": nets["w"].address, "port": opencode.PORT},  # the writer's
        {"kind": "tcp", "host": nets["r"].address, "port": opencode.REVIEW_PORT},  # the reviewer's
        *({"kind": "tcp", "host": n.gateway, "port": 22} for n in nets.values()),  # the host
        {"kind": "udp", "host": pod.host_ip, "port": 53},  # the host's forwarder
        {"kind": "tcp", "host": pod.host_ip, "port": 53},
        {"kind": "tcp", "host": "1.1.1.1", "port": 443},  # the internet
        {"kind": "dns", "host": "example.com"},
    ]
    report = probe(live, attempts)
    assert report["interfaces"] == ["lo"]
    for done in report["tries"]:
        assert done["result"] != "reached", done
        assert done["result"] in ("ENETUNREACH", "EAI_AGAIN", "EAI_NONAME"), done


def test_the_index_reaches_no_unix_socket_but_its_own(live):
    """Not the Docker socket, not the host's, not another task's: none is mounted, and an
    abstract one is of the network namespace, which the index does not share."""
    attempts = [
        {"kind": "unix", "path": "/var/run/docker.sock"},
        {"kind": "unix", "path": podspec.SOCKET},
        {"kind": "unix", "path": "@" + live.abstract[1:].decode()},
    ]
    report = probe(live, attempts)
    assert [d["result"] for d in report["tries"]] == ["ENOENT", "ENOENT", "ECONNREFUSED"], report["tries"]
    assert report["sockets_anywhere"] == [f"{podspec.INDEX_SOCKET_MOUNT}/{podspec.INDEX_SOCKET}"]
    assert all(path.startswith("@") is False or "vivibox-canary" not in path for path in report["unix"])


def test_the_index_writes_its_store_its_sockets_and_its_scratch_only(live):
    report = probe(live)
    # /dev/shm and /dev/mqueue are its own IPC namespace's (--ipc private).
    assert set(report["writable"]) == {
        "/config",
        podspec.INDEX_SOCKET_MOUNT,
        "/tmp",
        "/dev/shm",
        "/dev/mqueue",
    }


def test_the_index_has_no_capability_no_variable_of_vivibox_s_and_sees_its_own_processes(live):
    report = probe(live)
    assert report["status"]["CapEff"] == "0000000000000000"
    assert report["status"]["CapPrm"] == "0000000000000000"
    assert report["status"]["NoNewPrivs"] == "1" and report["status"]["Seccomp"] == "2"
    assert report["status"]["Uid"].split()[0] == "1000"
    assert not [p for p in report["processes"] if "opencode" in p or "sleep infinity" in p]
    secret_like = [
        n for n in report["environment"] if any(w in n for w in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
    ]
    assert secret_like == [] and "OPENCODE_CONFIG" not in report["environment"]


def test_the_planner_sees_the_sockets_and_the_wrapper_read_only(live):
    mounted = live.planner.exec("cat", "/proc/self/mountinfo").stdout.splitlines()
    channel = {line.split()[4]: line.split()[5] for line in mounted}
    assert channel[podspec.INDEX_SOCKET_MOUNT].startswith("ro")
    assert channel[f"{podspec.INDEX_WRAPPER_MOUNT}/vivibox/index"].startswith("ro")
    assert live.planner.exec("test", "-w", podspec.INDEX_SOCKET_MOUNT, check=False).returncode != 0


def test_neither_the_writer_nor_its_nested_containers_see_the_index(live):
    pod = live.pod
    assert pod.exec("test", "-e", podspec.INDEX_SOCKET_MOUNT, check=False).returncode != 0
    mounted = pod.exec("cat", "/proc/self/mountinfo").stdout
    assert f"/{podspec.INDEX_SOCKETS}" not in mounted and f"/{podspec.INDEX_STORE} " not in mounted
    found = pod.exec(
        "docker", "run", "--rm", "-v", "/:/host", "alpine:3", "sh", "-c",
        # Into every mount of the sidecar's (no -xdev): one of the task's disk would hold both.
        "find /host \\( -path /host/proc -o -path /host/sys -o -path /host/dev "
        "-o -path /host/var/lib/docker \\) -prune -o \\( -name index.sock -o -name index-socket "
        "-o -name own-canary \\) -print 2>/dev/null; true",
        check=False,
    )  # fmt: skip
    assert found.returncode == 0 and found.stdout.strip() == "", found.stdout + found.stderr


# --- the channel ----------------------------------------------------------------------------


def test_the_planners_wrapper_answers_the_six_tools_through_the_socket_and_nothing_of_the_index(live):
    mode(live.pod, "echo")
    replies = six_calls(live)
    assert [r["id"] for r in replies] == [1, 2, *range(10, 16)]
    assert replies[1]["result"]["tools"] == tools.listed()
    for reply, name in zip(replies[2:], VALID, strict=True):
        text = reply["result"]["content"][0]["text"]
        assert not reply["result"]["isError"] and f"answer of {name}" in text, text
    assert hostile_index.MARK not in json.dumps(replies)


# The planner's side of a race: the entry checked, then a pause until the test has had the index
# swap its name, then the connect; by the wrapper's own connector, or by name (the shape the
# wrapper had before, the positive control). A socket of the planner's own listens in its /tmp.
RACE = r"""
import json, os, socket, stat, sys
sys.path.insert(0, "/opt/vivibox-index")
from vivibox.index import channel
victim = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
if os.path.lexists("/tmp/victim.sock"):
    os.unlink("/tmp/victim.sock")
victim.bind("/tmp/victim.sock"); victim.listen(4); victim.settimeout(1)
plain = socket.socket
class Pausing(plain):
    def connect(self, address):
        print("checked", flush=True); sys.stdin.readline()
        return super().connect(address)
channel.socket.socket = Pausing
reached = "nothing"
try:
    if sys.argv[1] == "wrapper":
        ch = channel.unix_entry("/run/vivibox-index", "index.sock")(5)
        sock = ch._sock
    else:
        folder = os.open("/run/vivibox-index", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        assert stat.S_ISSOCK(os.stat("index.sock", dir_fd=folder, follow_symlinks=False).st_mode)
        sock = Pausing(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(f"/proc/self/fd/{folder}/index.sock")
    sock.sendall(b'{"jsonrpc":"2.0","id":"race","method":"initialize","params":{}}\n')
    sock.settimeout(3)
    reached = "index" if b'"race"' in sock.recv(65536) else "something else"
except OSError as e:
    reached = f"error {e}"
try:
    conn, _ = victim.accept(); reached += "; the planner's own socket"
except TimeoutError:
    pass
print(json.dumps(reached), flush=True)
"""


def race(live, how: str, index=None) -> str:
    """What the planner's connect reached when the index swapped the entry for a link to a
    socket of the planner's after the check."""
    index = index or live.index
    planner = subprocess.Popen(
        ["docker", "exec", "-i", live.planner.agent, "python3", "-I", "-c", RACE, how],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert planner.stdout.readline().strip() == "checked", planner.stderr.read()
        folder = podspec.INDEX_SOCKET_MOUNT
        index.exec(
            "sh", "-c", f"cd {folder} && mv index.sock aside.sock && ln -s /tmp/victim.sock index.sock"
        )
        planner.stdin.write("\n")
        planner.stdin.flush()
        out, err = planner.communicate(timeout=30)
        return json.loads(out.strip().splitlines()[-1])
    finally:
        planner.kill()
        back = "if [ -S aside.sock ]; then rm -f index.sock && mv aside.sock index.sock; fi"
        index.exec("sh", "-c", f"cd {podspec.INDEX_SOCKET_MOUNT} && {back}")


def test_a_name_the_index_swaps_after_the_check_is_never_followed_by_the_wrapper(live):
    """The wrapper's connect, through the descriptor of the entry it checked, reaches the
    index's socket; the same race with a connect by name, as the wrapper connected before,
    reaches the planner's own socket: the race is real here, and closed."""
    mode(live.pod, "echo")
    assert race(live, "wrapper") == "index"
    assert race(live, "by name").endswith("; the planner's own socket"), "the race is real here"


def test_the_fd_connect_works_with_the_index_under_sysbox(live):
    """The index's container under Sysbox (its users mapped, its mounts id-mapped): the socket it
    makes is still one the planner's connect through the descriptor reaches, and the race is
    closed the same way."""
    pod = live.pod
    made = pod.index_command()
    subprocess.run(["docker", "rm", "-f", pod.index], capture_output=True, check=True)
    try:
        subprocess.run([*made[:3], "--runtime=sysbox-runc", *made[3:]], capture_output=True, check=True)
        wait_for_socket(pod)
        runtime = subprocess.run(
            ["docker", "inspect", "-f", "{{.HostConfig.Runtime}}", pod.index], capture_output=True, text=True
        )
        assert runtime.stdout.strip() == "sysbox-runc"
        replies = six_calls(live)
        assert all(not r["result"].get("isError", False) for r in replies[2:]), replies
        assert race(live, "wrapper") == "index"
        assert probe(live)["interfaces"] == ["lo"]
    finally:
        subprocess.run(["docker", "rm", "-f", pod.index], capture_output=True)
        pod.up()
        wait_for_socket(pod)


# --- the planner's harness ------------------------------------------------------------------


def calls_for(live, *made: dict) -> None:
    """The tool calls the stand-in provider asks the planner's model for, in order."""
    lines = "".join(json.dumps(m) + "\n" for m in made)
    live.planner.exec("sh", "-c", f"cat > {MOCK_LOG}.calls <<'EOF'\n{lines}EOF")


def tool_results(body: dict) -> list[str]:
    return [m.get("content") for m in body["messages"] if m.get("role") == "tool"]


def planner_turn(live, prompt: str):
    harness = opencode.OpenCode(live.planner, MOCK_MODEL)
    provider_up(live.planner)
    return harness.turn(prompt, session="", title="index live test")


def test_the_planners_model_is_offered_the_wrappers_tools_and_nothing_of_the_index(live):
    mode(live.pod, "echo")
    calls_for(live, {"tool": "index_find_symbol", "arguments": VALID["find_symbol"]})
    done = planner_turn(live, "Find the symbol.")
    assert done.ok, done.error or done.text
    bodies = asked(live.planner)
    last = bodies[-1]
    offered = {
        t["function"]["name"]: t["function"]
        for t in last.get("tools", [])
        if t["function"]["name"].startswith("index_")
    }
    assert set(offered) == {f"index_{t.name}" for t in tools.TOOLS}
    for tool in tools.TOOLS:
        assert offered[f"index_{tool.name}"]["description"] == tool.description
    assert hostile_index.MARK not in json.dumps(bodies[-2:])
    results = tool_results(last)
    assert results and tools.DATA in results[-1] and "answer of find_symbol" in results[-1]


def test_a_call_past_the_harness_s_default_time_limit_still_reaches_the_planner(live):
    """opencode's MCP library ends a call at 60 seconds unless the server's entry says more; the
    wrapper's entry does, so an index that answers in 70 seconds is heard."""
    mode(live.pod, "slow:70")
    try:
        calls_for(live, {"tool": "index_find_symbol", "arguments": VALID["find_symbol"]})
        started = time.monotonic()
        done = planner_turn(live, "Find the symbol, slowly.")
        assert done.ok, done.error or done.text
        assert time.monotonic() - started >= 70
        results = tool_results(asked(live.planner)[-1])
        assert results and "answer of find_symbol" in results[-1], results
    finally:
        mode(live.pod, "echo")


# --- what the index can do to the host and the planner besides the channel --------------------


def test_what_the_index_prints_is_kept_to_a_megabyte_of_the_hosts_disk(live):
    """Its program's output is Docker's log on the host's disk: 27 MB printed, at most a megabyte
    and a little kept."""
    live.index.exec("sh", "-c", "head -c 20000000 /dev/zero | base64 > /proc/1/fd/1")
    time.sleep(2)
    kept = subprocess.run(["docker", "logs", live.pod.index], capture_output=True)
    assert len(kept.stdout) + len(kept.stderr) < 2 * 2**20


def test_the_index_learns_nothing_of_the_hosts_name_servers(live):
    report = probe(live)
    lines = [
        line.split() for line in report["resolv"].splitlines() if line.strip() and not line.startswith("#")
    ]
    assert [w for w in lines if w[0] == "nameserver"] == [["nameserver", "127.0.0.1"]], report["resolv"]
    assert not [w for w in lines if w[0] in ("search", "domain")], report["resolv"]


def test_the_index_gets_no_handle_to_the_planners_namespaces_through_the_wrappers_process(live):
    """The index asks the kernel for the connecting process (SO_PEERPIDFD) and for its mount
    namespace through it: the wrapper made itself undumpable, so that is refused."""
    mode(live.pod, "echo")
    live.index.exec("sh", "-c", "rm -f /config/peers.json")
    six_calls(live)
    time.sleep(0.5)
    peers = json.loads(live.index.exec("cat", "/config/peers.json").stdout)
    assert peers and set(peers) == {"EACCES"}, peers
