"""A task whose clone is blocked by what it added (repo.Blocked) starts all the same: its pod
down after a stop or a restart of the machine, r and s bring it up, it is at the blocked
checkpoint again or the writer has its turn, and nothing of the host's reads the clone on the
way. Where a planner has a container of its own, its copy of the commits is not brought up to
date from such a clone, and it is given no turn on the copy it has.

The method is the review's: a git first on PATH that writes down where and with what it was
called before it runs the real one, and a repository in the clone whose settings would run a
program if git on the host went into it."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_repo
from test_control import docker  # noqa: F401 (the fixture)
from test_supervisor import FakeHarness

from vivibox import actions, image, opencode, prepare, supervise, toolchain, ui
from vivibox import repo as r
from vivibox.config import load_config, load_project
from vivibox.states import State

CONTROL_FLOWS = ["planner_writer_reviewer", "planner_executor", "supervisor_worker"]
FLOWS = ["single_agent", *CONTROL_FLOWS]


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


class Watched:
    """Every git the host runs from here on, as (the folder it ran in, its arguments), and the
    file a hostile repository's settings would make."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.log, self.marker = tmp_path / "git-calls.log", tmp_path / "ran-on-the-host"
        real = shutil.which("git")
        bin_dir = tmp_path / "logging-git"
        bin_dir.mkdir()
        (bin_dir / "git").write_text(
            f'#!/bin/sh\nprintf \'%s\\t%s\\n\' "$PWD" "$*" >> "{self.log}"\nexec "{real}" "$@"\n'
        )
        (bin_dir / "git").chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.monkeypatch = monkeypatch

    def hostile(self, clone: Path, name: str = "web") -> None:
        """A repository of its own in the clone, tracked, whose settings name a program."""
        nested = make_repo(clone / name)
        script = self.log.parent / "agent-program"
        script.write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 1\n")
        script.chmod(0o755)
        for key in ("core.fsmonitor", "core.hooksPath"):
            git(nested, "config", key, str(script))
        git(clone, "-c", "advice.addEmbeddedRepo=false", "add", "--", name)

    def start(self) -> None:
        self.monkeypatch.setenv("PATH", self.path)

    def in_clone(self, clone: Path) -> list[str]:
        """The calls that ran in the clone or named it: none is allowed while it is blocked."""
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return [call for call in calls if str(clone) in call]

    def nothing_ran_in(self, clone: Path) -> None:
        assert self.in_clone(clone) == [], "git on the host ran in a blocked clone"
        assert not self.marker.exists(), "the nested repository's settings ran a program on the host"


@pytest.fixture
def watched(tmp_path, monkeypatch) -> Watched:
    return Watched(tmp_path, monkeypatch)


@pytest.fixture
def machine(docker, monkeypatch):  # noqa: F811
    """A real start, control.refresh and the pod's own folders included, on a Docker that records;
    the servers and the supervisor's process are stand-ins that say they were asked."""
    asked: list[str] = []
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(actions, "available_models", lambda refresh=False: {})
    monkeypatch.setattr(toolchain, "ensure", lambda pod, java, tools=(): None)
    monkeypatch.setattr(prepare, "begin", lambda task, project, pod: None)
    for what in ("ensure_server", "restart_server"):
        monkeypatch.setattr(opencode.OpenCode, what, lambda self: None)
    monkeypatch.setattr(opencode.OpenCode, "session_exists", lambda self, session: True)
    monkeypatch.setattr(actions, "start_supervisor", lambda task: asked.append(f"supervise {task.id}"))
    return asked


def blocked_task(flow: str, watched: Watched):
    """A task of the flow, started once while its clone was as recorded (so a planner with a
    container of its own has its copy of the commits), then at the blocked checkpoint for a
    repository the writer added, its pod stopped."""
    task = actions.create("demo", "Add divide", orchestration=flow)
    actions.start(task.id, supervise=False)
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    watched.hostile(task.repo)
    with pytest.raises(r.Blocked):
        r.check_protection(task.repo, task.meta)
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_BLOCKED, reason="verification could not run", environment="blocked")
    task.set_paused(True)  # the pod stopped under s, or the machine restarted
    watched.start()
    return task


def copy_of(task) -> tuple:
    """The planner's copy of the commits as it stands: its commit and its files."""
    src = actions.task_pod(task.id).control_src
    if not (src / ".git").exists():
        return ()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True)
    return head.stdout.strip(), tuple(sorted(p.name for p in src.iterdir()))


START_PATHS = {
    "s at the blocked checkpoint": lambda task: actions.start(task.id),
    "r": lambda task: (actions.reply(task, "please go on"), actions.carry_on(task)),
    "a start again after the reply": lambda task: (
        actions.reply(task, "please go on"),
        actions.start(task.id, resume=True),
    ),
}


