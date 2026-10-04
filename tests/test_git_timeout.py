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
from conftest import as_verified, make_repo, networks_up
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


# .git/packed-refs: rev-parse HEAD, the verification's first question, waits on it too.
WHERE_GIT_READS = [".gitignore", "sub/.gitignore", ".gitattributes", ".git/info/exclude", ".git/packed-refs"]


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
    assert str(said).startswith("the work could not be brought back from the task's clone: git fetch")
    assert f"vivibox status {stuck.id}" in str(said)
    shown = subprocess.run(["git", "for-each-ref", "refs/vivibox/"], cwd=source, capture_output=True)
    assert shown.stdout == b"", "nothing of the task's in your checkout"


class LocalPod:
    """The box's pod, its docker exec run here, in the clone, as docker exec -w <clone> runs it."""

    def __init__(self, clone: Path):
        self.repo = clone

    def exec(self, *cmd, check=True):
        from vivibox.pod import PodError

        p = subprocess.run(list(cmd), cwd=self.repo, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise PodError(f"{' '.join(cmd[:4])}…: {p.stderr.strip()}")
        return p


def a_box_pod(task, monkeypatch):
    """The box's pod, here: its git as the pod's, its limit the tests'."""
    monkeypatch.setattr(repo, "COPY_TIMEOUT", LIMIT, raising=False)
    for who in ("AUTHOR", "COMMITTER"):  # the pod's gitconfig, which is not here
        monkeypatch.setenv(f"GIT_{who}_NAME", "You")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "you@example.com")
    monkeypatch.setattr(actions, "task_pod", lambda task_id: LocalPod(task.repo))


def a_box(task, monkeypatch):
    a_box_pod(task, monkeypatch)
    task.set_box()
    task.transition(State.CHECKPOINT_PLAN)
    task.transition(State.IMPLEMENT)
    return task


@pytest.mark.parametrize("rel", [".gitignore", ".gitattributes", ".git/packed-refs"])
def test_a_box_does_not_close_when_the_clone_does_not_answer(task, monkeypatch, rel):
    """Its commit runs in the pod, where your shell is: git there is killed, not the container."""
    box_ = a_box(task, monkeypatch)
    (box_.repo / "work.txt").write_text("by hand\n")
    fifo(box_.repo, rel)
    said = in_time(lambda: box.close_box(box_, None))
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"
    assert str(said).startswith("the box could not close: git ")
    assert f"{rel} is a named pipe (FIFO)" in str(said) and f"vivibox accept {box_.id}" in str(said)
    assert box_.read_state().state is State.IMPLEMENT, "not brought to review"


def test_a_box_with_nothing_wrong_still_closes(task, monkeypatch):
    box_ = a_box(task, monkeypatch)
    (box_.repo / "work.txt").write_text("by hand\n")
    monkeypatch.setattr(actions, "prepare_review", lambda task, project: Path("review"))
    from types import SimpleNamespace

    assert box.close_box(box_, SimpleNamespace(risky_extra=[])) == Path("review")
    shown = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=box_.repo, capture_output=True, text=True)
    assert shown.stdout.strip() == box.BOX_COMMIT


def test_the_risky_check_scans_everything_when_git_cannot_say_what_it_ignores(task, monkeypatch):
    """ignored() and untracked() keep their own 30 s: past it, nothing is left out of the scan."""
    (task.repo / ".gitignore").write_text("pom.xml\n")
    (task.repo / "pom.xml").write_text("<project/>\n")

    def timed_out(command, cwd, seconds, clone):
        assert seconds == 30
        raise repo.GitTimeout(f"git ls-files did not answer in {seconds} s")

    monkeypatch.setattr(repo, "_bounded", timed_out)
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
    assert said.startswith(f"git status did not answer in {LIMIT} s: in the task's clone, ")
    assert ".gitattributes is a named pipe (FIFO)" in said
    assert said.endswith(f"delete it; the clone is {task.repo}")


def commit_file(clone: Path, rel: str, text: str) -> None:
    (clone / rel).parent.mkdir(parents=True, exist_ok=True)
    (clone / rel).write_text(text)
    subprocess.run(["git", "add", "--", rel], cwd=clone, check=True)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "Add a file"],
                   cwd=clone, check=True)  # fmt: skip


