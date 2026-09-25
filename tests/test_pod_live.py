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
from pathlib import Path

import pytest

from vivibox import image, toolchain
from vivibox.config import HostService
from vivibox.pod import CA_BUNDLE, FIREWALL, SOCKET, Pod

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


@pytest.fixture(scope="module")
def env():
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if subprocess.run(["sudo", "-n", "-l", FIREWALL], capture_output=True).returncode != 0:
        pytest.skip("firewall helper not installed; run host/setup.sh")
    allowed, denied = serve(), serve()
    tasks_dir = TASKS_ROOT / f".pytest-{secrets.token_hex(4)}"
    task_id = f"podcheck-{random.randint(1000, 9999)}"
    repo = tasks_dir / task_id / "repo"
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
    pod = Pod(
        task_id, repo, ref, [HostService("host.docker.internal", allowed.server_address[1])],
        gate_dir=tasks_dir / task_id / "gate",
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
        shutil.rmtree(tasks_dir, ignore_errors=True)
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
    pod = Pod(f"jdkcheck-{random.randint(1000, 9999)}", env["repo"], ref, [], agent_env=where)
    try:
        pod.up()
        toolchain.ensure(pod, "17")
        got = pod.exec("bash", "-c", "java -version 2>&1").stdout
        assert "17." in got, f"the project JDK has to come first on PATH: {got}"
    finally:
        pod.remove()
