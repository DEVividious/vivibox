"""History on request: a reader's view of the commits is shallow, history_depth commits of the
task's branch, and the host alone deepens the planner's, for a commit id the planner asks for in
its handoff, from the task's clone and then from the remote your base branch tracks. Real git in
temporary folders; no pod."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_repo
from test_control import commit
from test_control import docker as docker  # noqa: F401  (the fixture: a machine and a recording Docker)

from vivibox import actions, history, reading, repo, reviewing, supervise
from vivibox.config import config_dir, load_config
from vivibox.task import create_task

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


# --- the host's deepening of the planner's copy, for one commit id --------------------------------


class Planned:
    """A task of yours whose planner's copy is made, as the host makes it, depth commits deep."""

    def __init__(self, source: Path, tasks: Path, depth: int = 5, base_ref: str = ""):
        self.source, self.depth = source, depth
        self.task = create_task(tasks, "demo", "Read history", "+++\n+++\n")
        base = repo.prepare(source, self.task.repo, self.task.id, self.task.meta, base_ref)
        branch = repo.start_branch(source, base_ref)
        self.task.set_base_commit(base, branch, history.remote(source, branch))
        root = tasks / "disk"
        self.pod = SimpleNamespace(control_src=root / "control-src", control_work=root / "control-work")
        self.pod.control_src.mkdir(parents=True)
        self.pod.control_work.mkdir()
        self.view = self.pod.control_src
        reading.view(self.task.repo, repo.branch_name(self.task.id), self.view, depth=depth)

    def ensure(self, commit: str) -> history.Outcome:
        return history.ensure(self.task, self.pod, commit, depth=self.depth, source=self.source)

    def looks(self) -> tuple[str, str, str]:
        """What the planner's git sees of its copy: its shallow boundary, its commits, its head."""
        return shallow(self.view), git(self.view, "rev-list", "HEAD"), git(self.view, "rev-parse", "HEAD")


@pytest.fixture
def made(mine, tmp_path) -> tuple[Planned, list[str]]:
    """A task on your repository of 31 commits, its planner's copy 5 deep; your commits, oldest first."""
    ids = git(mine, "rev-list", "--reverse", "HEAD").split()
    return Planned(mine, tmp_path / "tasks"), ids


def test_an_older_commit_of_the_branch_is_brought_from_the_tasks_clone(made):
    """No remote, no network: the clone holds your checkout's whole history. The planner's git
    then shows the commit as a commit and its change, its parent there too."""
    planned, ids = made
    older = ids[10]
    assert git(planned.view, "cat-file", "-t", ids[-1]) == "commit"
    found = planned.ensure(older)
    assert found.said == "available" and found.available
    assert found.steps == 4, "each step five commits deeper, until the commit and its parent are there"
    assert git(planned.view, "remote") == "", "fetched from the clone by its path, no remote named"
    assert git(planned.view, "rev-parse", f"{older}^") == ids[9]
    shown = git(planned.view, "show", "--format=%H", older)
    assert shown.startswith(older) and "-f 8\n+f 9" in shown, "the commit's own change"
    assert planned.task.read_state().history_remote == ""


def test_a_commit_already_in_the_copy_is_available_with_no_step(made):
    planned, ids = made
    found = planned.ensure(ids[-2][:7])
    assert found.said == "available" and found.steps == 0


def test_a_refresh_after_history_was_brought_keeps_it(made):
    planned, ids = made
    planned.ensure(ids[10])
    after = shallow(planned.view)
    newer = commits(planned.task.repo, 2, "g")
    reading.view(planned.task.repo, repo.branch_name(planned.task.id), planned.view, depth=5)
    assert git(planned.view, "rev-parse", "HEAD") == newer[-1]
    assert shallow(planned.view) == after and git(planned.view, "rev-parse", f"{ids[10]}^") == ids[9]