@pytest.mark.real_start
@pytest.mark.parametrize("flow", FLOWS)
@pytest.mark.parametrize("path", START_PATHS)
def test_a_blocked_task_starts_and_nothing_reads_its_clone(machine, watched, flow, path):
    task = blocked_task(flow, watched)
    before = copy_of(task)
    START_PATHS[path](task)
    st = task.read_state()
    assert st.problem == "" and not st.paused, "it started"
    assert machine == [f"supervise {task.id}"], "and its supervisor with it"
    replied = path != "s at the blocked checkpoint"
    assert st.state is (State.IMPLEMENT if replied else State.CHECKPOINT_BLOCKED)
    assert copy_of(task) == before, "the planner's copy is as it was: not refreshed from a blocked clone"
    watched.nothing_ran_in(task.repo)
    if not replied:
        actions.reply(task, "please go on")  # the reply is taken where the task waits
        assert task.read_state().state is State.IMPLEMENT
        watched.nothing_ran_in(task.repo)


@pytest.mark.real_start
@pytest.mark.parametrize("flow", CONTROL_FLOWS)
def test_the_start_says_on_the_record_that_the_planners_copy_was_left(machine, watched, flow):
    task = blocked_task(flow, watched)
    actions.start(task.id)
    left = [e["data"] for e in task.events() if e["type"] == "view_not_refreshed"]
    assert len(left) == 1 and left[0]["whose"] == "the planner's"
    assert left[0]["why"].startswith("the task added a repository of its own to its clone")
    from vivibox import timeline

    said = "the planner's copy of the commits left as it was: the task's clone is blocked"
    assert [line for line in timeline.lines(task) if line.endswith(said)], timeline.lines(task)[-6:]


def supervised(task, monkeypatch):
    """The command line's supervisor of the task, the writer a stand-in that records its turns
    and the planner the real reader (control.Reader, with the real control.refresh) around one."""
    turns = {"writer": FakeHarness(task), "planner": FakeHarness(task)}
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: turns[role])
    monkeypatch.setattr(opencode, "OpenCode", lambda *a, **k: turns["writer"])
    config = load_config()
    sup = supervise.make_supervisor(task, load_project("demo"), actions.task_pod(task.id), config)
    notes: list[str] = []
    sup.ports.notify = lambda _id, message, kind="": notes.append(message)
    sup.ports.sleep = lambda seconds: None
    return sup, turns, notes


@pytest.mark.real_start
@pytest.mark.parametrize("flow", FLOWS)
def test_after_the_reply_the_writer_has_its_turn_and_the_task_is_blocked_again(
    machine, watched, flow, monkeypatch
):
    """The supervisor's own restart: made anew for a task in implement, it gives the writer its
    turn; the verification after it runs no git in the clone, and the task waits for you again."""
    task = blocked_task(flow, watched)
    actions.reply(task, "please go on")
    actions.carry_on(task)
    sup, turns, notes = supervised(task, monkeypatch)
    sup.step()  # implement: the writer's turn
    assert turns["writer"].prompts, "the writer got its turn (two where it reviews its own work)"
    assert task.read_state().state is State.VERIFY
    sup.step()  # verify: the clone is still blocked
    st = task.read_state()
    assert (st.state, st.rounds, st.paused) == (State.CHECKPOINT_BLOCKED, 0, False)
    assert ui.view(task, st, True, 3).status == "verification could not run"
    assert notes[-1].startswith("verification could not run: the task added a repository of its own")
    assert turns["planner"].prompts == [] or flow == "single_agent", "no turn for a reader"
    watched.nothing_ran_in(task.repo)


@pytest.mark.real_start
@pytest.mark.parametrize("flow", CONTROL_FLOWS)
def test_a_reader_is_given_no_turn_on_a_copy_that_could_not_be_brought_up_to_date(
    machine, watched, flow, monkeypatch
):
    """Before each of its turns the planner's copy is brought up to date; from a blocked clone it
    is not, and the turn is not run on the copy as it was."""
    task = blocked_task(flow, watched)
    actions.start(task.id)
    sup, turns, _ = supervised(task, monkeypatch)
    with pytest.raises(r.Blocked):
        sup.planner.turn("Read the work.")
    assert turns["planner"].prompts == [], "no turn was run"
    watched.nothing_ran_in(task.repo)


