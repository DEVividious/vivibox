"""Isolation and capability checks on a real task pod.

Needs host/setup.sh to have run, Docker, and internet access (images, Maven Central).
Run with: uv run pytest -m docker tests/test_pod_live.py
"""

import http.server
import os
import random
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from vivibox import image, toolchain
from vivibox.config import HostService, load_config
from vivibox.pod import CA_BUNDLE, SOCKET, Pod
from vivibox.pod_egress import helper_missing

pytestmark = pytest.mark.docker

TASKS_ROOT = Path("/srv/vivibox")
FIXTURE = Path(__file__).parent / "fixtures" / "pod-project"


class Empty(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


def serve() -> http.server.ThreadingHTTPServer:
    # All interfaces: the pod reaches the host through the Docker gateway.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Empty)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def lan_ip() -> str:
    out = subprocess.run(
        ["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True
    ).stdout.split()
    return out[out.index("src") + 1]


def own_folder(task_id: str) -> Path:
    """A task's folder, one level under the tasks directory and named by its id: the root helper
    attaches a disk there and nowhere else. Removed by the test that made it."""
    folder = TASKS_ROOT / task_id
    folder.mkdir()
    return folder


def check_id(kind: str) -> str:
    """An id of the task id's pattern that no earlier run left behind."""
    return f"{kind}{secrets.token_hex(3)}-{random.randint(1, 9999)}"


@pytest.fixture(scope="module")
def env():
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    allowed, denied = serve(), serve()
    task_id = check_id("podcheck")
    folder = own_folder(task_id)
    repo = folder / "repo"
    shutil.copytree(FIXTURE, repo)
    marker = secrets.token_hex(8)
    index = repo / "www" / "index.html"
    index.write_text(index.read_text().replace("__MARKER__", marker))
    # A repository with a commit: the gate verifies a fresh clone of it, never the working tree.
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Pod Check")
    git("config", "user.email", "podcheck@example.com")
    git("add", ".")
    git("commit", "-q", "-m", "Fixture project")
    ref, _ = image.build()
    cfg = load_config()  # the host's address and the pool this machine was set up with
    pod = Pod(
        task_id, repo, ref, [HostService("host.docker.internal", allowed.server_address[1])],
        gate_dir=folder / "disk" / "gate", host_address=cfg.host_address, network_pool=cfg.network_pool,
        limits=cfg.limits, folder=folder,
    )  # fmt: skip
    try:
        pod.up()
        yield {
            "pod": pod,
            "repo": repo,
            "marker": marker,
            "allowed": allowed.server_address[1],
            "denied": denied.server_address[1],
        }
    finally:
        pod.remove()
        shutil.rmtree(folder, ignore_errors=True)
        allowed.shutdown()
        denied.shutdown()


def agent(env, script: str, check: bool = True) -> subprocess.CompletedProcess:
    return env["pod"].exec("bash", "-c", script, check=check)


def gate(env, script: str, check: bool = True) -> subprocess.CompletedProcess:
    """In the gate container, in its fresh clone; gate_up() first."""
    return env["pod"].gate_exec("bash", "-c", script, check=check)


def test_agent_is_unprivileged(env):
    assert agent(env, "id -u").stdout.strip() == str(os.getuid())
    assert "CapEff:\t0000000000000000" in agent(env, "cat /proc/self/status").stdout
    assert agent(env, "touch /usr/local/x", check=False).returncode != 0, "root filesystem must be read-only"


def test_docker_in_pod_does_not_use_vfs(env):
    assert agent(env, "docker info --format '{{.Driver}}'").stdout.strip() not in ("", "vfs")


def test_compose_ports_on_localhost_and_bind_mount(env):
    agent(env, "docker compose -p podcheck up -d --wait")
    agent(env, "exec 3<>/dev/tcp/127.0.0.1/5432")
    assert env["marker"] in agent(env, "curl -fsS http://localhost:8080/").stdout


def test_nested_container_cannot_write_to_repo(env):
    repo = env["repo"]
    write = f"cp /bin/busybox {repo}/suid && chmod 4755 {repo}/suid"
    agent(env, f"docker run --rm -v {repo}:{repo} alpine:3 sh -c '{write}'", check=False)
    assert not (repo / "suid").exists()


def test_privileged_nested_container_cannot_reach_host(env):
    marker = Path.home() / f".vivibox-pod-check-{secrets.token_hex(4)}"
    marker.write_text("secret")
    dev = os.stat("/").st_dev
    probe = (
        f"test -e /host{marker} && echo FOUND_MARKER; "
        f"mknod /tmp/rootdev b {os.major(dev)} {os.minor(dev)} 2>/dev/null && mkdir -p /mnt/r "
        "&& mount -o ro /tmp/rootdev /mnt/r 2>/dev/null "
        "&& test -e /mnt/r/etc/shadow && echo MOUNTED_HOST_ROOT; "
        "echo PROBE_DONE"
    )
    try:
        out = agent(
            env, f"docker run --rm --privileged -v /:/host alpine:3 sh -c '{probe}'", check=False
        ).stdout
    finally:
        marker.unlink()
    assert "PROBE_DONE" in out
    assert "FOUND_MARKER" not in out and "MOUNTED_HOST_ROOT" not in out


def test_only_allowed_host_service_is_reachable(env):
    gw, ok, no = env["pod"].gateway(), env["allowed"], env["denied"]
    curl = "curl -fsS -m 5 -o /dev/null http://{}:{}/"
    wget = "docker run --rm alpine:3 wget -q -T 5 -O /dev/null http://{}:{}/"
    assert agent(env, curl.format(gw, ok), check=False).returncode == 0
    assert agent(env, curl.format(gw, no), check=False).returncode != 0
    assert agent(env, curl.format(lan_ip(), ok), check=False).returncode != 0, (
        "your LAN address must be blocked"
    )
    assert agent(env, wget.format(gw, ok), check=False).returncode == 0
    assert agent(env, wget.format(gw, no), check=False).returncode != 0


def test_every_dns_client_in_the_pod_asks_the_hosts_forwarder(env):
    """The agent (Docker's resolver in the sidecar), the nested daemon's containers, and a query to
    another server, which the host turns to the forwarder: each answers."""
    assert agent(env, "getent hosts example.com", check=False).returncode == 0
    nested = "docker run --rm alpine:3 nslookup example.com"
    assert agent(env, nested, check=False).returncode == 0, "the nested daemon inherits --dns"
    assert agent(env, nested + " 8.8.8.8", check=False).returncode == 0, "another server: DNAT"


def test_root_in_a_nested_container_cannot_remove_the_rules(env):
    """H10: a privileged container in the pod, on the sidecar's network, flushes all it sees. The
    host's rules are not among them. What is, it puts back as it found it, and the test checks
    that it did: the flush takes Docker's turn of 127.0.0.11 to its resolver and the nested
    daemon's own chains with it, and the pod resolves no name and pulls nothing until a restart
    makes them again; the tests after this one need both. Every rule in that namespace was made
    by iptables, so iptables-save and iptables-restore are the pair that keeps them (nft lists
    their targets as xt expressions, which nft -f does not load)."""
    pod = env["pod"]
    nested = "docker exec h10 sh -c"
    agent(env, "docker rm -f h10", check=False)
    agent(env, "docker run -d --privileged --network host --name h10 --tmpfs /tmp alpine:3 sleep 600")
    try:
        keep = (
            "apk add -q --no-cache nftables iptables && iptables-save > /tmp/v4"
            " && (ip6tables-save > /tmp/v6 2>/dev/null || :) && echo kept"
        )
        kept = agent(env, f"{nested} '{keep}'", check=False)
        assert "kept" in kept.stdout, kept
        flushed = agent(env, f"{nested} 'nft flush ruleset; iptables -F; echo done'", check=False)
        assert "done" in flushed.stdout, flushed
        gw, no = pod.gateway(), env["denied"]
        assert agent(env, f"curl -fsS -m 5 -o /dev/null http://{gw}:{no}/", check=False).returncode != 0
        assert agent(env, f"curl -fsS -m 5 -o /dev/null http://{lan_ip()}:22/", check=False).returncode != 0
        back = "iptables-restore < /tmp/v4 && (ip6tables-restore < /tmp/v6 2>/dev/null || :) && echo back"
        restored = agent(env, f"{nested} '{back}'", check=False)
        assert "back" in restored.stdout, restored
    finally:
        agent(env, "docker rm -f h10", check=False)
    assert agent(env, "getent hosts example.com", check=False).returncode == 0, "the pod resolves names again"
    pulled = agent(env, "docker run --rm --pull always alpine:3 true", check=False)
    assert pulled.returncode == 0, f"and its daemon pulls again: {pulled.stderr[-500:]}"
    # A published port is the nested daemon's own chains, which the flush took as well.
    published = agent(env, "docker run -d --rm --name h10port -p 18081:80 nginx:alpine", check=False)
    try:
        assert published.returncode == 0, f"and publishes a port again: {published.stderr[-500:]}"
        got = agent(env, "for i in $(seq 20); do curl -fsS -o /dev/null http://localhost:18081/ && exit 0;"
                    " sleep 0.5; done; exit 1", check=False)  # fmt: skip
        assert got.returncode == 0, "the port it published answers"
    finally:
        agent(env, "docker rm -f h10port", check=False)


def test_internet_is_reachable(env):
    central = "https://repo.maven.apache.org/maven2/"
    assert agent(env, f"curl -fsS -m 10 -o /dev/null {central}", check=False).returncode == 0


def test_parallel_maven_builds_share_a_local_repository(env):
    # A throwaway repository on tmpfs: the point is two resolvers filling an empty one at once.
    go = "mvn -B -q -Dmaven.repo.local=/tmp/m2-parallel -f app/pom.xml dependency:go-offline"
    agent(env, f"{go} & a=$!; {go} & b=$!; wait $a && wait $b")


def test_docker_answers_the_api_version_older_testcontainers_speaks(env):
    # Testcontainers before 1.21 asks for API 1.32, which Docker 29 refuses unless told otherwise.
    result = agent(env, "DOCKER_API_VERSION=1.32 docker version --format '{{.Server.Version}}'", check=False)
    assert result.returncode == 0, result.stderr


def test_maven_build_with_testcontainers(env):
    result = agent(env, "mvn -B -f app/pom.xml verify", check=False)
    assert result.returncode == 0, result.stdout[-3000:]
    assert "Tests run: 1, Failures: 0, Errors: 0" in result.stdout
    assert agent(env, "ls /cache/m2/org/testcontainers").stdout.strip(), "the shared Maven cache is used"


def test_what_the_agent_installs_the_gate_does_not_see(env):
    result = agent(env, "mvn -B -q -f app/pom.xml install -DskipTests", check=False)
    assert result.returncode == 0, result.stdout[-3000:]
    assert agent(env, "ls /cache/m2/installed/podcheck/pod-check/1.0").stdout.strip()
    assert agent(env, "ls /cache/m2/cached/org/testcontainers").stdout.strip(), "downloads stay shared"
    env["pod"].gate_up()
    try:
        assert gate(env, "ls /cache/m2/installed/podcheck", check=False).returncode != 0
    finally:
        env["pod"].gate_down()


def test_the_preparation_runs_in_the_background_and_says_how_it_ended(env):
    import time

    pod = env["pod"]
    pod.prepare_start(["sleep 2", "pwd > /tmp/prep.where", "false"], "/tmp/prep.log", "/tmp/prep.exit")
    assert pod.prepare_running(), "the exec that started it is over, the commands are not"
    deadline = time.monotonic() + 30
    while pod.prepare_running() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not pod.prepare_running()
    assert agent(env, "cat /tmp/prep.where").stdout.strip() == str(env["repo"]), "in the clone"
    assert agent(env, "cat /tmp/prep.exit").stdout.strip() == "1", "the failing command's code"


def test_the_gates_build_cache_is_its_own_and_outlives_a_verification(env):
    pod = env["pod"]
    agent(env, "touch /cache/build-cache/from-the-writer")
    pod.gate_up()
    try:
        assert gate(env, "test -e /cache/build-cache/from-the-writer", check=False).returncode != 0
        gate(env, "touch /cache/build-cache/from-the-gate")
    finally:
        pod.gate_down()
    pod.gate_up()
    try:
        assert gate(env, "test -e /cache/build-cache/from-the-gate", check=False).returncode == 0
    finally:
        pod.gate_down()
        agent(env, "rm -f /cache/build-cache/from-the-writer")


def test_a_sidecar_just_made_is_not_outdated(env):
    """Docker keeps the mounts as given, so what up() compares them with is what inspect returns."""
    assert not env["pod"]._outdated_sidecar()


def test_the_pod_trusts_the_authorities_the_host_trusts(env):
    """The daemon in the sidecar, the agent and the gate see the host's CA bundle, not their own."""
    pod = env["pod"]
    host = subprocess.run(
        ["sha256sum", CA_BUNDLE], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    sidecar = subprocess.run(
        ["docker", "exec", pod.sidecar, "sha256sum", CA_BUNDLE], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    assert sidecar == host, "the daemon that pulls images verifies with the host's authorities"
    assert agent(env, f"sha256sum {CA_BUNDLE}").stdout.split()[0] == host
    pod.gate_up()
    try:
        assert gate(env, f"sha256sum {CA_BUNDLE}").stdout.split()[0] == host
    finally:
        pod.gate_down()


# --- the gate: the same pod, a fresh container, only committed work -------------------------


def test_the_gate_sees_the_daemon_and_localhost_like_the_agent(env):
    """The work laptop's case: Testcontainers passed for the agent and the gate could not reach Docker. The
    gate container gets the same socket and the same variables, so a container it starts is on
    localhost for it, as it is for the agent."""
    pod = env["pod"]
    pod.gate_up()
    try:
        assert gate(env, "docker info --format '{{.ServerVersion}}'", check=False).returncode == 0
        gate(env, "docker run -d --rm --name gatecheck -p 18080:80 nginx:alpine")
        try:
            got = gate(env, "for i in $(seq 20); do curl -fsS -o /dev/null http://localhost:18080/ && exit 0;"
                       " sleep 0.5; done; exit 1", check=False)  # fmt: skip
            assert got.returncode == 0, "a port published in the pod's daemon is on the gate's localhost"
        finally:
            gate(env, "docker rm -f gatecheck", check=False)
    finally:
        pod.gate_down()


def test_the_gate_hands_a_commands_output_over_as_it_is_printed(env):
    """A verification's log is watched while it runs, so a line the command prints must reach the
    log then, not when the command is over."""
    import time

    pod = env["pod"]
    pod.gate_up()
    try:
        got: list[tuple[float, str]] = []
        code = pod.gate_stream(
            "bash", "-c", "for i in 1 2 3; do echo $i; sleep 0.5; done; echo err >&2; exit 3",
            sink=lambda line: got.append((time.monotonic(), line)),
        )  # fmt: skip
        assert code == 3 and [line for _, line in got] == ["1\n", "2\n", "3\n", "err\n"]
        assert got[-1][0] - got[0][0] >= 0.9, "the first line came while the command still ran"
    finally:
        pod.gate_down()


def test_the_gate_runs_the_maven_build_with_testcontainers(env):
    """What the agent's build did, done again where the gate does it: a fresh clone, an empty home."""
    pod = env["pod"]
    pod.gate_up()
    try:
        result = gate(env, "mvn -B -f app/pom.xml verify", check=False)
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
        assert "Tests run: 1, Failures: 0, Errors: 0" in result.stdout
    finally:
        pod.gate_down()


def test_the_gate_verifies_committed_work_only(env):
    pod = env["pod"]
    (pod.repo / "uncommitted.txt").write_text("not in any commit\n")
    try:
        pod.gate_up()
        try:
            assert gate(env, "test -e app/pom.xml", check=False).returncode == 0, "the commit is there"
            assert gate(env, "test -e uncommitted.txt", check=False).returncode != 0, (
                "the working tree is not what is verified"
            )
        finally:
            pod.gate_down()
    finally:
        (pod.repo / "uncommitted.txt").unlink()


def test_pod_restart_keeps_images_and_firewall(env):
    pod = env["pod"]
    pod.down()
    pod.up()
    assert pod.sidecar_docker("image", "inspect", "postgres:16-alpine", check=False).returncode == 0
    # The agent, not only root in the sidecar: a restart once left it a socket it could not use.
    assert agent(env, "docker info --format '{{.ServerVersion}}'", check=False).returncode == 0
    no = env["denied"]
    assert (
        agent(env, f"curl -fsS -m 5 -o /dev/null http://{pod.gateway()}:{no}/", check=False).returncode != 0
    )


def test_a_pod_made_by_an_older_vivibox_works_after_a_restart(env, monkeypatch):
    """The work laptop's case: a sidecar made with the old script, stopped, then started by this version."""
    pod = env["pod"]
    old = (
        "dind dockerd --host=unix://{s} >/var/log/dockerd.log 2>&1 & "
        "while [ ! -S {s} ]; do sleep 0.2; done; chmod 666 {s}; wait"
    )
    current = Pod.sidecar_command
    pod.remove()
    monkeypatch.setattr(
        Pod, "sidecar_command", lambda self, address="": [*current(self, address)[:-1], old.format(s=SOCKET)]
    )
    pod.up()
    pod.down()
    monkeypatch.setattr(Pod, "sidecar_command", current)
    pod.up()
    assert agent(env, "docker info --format '{{.ServerVersion}}'", check=False).returncode == 0


def test_demo_finds_the_port_the_project_opened(env):
    """The whole point: nothing says which port, so vivibox asks the kernel what opened."""
    pod = env["pod"]
    serve_on = 'node -e \'require("http").createServer((q,s)=>s.end("up")).listen(8931,"0.0.0.0")\''
    heard = pod.demo_start([serve_on], wait=30)
    try:
        assert [listener.port for listener in heard] == [8931]
        assert heard[0].reachable and not heard[0].why_not
        page = subprocess.run(
            ["curl", "-sS", "-m", "5", f"http://{pod.address()}:8931/"], capture_output=True, text=True
        )
        assert page.stdout == "up", "reachable from this machine, not only inside the pod"
    finally:
        pod.demo_stop()


def test_demo_says_when_the_project_listens_where_nobody_can_reach_it(env):
    pod = env["pod"]
    loopback = 'node -e \'require("http").createServer((q,s)=>s.end("up")).listen(8932,"127.0.0.1")\''
    heard = pod.demo_start([loopback], wait=30)
    try:
        assert [listener.port for listener in heard] == [8932]
        assert not heard[0].reachable
        assert "nothing outside can reach it" in heard[0].why_not
    finally:
        pod.demo_stop()


def test_demo_gives_up_and_keeps_the_output_when_the_command_fails(env):
    pod = env["pod"]
    assert pod.demo_start(["echo 'no such thing' >&2; exit 1"], wait=10) == []
    assert "no such thing" in pod.demo_log(), "the log says why, instead of a link to nowhere"
    assert not pod.demo_running()


def test_every_default_toolchain_runs_without_project_setup(env):
    """Node was baked into the image and Python was only a mise shim with no version selected, so
    'python -V' failed while 'node -v' worked. An agent reading the container then picked Node for
    every task, whatever the task needed, and called it a constraint of the environment."""
    wanted = (("node", "-v"), ("npm", "-v"), ("python", "-V"), ("uv", "--version"), ("java", "-version"))
    for tool, flag in wanted:
        got = agent(env, f"{tool} {flag}", check=False)
        assert got.returncode == 0, f"{tool} is not usable out of the box: {got.stdout}{got.stderr}"
    # The README promises Java 21 when a project names no other; it held only while a cache shared
    # between tasks happened to have one version of it installed.
    assert "21." in agent(env, "java -version 2>&1").stdout


def test_a_project_can_bring_a_toolchain_the_image_never_had(env):
    """The sandbox is the whole point: a task that needs Go must be able to have Go."""
    repo = env["pod"].repo
    agent(env, f"cd {repo} && mise use go@1.25")
    got = agent(env, f"cd {repo} && go version")
    assert "go1.25" in got.stdout, got.stdout + got.stderr


def test_a_project_jdk_still_beats_the_image_default(env):
    """The image now declares Java 21, and a project that needs another JDK (Gradle 7 on Java 17)
    must still get it. The mise shim is on PATH in both containers, so a project JDK that did not
    come first would be silently overruled by the default."""
    ref = image.build()[0]
    # Built the way actions.py builds it: the project JDK reaches the containers through this env.
    where = toolchain.agent_env("17", image.env(ref))
    task_id = check_id("jdkcheck")
    folder = own_folder(task_id)
    pod = Pod(task_id, env["repo"], ref, [], agent_env=where, folder=folder)
    try:
        pod.up()
        toolchain.ensure(pod, "17")
        got = pod.exec("bash", "-c", "java -version 2>&1").stdout
        assert "17." in got, f"the project JDK has to come first on PATH: {got}"
    finally:
        pod.remove()
        shutil.rmtree(folder, ignore_errors=True)


def test_a_fifo_in_the_clone_ends_the_gates_clone_in_time_and_takes_the_gate_down(env, monkeypatch):
    """A process the agent left running makes .git/packed-refs a named pipe after the host's
    checks: the gate's clone of the clone waits on it, and past its limit goes down with the gate."""
    from vivibox import repo

    pod, packed = env["pod"], env["repo"] / ".git" / "packed-refs"
    monkeypatch.setattr(repo, "COPY_TIMEOUT", 5, raising=False)  # the red run has none
    kept = packed.read_bytes() if packed.exists() else None
    packed.unlink(missing_ok=True)
    os.mkfifo(packed)
    done = []

    def up():
        try:
            pod.gate_up()
            done.append(None)
        except Exception as e:
            done.append(e)

    try:
        worker = threading.Thread(target=up, daemon=True)
        worker.start()
        worker.join(timeout=60)
        assert done, "the gate's clone hung on the named pipe"
        assert isinstance(done[0], repo.GitTimeout), f"not a timeout: {done[0]!r}"
        assert str(done[0]).startswith("git clone did not answer in 5 s: ")
        assert ".git/packed-refs is a named pipe (FIFO)" in str(done[0])
        assert pod._state(pod.gate) is None, "the gate, and the git waiting in it, are gone"
    finally:
        packed.unlink()
        if kept is not None:
            packed.write_bytes(kept)
        pod.gate_down()


def test_git_in_a_box_that_waits_on_a_fifo_is_killed_and_the_box_stays_up(env, monkeypatch):
    """Closing a box commits in the agent's container, where your shell is: on a named pipe at
    .gitignore git is killed in it past its limit, and the container stays."""
    from vivibox import box, repo

    pod, ignore = env["pod"], env["repo"] / ".gitignore"
    monkeypatch.setattr(repo, "COPY_TIMEOUT", 3, raising=False)  # the red run has none
    kept = ignore.read_bytes() if ignore.exists() else None
    ignore.unlink(missing_ok=True)
    os.mkfifo(ignore)
    done = []

    def add():
        try:
            done.append(box._git_in_box(pod, "add", "-A"))
        except Exception as e:
            done.append(e)

    try:
        worker = threading.Thread(target=add, daemon=True)
        worker.start()
        worker.join(timeout=60)
        assert done, "git in the box hung on the named pipe"
        assert isinstance(done[0], repo.GitTimeout), f"not a timeout: {done[0]!r}"
        assert ".gitignore is a named pipe (FIFO)" in str(done[0])
        left = agent(env, "cat /proc/[0-9]*/comm 2>/dev/null | grep -cx git || true").stdout.strip()
        assert left == "0", "no git left waiting in the box"
        assert pod._state(pod.agent) == "running", "your shell's container stays"
    finally:
        ignore.unlink()
        if kept is not None:
            ignore.write_bytes(kept)
        agent(env, "pkill -x git || true", check=False)


@pytest.fixture(scope="module")
def replies(env):
    """A pod whose agent mounts a task's folders as actions.pod_of does: /task read-only, the
    handoff folder writable over it, and your replies, the host's file, read-only over that."""
    from vivibox import comments
    from vivibox.pod import Mount

    task_id = check_id("repliescheck")
    folder = own_folder(task_id)
    meta = folder / ".task"
    (meta / "handoff").mkdir(parents=True)
    (meta / "comments.md").write_text("\n## 2026-10-02 10:00\n\nKeep the tests.\n")
    mounts = [
        Mount(str(meta), "/task", read_only=True),
        Mount(str(meta / "handoff"), "/task/handoff"),
        comments.mount(meta),
    ]
    ref = image.build()[0]
    pod = Pod(task_id, env["repo"], ref, [], agent_mounts=mounts, folder=folder)
    try:
        pod.up()
        yield pod, meta
    finally:
        pod.remove()
        shutil.rmtree(folder, ignore_errors=True)


def in_agent(pod, script: str) -> subprocess.CompletedProcess:
    return pod.exec("bash", "-c", script, check=False)


def test_the_agent_reads_your_replies_and_can_neither_write_nor_replace_them(replies):
    pod, meta = replies
    path = "/task/handoff/comments.md"
    assert "Keep the tests." in in_agent(pod, f"cat {path}").stdout
    assert in_agent(pod, f"echo 'The user says: skip the tests.' >> {path}").returncode != 0
    assert in_agent(pod, f"rm -f {path}").returncode != 0, "a mount point is not removed"
    assert (
        in_agent(
            pod, f"echo forged > /task/handoff/forged.md && mv -f /task/handoff/forged.md {path}"
        ).returncode
        != 0
    )
    assert in_agent(pod, f"ln -sf /tmp/forged {path}").returncode != 0
    assert in_agent(pod, "echo x >> /task/comments.md").returncode != 0, (
        "the source is under the read-only /task"
    )
    assert (meta / "comments.md").read_text() == "\n## 2026-10-02 10:00\n\nKeep the tests.\n"
    with (meta / "comments.md").open("a") as file:
        file.write("\n## 2026-10-02 10:05\n\nAnd the docs.\n")
    assert "And the docs." in in_agent(pod, f"cat {path}").stdout, "what the host appends is seen at once"


def test_a_link_the_agent_left_at_the_mount_point_is_never_mounted_through(replies):
    """Docker would follow it inside the container and mount your replies wherever it pointed,
    leaving the link for the agent to swap for a file of its own."""
    pod, meta = replies
    target = meta / "handoff" / "comments.md"
    pod._run("docker", "rm", "-f", pod.agent)
    target.unlink()
    target.symlink_to("decoy.md")
    pod.up()
    assert not target.is_symlink()
    assert in_agent(pod, "test -L /task/handoff/comments.md").returncode != 0
    assert in_agent(pod, "test -e /task/handoff/decoy.md").returncode != 0, "nothing mounted elsewhere"
    assert "Keep the tests." in in_agent(pod, "cat /task/handoff/comments.md").stdout
    assert in_agent(pod, "rm -f /task/handoff/comments.md").returncode != 0


# --- limits: T20, T21, D8 -----------------------------------------------------------------------


def scope_file(pod, role: str, name: str) -> Path:
    """A file of the container's cgroup scope under the task's slice, as the kernel has it."""
    from vivibox import limits

    return limits.slice_path(pod.task_id) / f"docker-{pod.scopes()[role]}.scope" / name


def test_a_fork_bomb_in_the_agent_stops_at_the_pids_limit_and_the_pod_lives(env):
    """T20: the agent's container may hold 2048 processes; a bomb of forks past them fails
    ("can't fork"), the daemon in the sidecar answers all along, and the container answers again
    once the children are gone."""
    pod = env["pod"]
    assert scope_file(pod, "agent", "pids.max").read_text().strip() == str(pod.limits.pids_of("agent"))
    assert agent(env, "cat /sys/fs/cgroup/pids.max").stdout.strip() == str(pod.limits.pids_of("agent"))
    bomb = (
        "import os, time\n"
        "fails = 0\n"
        "for i in range(3000):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except BlockingIOError:\n"
        "        fails += 1\n"
        "        continue\n"
        "    if pid == 0:\n"
        "        time.sleep(8)\n"
        "        os._exit(0)\n"
        "print(fails)\n"
    )
    done = pod.exec("python3", "-c", bomb, check=False)
    assert done.returncode == 0 and int(done.stdout.strip()) > 0, f"forks past the limit must fail: {done}"
    assert pod.sidecar_docker("info", check=False).returncode == 0, "the daemon is not the agent's"
    assert pod.running(), "the container lives"
    time.sleep(10)  # the children end
    assert agent(env, "true", check=False).returncode == 0, "and answers again"


def test_an_allocation_past_the_memory_limit_is_killed_and_attributed_to_the_agent(env):
    """T21: the agent's container may hold 4 GiB, swap included; an allocation past it ends
    with 137 (the kernel's OOM killer), the pod and the host's Docker live, the kernel counts
    the kill in the agent's scope, and vivibox reads it as the agent's."""
    from vivibox import limits

    pod = env["pod"]
    limit = pod.limits.memory_of("agent")
    assert scope_file(pod, "agent", "memory.max").read_text().strip() == str(limit)
    assert scope_file(pod, "agent", "memory.swap.max").read_text().strip() == "0", "--memory-swap == --memory"
    before = limits.Watch(pod.task_id, pod.limits, pod.disk, pod.scopes).check({})
    seen = before[0].counts if before else {}
    hog = f"x = bytearray(b'a' * ({limit} + 2**30))\nprint(len(x))"
    done = pod.exec("python3", "-c", hog, check=False)
    assert done.returncode == 137, f"killed for memory, not finished: {done}"
    assert pod.running() and pod.sidecar_docker("info", check=False).returncode == 0, "the pod lives"
    assert subprocess.run(["docker", "info"], capture_output=True).returncode == 0, "the host's Docker too"
    [hit] = limits.Watch(pod.task_id, pod.limits, pod.disk, pod.scopes).check(seen)
    assert (hit.kind, hit.workload, hit.limit) == ("memory", "agent", limit)
    assert hit.words.startswith("oom_kill ") and hit.used > 0


def test_a_nested_containers_processes_count_under_the_tasks_slice(env):
    """D8: cgroup v2 counts hierarchically: what the pod's own Docker runs in a nested container
    is under the sidecar's scope, so the slice's pids.current grows by its processes."""
    from vivibox import limits

    pod = env["pod"]
    pids = limits.slice_path(pod.task_id) / "pids.current"
    before = int(pids.read_text())
    sleeps = "sh -c 'sleep 300 & sleep 300 & sleep 300 & wait'"
    agent(env, f"docker rm -f d8 2>/dev/null; docker run -d --name d8 alpine:3 {sleeps}")
    try:
        time.sleep(1)
        assert int(pids.read_text()) >= before + 4, "sh and its three sleeps, under the slice"
    finally:
        agent(env, "docker rm -f d8", check=False)


def test_a_full_disk_is_the_tasks_and_the_hosts_keeps_its_room():
    """D8: a write in /config of the agent ends in ENOSPC at the image's size, the host's
    filesystem under the tasks directory is not filled, and the room comes back once the file is
    gone (the mount's discard reaches the sparse image). A pod of its own, with a disk of 1 GiB."""
    from vivibox import limits

    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    task_id = check_id("diskcheck")
    folder = own_folder(task_id)
    repo = folder / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    ref, _ = image.build()
    cfg = load_config()
    pod = Pod(
        task_id, repo, ref, [], host_address=cfg.host_address, network_pool=cfg.network_pool,
        limits=cfg.limits.with_values({"disk": "1g"}), folder=folder,
    )  # fmt: skip
    try:
        pod.up()
        host_before = os.statvfs(TASKS_ROOT)
        # dd's whole output: its ENOSPC line comes before the records and bytes it reports last.
        done = pod.exec("dd", "if=/dev/zero", "of=/config/fill", "bs=1M", check=False)
        assert done.returncode != 0, done
        assert "No space left on device" in done.stdout + done.stderr, done
        host_after = os.statvfs(TASKS_ROOT)
        assert host_after.f_bavail > 0, "the host's filesystem is not full"
        taken = (host_before.f_bavail - host_after.f_bavail) * host_before.f_frsize
        assert taken <= 2**30 + 64 * 2**20, f"the image took no more than its size: {taken}"
        [hit] = limits.Watch(pod.task_id, pod.limits, pod.disk, pod.scopes).check({})
        assert hit.kind == "disk" and hit.limit <= 2**30 and hit.words.startswith("statvfs: ")
        assert hit.used >= hit.limit * 0.9, "ENOSPC leaves XFS a few blocks: full all the same"
        pod.exec("bash", "-c", "rm /config/fill && sync")
        time.sleep(2)
        assert limits.Watch(pod.task_id, pod.limits, pod.disk, pod.scopes).check({}) == [], "room again"
        freed = os.statvfs(TASKS_ROOT)
        assert freed.f_bavail > host_after.f_bavail, "the host's room came back: discard reached the image"
    finally:
        assert pod.remove() == ""
        shutil.rmtree(folder, ignore_errors=True)
