"""git on the host in the task's clone, which the agent writes, answers in time or the operation
stops with why. git 2.43 waits forever on a named pipe (FIFO) where it reads its settings
(.gitignore, .gitattributes, .git/info/exclude) or its refs (.git/packed-refs); one mkfifo by the
agent would otherwise hang every verification with no word. A call that ran out of time is never
taken as an answer: not "no uncommitted files", not "no risky changes", not an empty diff."""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import make_repo
from test_gate import PLAN, FakePod
from test_supervisor import FakeHarness

from vivibox import (  # noqa: F401 (actions first: box imports it)
    actions,
    box,
    gate,
    init,
    proposal,
    repo,
    risky,
    supervisor,
    ui,
)
from vivibox.risky import Approvals
from vivibox.states import State
from vivibox.task import create_task

LIMIT = 2  # seconds, for the tests: the real limit is repo.CLONE_TIMEOUT


@pytest.fixture
def source(tmp_path):
    return make_repo(tmp_path / "source")


@pytest.fixture
def task(tmp_path, source, monkeypatch):
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    # raising=False: the red run has no such limit yet, and must fail on the hang, not on this.
    monkeypatch.setattr(repo, "CLONE_TIMEOUT", LIMIT, raising=False)
    return t


def in_time(call, seconds: float = LIMIT * 5):
    """call's result, or the error it raised; a failure when it hangs, so a regression fails the
    test instead of hanging the suite."""
    done = []

    def run():
        try:
            done.append(call())
        except Exception as e:
            done.append(e)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=seconds)
    assert done, "the host hung on git in the clone"
    return done[0]


def fifo(clone: Path, rel: str) -> None:
    path = clone / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    os.mkfifo(path)


WHERE_GIT_READS = [".gitignore", "sub/.gitignore", ".gitattributes", ".git/info/exclude"]


@pytest.mark.parametrize("rel", WHERE_GIT_READS)
def test_a_fifo_where_git_reads_ends_the_verification_in_time_and_not_green(task, rel):
    fifo(task.repo, rel)
    pod = FakePod()
    began = time.monotonic()
    result = in_time(lambda: gate.run_gate(task, pod, ["make test"], []))
    took = time.monotonic() - began
    assert isinstance(result, gate.GateResult), f"the verification raised instead: {result!r}"
    assert took < LIMIT * 3, f"took {took:.1f} s with a limit of {LIMIT} s"
    assert not result.passed
    assert "did not answer" in result.environment and rel in result.environment
    assert pod.commands == [], "nothing was built on a tree git could not describe"
    assert not result.commands and not result.unchanged
    assert gate.next_state(result, 0, 3) is State.CHECKPOINT_BLOCKED
    event = next(e["data"] for e in reversed(task.events()) if e["type"] == "gate")
    assert event["passed"] is False and event["environment"] == result.environment
    assert result.log.exists() and result.environment in result.log.read_text()


