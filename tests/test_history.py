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

from vivibox import (
    actions,
    control,
    history,
    orchestration,
    prompts,
    reading,
    repo,
    reviewing,
    supervise,
    supervisor,
)
from vivibox.config import config_dir, load_config
from vivibox.harness import Harness, Turn
from vivibox.states import State
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
    commits(config_dir().parent / "repo", 4)  # your history before the task's base
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    commit(task.repo, "calc.py", "x = 1\n")
    task.set_verified_commit(git(task.repo, "rev-parse", "HEAD"))
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, pod, load_config())
    sup.planner.bring_up_to_date()
    assert counted(pod.control_src) == 2
    reviewing.up(task, pod, load_config())
    assert counted(Path(pod.review_src)) == 2


def test_the_planner_the_command_line_runs_has_its_requests_answered(docker):  # noqa: F811
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    head = commit(task.repo, "calc.py", "x = 1\n")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, pod, load_config())
    sup.planner.bring_up_to_date()
    (task.meta / "handoff" / "history-request.md").write_text(f"{head}\n")
    assert sup.planner.answer_history() is True
    assert (task.meta / "handoff" / "history.md").read_text() == f"{head}: available\n"


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
    "", "abc", "abcdef", "a" * 65, "ABCDEFG1", "zzzzzzz", "main", "HEAD", "HEAD~3", "1234567..89abcde",
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


def test_a_commit_a_whole_clone_has_not_is_not_found(made):
    """The clone of a whole checkout holds the branch's whole history as of the base: deepened
    until it has no more, a commit it lacks is on no history of the base branch."""
    planned, _ = made
    before = planned.looks()
    found = planned.ensure("0123456789abcdef0123456789abcdef01234567")
    assert found.said == "not found within 100 commits before the base on its branch" and not found.available
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
    said = "the task started on no branch to fetch along"
    assert found.said == f"could not be fetched from a repository on this machine: {said}"


def test_a_remotes_address_is_said_without_the_password_written_into_it():
    assert history.shown("https://me:s3cret@example.com/r.git") == "https://example.com"
    assert (
        history.shown("https://u:p@ss@host.example/group/r.git?private_token=t0k") == "https://host.example"
    )
    assert history.shown("ssh://git@host.example:2222/srv/r.git") == "ssh://host.example"
    assert history.shown("git@example.com:org/r.git") == "example.com"
    assert history.shown("file:///home/you/r.git") == "a repository on this machine"
    assert history.shown("/home/you/r.git") == "a repository on this machine"


class OverSsh:
    """Your shallow checkout, whose remote answers only to the ssh command your shell names; a
    git first on PATH writes down every git started and whether it was handed that command."""

    def __init__(self, bare: Path, tmp: Path, whole: bool = False):
        if whole:
            subprocess.run(["git", "clone", "-q", f"file://{bare}", str(tmp / "mine")], check=True)
            git(tmp / "mine", "config", "user.name", "Test User")
            git(tmp / "mine", "config", "user.email", "test@example.com")
            self.checkout = tmp / "mine"
        else:
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
    assert found.said == f"could not be fetched from ssh://git.example.invalid: {said}"
    assert yours.handed() == [] and not yours.asked.exists()


def test_what_your_remote_says_names_it_without_the_password_written_into_it(behind, tmp_path):
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    git(checkout, "remote", "set-url", "origin", "https://me:s3cret@127.0.0.1:9/r.git")
    planned = Planned(checkout, tmp_path / "tasks")
    found = planned.ensure(ids[10])
    assert found.said.startswith("could not be fetched from https://127.0.0.1: ")
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
    assert found.said == "could not be fetched from a repository on this machine: no project demo"
    assert asked == ["demo"]


# --- the request in the handoff, answered at the end of a reader's turn ---------------------------


def request(planned: Planned, *lines: str) -> Path:
    path = planned.task.meta / "handoff" / "history-request.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def answered(planned: Planned) -> list[str]:
    return (planned.task.meta / "handoff" / "history.md").read_text().splitlines()


