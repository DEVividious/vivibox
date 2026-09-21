import subprocess
from pathlib import Path

import pytest


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
    return tmp_path