def test_the_task_waits_for_you_with_no_round_spent_when_git_does_not_answer(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    fifo(task.repo, ".gitignore")
    notes = []
    ports = supervisor.Ports(
        run_gate=lambda t: gate.run_gate(t, FakePod(), ["make test"], []),
        risky_changes=lambda: [],
        notify=lambda _id, msg, kind="": notes.append(msg),
        sleep=lambda seconds: None,
    )
    sup = supervisor.Supervisor(task, FakeHarness(task), ports, max_rounds=3, project_verify=["make test"])
    sup.step()  # implement
    in_time(sup.step)  # verify
    st = task.read_state()
    assert (st.state, st.rounds) == (State.CHECKPOINT_BLOCKED, 0), "no round spent"
    assert notes[-1].startswith("verification could not run: git status did not answer")
    assert ".gitignore" in notes[-1] and "press g" in notes[-1]
    shown = ui.view(task, st, False, 3)
    assert shown.status == "verification could not run"
    assert "did not answer" in ui.environment_problem(task)
    assert not (task.meta / supervisor.NEXT_PROMPT).exists(), "no turn for the writer"


@pytest.fixture
def stuck(task):
    """The clone with its refs behind a FIFO: every git command that reads a ref waits."""
    fifo(task.repo, ".git/packed-refs")
    return task


def _base(t):
    return t.read_state().base_commit


CALLERS = {
    "uncommitted files": lambda t: gate.uncommitted(t.repo),
    "commit problems": lambda t: gate.commit_problems(t.repo, _base(t)),
    "added lines": lambda t: list(gate.added_lines(t.repo, _base(t))),
    "switched-off tests": lambda t: gate.switched_off_tests(t.repo, _base(t)),
    "changed test files": lambda t: gate.changed_test_files(t.repo, _base(t)),
    "programs the commands run": lambda t: gate.missing_program(t.repo, ["./build.sh"]),
    "files the work changed": lambda t: proposal._changed(t),
}


@pytest.mark.parametrize("caller", CALLERS)
def test_a_call_that_ran_out_of_time_is_never_an_answer(stuck, caller):
    """Not an empty list, not "every program is there": the error, with what git waited on."""
    said = in_time(lambda: CALLERS[caller](stuck))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"
    assert ".git/packed-refs" in str(said)


def test_new_untracked_files_are_not_taken_as_none_when_git_does_not_answer(task):
    """.gitignore as a FIFO: the diff answers, the list of untracked files does not."""
    fifo(task.repo, ".gitignore")
    said = in_time(lambda: gate.hidden_characters(task.repo, _base(task)))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"


def test_a_build_wrapper_is_not_guessed_when_git_does_not_answer(task):
    fifo(task.repo, ".git/index")
    said = in_time(lambda: init.wrapper(task.repo, "mvnw"))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"


def test_the_work_is_not_fetched_when_the_clone_does_not_answer(stuck, source):
    """git fetch runs in your checkout but reads the clone's refs."""
    said = in_time(lambda: repo.fetch_to(source, stuck.repo, stuck.meta, stuck.id))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"
    assert "fetch" in str(said)
    shown = subprocess.run(["git", "for-each-ref", "refs/vivibox/"], cwd=source, capture_output=True)
    assert shown.stdout == b"", "nothing of the task's in your checkout"


def test_a_box_does_not_close_when_the_clone_does_not_answer(stuck, monkeypatch):
    monkeypatch.setattr(box, "commit_in_box", lambda task: None)  # in the pod, which is not here
    stuck.set_box()
    stuck.transition(State.CHECKPOINT_PLAN)
    stuck.transition(State.IMPLEMENT)
    said = in_time(lambda: box.close_box(stuck, None))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"
    assert stuck.read_state().state is State.IMPLEMENT, "not brought to review"


def test_the_risky_check_scans_everything_when_git_cannot_say_what_it_ignores(task, monkeypatch):
    """ignored() and untracked() keep their own 30 s: past it, nothing is left out of the scan."""
    (task.repo / ".gitignore").write_text("pom.xml\n")
    (task.repo / "pom.xml").write_text("<project/>\n")

    def timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(risky.subprocess, "run", timed_out)
    assert "pom.xml" in [c.path for c in Approvals(task.meta, task.repo).changes()]


NORMAL = [
    ("status", "--porcelain", "--untracked-files=all"),
    ("-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff", "--unified=0", "BASE"),
    ("ls-files", "-z", "--others", "--exclude-standard"),
    ("log", "--format=%h%x1f%B%x1e", "BASE..HEAD"),
    ("cat-file", "-e", "HEAD:nothing-here"),
]


@pytest.mark.parametrize("args", NORMAL, ids=lambda a: a[0] if a[0] != "-c" else "diff")
def test_on_an_ordinary_repository_the_limit_changes_no_answer(task, args):
    (task.repo / "README.md").write_text("changed\n")
    (task.repo / "new.txt").write_text("new\n")
    subprocess.run(["git", "add", "README.md"], cwd=task.repo, check=True)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "Change"],
                   cwd=task.repo, check=True)  # fmt: skip
    (task.repo / "README.md").write_text("changed again\n")
    args = tuple(a.replace("BASE", _base(task)) for a in args)
    before = repo.git(*args, cwd=task.repo, check=False)
    after = repo.in_clone(task.repo, *args, check=False)
    assert (after.returncode, after.stdout, after.stderr) == (before.returncode, before.stdout, before.stderr)
    assert before.stdout or before.returncode, "the case says something to compare"