def of(planned: Planned, *kinds: str) -> list[tuple[str, dict]]:
    return [(e["type"], e["data"]) for e in planned.task.events() if e["type"] in kinds]


KINDS = ("history_requested", "history_acquired", "history_refused")


def test_a_request_is_answered_a_line_an_id_and_taken_away(made):
    """One line a commit, in the order asked; a line that is no commit id is said to be none,
    and the others are looked for all the same. The request goes, the answer stays."""
    planned, ids = made
    asked = request(planned, ids[10], "main", "", "  ", ids[-1][:7])
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is True
    assert not asked.exists()
    assert answered(planned) == [
        f"{ids[10]}: available",
        "main: not a commit id",
        f"{ids[-1][:7]}: available",
    ]
    assert of(planned, *KINDS) == [
        ("history_requested", {"ids": [ids[10], "main", ids[-1][:7]], "round": 1}),
        ("history_acquired", {"id": ids[10], "outcome": "available", "steps": 4}),
        ("history_refused", {"id": "main", "outcome": "not a commit id"}),
        ("history_acquired", {"id": ids[-1][:7], "outcome": "available", "steps": 0}),
    ]
    assert git(planned.view, "rev-parse", f"{ids[10]}^") == ids[9], "and the copy, brought up to date, has it"


def test_without_a_request_there_is_nothing_to_answer(made):
    planned, _ = made
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is False
    assert not (planned.task.meta / "handoff" / "history.md").exists() and of(planned, *KINDS) == []


def test_a_request_holds_five_ids_and_the_rest_are_not_looked_for(made, monkeypatch):
    planned, ids = made
    looked = []
    monkeypatch.setattr(history, "ensure", lambda task, pod, commit, **kw: looked.append(commit) or
                        history.Outcome(commit, "available"))  # fmt: skip
    request(planned, *ids[20:27])
    history.answer(planned.task, planned.pod, depth=5, source=planned.source)
    assert looked == ids[20:25]
    assert answered(planned)[5:] == ["2 more lines not looked for: a request holds at most 5 ids"]


def test_the_fourth_request_of_a_phase_is_refused_and_the_turn_goes_on(made):
    """Three requests a planning phase are answered; the fourth is refused, nothing is fetched,
    and the reader is not given a turn for it. A new phase (the task moved on) has three again."""
    planned, ids = made
    for i in range(3):
        request(planned, ids[-2 - i])
        assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is True
    before = planned.looks()
    asked = request(planned, ids[3])
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is False
    assert not asked.exists() and planned.looks() == before
    assert answered(planned) == [f"{ids[3]}: refused: 3 requests were answered in this phase; ask the person"]
    past = "refused: 3 requests were answered in this phase; ask the person"
    assert of(planned, *KINDS)[-1] == ("history_refused", {"id": ids[3], "outcome": past})
    planned.task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
    planned.task.transition(State.PLAN, reason="your reply")
    request(planned, ids[3])
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is True


def test_a_request_the_writer_wrote_is_answered_within_the_same_bounds_and_leaves_its_clone(made):
    """The writer's pod writes the handoff too: what it puts there is a request like any other,
    ids and nothing else, from the clone as recorded, which stays as it was."""
    planned, ids = made
    clone = planned.task.repo / ".git"
    before = {p: p.read_bytes() for p in sorted(clone.rglob("*")) if p.is_file()}
    request(planned, ids[10], "--upload-pack=touch /tmp/vivibox-history-x", "refs/heads/main")
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is True
    assert answered(planned) == [
        f"{ids[10]}: available", "--upload-pack=touch /tmp/vivibox-history-x: not a commit id",
        "refs/heads/main: not a commit id",
    ]  # fmt: skip
    assert {p: p.read_bytes() for p in sorted(clone.rglob("*")) if p.is_file()} == before
    assert not Path("/tmp/vivibox-history-x").exists()


