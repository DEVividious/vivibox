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
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    # A test run from an agent's CLI would record that CLI's session with a task, and look for it.
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


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
    # vivibox new asks for the agent image before it creates a task; nor is there an image.
    from vivibox import image

    monkeypatch.setattr(image, "exists", lambda ref, runner=None: True)
