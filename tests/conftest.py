import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Git's global configuration for the tests: an identity of their own, so a repository they make
# without one commits the same on this machine and on a runner with no ~/.gitconfig, and nothing
# the tests do reads or writes yours.
GIT_CONFIG = Path(tempfile.mkdtemp(prefix="vivibox-tests-")) / "gitconfig"
GIT_CONFIG.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
os.environ["GIT_CONFIG_GLOBAL"] = str(GIT_CONFIG)


def make_repo(path: Path) -> Path:
    """A repository with one commit and a local identity, like a real project."""
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)  # noqa: E731
    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main")
    run("config", "user.name", "Test User")
    run("config", "user.email", "test@example.com")
    (path / "README.md").write_text("demo\n")
    run("add", ".")
    run("commit", "-q", "-m", "Initial commit")
    return path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config dir with one project 'demo' backed by an empty git repo."""
    cfg = tmp_path / "config"
    (cfg / "projects").mkdir(parents=True)
    repo = tmp_path / "repo"
    make_repo(repo)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{tmp_path / "tasks"}"\n'
        '[roles.planner]\nharness = "opencode"\nmodel = "m"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )
    (cfg / "projects" / "demo.toml").write_text(f'repo = "{repo}"\nverify = ["true"]\n')
    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # keys and history, never yours
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # opencode's configuration, never yours
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # a task's secrets, never yours
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    # A test run from an agent's CLI would record that CLI's session with a task, and look for it.
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# The suite's own temporary folder: what vivibox copies there for a pager (display.terminal_copy)
# never lands in yours.
TEMPORARY = Path(tempfile.mkdtemp(prefix="vivibox-tests-tmp-"))


@pytest.fixture(autouse=True)
def own_temporary_folder(monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(TEMPORARY))


@pytest.fixture(autouse=True)
def no_real_start(request, monkeypatch):
    """A decision now starts a task nobody is working on, and starting means Docker. Tests get a
    stand-in that records the call in vivibox.actions.started; one marked real_start gets the real
    thing."""
    from vivibox import actions, probe

    if "real_start" in request.keywords:
        return
    started: list[str] = []
    monkeypatch.setattr(actions, "started", started, raising=False)
    monkeypatch.setattr(
        actions, "start", lambda task_id, resume=False, on_step=None: started.append(task_id) or "m"
    )
    # The view asks the daemon for the header's warning; the tests have no daemon to ask.
    monkeypatch.setattr(probe, "docker_running", lambda: True)
    # Nor a DNS forwarder to ask about; and none is started by a test but one marked real_hostd.
    monkeypatch.setattr(probe, "forwarder_problem", lambda config: "")
    # vivibox new asks for the agent image before it creates a task; nor is there an image.
    from vivibox import image

    monkeypatch.setattr(image, "exists", lambda ref, runner=None: True)


# What a test may ask a Docker that is not there: whether something exists or runs. Anything else
# would change the machine's containers, images or networks, a real task's among them.
# A throwaway container that lists the models (roles.py) leaves nothing behind either.
READ_ONLY_DOCKER = (("inspect",), ("image", "inspect"), ("info",), ("version",), ("ps",), ("run", "--rm"))
FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$VIVIBOX_TEST_DOCKER_LOG"
echo "the tests reach no Docker (tests/conftest.py: no_real_docker)" >&2
exit 1
"""


@pytest.fixture(autouse=True)
def no_real_hostd(request, monkeypatch, tmp_path_factory):
    """No test starts the host's DNS forwarder but one marked real_hostd or docker: a pod's start
    asks for it (hostd.ensure), and it would outlive the test. Nor takes its locks in your
    runtime folder: every test has one of its own."""
    if "docker" in request.keywords:
        return
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))
    if "real_hostd" in request.keywords:
        return
    from vivibox.egress import hostd

    monkeypatch.setattr(hostd, "ensure", lambda address, port=0, upstream="", timeout=0: None)


@pytest.fixture(autouse=True)
def no_real_docker(request, monkeypatch, tmp_path_factory):
    """No test but one marked docker reaches the machine's Docker, nor sudo: a docker first on PATH answers
    every call as a daemon with nothing in it would, and a test that asked it to change something
    fails, so a stand-in that stops intercepting (a pod built another way) is caught. A task's pod
    (actions.Pod) answers the same way without asking, as a test's own stand-in may still do."""
    if "docker" in request.keywords:
        yield
        return
    from functools import partial

    from vivibox import actions
    from vivibox.pod import Pod

    def nothing_there(cmd, timeout=None):
        return subprocess.CompletedProcess(list(cmd), 1, "", "Error: No such object")

    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=nothing_there))
    folder = tmp_path_factory.mktemp("docker")
    fake = folder / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    # Nor sudo: the root helper is the person's to run, through host/setup.sh and a pod's start.
    sudo = folder / "sudo"
    sudo.write_text('#!/bin/sh\necho "the tests reach no sudo (tests/conftest.py)" >&2\nexit 1\n')
    sudo.chmod(0o755)
    log = folder / "calls.log"
    monkeypatch.setenv("VIVIBOX_TEST_DOCKER_LOG", str(log))
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    yield
    calls = log.read_text().splitlines() if log.exists() else []
    changing = [c for c in calls if not any(tuple(c.split()[: len(r)]) == r for r in READ_ONLY_DOCKER)]
    if changing:
        pytest.fail(f"asked Docker to change something: {changing[:5]}; fake it, or mark the test docker")