def test_a_request_that_is_a_link_is_not_read_and_goes(made, tmp_path):
    planned, ids = made
    yours = tmp_path / "yours.txt"
    yours.write_text(f"{ids[10]}\n")
    link = planned.task.meta / "handoff" / "history-request.md"
    link.symlink_to(yours)
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is False
    assert not link.is_symlink() and yours.read_text() == f"{ids[10]}\n"
    assert answered(planned)[0].startswith("history-request.md was not read: ")
    assert of(planned, *KINDS) == []


# --- the turn after a request ---------------------------------------------------------------------


class Asking(Harness):
    """A reader whose turns do what it is given, a turn each, and record what each was asked
    and whether history.md was there when it began."""

    name = "opencode"

    def __init__(self, task, *turns):
        self.task, self.turns, self.asked = task, list(turns), []

    def turn(self, prompt, session="", title="", on_step=None):
        had = (self.task.meta / "handoff" / "history.md").exists()
        self.asked.append((prompt, had))
        if self.turns:
            self.turns.pop(0)(self.task)
        return Turn("ses_1", True, 0.01, 100, "done")


def asks_for(commit: str):
    def ask(task):
        (task.meta / "handoff" / "history-request.md").write_text(f"{commit}\n")

    return ask


DRAFT = '+++\nsummary = "Read history"\n+++\n\n## Acceptance criteria\n\n- [ ] the plan quotes the commit\n'


def drafts(task):
    (task.meta / "handoff" / "plan-draft.md").write_text(DRAFT)


def supervised(planned: Planned, planner: Harness, mode: str) -> supervisor.Supervisor:
    task, pod = planned.task, planned.pod
    reader = control.Reader(
        planner, lambda: None, lambda: history.answer(task, pod, depth=5, source=planned.source)
    )
    ports = supervisor.Ports(
        run_gate=lambda t: None, risky_changes=lambda: [], clone_refused=lambda: "",
        notify=lambda *a, **kw: None, sleep=lambda seconds: None,
    )  # fmt: skip
    task.set_orchestration(mode)
    writer = Asking(task)
    return supervisor.Supervisor(
        task,
        writer,
        ports,
        max_rounds=1,
        mode=orchestration.MODES[mode],
        planner=reader,
        project_verify=["true"],
    )


def test_a_planner_that_asks_for_history_reads_the_answer_in_its_next_turn(made):
    planned, ids = made
    planner = Asking(planned.task, asks_for(ids[10]), drafts)
    sup = supervised(planned, planner, "planner_executor")
    sup.step()
    assert [had for _, had in planner.asked] == [False, True], "the answer is there for the second turn"
    assert planner.asked[1][0] == prompts.HISTORY_PROMPT
    assert planned.task.read_state().state is State.CHECKPOINT_PLAN
    assert answered(planned) == [f"{ids[10]}: available"]
    turns = [e["data"].get("kind", "") for e in planned.task.events() if e["type"] == "turn"]
    assert turns == ["", "history"], "the timeline says what the second turn was for"


def test_a_planner_past_the_requests_of_its_phase_goes_on_with_what_it_has(made):
    """Three answered; the fourth is refused, and the plan state reads the draft as it is."""
    planned, ids = made
    planner = Asking(planned.task, *(asks_for(i) for i in ids[-5:-1]), drafts)
    sup = supervised(planned, planner, "planner_executor")
    sup.step()
    asked = [p for p, _ in planner.asked]
    assert asked[1:4] == [prompts.HISTORY_PROMPT] * 3 and len(asked) == 5
    assert "ask the person" in answered(planned)[0]
    assert asked[4].startswith("The plan draft in /task/handoff/plan-draft.md is not ready"), (
        "as after a turn with no request: no draft, so its one repair turn"
    )
    assert planned.task.read_state().state is State.CHECKPOINT_PLAN