def test_a_file_the_commits_have_is_restored_not_deleted(task):
    """Deleted, a committed .gitattributes is an uncommitted change, and the next verification
    spends a round of the writer's on it. (A committed .gitignore git reads from its index.)"""
    commit_file(task.repo, ".gitattributes", "* text=auto\n")
    fifo(task.repo, ".gitattributes")
    said = str(in_time(lambda: gate.uncommitted(task.repo)))
    assert said.endswith(f"restore it from the commits; the clone is {task.repo}")
    assert "delete" not in said


# A name the agent chose, which a shell pasting it would run: no message suggests a command with it.
PWNED = "$(touch {marker}/PWNED) x; y"
COMMANDS = ("rm ", "git -C", "checkout --", "$ ")


@pytest.mark.parametrize("variant", ["restore", "delete"])
def test_a_name_the_agent_chose_is_never_part_of_a_command_to_paste(task, tmp_path, variant):
    folder = PWNED.format(marker=tmp_path)
    if variant == "restore":
        commit_file(task.repo, f"{folder}/.gitattributes", "* text=auto\n")
        commit_file(task.repo, f"{folder}/f.txt", "one\n")
        (task.repo / folder / "f.txt").write_text("two\n")  # a change there: git diff reads its attributes
        fifo(task.repo, f"{folder}/.gitattributes")
        said = str(in_time(lambda: list(gate.added_lines(task.repo, _base(task)))))
    else:
        fifo(task.repo, f"{folder}/.gitignore")
        said = str(in_time(lambda: gate.uncommitted(task.repo)))
    assert "did not answer" in said, said
    assert not [c for c in COMMANDS if c in said], f"a command to paste: {said}"
    assert f"in the task's clone, {folder}/.git" in said, "the name as text, whole"
    assert ("restore it from the commits" if variant == "restore" else "delete it") in said
    assert not (tmp_path / "PWNED").exists()


def test_control_characters_in_a_name_reach_no_terminal(task):
    folder = "sub\x1b[31mRED\x1b[0m\nFAKE LINE"
    fifo(task.repo, f"{folder}/.gitignore")
    said = str(in_time(lambda: gate.uncommitted(task.repo)))
    assert not [c for c in said if ord(c) < 0x20 or ord(c) == 0x7F], repr(said)
    assert "sub‹U+001B›[31mRED‹U+001B›[0m‹U+000A›FAKE LINE/.gitignore is a named pipe" in said


def test_whether_the_commits_have_a_file_is_asked_of_its_name_alone(task):
    """A folder named * is no pattern: the committed a/.gitattributes does not make its file one."""
    commit_file(task.repo, "a/.gitattributes", "* text=auto\n")
    fifo(task.repo, "*/.gitattributes")
    said = str(repo.timed_out(["status"], LIMIT, task.repo))
    assert "*/.gitattributes is a named pipe (FIFO)" in said
    assert "delete it" in said and "restore" not in said


