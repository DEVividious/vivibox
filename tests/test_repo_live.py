"""The task clone inside a real pod: the agent commits but cannot touch git files the host runs.

Needs host/setup.sh to have run and the agent image. Run with: uv run pytest -m docker tests/test_repo_live.py
"""

import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import image, repo
from vivibox import secrets as task_secrets
from vivibox.actions import task_pod
from vivibox.cli import main
from vivibox.pod import FIREWALL

pytestmark = pytest.mark.docker

TASKS_ROOT = Path("/srv/vivibox")


@pytest.fixture(scope="module")
def task(tmp_path_factory):
    if subprocess.run(["sudo", "-n", "-l", FIREWALL], capture_output=True).returncode != 0:
        pytest.skip("run host/setup.sh first")
    image.build()
    tmp = tmp_path_factory.mktemp("repo-live")
    source = make_repo(tmp / "source")
    tasks_dir = TASKS_ROOT / f".pytest-{secrets.token_hex(4)}"
    cfg = tmp / "config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{tasks_dir}"\n[roles.planner]\nharness = "manual"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )
    (cfg / "projects" / "repocheck.toml").write_text(f'repo = "{source}"\nverify = ["test -f hello.txt"]\n')
    mp = pytest.MonkeyPatch()
    mp.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    assert main(["new", "repocheck", "Check the task clone", "--draft"]) == 0
    assert main(["pod", "up", "repocheck-1"]) == 0
    pod = task_pod("repocheck-1")
    try:
        yield {"pod": pod, "source": source, "repo": pod.repo}
    finally:
        pod.remove()
        task_secrets.remove("repocheck-1")
        shutil.rmtree(tasks_dir, ignore_errors=True)
        mp.undo()


def agent(task, script: str) -> subprocess.CompletedProcess:
    return task["pod"].exec("bash", "-c", script, check=False)


def test_agent_commits_as_you_on_task_branch(task):
    commit = "echo hi > hello.txt && git add hello.txt && git commit -q -m 'Add hello'"
    result = agent(task, f"{commit} && git log -1 --format='%an <%ae> %s'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Test User <test@example.com> Add hello"
    assert agent(task, "git branch --show-current").stdout.strip() == "vivibox/repocheck-1"


@pytest.mark.parametrize(
    "attack",
    [
        "git config core.fsmonitor 'touch /tmp/x'",
        "git config --global core.fsmonitor 'touch /tmp/x'",
        "echo '#!/bin/sh' > .git/hooks/post-checkout",
        "mv .git .git-old",
        "rm .git/config",
        "rm -rf .git/hooks",
        "echo x >> /task/plan.md",
    ],
)
def test_agent_cannot_change_what_the_host_runs(task, attack):
    assert agent(task, attack).returncode != 0
    repo.check_protection(task["repo"], task["repo"].parent / ".task")


def test_agent_can_write_handoff(task):
    assert agent(task, "echo note > /task/handoff/note.md").returncode == 0


def test_review_fetches_the_agents_commit(task, capsys):
    assert main(["review", "repocheck-1"]) == 0
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s", "vivibox/repocheck-1"],
        cwd=task["source"],
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "Add hello"


def test_verify_runs_in_the_agent_container(task, capsys):
    from vivibox import gate
    from vivibox.config import load_project
    from vivibox.task import Task

    t = Task(task["repo"].parent)
    t.plan_path.write_text(t.plan_path.read_text().replace(gate.PLACEHOLDER, "hello.txt exists"))
    gate.accept_plan(t, load_project("repocheck").verify)
    checklist = t.meta / "handoff" / gate.CRITERIA_FILE
    checklist.write_text(checklist.read_text().replace("- [ ]", "- [x]"))
    assert main(["verify", "repocheck-1"]) == 0
    log = sorted((t.meta / "log").glob("verify-*.log"))[-1].read_text()
    assert "[exit 0]" in log


def test_deleting_git_dir_leaves_protected_files(task):
    # Runs last: the agent can destroy its own history, but not what the host would run.
    agent(task, "rm -rf .git")
    repo.check_protection(task["repo"], task["repo"].parent / ".task")