def test_the_supervisor_that_asks_for_history_in_its_review_reads_the_answer_next(made):
    """supervisor_worker: the review turn is the planner's, in its control container."""
    planned, ids = made
    task = planned.task

    def reviews(t):
        (t.meta / "review").mkdir(exist_ok=True)
        (t.meta / "review" / "review.md").write_text(
            "## Blocking\n\n## Not blocking\n\n## Checked\n\n- the diff\n"
        )

    planner = Asking(task, asks_for(ids[10]), reviews)
    sup = supervised(planned, planner, "supervisor_worker")
    for target in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.REVIEW):
        task.transition(target)
    task.set_verified_commit(git(task.repo, "rev-parse", "HEAD"))
    sup.step()
    assert [had for _, had in planner.asked] == [False, True]
    assert planner.asked[1][0] == prompts.HISTORY_PROMPT
    assert task.read_state().state is State.CHECKPOINT_FINAL


def test_a_writer_or_a_reviewer_of_its_own_is_never_answered(made):
    """Only a reader in its control container has a copy the host deepens: a request left
    where the writer plans (a single agent) is no request of a reader's."""
    planned, ids = made
    writer = Asking(planned.task, asks_for(ids[10]), drafts)
    ports = supervisor.Ports(run_gate=lambda t: None, risky_changes=lambda: [], clone_refused=lambda: "",
                             notify=lambda *a, **kw: None)  # fmt: skip
    planned.task.set_orchestration("single_agent")
    sup = supervisor.Supervisor(
        planned.task, writer, ports, max_rounds=1, mode=orchestration.MODES["single_agent"]
    )
    sup.step()
    assert prompts.HISTORY_PROMPT not in [p for p, _ in writer.asked]
    assert not (planned.task.meta / "handoff" / "history.md").exists()


def test_the_planners_brief_says_how_deep_its_copy_is_made(docker):  # noqa: F811
    depth_of(7)
    task = actions.create("demo", "Add divide", orchestration="supervisor_worker")
    control.prepare(task, load_config(), ["true"])
    told = " ".join((control.harness_dir(task) / "instructions.md").read_text().split())
    assert "It holds the last 7 commits of the branch" in told


def test_every_fetch_of_a_request_has_its_input_closed_its_output_counted_and_a_time(made, monkeypatch):
    planned, ids = made
    real, asked = repo.run_git, []

    def run_git(args, **how):
        asked.append((list(args), how))
        return real(args, **how)

    monkeypatch.setattr(repo, "run_git", run_git)
    assert planned.ensure(ids[10]).available
    fetches = [(args, how) for args, how in asked if args[0] == "fetch"]
    assert len(fetches) == 4
    for args, how in fetches:
        assert how["stdin"] is subprocess.DEVNULL and how["limit"] == history.LIMIT
        assert 0 < how["timeout"] <= repo.CLONE_TIMEOUT and how["to_remotes"] is False
        assert args[-2:] == [str(planned.task.repo), f"refs/heads/{BRANCH}"] and "--deepen=5" in args


# --- round 1 --------------------------------------------------------------------------------------


def test_the_first_commit_of_the_repository_is_available_with_its_change(made):
    """A real root, not a boundary of the copy: its change is against the empty tree."""
    planned, ids = made
    found = planned.ensure(ids[0])
    assert found.said == "available" and found.available
    shown = git(planned.view, "show", "--format=%H", ids[0])
    assert shown.startswith(ids[0]) and "+demo" in shown
    assert ids[0] not in shallow(planned.view)


def test_a_boundary_of_the_copy_is_no_root_and_is_made_deeper(made, monkeypatch):
    """The copy's oldest commit has its parents cut away (.git/shallow lists it): it is not
    taken for a root, so it needs a step, and with none left it is not found."""
    planned, ids = made
    boundary = ids[-5]
    assert boundary in shallow(planned.view)
    monkeypatch.setattr(history, "STEPS", 0)
    assert planned.ensure(boundary).said == "not found within 0 commits before the base on its branch"
    monkeypatch.setattr(history, "STEPS", 20)
    found = planned.ensure(boundary)
    assert found.available and found.steps == 1