NOT_IDS = [
    "", "abc", "abcdef", "a" * 65, "ABCDEF12", "zzzzzzz", "main", "HEAD", "HEAD~3", "1234567..89abcde",
    "--upload-pack=touch /tmp/x", "-1234567", "1234567 --depth=1", "https://example.com/r.git",
    "git@example.com:r.git", "refs/heads/main", "1234567\n", " 1234567",
]  # fmt: skip


@pytest.mark.parametrize("asked", NOT_IDS)
def test_what_is_no_commit_id_is_refused_before_any_git_runs(made, monkeypatch, asked):
    planned, _ = made
    before = planned.looks()
    ran = []
    monkeypatch.setattr(repo, "run_git", lambda *a, **kw: ran.append(a))
    monkeypatch.setattr(repo, "git", lambda *a, **kw: ran.append(a))
    found = planned.ensure(asked)
    assert found.said == "not a commit id" and not found.available and found.steps == 0
    assert ran == [] and planned.looks() == before


def test_a_commit_the_clone_has_not_and_no_remote_recorded_is_said_so(made):
    """The clone is all there is: deepened until it has no more, then nothing to fetch from."""
    planned, _ = made
    before = planned.looks()
    found = planned.ensure("0123456789abcdef0123456789abcdef01234567")
    assert found.said == "no remote recorded" and not found.available
    assert planned.looks() == before, "the copy as it was"


def test_the_steps_are_bounded_and_a_commit_past_them_is_not_found(made, monkeypatch):
    planned, ids = made
    monkeypatch.setattr(history, "STEPS", 2)
    before = planned.looks()
    found = planned.ensure(ids[3])
    assert found.said == "not found within 10 commits before the base on its branch"
    assert found.steps == 2 and planned.looks() == before, "the copy as it was"


def test_a_step_past_its_time_ends_the_request_with_the_copy_as_it_was(made, monkeypatch):
    planned, ids = made
    monkeypatch.setattr(repo, "CLONE_TIMEOUT", 1e-6)
    before = planned.looks()
    found = planned.ensure(ids[3])
    assert found.said == "could not be fetched from the task's clone: no answer within 1e-06 s"
    assert found.steps == 1 and planned.looks() == before
    assert not (planned.view / ".git" / "shallow.lock").exists(), "the next fetch is not refused"


