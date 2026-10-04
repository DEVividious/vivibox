"""The task clone inside a real pod: the agent commits but cannot touch git files the host runs.

Needs host/setup.sh to have run and the agent image. Run with: uv run pytest -m docker tests/test_repo_live.py
"""

import contextlib
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import LOCAL_SUBMODULES, make_repo

from vivibox import actions, control, image, repo, reviewing
from vivibox import secrets as task_secrets
from vivibox.cli import main
from vivibox.config import ConfigError, load_config
from vivibox.pod_egress import helper_missing
from vivibox.task import DELETE

pytestmark = pytest.mark.docker

TASKS_ROOT = Path("/srv/vivibox")


def ready() -> None:
    """Skips where no pod can be made, before anything is: no tasks directory, no root helper."""
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)


def clean_up(name: str) -> None:
    """Exactly what a fixture made in the tasks directory, which is yours too: the tasks of its
    project, by the product's own removal (the pod, its disk, networks and rules, the secrets
    of each of its containers), and the project's counter."""
    for root in sorted(TASKS_ROOT.glob(f"{name}-*")):
        try:
            found, project = actions.load(root.name, DELETE)
            actions.remove(found, project)
        except Exception as e:  # said; what nothing holds still goes
            print(f"{root.name} was not removed cleanly: {e}")
            with contextlib.suppress(Exception):
                actions.task_pod(root.name).remove()
        for forget in (task_secrets.remove, control.forget, reviewing.forget):
            forget(root.name)
        shutil.rmtree(root, ignore_errors=True)
    (TASKS_ROOT / f".last-{name}").unlink(missing_ok=True)


@contextlib.contextmanager
def a_task(
    tmp: Path, kind: str, source: Path, goal: str, verify: str, git_settings: str = ""
) -> Iterator[dict]:
    """A draft task for source with its pod up, made by the command line as you would make it,
    and gone with everything of it when the block ends, however it ends.

    The root helper attaches a task's disk only in a folder directly under the tasks directory,
    so the task is made there, among yours, under a project named for this run alone: its id,
    <kind><hex>-1, is the first of that project. The pod's networks are from the pool this
    machine was set up with. git_settings: git configuration the host's git needs while the
    task is made (it takes no GIT_* variable from the environment, so it reads the setting
    where git keeps yours: a configuration folder of the fixture's, for that step alone; your
    home stays yours, where the Docker client keeps its own settings)."""
    try:
        machine = load_config()  # this machine's pool and address, before the fixture's own files
    except ConfigError as e:
        pytest.skip(f"this machine's config.toml cannot be read: {e}")
    name = f"{kind}{secrets.token_hex(3)}"
    task_id = f"{name}-1"
    cfg = tmp / "config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{TASKS_ROOT}"\n[roles.planner]\nharness = "manual"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
        f'[network]\npool = "{machine.network_pool}"\nhost_address = "{machine.host_address}"\n'
    )
    (cfg / "projects" / f"{name}.toml").write_text(f'repo = "{source}"\nverify = ["{verify}"]\n')
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
        mp.setenv("XDG_DATA_HOME", str(tmp / "data"))  # the history a removal writes, never yours
        with pytest.MonkeyPatch.context() as making:
            if git_settings:
                settings = tmp / "xdg" / "git" / "config"
                settings.parent.mkdir(parents=True)
                settings.write_text(git_settings)
                making.setenv("XDG_CONFIG_HOME", str(tmp / "xdg"))
            assert main(["new", name, goal, "--draft"]) == 0
        assert (TASKS_ROOT / task_id / ".task").is_dir(), f"the task is not {task_id}"
        assert main(["pod", "up", task_id]) == 0
        pod = actions.task_pod(task_id)
        yield {"id": task_id, "pod": pod, "source": source, "repo": pod.repo}
    finally:
        clean_up(name)
        mp.undo()


@pytest.fixture(scope="module")
def task(tmp_path_factory):
    ready()
    image.build()
    tmp = tmp_path_factory.mktemp("repo-live")
    source = make_repo(tmp / "source")
    with a_task(tmp, "repocheck", source, "Check the task clone", "test -f hello.txt") as made:
        yield made


def agent(task, script: str) -> subprocess.CompletedProcess:
    return task["pod"].exec("bash", "-c", script, check=False)


def test_agent_commits_as_you_on_task_branch(task):
    commit = "echo hi > hello.txt && git add hello.txt && git commit -q -m 'Add hello'"
    result = agent(task, f"{commit} && git log -1 --format='%an <%ae> %s'")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Test User <test@example.com> Add hello"
    assert agent(task, "git branch --show-current").stdout.strip() == f"vivibox/{task['id']}"


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
    assert main(["review", task["id"]]) == 0
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s", f"vivibox/{task['id']}"],
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
    assert main(["verify", task["id"]]) == 0
    log = sorted((t.meta / "log").glob("verify-*.log"))[-1].read_text()
    assert "[exit 0 after " in log and "# test -f hello.txt: ok after " in log


def test_deleting_git_dir_leaves_protected_files(task):
    # Runs last: the agent can destroy its own history, but not what the host would run.
    agent(task, "rm -rf .git")
    repo.check_protection(task["repo"], task["repo"].parent / ".task")


@pytest.fixture(scope="module")
def lib_task(tmp_path_factory):
    """A task whose repository has a submodule: its git dir under .git/modules is protected alike."""
    ready()
    image.build()
    tmp = tmp_path_factory.mktemp("repo-live-lib")
    lib = make_repo(tmp / "lib")
    source = make_repo(tmp / "source")
    # Submodules from local paths are blocked by default since git 2.38: the fixture's own git
    # is told on its command line, and the host's git that fetches the task's submodule by the
    # setting where git keeps yours (a_task).
    allowed = ["git", "-c", "protocol.file.allow=always"]
    subprocess.run([*allowed, "submodule", "add", "-q", str(lib), "lib"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Add lib"], cwd=source, check=True)
    with a_task(tmp, "libcheck", source, "Check the submodule", "true", LOCAL_SUBMODULES) as made:
        yield made


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