def test_a_whole_checkouts_remote_is_never_asked_for_a_commit_its_clone_lacks(behind, tmp_path, monkeypatch):
    """A remote is recorded and reached your way, and the clone, of a whole checkout, has the
    base branch's whole history: no fetch goes to the remote for what it lacks."""
    bare, _ = behind
    yours = OverSsh(bare, tmp_path, whole=True)
    planned = Planned(yours.checkout, tmp_path / "tasks")
    assert planned.task.read_state().history_remote == yours.address
    yours.reach(monkeypatch)
    found = planned.ensure("0123456789abcdef0123456789abcdef01234567")
    assert found.said == "not found within 100 commits before the base on its branch"
    assert yours.handed() == [] and not yours.asked.exists()


def test_an_id_in_capitals_is_the_same_commit_and_answered_in_small_letters(made):
    planned, ids = made
    found = planned.ensure(ids[10].upper())
    assert found.available and found.commit == ids[10]
    request(planned, ids[11].upper(), ids[-1][:7].upper())
    assert history.answer(planned.task, planned.pod, depth=5, source=planned.source) is True
    assert answered(planned) == [f"{ids[11]}: available", f"{ids[-1][:7]}: available"]


# --- fix round 1: a reader's copy always holds the task's base -----------------------------------


@pytest.fixture
def long_work(mine, tmp_path) -> tuple[Path, str, list[str]]:
    """A task's clone whose work is seven commits past its base; the base, and the work."""
    clone = a_task(mine, tmp_path / "task")
    base = git(clone, "rev-parse", "HEAD")
    return clone, base, commits(clone, 7, "work")


def test_a_copy_too_shallow_for_its_base_is_made_deeper_to_it(long_work, tmp_path):
    clone, base, work = long_work
    view = tmp_path / "view"
    view.mkdir()
    reading.view(clone, BRANCH, view, depth=3, base=base)
    done = subprocess.run(["git", "-C", str(view), "diff", "--stat", f"{base}..HEAD"], capture_output=True)
    assert done.returncode == 0 and b"work" in done.stdout
    assert git(view, "rev-parse", "HEAD") == work[-1]
    assert counted(view) == 9, "three, then three more a step, until the base is there"


def test_a_copy_whose_base_is_past_every_step_is_refused_as_it_was(long_work, tmp_path, monkeypatch):
    clone, base, _ = long_work
    view = tmp_path / "view"
    view.mkdir()
    monkeypatch.setattr(reading, "STEPS", 1)
    with pytest.raises(repo.RepoError) as refused:
        reading.view(clone, BRANCH, view, depth=3, base=base)
    assert str(refused.value) == (
        f"the work is more than 3 commits past its base {base[:7]}; raise history_depth in the "
        "configuration and start the task again"
    )
    assert git(view, "rev-list", "--count", "FETCH_HEAD") == "3", "the copy as the first fetch made it"


def test_a_clone_blocked_on_the_way_to_the_base_gets_no_further_git(long_work, tmp_path, monkeypatch):
    clone, base, _ = long_work
    view = tmp_path / "view"
    view.mkdir()
    checks, fetches = [], []
    real_run = repo.run_git

    def guard(at):
        checks.append(at)
        if len(checks) > 2:  # the first fetch's check, the branch's, then blocked before a step
            raise repo.Blocked("blocked by what the task added", [])

    def run_git(args, **how):
        fetches.append((len(checks), list(args)))
        return real_run(args, **how)

    monkeypatch.setattr(repo, "guard", guard)
    monkeypatch.setattr(repo, "run_git", run_git)
    with pytest.raises(repo.Blocked):
        reading.view(clone, BRANCH, view, depth=3, base=base)
    assert not [args for at, args in fetches if at > 2 and "fetch" in args]


def a_long_task(mode: str) -> tuple:
    """A task of the docker fixture's machine, history_depth 3, its work five commits past its
    base and verified; its pod up."""
    depth_of(3)
    task = actions.create("demo", "Add divide", orchestration=mode)
    for i in range(5):
        commit(task.repo, "calc.py", f"x = {i}\n")
    task.set_verified_commit(git(task.repo, "rev-parse", "HEAD"))
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    return task, pod


