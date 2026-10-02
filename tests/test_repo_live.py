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
from vivibox.pod_egress import helper_missing

pytestmark = pytest.mark.docker

TASKS_ROOT = Path("/srv/vivibox")


@pytest.fixture(scope="module")
def task(tmp_path_factory):
    if why := helper_missing():
        pytest.skip(why)
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
        "echo /elsewhere/objects > .git/objects/info/alternates",
        "rm .git/objects/info/alternates",
        # A folder that holds a read-only mount, renamed, would take the mount with it.
        "mv .git/objects .git/objects-moved",
        "mv .git/objects/info .git/objects/info-moved",
    ],
)
def test_agent_cannot_change_what_the_host_runs(task, attack):
    assert agent(task, attack).returncode != 0
    repo.check_protection(task["repo"], task["repo"].parent / ".task")


def test_the_agent_cannot_write_alternates(task):
    """Alternates name other object stores, which git on the host would read from."""
    for name in ("alternates", "http-alternates"):
        said = agent(task, f"echo /elsewhere/objects > .git/objects/info/{name}")
        assert said.returncode != 0 and "Read-only file system" in said.stderr, said


# git's own upkeep writes objects/info (the commit-graph, packs): what is mounted there must not stop it.
NORMAL_GIT = (
    "set -e; git checkout -q -b upkeep; for n in 1 2 3; do echo $n > upkeep-$n.txt; git add upkeep-$n.txt; "
    'git -c gc.auto=1 -c gc.autoPackLimit=1 -c gc.autoDetach=false commit -q -m "Upkeep $n"; done; '
    "git -c gc.autoDetach=false gc --auto --quiet; git commit-graph write --reachable; "
    "git repack -d -q; git maintenance run --quiet; git gc --quiet; test -s .git/objects/info/commit-graph; "
    "git checkout -q -"  # back where the work is, for the tests after it
)


def test_the_agents_own_git_upkeep_still_works(task):
    said = agent(task, NORMAL_GIT)
    assert said.returncode == 0, said.stderr
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


def test_verify_checks_the_committed_work(task, capsys):
    from vivibox import gate
    from vivibox.task import Task

    t = Task(task["repo"].parent)
    t.plan_path.write_text(t.plan_path.read_text().replace(gate.PLACEHOLDER, "hello.txt exists"))
    gate.accept_plan(t)
    checklist = t.meta / "handoff" / gate.CRITERIA_FILE
    checklist.write_text(checklist.read_text().replace("- [ ]", "- [x]"))
    assert main(["verify", "repocheck-1"]) == 0
    log = sorted((t.meta / "log").glob("verify-*.log"))[-1].read_text()
    assert "[exit 0 after " in log and "# test -f hello.txt: ok after " in log


def test_deleting_git_dir_leaves_protected_files(task):
    # Runs last: the agent can destroy its own history, but not what the host would run.
    agent(task, "rm -rf .git")
    repo.check_protection(task["repo"], task["repo"].parent / ".task")


@pytest.fixture(scope="module")
def lib_task(tmp_path_factory):
    """A task whose repository has a submodule: its git dir under .git/modules is protected alike."""
    if why := helper_missing():
        pytest.skip(why)
    image.build()
    tmp = tmp_path_factory.mktemp("repo-live-lib")
    mp = pytest.MonkeyPatch()
    # Submodules from local paths are blocked by default since git 2.38.
    mp.setenv("GIT_CONFIG_COUNT", "1")
    mp.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    mp.setenv("GIT_CONFIG_VALUE_0", "always")
    lib = make_repo(tmp / "lib")
    source = make_repo(tmp / "source")
    subprocess.run(["git", "submodule", "add", "-q", str(lib), "lib"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Add lib"], cwd=source, check=True)
    tasks_dir = TASKS_ROOT / f".pytest-{secrets.token_hex(4)}"
    cfg = tmp / "config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{tasks_dir}"\n[roles.planner]\nharness = "manual"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )
    (cfg / "projects" / "libcheck.toml").write_text(f'repo = "{source}"\nverify = ["true"]\n')
    mp.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    assert main(["new", "libcheck", "Check the submodule", "--draft"]) == 0
    assert main(["pod", "up", "libcheck-1"]) == 0
    pod = task_pod("libcheck-1")
    try:
        yield {"pod": pod, "repo": pod.repo}
    finally:
        pod.remove()
        task_secrets.remove("libcheck-1")
        shutil.rmtree(tasks_dir, ignore_errors=True)
        mp.undo()


@pytest.mark.parametrize(
    "attack",
    [
        "echo /elsewhere/objects > .git/modules/lib/objects/info/alternates",
        "echo '#!/bin/sh' > .git/modules/lib/hooks/post-checkout",
        "git -C lib config core.fsmonitor 'touch /tmp/x'",
        "mv .git/modules/lib .git/modules/lib-moved",
        "mv .git/modules .git/modules-moved",
        "mv .git/modules/lib/objects .git/modules/lib/objects-moved",
    ],
)
def test_the_agent_cannot_change_what_the_host_runs_in_a_submodule(lib_task, attack):
    assert agent(lib_task, attack).returncode != 0
    repo.check_protection(lib_task["repo"], lib_task["repo"].parent / ".task")


def test_the_submodules_alternates_are_read_only(lib_task):
    said = agent(lib_task, "echo /elsewhere/objects > .git/modules/lib/objects/info/alternates")
    assert said.returncode != 0 and "Read-only file system" in said.stderr, said


def test_the_agents_own_git_upkeep_in_a_submodule_still_works(lib_task):
    said = agent(lib_task, f"cd lib && {NORMAL_GIT.replace('.git/objects', '../.git/modules/lib/objects')}")
    assert said.returncode == 0, said.stderr
    repo.check_protection(lib_task["repo"], lib_task["repo"].parent / ".task")
