"""The code index's project data and lifecycle on a real pod, run by a compromised index: the
host's read-only view for I, I's private copy made and refreshed by I's own fixed operation, the
discard of what I wrote when that operation fails, and the provisioning run on a network of its
own that exists only while it runs. The index is the hostile index of tests/hostile_index.py in
its "project" mode, which answers with what its copy holds (not an index: a stand-in that shows
which version of the commits I reads); the provisioning run is tests/index_provision.py.

Needs host/setup.sh to have run (root helper 4), Docker with runc, and internet access.
Run with: uv run pytest -m docker tests/test_index_lifecycle_live.py
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import hostile_index
import index_probe
import index_provision
import pytest
from index_harness import INITIALIZE, INITIALIZED, VALID, call, listing
from test_control_live import a_pod, a_task, gone
from test_pod_live import TASKS_ROOT

from vivibox import control, image, names, opencode, pod_index, podspec
from vivibox.egress import resolver
from vivibox.pod import ContainerSide, Mount
from vivibox.pod_egress import helper_missing
from vivibox.podspec import IndexServer

pytestmark = pytest.mark.docker

PROGRAM = "/opt/index"
SYS_NET = Path("/sys/class/net")


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def commit(task, files: dict[str, str | None], message: str) -> str:
    """The writer's commit on the task's branch: each file written, or deleted for None."""
    for name, text in files.items():
        if text is None:
            sh("git", "rm", "-q", name, cwd=task.repo)
            continue
        (task.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (task.repo / name).write_text(text)
        sh("git", "add", name, cwd=task.repo)
    sh("git", "commit", "-q", "-m", message, cwd=task.repo)
    return sh("git", "rev-parse", "HEAD", cwd=task.repo)


def index_side(pod) -> ContainerSide:
    return ContainerSide(pod, pod.index)


def running(pod, name: str) -> bool:
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name], capture_output=True, text=True
    )
    return state.returncode == 0 and state.stdout.strip() == "running"


def wait_for(what, seconds: float = 60, step: float = 0.2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if what():
            return
        time.sleep(step)
    raise AssertionError(f"not within {seconds} s")


def wait_for_socket(pod) -> None:
    entry = f"{podspec.INDEX_SOCKET_MOUNT}/{podspec.INDEX_SOCKET}"
    wait_for(
        lambda: (
            running(pod, pod.index) and index_side(pod).exec("test", "-S", entry, check=False).returncode == 0
        )
    )


def mode(pod, chosen: str) -> None:
    index_side(pod).exec("sh", "-c", f"printf %s '{chosen}' > /config/mode")
    time.sleep(0.3)


def wrapper(live, *lines: dict) -> list[dict]:
    done = subprocess.run(
        ["docker", "exec", "-i", live.planner.agent, *pod_index.wrapper_command()],
        input="".join(json.dumps(m) + "\n" for m in lines), capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return [json.loads(one) for one in done.stdout.splitlines()]


def what_the_index_reads(live) -> str:
    """The text the planner's wrapper hands on from one call, the index in its project mode."""
    mode(live.pod, "project")
    replies = wrapper(
        live, INITIALIZE, INITIALIZED, call(2, "get_symbols_overview", VALID["get_symbols_overview"])
    )
    reply = replies[-1]["result"]
    assert not reply.get("isError"), reply
    return reply["content"][0]["text"]