def diffed(view: Path, base: str) -> int:
    asked = ["git", "-C", str(view), "diff", "--stat", f"{base}..HEAD"]
    return subprocess.run(asked, capture_output=True).returncode


def test_a_reviewers_round_reads_the_work_from_its_base_however_long_it_is(docker):  # noqa: F811
    task, pod = a_long_task("planner_writer_reviewer")
    reviewing.up(task, pod, load_config())
    assert diffed(Path(pod.review_src), task.read_state().base_commit) == 0


def test_the_planners_copy_made_fresh_holds_the_base_however_long_the_work_is(docker):  # noqa: F811
    task, pod = a_long_task("supervisor_worker")
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, pod, load_config())
    sup.planner.bring_up_to_date()
    assert diffed(pod.control_src, task.read_state().base_commit) == 0


# --- fix round 1: available means reachable from the copy's HEAD, with its parent ------------------


def test_a_commit_left_behind_by_a_request_that_failed_is_no_root(made, monkeypatch):
    """A request that ran out of steps puts the copy's boundary back and leaves what it fetched
    in the object store: its oldest commit has no parent there and is on no boundary listed.
    Asked for next, it is made reachable again, and git show of it is its change."""
    planned, ids = made
    monkeypatch.setattr(history, "STEPS", 2)
    assert not planned.ensure("0123456789abcdef0123456789abcdef01234567").available
    monkeypatch.setattr(history, "STEPS", 20)
    found = planned.ensure(ids[16])
    assert found.available and found.steps > 0
    shown = subprocess.run(["git", "-C", str(planned.view), "show", ids[16]], capture_output=True)
    assert shown.returncode == 0, shown.stderr


def test_a_commit_your_remote_has_past_the_base_is_not_found_nor_reachable(behind, tmp_path):
    """Your remote moved on by three commits since your checkout: the newest is no ancestor of
    the task's base, so it is not found, and the copy shows the history it showed: none of
    them on it, nor reachable from any of its refs."""
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    planned = Planned(checkout, tmp_path / "tasks")
    work = tmp_path / "upstream-work"
    newer = commits(work, 3, "newer")
    git(work, "push", "-q", str(bare), "main")
    before = planned.looks()
    found = planned.ensure(newer[-1])
    assert found.said == "not found within 100 commits before the base on its branch"
    assert planned.looks() == before
    assert not set(newer) & set(git(planned.view, "rev-list", "--all").split())


def test_a_step_from_your_remote_leaves_its_newer_commits_readable_in_the_copys_store(behind, tmp_path):
    """What docs/security.md says the copy may hold: the remote step fetches your base branch
    as the remote has it now, so commits of it newer than the base come into the copy's object
    store, where the planner can read them by their id, though none is ever available or
    reachable from the copy's HEAD. A change that prunes them changes this test."""
    bare, ids = behind
    checkout = shallow_checkout(f"file://{bare}", tmp_path / "mine")
    planned = Planned(checkout, tmp_path / "tasks")
    work = tmp_path / "upstream-work"
    newer = commits(work, 3, "newer")
    git(work, "push", "-q", str(bare), "main")
    assert planned.ensure(ids[10]).available, "an older commit, from the remote"
    held = subprocess.run(["git", "-C", str(planned.view), "cat-file", "-e", newer[-1]], capture_output=True)
    assert held.returncode == 0, "in the store"
    on = subprocess.run(["git", "-C", str(planned.view), "merge-base", "--is-ancestor", newer[-1], "HEAD"])
    assert on.returncode != 0 and not set(newer) & set(git(planned.view, "rev-list", "--all").split())


# --- fix round 1: what is recorded, and how it is fetched -----------------------------------------