def test_a_git_that_fails_in_time_fails_as_before(task):
    with pytest.raises(repo.RepoError) as unbounded:
        repo.git("cat-file", "-e", "HEAD:nothing-here", cwd=task.repo)
    with pytest.raises(repo.RepoError) as bounded:
        repo.in_clone(task.repo, "cat-file", "-e", "HEAD:nothing-here")
    assert type(bounded.value) is repo.RepoError and str(bounded.value) == str(unbounded.value)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return False
    return state != "Z"


def test_no_git_process_is_left_after_a_timeout(task, tmp_path, monkeypatch):
    """A git that never answers and has started a child of its own: both go."""
    bin_dir, pids = tmp_path / "bin", tmp_path / "pids"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(f'#!/bin/sh\necho $$ >> "{pids}"\nsleep 300 &\necho $! >> "{pids}"\nwait\n')
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    said = in_time(lambda: repo.in_clone(task.repo, "status"))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"
    started = [int(p) for p in pids.read_text().split()]
    assert len(started) == 2, "git and its child both started"
    deadline = time.monotonic() + 3
    while any(_alive(p) for p in started) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [p for p in started if _alive(p)], "a git process outlived its timeout"


def test_the_reason_says_what_timed_out_and_what_to_do(task):
    fifo(task.repo, ".gitattributes")
    said = str(in_time(lambda: gate.uncommitted(task.repo)))
    assert said.startswith(f"git status did not answer in {LIMIT} s: ")
    assert f"{task.repo / '.gitattributes'} is a named pipe (FIFO)" in said
    assert said.endswith("delete it")


def test_the_reason_without_a_special_file_found_names_where_to_look(task, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nexec sleep 300\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    said = str(in_time(lambda: gate.uncommitted(task.repo)))
    assert said.startswith(f"git status did not answer in {LIMIT} s in {task.repo}")
    assert ".gitignore" in said and ".git/info/exclude" in said


def test_what_the_verification_asks_the_clone_before_it_runs_ends_it_the_same_way(env, monkeypatch):
    """By modules, the changed files decide what is built: asked of git before the gate runs."""
    from test_supervise import config

    from vivibox import supervise
    from vivibox.cli import main
    from vivibox.config import load_project
    from vivibox.task import find_task

    path = env / "config" / "projects" / "demo.toml"
    by_modules = 'verify = ["mvn -pl {modules} -am verify"]'
    path.write_text(path.read_text().replace('verify = ["true"]', by_modules))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    t = find_task(supervise.load_config().tasks_dir, "demo-1")
    (t.meta / gate.ACCEPTED_PLAN).write_text('+++\nmodules = ["core"]\n+++\n')
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())
    monkeypatch.setattr(repo, "CLONE_TIMEOUT", LIMIT, raising=False)
    built = []
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: built.append(a))

    class Pod:
        def review_down(self):
            pass

        def review_side(self):
            return self

    sup = supervise.make_supervisor(t, load_project("demo"), Pod(), config())
    fifo(t.repo, ".git/packed-refs")
    result = in_time(lambda: sup.ports.run_gate(t))
    assert isinstance(result, gate.GateResult), f"the verification raised instead: {result!r}"
    assert not result.passed and "did not answer" in result.environment and built == []