def probe(side: ContainerSide, attempts: list[dict] | None = None) -> dict:
    asked = json.dumps({"canaries": [], "tries": attempts or []})
    done = side.exec("python3", f"{PROGRAM}/index_probe.py", asked, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def mounts_of(report: dict) -> dict[str, tuple[str, str]]:
    return {m["point"]: (m["options"], m["root"]) for m in report["mounts"]}


@pytest.fixture(scope="module")
def live():
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    ref, _ = image.build()
    task = a_task("indexlife")
    pod = a_pod(task, ref)
    program = task.root / "index-program"
    program.mkdir()
    for module in (hostile_index, index_probe, index_provision):
        shutil.copy(module.__file__, program / Path(module.__file__).name)
    steady = IndexServer(
        mounts=(Mount(str(program), PROGRAM, read_only=True),),
        command=(
            "python3",
            f"{PROGRAM}/hostile_index.py",
            podspec.INDEX_SOCKET_MOUNT,
            "echo",
            str(task.repo),
        ),
    )
    pod.index_server = steady
    try:
        pod.up()
        control.refresh(task, pod)
        wait_for_socket(pod)
        provision = ("python3", f"{PROGRAM}/index_provision.py")
        yield SimpleNamespace(
            task=task, pod=pod, planner=pod.control_side(), steady=steady,
            provisioning=IndexServer(steady.mounts, steady.command, provision=provision),
        )  # fmt: skip
    finally:
        gone(task, pod)
        assert not running(pod, pod.index_provision)


# --- the source and the copy ----------------------------------------------------------------


def test_the_index_has_the_hosts_view_read_only_and_its_copy_at_the_planners_path(live):
    pod = live.pod
    found = mounts_of(probe(index_side(pod)))
    assert found[podspec.INDEX_SOURCE_MOUNT][0].startswith("ro")
    assert found[podspec.INDEX_SOURCE_MOUNT][1] == f"/{podspec.INDEX_SOURCE}", "a folder of the task's disk"
    assert found[str(live.task.repo)][0].startswith("rw")
    assert found[str(live.task.repo)][1] == f"/{podspec.INDEX_COPY}", "I's copy, not the writer's clone"
    assert found["/config"][1] == f"/{podspec.INDEX_STORE}"
    head = sh("git", "rev-parse", "HEAD", cwd=live.task.repo)
    assert sh("git", "rev-parse", "HEAD", cwd=pod.index_source) == head, "the host's view, on the branch"


def test_the_index_cannot_change_nor_replace_its_source(live):
    pod = live.pod
    before = sh("git", "rev-parse", "HEAD", cwd=pod.index_source)
    files = sorted(p.name for p in pod.index_source.iterdir())
    src = podspec.INDEX_SOURCE_MOUNT
    tries = {
        "write": f"echo x > {src}/new",
        "change": f"echo x >> {src}/calc.py",
        "delete": f"rm {src}/calc.py",
        "git": f"echo '[core]' >> {src}/.git/config",
        "rename": f"mv {src} /tmp/elsewhere",
        "link": f"rm -rf {src} && ln -s / {src}",
        "mount": f"mount --bind /tmp {src}",
        "unmount": f"umount {src}",
    }
    for what, script in tries.items():
        done = index_side(pod).exec("sh", "-c", script, check=False)
        assert done.returncode != 0, what
    assert sh("git", "rev-parse", "HEAD", cwd=pod.index_source) == before
    assert sorted(p.name for p in pod.index_source.iterdir()) == files
    assert "x" not in (pod.index_source / "calc.py").read_text().splitlines()
    assert index_side(pod).exec("test", "-f", f"{src}/calc.py", check=False).returncode == 0, "still read"


def test_the_writer_reaches_neither_the_source_nor_the_copy(live):
    pod = live.pod
    mounted = pod.exec("cat", "/proc/self/mountinfo").stdout
    for folder in (podspec.INDEX_SOURCE, podspec.INDEX_COPY, podspec.INDEX_STORE):
        assert f"/{folder} " not in mounted, folder
    assert pod.exec("test", "-e", podspec.INDEX_SOURCE_MOUNT, check=False).returncode != 0


def test_the_planner_reads_one_version_then_the_next_through_the_wrapper(live):
    """A, then B (a file deleted, one renamed, one changed): what the index reads follows the
    verified commit, and nothing of A that B removed survives in its copy."""
    task, pod = live.task, live.pod
    commit(task, {"old.txt": "old\n", "src/app.py": "version A\n"}, "A")
    control.refresh(task, pod)
    seen = what_the_index_reads(live)
    assert "version A" in seen and '"old.txt"' in seen and '"src/app.py"' in seen
    sh("git", "mv", "src/app.py", "app.py", cwd=task.repo)
    b = commit(task, {"old.txt": None, "app.py": "version B\n"}, "B")
    control.refresh(task, pod, b)
    seen = what_the_index_reads(live)
    assert "version B" in seen and '"app.py"' in seen
    assert "version A" not in seen and '"old.txt"' not in seen and '"src/app.py"' not in seen


def test_a_later_commit_of_the_writers_is_not_in_the_copy_of_the_verified_one(live):
    task, pod = live.task, live.pod
    verified = commit(task, {"v.txt": "verified\n"}, "verified")
    commit(task, {"v.txt": "after the verification\n"}, "later")
    (task.repo / "v.txt").write_text("not even committed\n")
    control.refresh(task, pod, verified)
    seen = what_the_index_reads(live)
    assert '"v.txt": "verified' in seen and "after the verification" not in seen and "not even" not in seen
    sh("git", "checkout", "-q", "--", "v.txt", cwd=task.repo)


def test_the_six_wrapper_calls_still_answer_through_the_socket(live):
    mode(live.pod, "echo")
    lines = [INITIALIZE, INITIALIZED, listing(2)]
    lines += [call(10 + i, name, VALID[name]) for i, name in enumerate(VALID)]
    replies = wrapper(live, *lines)
    assert [r["id"] for r in replies] == [1, 2, *range(10, 16)]
    for reply, name in zip(replies[2:], VALID, strict=True):
        assert f"answer of {name}" in reply["result"]["content"][0]["text"]
    assert hostile_index.MARK not in json.dumps(replies)


def test_a_copy_the_index_broke_is_discarded_and_made_again_at_the_next_start(live):
    task, pod = live.task, live.pod
    index_side(pod).exec("sh", "-c", "echo mine > /config/own")
    index_side(pod).exec("sh", "-c", f"cd {task.repo} && rm -rf .git && echo junk > .git")
    control.refresh(task, pod)
    assert not running(pod, pod.index), "a copy behind the planner's is never answered from"
    assert "from scratch" in pod.index_failure
    assert list(pod.index_copy.iterdir()) == [] and list(pod.index_store.iterdir()) == []
    pod.up()
    wait_for_socket(pod)
    control.refresh(task, pod)
    head = sh("git", "rev-parse", "HEAD", cwd=task.repo)
    assert sh("git", "rev-parse", "HEAD", cwd=pod.index_source) == head
    assert '"calc.py"' in what_the_index_reads(live)


# --- the provisioning run -------------------------------------------------------------------


def provisioning(live, asked: dict) -> None:
    """The index made to be provisioned again: down, its data discarded, the run's request in its
    store (the test's word to the fixture, written where the run reads it)."""
    pod = live.pod
    pod.index_server = live.provisioning
    pod.index_discard()
    (pod.index_store / "provision-asked.json").write_text(json.dumps(asked))


def none_left(pod) -> None:
    net = pod.network_name(pod_index.INDEX_ROLE)
    assert not running(pod, pod.index_provision)
    inspect = subprocess.run(["docker", "inspect", pod.index_provision], capture_output=True)
    assert inspect.returncode != 0, "the run's container is removed"
    assert subprocess.run(["docker", "network", "inspect", net], capture_output=True).returncode != 0
    assert not (SYS_NET / names.bridge(pod.task_id, pod_index.INDEX_ROLE)).exists(), "no bridge left"


def tcp(host: str, port: int) -> dict:
    return {"kind": "tcp", "host": host, "port": port}


def test_the_provisioning_run_reaches_the_internet_and_the_forwarder_and_nothing_else(live):
    pod = live.pod
    nets = {role: pod.recorded_network(role) for role in ("w", "r", "p", "i")}
    forbidden = [
        tcp(nets["p"].address, opencode.PORT),  # the planner's server
        tcp(nets["w"].address, opencode.PORT),  # the writer's
        tcp(nets["r"].address, opencode.REVIEW_PORT),  # the reviewer's
        *(tcp(n.gateway, 22) for n in nets.values()),  # the host, on every bridge
        tcp(pod.host_ip, 22),
        tcp(pod.host_ip, 5055),  # the Docker Hub mirror's port, a host service
        tcp("192.168.0.1", 80),
        tcp("10.0.0.1", 80),  # private ranges
    ]
    allowed = [tcp("1.1.1.1", 443), {"kind": "dns", "host": "example.com"}, tcp(pod.host_ip, resolver.PORT)]
    asked = {
        "mode": "wait",
        "tries": [*forbidden, *allowed],
        "download": "https://repo.maven.apache.org/maven2/",
    }
    provisioning(live, asked)
    up = threading.Thread(target=pod.up, daemon=True)
    up.start()
    wait_for(lambda: (pod.index_store / "listening").exists(), seconds=120)
    try:
        attached = subprocess.run(
            ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", pod.index_provision],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        assert list(json.loads(attached.stdout)) == [pod.network_name("i")]
        target = (nets["i"].address, index_provision.LISTEN)
        # The run listens: the host itself reaches it (the positive control), the roles do not.
        socket.create_connection(target, timeout=3).close()
        knock = f"import socket; socket.create_connection({target!r}, timeout=3)"
        for side in (pod, live.planner):
            assert side.exec("python3", "-c", knock, check=False).returncode != 0, side
    finally:
        (pod.index_store / "go").write_text("")
        up.join(timeout=180)
    report = json.loads((pod.index_store / "provision-report.json").read_text())
    results = {(t.get("host"), t.get("port")): t["result"] for t in report["tries"]}
    for t in forbidden:
        assert results[(t["host"], t["port"])] != "reached", t
    assert all(results[(t["host"], t.get("port"))] == "reached" for t in allowed), report["tries"]
    assert report["download"].startswith("200"), report["download"]
    assert sorted(report["interfaces"]) == ["eth0", "lo"]
    secret_like = [
        n for n in report["environment"] if any(w in n for w in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
    ]
    assert secret_like == [] and "OPENCODE_CONFIG" not in report["environment"]
    points = {m["point"] for m in report["mounts"]}
    assert podspec.INDEX_SOCKET_MOUNT not in points and "/task" not in points and "/work" not in points
    reached = (pod.index_store / "reached").read_text().splitlines()
    assert reached == ["one"], "the host's own connection (the positive control) and no role's"
    assert pod.index_failure == ""
    none_left(pod)
    wait_for_socket(pod)
    assert (pod.index_store / "provisioned").read_text() == "downloaded\n"


def test_after_provisioning_the_index_has_no_network_at_all(live):
    pod = live.pod
    attached = subprocess.run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", pod.index],
        capture_output=True,
        text=True,
    )
    assert list(json.loads(attached.stdout)) == ["none"]
    nets = {role: pod.recorded_network(role) for role in ("w", "r", "p", "i")}
    report = probe(
        index_side(pod),
        [tcp("1.1.1.1", 443), {"kind": "dns", "host": "example.com"}, tcp(pod.host_ip, resolver.PORT),
         *(tcp(n.gateway, 22) for n in nets.values()), tcp(nets["p"].address, opencode.PORT)],
    )  # fmt: skip
    assert report["interfaces"] == ["lo"]
    assert all(t["result"] in ("ENETUNREACH", "EAI_AGAIN", "EAI_NONAME") for t in report["tries"]), report[
        "tries"
    ]
    none_left(pod)


@pytest.mark.parametrize("ending", ["fail", "hang"])
def test_a_provisioning_run_that_fails_or_runs_out_of_time_leaves_nothing_attached(live, ending, monkeypatch):
    pod = live.pod
    provisioning(live, {"mode": ending})
    monkeypatch.setattr(pod, "PROVISION_SECONDS", 8.0)
    pod.up()
    none_left(pod)
    assert not running(pod, pod.index)
    assert ("exit status 1" if ending == "fail" else "ran out of time") in pod.index_failure


def test_a_stop_of_the_pod_during_provisioning_takes_the_run_and_its_network(live):
    pod = live.pod
    provisioning(live, {"mode": "hang"})
    up = threading.Thread(target=pod.up, daemon=True)
    up.start()
    try:
        wait_for(lambda: running(pod, pod.index_provision), seconds=120)
        pod.down()
        up.join(timeout=120)
        assert not up.is_alive()
        none_left(pod)
        assert not running(pod, pod.index)
    finally:
        pod.index_server = live.steady
        pod.index_discard()
        pod.up()
        wait_for_socket(pod)