def test_a_remote_that_reads_as_an_option_is_not_recorded(mine):
    git(mine, "config", "remote.origin.url", "--upload-pack=touch /tmp/x; git-upload-pack")
    assert history.remote(mine, "main") == ""


def test_a_remote_at_a_relative_path_is_recorded_from_your_checkout(mine, tmp_path):
    git(mine, "remote", "add", "origin", "../upstream.git")
    assert history.remote(mine, "main") == str(tmp_path / "upstream.git")
    git(mine, "remote", "set-url", "origin", "git@example.com:r.git")
    assert history.remote(mine, "main") == "git@example.com:r.git", "an address, as it is"


def test_a_recorded_remote_is_never_taken_for_an_option(behind, tmp_path):
    bare, ids = behind
    planned = Planned(shallow_checkout(f"file://{bare}", tmp_path / "mine"), tmp_path / "tasks")
    mark = tmp_path / "ran"
    st = planned.task.read_state()
    st.history_remote = f"--upload-pack=touch {mark}; git-upload-pack"
    planned.task._write_state(st)
    found = planned.ensure(ids[10])
    assert not found.available and not mark.exists()


def test_a_shallow_file_the_writer_left_in_a_whole_clone_reaches_no_remote(behind, tmp_path, monkeypatch):
    """Whether your remote is asked is a fact of your checkout, not of the clone the writer
    writes: a .git/shallow planted there makes no fetch go to your remote."""
    bare, ids = behind
    yours = OverSsh(bare, tmp_path, whole=True)
    planned = Planned(yours.checkout, tmp_path / "tasks")
    (planned.task.repo / ".git" / "shallow").write_text(f"{ids[20]}\n")
    yours.reach(monkeypatch)
    found = planned.ensure("0123456789abcdef0123456789abcdef01234567")
    assert found.said == "not found within 100 commits before the base on its branch"
    assert yours.handed() == [] and not yours.asked.exists()


@pytest.mark.parametrize("fourth", [False, True], ids=["answered", "past the requests of the phase"])
def test_a_request_of_ten_thousand_lines_is_answered_in_six(made, monkeypatch, fourth):
    planned, ids = made
    nothing = lambda task, pod, commit, **kw: history.Outcome(commit, "not a commit id")  # noqa: E731
    monkeypatch.setattr(history, "ensure", nothing)
    for _ in range(3 if fourth else 0):
        request(planned, ids[-1])
        history.answer(planned.task, planned.pod, depth=5, source=planned.source)
    before = len(planned.task.events())
    (planned.task.meta / "handoff" / "history-request.md").write_bytes(b"x\n" * 10_000)
    history.answer(planned.task, planned.pod, depth=5, source=planned.source)
    assert len(answered(planned)) <= 6
    kinds = ("history_acquired", "history_refused")
    assert len([e for e in planned.task.events()[before:] if e["type"] in kinds]) <= 6
    assert answered(planned)[-1].endswith("more lines not looked for: a request holds at most 5 ids")


def test_a_request_that_begins_with_a_byte_order_mark_is_read_without_it(made):
    planned, ids = made
    (planned.task.meta / "handoff" / "history-request.md").write_text(f"\ufeff{ids[-1]}\n")
    history.answer(planned.task, planned.pod, depth=5, source=planned.source)
    assert answered(planned) == [f"{ids[-1]}: available"]


def test_an_answer_is_kept_when_the_copy_cannot_be_brought_up_to_date(made, monkeypatch):
    planned, ids = made

    def blocked(*args, **kw):
        raise repo.Blocked("the task's clone is blocked by what was added to it", [])

    monkeypatch.setattr(control, "refresh_for", blocked)
    request(planned, ids[-1])
    with pytest.raises(repo.Blocked):
        history.answer(planned.task, planned.pod, depth=5, source=planned.source)
    assert answered(planned) == [f"{ids[-1]}: available"]
    assert [kind for kind, _ in of(planned, *KINDS)] == ["history_requested", "history_acquired"]


