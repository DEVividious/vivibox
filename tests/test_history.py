"""History on request: a reader's view of the commits is shallow, history_depth commits of the
task's branch, and the host alone deepens the planner's, for a commit id the planner asks for in
its handoff, from the task's clone and then from the remote your base branch tracks. Real git in
temporary folders; no pod."""

import subprocess
from pathlib import Path

import pytest
from conftest import make_repo
from test_control import commit
from test_control import docker as docker  # noqa: F401  (the fixture: a machine and a recording Docker)

from vivibox import actions, history, reading, repo, reviewing, supervise
from vivibox.config import config_dir, load_config

BRANCH = repo.branch_name("demo-1")


def git(where: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commits(where: Path, count: int, name: str = "f") -> list[str]:
    """count commits on where's branch, each changing name; their ids, oldest first."""
    made = []
    for i in range(count):
        (where / name).write_text(f"{name} {i}\n")
        git(where, "add", name)
        git(where, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", f"{name} {i}")
        made.append(git(where, "rev-parse", "HEAD"))
    return made


def a_task(source: Path, root: Path) -> Path:
    """A task's clone of source, as vivibox makes one; its path."""
    meta = root / ".task"
    meta.mkdir(parents=True)
    repo.prepare(source, root / "repo", "demo-1", meta)
    return root / "repo"


def counted(view: Path) -> int:
    return int(git(view, "rev-list", "--count", "HEAD"))


def shallow(view: Path) -> str:
    path = view / ".git" / "shallow"
    return path.read_text() if path.exists() else ""


@pytest.fixture
def mine(tmp_path) -> Path:
    """Your repository: thirty commits after the first."""
    source = make_repo(tmp_path / "mine")
    commits(source, 30)
    return source


def test_a_view_of_a_full_repository_holds_its_depth_and_no_more(mine, tmp_path):
    clone = a_task(mine, tmp_path / "task")
    view = tmp_path / "view"
    view.mkdir()
    reading.view(clone, BRANCH, view, depth=5)
    assert counted(view) == 5 and shallow(view), "five commits of the branch, the rest not taken"
    assert git(view, "rev-parse", "HEAD") == git(clone, "rev-parse", "HEAD")
    assert counted(clone) == 31, "the task's clone keeps its whole history"


def test_a_view_of_a_clone_of_a_shallow_checkout_holds_what_the_clone_has(mine, tmp_path):
    checkout = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "8", f"file://{mine}", str(checkout)], check=True)
    clone = a_task(checkout, tmp_path / "task")
    view, deeper = tmp_path / "view", tmp_path / "deeper"
    view.mkdir()
    deeper.mkdir()
    reading.view(clone, BRANCH, view, depth=5)
    assert counted(view) == 5 and git(view, "rev-parse", "HEAD") == git(mine, "rev-parse", "HEAD")
    reading.view(clone, BRANCH, deeper, depth=20)
    assert counted(deeper) == 8, "as deep as the clone, which is as deep as your checkout"


def test_a_refresh_keeps_the_views_depth(mine, tmp_path):
    """The tip moves; the view takes the new commits and goes no deeper and no shallower."""
    clone = a_task(mine, tmp_path / "task")
    view = tmp_path / "view"
    view.mkdir()
    reading.view(clone, BRANCH, view, depth=5)
    boundary = shallow(view)
    newer = commits(clone, 3, "g")
    reading.view(clone, BRANCH, view, depth=5)
    assert git(view, "rev-parse", "HEAD") == newer[-1]
    assert counted(view) == 8 and shallow(view) == boundary


def test_a_view_made_whole_before_stays_whole(mine, tmp_path):
    """A view of a task made before views were shallow is refreshed as it is."""
    clone = a_task(mine, tmp_path / "task")
    view = tmp_path / "view"
    view.mkdir()
    git(view, "init", "-q", "--template=", "-b", BRANCH)
    git(view, "fetch", "-q", "--no-tags", str(clone), f"refs/heads/{BRANCH}")
    git(view, "reset", "-q", "--hard", "FETCH_HEAD")
    commits(clone, 2, "g")
    reading.view(clone, BRANCH, view, depth=5)
    assert counted(view) == 33 and not shallow(view)


def depth_of(depth: int) -> None:
    """config.toml with history_depth set."""
    path = config_dir() / "config.toml"
    path.write_text(f"history_depth = {depth}\n" + path.read_text())


def test_both_readers_views_are_made_as_deep_as_config_toml_says(docker):  # noqa: F811
    """The planner's copy, brought up to date before its turn, and the reviewer's of a round."""
    depth_of(2)
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    for i in range(4):
        commit(task.repo, "calc.py", f"x = {i}\n")
    task.set_verified_commit(git(task.repo, "rev-parse", "HEAD"))
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, pod, load_config())
    sup.planner.bring_up_to_date()
    assert counted(pod.control_src) == 2
    reviewing.up(task, pod, load_config())
    assert counted(Path(pod.review_src)) == 2


# --- the remote your base branch tracks, recorded when the task is made ---------------------------


def test_the_remote_recorded_is_the_one_your_base_branch_tracks(mine):
    git(mine, "remote", "add", "origin", "https://example.com/origin.git")
    git(mine, "remote", "add", "upstream", "https://example.com/upstream.git")
    git(mine, "config", "branch.main.remote", "upstream")
    assert history.remote(mine, "main") == "https://example.com/upstream.git"


@pytest.mark.parametrize("branch", ["main", ""], ids=["tracking nothing", "no branch"])
def test_without_a_remote_of_its_own_the_base_branch_has_origins(mine, branch):
    git(mine, "remote", "add", "origin", "https://example.com/origin.git")
    git(mine, "remote", "add", "upstream", "https://example.com/upstream.git")
    assert history.remote(mine, branch) == "https://example.com/origin.git"


def test_a_repository_without_remotes_records_none(mine):
    git(mine, "config", "branch.main.remote", "gone")
    assert history.remote(mine, "main") == ""


def test_a_task_records_the_remote_its_base_branch_tracks_when_it_is_made(env):
    git(env / "repo", "remote", "add", "origin", "https://example.com/demo.git")
    task = actions.create("demo", "Add divide")
    st = task.read_state()
    assert st.history_remote == "https://example.com/demo.git" and st.base_branch == "main"


def test_a_task_made_before_the_remote_was_recorded_is_run_as_one_without(env):
    """A state.json of this layout without history_remote (0.2.0's release candidate)."""
    import json

    from vivibox.task import CURRENT, layout

    task = actions.create("demo", "Add divide")
    path = task.meta / "state.json"
    data = json.loads(path.read_text())
    data.pop("history_remote", None)
    path.write_text(json.dumps(data))
    st = task.read_state()
    assert layout(st) == CURRENT and st.history_remote == ""