def test_a_clone_not_as_recorded_is_not_read(made):
    planned, ids = made
    (planned.task.repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n")
    before = planned.looks()
    found = planned.ensure(ids[10])
    assert found.said.startswith("could not be fetched from the task's clone: ")
    assert "hooks/post-checkout" in found.said and found.steps == 0 and planned.looks() == before


def marking(path: Path, mark: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$0" >> "{mark}"\n')
    path.chmod(0o755)
    return path


def test_what_the_writer_sets_in_its_clone_is_neither_run_nor_followed(made, tmp_path, monkeypatch):
    """Past the check of the clone (a change made the moment after it), git on the host fetches
    from the clone by its path: the writer's configuration names a remote, how to fetch, how to
    serve a fetch, hooks and a file-system monitor, and none of it runs or is followed."""
    planned, ids = made
    mark = tmp_path / "ran"
    program = marking(tmp_path / "writers" / "program", mark)
    hooks = tmp_path / "writers" / "hooks"
    for name in ("post-checkout", "post-merge", "reference-transaction", "pre-auto-gc", "post-index-change"):
        marking(hooks / name, mark)
        marking(planned.task.repo / ".git" / "hooks" / name, mark)
    elsewhere = make_repo(tmp_path / "elsewhere")
    for key, value in (
        ("remote.origin.url", str(elsewhere)), ("remote.origin.uploadpack", str(program)),
        ("fetch.recurseSubmodules", "true"), ("uploadpack.packObjectsHook", str(program)),
        ("uploadpack.allowAnySHA1InWant", "true"), ("core.hooksPath", str(hooks)),
        ("core.fsmonitor", str(program)), ("core.sshCommand", str(program)),
    ):  # fmt: skip
        git(planned.task.repo, "config", key, value)
    monkeypatch.setattr(repo, "guard", lambda clone: None)
    recorded = planned.task.read_state().history_remote
    found = planned.ensure(ids[10])
    assert found.available and not mark.exists(), "nothing of the writer's ran"
    assert planned.task.read_state().history_remote == recorded == ""
    assert git(planned.view, "config", "--local", "--list").splitlines() == [
        "core.repositoryformatversion=0", "core.filemode=true", "core.bare=false",
        "core.logallrefupdates=true", "core.symlinks=false",
    ], "the copy's configuration is the host's"  # fmt: skip


@pytest.fixture
def behind(tmp_path) -> tuple[Path, list[str]]:
    """Your remote, a bare repository of 31 commits; its commits, oldest first."""
    work = make_repo(tmp_path / "upstream-work")
    commits(work, 30)
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, git(work, "rev-list", "--reverse", "HEAD").split()


def shallow_checkout(remote: str, where: Path, depth: int = 8) -> Path:
    """Your checkout, cloned from remote with --depth, as a large repository often is."""
    subprocess.run(["git", "clone", "-q", "--depth", str(depth), remote, str(where)], check=True)
    git(where, "config", "user.name", "Test User")
    git(where, "config", "user.email", "test@example.com")
    return where


def test_an_older_commit_your_shallow_checkout_has_not_comes_from_your_remote(behind, tmp_path):
    """The clone has what your checkout has; the rest comes from the remote your branch
    tracks, as recorded when the task was made, along your base branch."""
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    planned = Planned(checkout, tmp_path / "tasks")
    assert planned.task.read_state().history_remote == f"file://{bare}"
    found = planned.ensure(ids[10])
    assert found.available and found.steps == 5, (
        "two steps from the clone, which has eight, three from the remote"
    )
    assert git(planned.view, "rev-parse", f"{ids[10]}^") == ids[9]
    assert git(planned.task.repo, "rev-list", "--count", "HEAD") == "8", "the clone as it was"
    assert git(checkout, "rev-list", "--count", "HEAD") == "8", "and your checkout"


def test_the_clones_own_remote_is_never_fetched_from(behind, tmp_path):
    """A task with no remote recorded: the clone's configuration names one that has the
    commit, and the request still ends where the clone does."""
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    git(checkout, "remote", "remove", "origin")
    planned = Planned(checkout, tmp_path / "tasks")
    git(planned.task.repo, "remote", "add", "origin", f"file://{bare}")
    repo.record_protection(planned.task.repo, planned.task.meta)  # as if it had always been there
    found = planned.ensure(ids[10])
    assert found.said == "no remote recorded" and found.steps == 2


def test_a_task_started_on_no_branch_has_none_to_fetch_along(behind, tmp_path):
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    git(checkout, "checkout", "-q", "--detach")
    planned = Planned(checkout, tmp_path / "tasks")
    found = planned.ensure(ids[10])
    assert (
        found.said == f"could not be fetched from file://{bare}: the task started on no branch to fetch along"
    )


def test_a_remotes_address_is_said_without_the_password_written_into_it():
    assert history.shown("https://me:s3cret@example.com/r.git") == "https://example.com/r.git"
    assert history.shown("git@example.com:r.git") == "git@example.com:r.git"


class OverSsh:
    """Your shallow checkout, whose remote answers only to the ssh command your shell names; a
    git first on PATH writes down every git started and whether it was handed that command."""

    def __init__(self, bare: Path, tmp: Path):
        self.checkout = shallow_checkout(f"file://{bare}", tmp / "mine")
        self.address = f"ssh://git.example.invalid{bare}"
        git(self.checkout, "remote", "set-url", "origin", self.address)
        self.asked, self.log = tmp / "asked-of-your-ssh", tmp / "git.log"
        # ssh as git runs it: the host, then the command for the far side, which is run here.
        self.ssh = marking(tmp / "yours" / "ssh", self.asked)
        self.ssh.write_text(
            f'#!/bin/sh\necho "$*" >> "{self.asked}"\nfor far; do :; done\nexec sh -c "$far"\n'
        )
        real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
        self.spy = tmp / "spy"
        marking(self.spy / "git", tmp / "unused").write_text(
            f'#!/bin/sh\n{{ echo "== git $*"; env | grep "^GIT_SSH_COMMAND=" ; }} >> "{self.log}"\n'
            f'exec "{real}" "$@"\n'
        )

    def reach(self, monkeypatch) -> None:
        """Your shell from here on: your ssh command exported, the spy first on PATH."""
        import os

        monkeypatch.setenv("GIT_SSH_COMMAND", str(self.ssh))
        monkeypatch.setenv("PATH", f"{self.spy}{os.pathsep}{os.environ['PATH']}")

    def handed(self) -> list[str]:
        """The gits started since reach that were handed your ssh command, by their arguments."""
        if not self.log.exists():
            return []
        runs = [run.splitlines() for run in self.log.read_text().split("== git ")[1:]]
        return [lines[0] for lines in runs if f"GIT_SSH_COMMAND={self.ssh}" in lines[1:]]


def test_your_remote_is_reached_your_way_for_a_task_that_starts_from_your_work(behind, tmp_path, monkeypatch):
    """The fetches from your remote are the one git here handed how you reach it (as the
    submodules of a new clone are): your ssh command, asked once a step."""
    bare, ids = behind
    yours = OverSsh(bare, tmp_path)
    planned = Planned(yours.checkout, tmp_path / "tasks")
    assert planned.task.read_state().history_remote == yours.address
    yours.reach(monkeypatch)
    found = planned.ensure(ids[10])
    handed = yours.handed()
    assert found.available and found.steps == 5
    assert len(handed) == 3 and all(c.startswith("fetch ") and yours.address in c for c in handed), handed
    assert len(yours.asked.read_text().splitlines()) == 3


def test_your_remote_is_not_reached_for_a_task_on_work_not_yet_yours(behind, tmp_path, monkeypatch):
    """A task whose base is a task's work you have not accepted (refs/vivibox/<id>) and is on no
    branch of yours: the commit is not your own, and no git is sent to your remote for it."""
    bare, ids = behind
    yours = OverSsh(bare, tmp_path)
    git(yours.checkout, "switch", "-q", "-c", "feature")
    git(yours.checkout, "commit", "-q", "--allow-empty", "-m", "A task's work")
    planned = Planned(yours.checkout, tmp_path / "tasks")
    work = git(yours.checkout, "rev-parse", "HEAD")
    git(yours.checkout, "update-ref", "refs/vivibox/demo-9", work)
    git(yours.checkout, "switch", "-q", "--detach", "main")
    git(yours.checkout, "branch", "-q", "-D", "feature")
    yours.reach(monkeypatch)
    found = planned.ensure(ids[10])
    said = "the task does not start from your own work"
    assert found.said == f"could not be fetched from {yours.address}: {said}"
    assert yours.handed() == [] and not yours.asked.exists()


def test_what_your_remote_says_names_it_without_the_password_written_into_it(behind, tmp_path):
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    git(checkout, "remote", "set-url", "origin", "https://me:s3cret@127.0.0.1:9/r.git")
    planned = Planned(checkout, tmp_path / "tasks")
    found = planned.ensure(ids[10])
    assert found.said.startswith("could not be fetched from https://127.0.0.1:9/r.git: ")
    assert "s3cret" not in found.said and found.steps == 3, found.said


def test_a_project_whose_checkout_vivibox_no_longer_knows_is_not_fetched_for(behind, tmp_path, monkeypatch):
    """Whether the base is your own work is asked of your checkout, by the project's file."""
    from vivibox.config import ConfigError

    bare, ids = behind
    planned = Planned(shallow_checkout(f"file://{bare}", tmp_path / "mine"), tmp_path / "tasks")
    asked = []

    def gone(name):
        asked.append(name)
        raise ConfigError("no project demo")

    monkeypatch.setattr(history, "load_project", gone)
    found = history.ensure(planned.task, planned.pod, ids[10], depth=5)
    assert found.said == f"could not be fetched from file://{bare}: no project demo" and asked == ["demo"]