@pytest.mark.parametrize("kind", ["a link", "a named pipe"])
def test_a_shallow_file_of_the_clone_that_is_no_plain_file_is_a_change(mine, tmp_path, kind):
    """git reads the clone's .git/shallow to serve a fetch from it: a link there would have it
    read a file of yours, and a named pipe would have it wait."""
    import os

    clone = a_task(mine, tmp_path / "task")
    yours = tmp_path / "yours.txt"
    yours.write_text("SECRET\n")
    shallow_file = clone / ".git" / "shallow"
    shallow_file.symlink_to(yours) if kind == "a link" else os.mkfifo(shallow_file)
    with pytest.raises(repo.RepoError, match="shallow"):
        repo.check_protection(clone, tmp_path / "task" / ".task")


def test_a_plain_shallow_file_of_the_clone_is_no_change_also_for_an_older_record(mine, tmp_path):
    import json

    clone = a_task(mine, tmp_path / "task")
    meta = tmp_path / "task" / ".task"
    (clone / ".git" / "shallow").write_text(git(clone, "rev-parse", "HEAD") + "\n")
    repo.check_protection(clone, meta)
    record = json.loads((meta / "git-protection.json").read_text())
    record.pop(".git/shallow", None)  # recorded before the clone's shallow file was
    (meta / "git-protection.json").write_text(json.dumps(record))
    repo.check_protection(clone, meta)


def remote_fetches(monkeypatch) -> list[tuple[list[str], dict]]:
    """Every fetch sent to a remote from here on: its arguments, and what vivibox set for it."""
    real, sent = repo.run_git, []

    def run_git(args, **how):
        if args[0] == "fetch" and how.get("to_remotes"):
            sent.append((list(args), dict(how.get("env") or {})))
        return real(args, **how)

    monkeypatch.setattr(repo, "run_git", run_git)
    return sent


def test_the_recorded_remote_is_fetched_whatever_the_clone_names(behind, tmp_path, monkeypatch):
    bare, ids = behind
    planned = Planned(shallow_checkout(f"file://{bare}", tmp_path / "mine"), tmp_path / "tasks")
    elsewhere = tmp_path / "elsewhere.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(bare), str(elsewhere)], check=True)
    git(planned.task.repo, "remote", "set-url", "origin", f"file://{elsewhere}")
    repo.record_protection(planned.task.repo, planned.task.meta)
    sent = remote_fetches(monkeypatch)
    assert planned.ensure(ids[10]).available
    assert sent and all(f"file://{bare}" in args and f"file://{elsewhere}" not in args for args, _ in sent)


def test_the_fetch_from_your_remote_asks_nowhere_for_what_it_lacks(behind, tmp_path, monkeypatch):
    """No terminal, no askpass window on your desktop, nothing fetched lazily: a remote that
    wants credentials no helper of yours gives fails at once."""
    bare, ids = behind
    planned = Planned(shallow_checkout(f"file://{bare}", tmp_path / "mine"), tmp_path / "tasks")
    sent = remote_fetches(monkeypatch)
    assert planned.ensure(ids[10]).available and sent
    for _, env in sent:
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_NO_LAZY_FETCH"] == "1"
        assert env["GIT_ASKPASS"] == "" and env["SSH_ASKPASS_REQUIRE"] == "never"


# --- fix round 2 ----------------------------------------------------------------------------------


def test_a_branch_reset_below_its_base_is_refused_for_what_it_is(long_work, tmp_path):
    """The writer moved the task's branch below its base: no depth brings the base back, and
    raising history_depth is not the step."""
    clone, base, _ = long_work
    git(clone, "reset", "-q", "--hard", f"{base}~3")
    view = tmp_path / "view"
    view.mkdir()
    with pytest.raises(repo.RepoError) as refused:
        reading.view(clone, BRANCH, view, depth=3, base=base)
    assert str(refused.value) == (
        f"the task's branch no longer holds its base {base[:7]}: its commits were reset below it; reply, "
        "and the writer puts the branch back on its base, or delete the task"
    )