def test_the_reason_without_a_special_file_found_names_where_to_look(task, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nexec sleep 300\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    said = str(in_time(lambda: gate.uncommitted(task.repo)))
    assert said.startswith(f"git status did not answer in {LIMIT} s in {task.repo}")
    assert ".gitignore" in said and ".git/info/exclude" in said


def by_modules(env, monkeypatch, **settings):
    """The command line's supervisor of a task in a project verified by its modules, its plan
    accepted, and its clone's refs a FIFO; and the list of gate runs it reached."""
    from test_supervise import config

    from vivibox import supervise
    from vivibox.cli import main
    from vivibox.config import load_project
    from vivibox.task import find_task

    path = env / "config" / "projects" / "demo.toml"
    command = 'verify = ["mvn -pl {modules} -am verify"]'
    path.write_text(path.read_text().replace('verify = ["true"]', command))
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

    sup = supervise.make_supervisor(t, load_project("demo"), Pod(), config(**settings))
    fifo(t.repo, ".git/packed-refs")
    return t, sup, built


def test_what_the_verification_asks_the_clone_before_it_runs_ends_it_the_same_way(env, monkeypatch):
    """By modules, the changed files decide what is built: asked of git before the gate runs."""
    t, sup, built = by_modules(env, monkeypatch)
    result = in_time(lambda: sup.ports.run_gate(t))
    assert isinstance(result, gate.GateResult), f"the verification raised instead: {result!r}"
    assert not result.passed and "did not answer" in result.environment and built == []


def test_the_whole_build_before_review_ends_the_same_way(env, monkeypatch):
    t, sup, built = by_modules(env, monkeypatch, whole_build_before_review=True)
    result = in_time(lambda: sup.ports.run_whole_gate(t))
    assert isinstance(result, gate.GateResult), f"the verification raised instead: {result!r}"
    assert not result.passed and "did not answer" in result.environment and built == []


def test_the_supervisor_asks_where_the_work_stands_within_the_limit(env, monkeypatch):
    """Before a review and at the end, it compares the clone's HEAD with what was verified."""
    t, sup, _ = by_modules(env, monkeypatch)
    said = in_time(sup.ports.head)
    assert isinstance(said, repo.GitTimeout), f"taken as an answer: {said!r}"
    assert ".git/packed-refs" in str(said)


def notified(tmp_path, monkeypatch, rel: str, tasks: str = "tasks"):
    """The notification and the status of a verification git did not answer in, rel a FIFO."""
    source = make_repo(tmp_path / "source")
    t = create_task(tmp_path / tasks, "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    monkeypatch.setattr(repo, "CLONE_TIMEOUT", LIMIT, raising=False)
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        t.transition(s)
    fifo(t.repo, rel)
    notes = []
    ports = supervisor.Ports(
        run_gate=lambda x: gate.run_gate(x, FakePod(), ["make test"], []),
        risky_changes=lambda: [],
        notify=lambda _id, msg, kind="": notes.append(msg),
        sleep=lambda seconds: None,
    )
    sup = supervisor.Supervisor(t, FakeHarness(t), ports, max_rounds=3, project_verify=["make test"])
    sup.step()  # implement
    in_time(sup.step)  # verify
    return t, notes[-1], ui.environment_problem(t)


def test_the_notification_names_the_file_from_the_clones_root_whole(tmp_path, monkeypatch):
    """Under a long tasks folder the clone's own path would take the notification's whole length."""
    t, note, whole = notified(tmp_path, monkeypatch, "src/main/resources/.gitignore", "a-long-folder-" * 6)
    assert "in the task's clone, src/main/resources/.gitignore is a named pipe (FIFO)" in note
    assert "press g" in note
    said = note.removeprefix("verification could not run: ").split("; fix it, then press g")[0]
    cut = said.removesuffix("…")
    assert said == whole or (said.endswith("…") and whole[len(cut)] == " "), "never cut in a word"


def test_the_notification_suggests_nothing_to_run_in_your_repository(tmp_path, monkeypatch):
    """rm .git/packed-refs, pasted where you work, would delete your branches."""
    t, note, whole = notified(tmp_path, monkeypatch, ".git/packed-refs")
    assert "in the task's clone, .git/packed-refs is a named pipe (FIFO)" in note
    assert not [c for c in COMMANDS if c in note], note
    assert f"the clone is {t.repo}" in whole, "the whole path, in the status"


def test_control_characters_reach_neither_the_notification_nor_the_status(tmp_path, monkeypatch):
    t, note, whole = notified(tmp_path, monkeypatch, "x\x1b]0;title\x07\nFAKE/.gitignore")
    for shown in (note, whole):
        assert not [c for c in shown if ord(c) < 0x20 or ord(c) == 0x7F], repr(shown)
        assert "x‹U+001B›]0;title‹U+0007›‹U+000A›FAKE/.gitignore" in shown


class BlockingDocker:
    """docker as the pod runs it, where git in a container that copies the clone waits on it until
    the container is removed, as git on a FIFO does. The FIFO is made as git starts there: by a
    process the agent left in its pod, after every check the host made."""

    def __init__(self, clone: Path, waits_on: str, make_fifo: bool = True):
        self.clone, self.waits_on, self.make_fifo = clone, waits_on, make_fifo
        self.calls: list[list[str]] = []
        self.waiting: dict[str, threading.Event] = {}

    def __call__(self, cmd, timeout=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "rm", "-f"]:
            for name in cmd[3:]:
                self.waiting.pop(name, threading.Event()).set()
        elif cmd[:2] == ["docker", "exec"] and "git" in cmd and cmd[cmd.index("git") + 1] == self.waits_on:
            if self.make_fifo:
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

    pod = Pod(
        task.id, task.repo, "img", gate_dir=tmp_path / "gate", review_dir=tmp_path / "review", runner=docker
    )
    networks_up(pod)  # as up would have: the review checks its networks and the host's rules first
    return pod


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


def test_the_suites_own_git_in_a_clone_that_does_not_answer_is_ended(task, monkeypatch):
    """A round asked for twice (a regression in what stops it) had the suite's own question of
    the clone, rev-parse HEAD, wait on the pipe the first round left, in a thread nobody
    joined: git outlived the suite by hours. Asked within a time limit, it is ended."""
    import conftest

    monkeypatch.setattr(conftest, "OWN_GIT_SECONDS", LIMIT)
    fifo(task.repo, ".git/packed-refs")
    ended = in_time(lambda: as_verified(task))
    assert isinstance(ended, subprocess.TimeoutExpired), f"not ended: {ended!r}"
    waiting = subprocess.run(["pgrep", "-f", f"git -C {task.repo} rev-parse HEAD"], capture_output=True)
    assert not waiting.stdout.split(), "the suite's git is still waiting on the clone"


def test_the_reviewers_view_of_a_clone_that_does_not_answer_stops_the_round_for_you(
    task, tmp_path, monkeypatch
):
    """The host makes the reviewer's view of the commits from the clone, within the time a copy
    may take: a clone git waits on (a FIFO made by a process the agent left, after every check)
    stops the round with why, and no review container is made."""
    from test_supervisor import BLOCKING, reviewed

    from vivibox import reviewing

    monkeypatch.setattr(repo, "COPY_TIMEOUT", LIMIT, raising=False)
    sup, notes, reviewer = reviewed(task, tmp_path, [BLOCKING])
    docker = BlockingDocker(task.repo, "clone")
    pod = a_pod(task, docker, tmp_path)

    asked = []

    def review_up():
        pod.review_fresh()
        if not asked:  # once: a round asked for again finds the pipe, and the suite's git would wait
            asked.append(as_verified(task))  # what the verification passed, as the supervisor records it
            fifo(task.repo, ".git/packed-refs")
        reviewing.view(task, pod)
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
    assert "git fetch did not answer" in st.problem and ".git/packed-refs" in st.problem
    assert "git fetch did not answer" in notes[-1]
    assert st.problem.startswith(
        "stopped on an error: the review could not start: the reviewer's copy of the work could not be"
    )
    assert f"vivibox start {task.id}" in st.problem
    assert reviewer.prompts == [] and not (task.meta / "handoff" / "review-1.md").exists()
    assert not [c for c in docker.calls if c[:2] == ["docker", "run"]], "no review container was made"


def test_a_copy_that_did_not_finish_without_a_special_file_is_not_blamed_on_one(task, tmp_path, monkeypatch):
    """A large repository, or its submodules over a slow network, take long with nothing wrong."""
    monkeypatch.setattr(repo, "COPY_TIMEOUT", LIMIT, raising=False)
    docker = BlockingDocker(task.repo, "clone", make_fifo=False)
    result = in_time(lambda: gate.run_gate(task, a_pod(task, docker, tmp_path), ["make test"], []))
    assert isinstance(result, gate.GateResult), f"the verification raised instead: {result!r}"
    assert result.environment.startswith(f"git clone did not finish in {LIMIT} s in {task.repo}: ")
    assert "very large repository" in result.environment and "submodules" in result.environment
    assert "named pipe (FIFO) where" not in result.environment and not result.passed


def test_control_characters_reach_no_terminal_in_the_command_lines_error(env, monkeypatch, capsys):
    """vivibox accept on an open box closes it; its git waits on a FIFO the agent named."""
    from vivibox.cli import main

    monkeypatch.setattr(actions, "start_box", lambda task_id: None)
    monkeypatch.setattr(box, "start_box", lambda task_id: None)
    t = actions.open_box("demo")
    a_box_pod(t, monkeypatch)
    (t.repo / "work.txt").write_text("by hand\n")
    fifo(t.repo, "x\x1b[2J\nFAKE/.gitignore")
    assert in_time(lambda: main(["accept", t.id])) == 1
    said = capsys.readouterr().err
    assert said.startswith("vivibox: the box could not close: git ")
    assert not [c for c in said.rstrip("\n") if ord(c) < 0x20 or ord(c) == 0x7F], repr(said)
    assert "x‹U+001B›[2J‹U+000A›FAKE/.gitignore is a named pipe (FIFO)" in said
