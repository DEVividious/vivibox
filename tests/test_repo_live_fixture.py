"""The fixtures of the live suite of the task's clone (tests/test_repo_live.py), checked without a
pod: a mistake there is found only on a machine with one. The task they make stands where the
root helper attaches a disk, directly under the tasks directory, and whatever happens they leave
that directory, which is yours too, as they found it.
"""

import os
import subprocess
from pathlib import Path

import pytest
import test_repo_live as live
from conftest import LOCAL_SUBMODULES, make_repo

from vivibox import control, reviewing, secrets
from vivibox.config import load_config
from vivibox.task import TASK_ID

POOL, HOST = "192.168.240.0/20", "192.168.239.1/32"


@pytest.fixture
def tasks(env, monkeypatch) -> Path:
    """This machine, as the fixtures find it: its tasks directory with a task and a counter of
    yours in it, and its own network settings."""
    root = env / "tasks"
    (root / "yours-3" / ".task").mkdir(parents=True)
    (root / "yours-3" / "kept.txt").write_text("yours\n")
    (root / ".last-yours").write_text("3")
    with (env / "config" / "config.toml").open("a") as f:
        f.write(f'[network]\npool = "{POOL}"\nhost_address = "{HOST}"\n')
    monkeypatch.setattr(live, "TASKS_ROOT", root)
    return root


def everything(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def secrets_of(task_id: str) -> list[Path]:
    """Where the keys of a task's containers are while its pod is up: the agent's, the
    planner's and the reviewer's."""
    names = (task_id, control.secrets_id(task_id), reviewing.secrets_id(task_id))
    return [secrets.runtime_dir(name) for name in names]


def test_a_task_whose_pod_does_not_start_leaves_nothing_behind(tasks, env):
    """There is no Docker here, so the pod does not come up: the fixture fails, and what it had
    made by then (the task's folder, the project's counter) is gone; yours is as it was."""
    before, yours = everything(tasks), os.environ["VIVIBOX_CONFIG_DIR"]
    source = make_repo(env / "source")
    with pytest.raises(AssertionError), live.a_task(env / "fixture", "repocheck", source, "Check", "true"):
        pytest.fail("a pod came up with no Docker")
    assert everything(tasks) == before
    assert os.environ["VIVIBOX_CONFIG_DIR"] == yours, "the fixture's own configuration is put away"


def test_a_task_is_made_directly_under_the_tasks_directory_and_removed_whole(tasks, env, monkeypatch):
    """With its pod up (said to be, here): the task is one level under the tasks directory, on
    this machine's network settings, its submodule fetched by the host's git under a setting
    given for that step alone; and after the block nothing of it is left, the keys of its three
    containers and the history of its removal included."""
    before, data = everything(tasks), env / "data"
    lib, source = make_repo(env / "lib"), make_repo(env / "source")
    allowed = ["git", "-c", "protocol.file.allow=always"]
    subprocess.run([*allowed, "submodule", "add", "-q", str(lib), "lib"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Add lib"], cwd=source, check=True)
    real = live.main
    monkeypatch.setattr(live, "main", lambda argv: 0 if argv[:2] == ["pod", "up"] else real(argv))
    with live.a_task(env / "fixture", "libcheck", source, "Check", "true", LOCAL_SUBMODULES) as made:
        task_id = made["id"]
        assert TASK_ID.match(task_id) and task_id.startswith("libcheck") and task_id.endswith("-1")
        assert made["repo"] == tasks / task_id / "repo", "where the root helper attaches its disk"
        assert made["pod"].repo == made["repo"] and made["source"] == source
        assert (made["repo"] / "lib" / "README.md").is_file(), "the submodule, fetched on the host"
        config = load_config()
        assert (config.tasks_dir, config.network_pool, config.host_address) == (tasks, POOL, HOST)
        assert os.environ["XDG_CONFIG_HOME"] == str(env / "xdg"), "the git setting was for one step"
        for folder in secrets_of(task_id):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "key").write_text("secret")
    assert everything(tasks) == before
    assert not [folder for folder in secrets_of(task_id) if folder.exists()]
    assert not (data / "vivibox" / "history.jsonl").exists(), "the removal is in the fixture's history"
    assert task_id in (env / "fixture" / "data" / "vivibox" / "history.jsonl").read_text()