class BlockingDocker:
    """docker as the pod runs it, where git in a container that copies the clone waits on it until
    the container is removed, as git on a FIFO does. The FIFO is made as git starts there: by a
    process the agent left in its pod, after every check the host made."""

    def __init__(self, clone: Path, waits_on: str):
        self.clone, self.waits_on = clone, waits_on
        self.calls: list[list[str]] = []
        self.waiting: dict[str, threading.Event] = {}

    def __call__(self, cmd, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "rm", "-f"]:
            for name in cmd[3:]:
                self.waiting.pop(name, threading.Event()).set()
        elif cmd[:2] == ["docker", "exec"] and "git" in cmd and cmd[cmd.index("git") + 1] == self.waits_on:
            fifo(self.clone, ".git/packed-refs")
            removed = self.waiting[cmd[4]] = threading.Event()  # docker exec -w DIR CONTAINER git …
            removed.wait(timeout=60)
            return subprocess.CompletedProcess(cmd, 137, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def removed_after(self, container: str, step: str) -> bool:
        started = next(i for i, c in enumerate(self.calls) if step in c)
        return ["docker", "rm", "-f", container] in self.calls[started:]


def a_pod(task, docker, tmp_path):
    from vivibox.pod import Pod

    return Pod(
        task.id, task.repo, "img", gate_dir=tmp_path / "gate", review_dir=tmp_path / "review", runner=docker
    )


@pytest.mark.parametrize("step", ["clone", "submodule"])
def test_git_in_the_gate_container_that_does_not_answer_ends_the_verification(
    task, tmp_path, monkeypatch, step
):
    monkeypatch.setattr(repo, "COPY_TIMEOUT", LIMIT, raising=False)
    if step == "submodule":
        (task.repo / ".gitmodules").write_text('[submodule "lib"]\n\tpath = lib\n\turl = https://x/y.git\n')
        subprocess.run(["git", "add", ".gitmodules"], cwd=task.repo, check=True)
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "Add lib"],
                       cwd=task.repo, check=True)  # fmt: skip
    docker = BlockingDocker(task.repo, step)
    pod = a_pod(task, docker, tmp_path)
    result = in_time(lambda: gate.run_gate(task, pod, ["make test"], []))
    assert isinstance(result, gate.GateResult), f"the verification raised instead: {result!r}"
    assert not result.passed and gate.next_state(result, 0, 3) is State.CHECKPOINT_BLOCKED
    assert result.environment.startswith(f"git {step} did not answer in {LIMIT} s: ")
    assert ".git/packed-refs is a named pipe (FIFO)" in result.environment
    assert docker.removed_after(pod.gate, step), "the gate container, and git in it, went down"
    assert not [c for c in docker.calls if "make test" in " ".join(c)], "nothing built"


def test_the_reviewers_clone_of_a_clone_that_does_not_answer_stops_the_round_for_you(
    task, tmp_path, monkeypatch
):
    from test_supervisor import BLOCKING, reviewed

    monkeypatch.setattr(repo, "COPY_TIMEOUT", LIMIT, raising=False)
    sup, notes, reviewer = reviewed(task, tmp_path, [BLOCKING])
    docker = BlockingDocker(task.repo, "clone")
    pod = a_pod(task, docker, tmp_path)

    def review_up():
        pod.review_fresh()
        pod.review_start([], {})
        return tmp_path / "review-out"

    sup.ports.review_up = review_up
    sup.step()  # implement
    sup.step()  # verify: passes, so the reviewer is next
    assert task.read_state().state is State.REVIEW

    class Stopped(Exception):
        pass

    def stop_once_paused(st):
        if st.paused:
            raise Stopped

    ended = in_time(lambda: sup.run(poll=0, on_step=stop_once_paused))
    assert isinstance(ended, Stopped), f"the round did not stop for you: {ended!r}"
    st = task.read_state()
    assert st.state is State.REVIEW and st.reviews == 0, "no review taken, empty or passing"
    assert "git clone did not answer" in st.problem and ".git/packed-refs" in st.problem
    assert "git clone did not answer" in notes[-1]
    assert reviewer.prompts == [] and not (task.meta / "handoff" / "review-1.md").exists()
    assert docker.removed_after(pod.review, "clone"), "the review container, and git in it, went down"