@pytest.mark.real_start
@pytest.mark.parametrize("flow", CONTROL_FLOWS)
def test_a_clone_that_is_as_recorded_is_still_copied_for_the_planner_at_every_start(machine, watched, flow):
    """And the method sees it: the git that copies the commits is on the record, in the clone."""
    task = actions.create("demo", "Add divide", orchestration=flow)
    actions.start(task.id, supervise=False)
    (task.repo / "new.txt").write_text("x\n")
    git(task.repo, "add", "new.txt")
    git(task.repo, "commit", "-q", "-m", "Add a file")
    watched.start()
    actions.start(task.id, supervise=False)
    assert "new.txt" in copy_of(task)[1], "brought up to date"
    assert not [e for e in task.events() if e["type"] == "view_not_refreshed"]
    assert any("\tfetch " in call for call in watched.in_clone(task.repo)), "the watch sees host git"


@pytest.mark.real_start
@pytest.mark.parametrize("flow", CONTROL_FLOWS)
def test_any_other_refusal_of_the_clone_still_stops_the_start(machine, flow):
    """Only what the task added in ordinary work is passed over: a clone changed in any other
    way is not started on, as before."""
    task = actions.create("demo", "Add divide", orchestration=flow)
    actions.start(task.id, supervise=False)
    (task.repo / ".git" / "commondir").write_text("../elsewhere\n")
    with pytest.raises(r.RepoError, match="delete the task"):
        actions.start(task.id, supervise=False)
    assert task.read_state().problem.startswith("could not start: git files in the task's clone changed")


# --- the paths this round changed, watched the same way ----------------------------------------------


def test_the_gate_the_views_and_the_reply_run_no_git_in_a_clone_with_both_additions(
    tmp_path, watched, monkeypatch
):
    """A worktree and a hostile repository at once: the index is now read beside the added
    worktree (by vivibox, as a file), the reply looks at the clone again to write the feedback,
    and the status and the panel read the question and the reason. None of it is git's doing."""
    from test_blocked_clone import at_the_gate, worktree
    from test_gate import PLAN

    from vivibox import panel
    from vivibox.risky import Approvals
    from vivibox.task import create_task

    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(tmp_path / "config"))  # never your projects
    source = make_repo(tmp_path / "source")
    task = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    task.set_base_commit(r.prepare(source, task.repo, task.id, task.meta))
    Approvals(task.meta, task.repo).approve()
    worktree(task.repo, tmp_path / "elsewhere")
    watched.hostile(task.repo)
    watched.start()
    sup, notes, pod = at_the_gate(task)
    st = task.read_state()
    assert st.state is State.CHECKPOINT_BLOCKED and pod.commands == []
    assert notes[-1].startswith("verification could not run: the task added a repository of its own and a ")
    ui.view(task, st, True, 3)
    ui.task_detail(task, lambda t: "0/1", 3, 5, ui.Style(False), True)
    panel.detail(task, st, 3, running=False, pod=panel.PodView())
    actions.verify_again(task)
    sup.step()
    actions.reply(task, "please go on")
    fed = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "`git rm -f --cached`" in fed and "`git worktree remove`" in fed
    sup.step()  # the writer's turn
    sup.step()  # verified again: still blocked
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    watched.nothing_ran_in(task.repo)


@pytest.mark.parametrize("state", ["added", "added and committed"])
def test_a_worktree_the_index_lists_is_told_apart_with_no_git_of_the_hosts(
    tmp_path, watched, monkeypatch, state
):
    """Telling the worktree's own folder from a repository reads three files (its .git, and the
    entry's commondir and gitdir) and runs nothing: the gate, the views, g and the reply on such
    a clone leave no call of the host's git in it."""
    from test_blocked_clone import at_the_gate, worktree_inside
    from test_gate import PLAN

    from vivibox import panel
    from vivibox.risky import Approvals
    from vivibox.task import create_task

    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(tmp_path / "config"))  # never your projects
    source = make_repo(tmp_path / "source")
    task = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    task.set_base_commit(r.prepare(source, task.repo, task.id, task.meta))
    Approvals(task.meta, task.repo).approve()
    worktree_inside(task.repo, ".worktrees/feature", state)
    watched.start()
    sup, notes, pod = at_the_gate(task)
    st = task.read_state()
    assert st.state is State.CHECKPOINT_BLOCKED and pod.commands == []
    assert notes[-1].startswith("verification could not run: the task added a worktree to its clone")
    ui.view(task, st, True, 3)
    ui.task_detail(task, lambda t: "0/1", 3, 5, ui.Style(False), True)
    panel.detail(task, st, 3, running=False, pod=panel.PodView())
    actions.verify_again(task)
    sup.step()
    actions.reply(task, "please go on")
    assert "`git worktree remove`" in (task.meta / "handoff" / "verify-feedback.md").read_text()
    sup.step()  # the writer's turn
    sup.step()  # verified again: still blocked
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    watched.nothing_ran_in(task.repo)
