"""The host's files in handoff/, which the agent's pod mounts read-write: the agent names every file
there, so a link or a special file in place of one is never written or read through."""

import os
import stat
import threading
from pathlib import Path

import pytest

from vivibox import actions, feedback, gate, handoff, reviewing, supervisor
from vivibox.states import State
from vivibox.task import create_task

PLAN = """+++
mode = "code-only"
verify = ["true"]
+++

# Goal

Reset view.

## Acceptance criteria

- [ ] clicking the button resets the camera
"""
OUTSIDE = "the host's own file\n"


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "handoff"
    path.mkdir()
    return path


def linked(path: Path) -> Path:
    """path made a link to a file outside handoff/, as an agent in the pod could make it."""
    outside = path.parent.parent / "outside.txt"
    outside.write_text(OUTSIDE)
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    return outside


def test_a_link_is_not_written_through(folder):
    outside = linked(folder / "verify-feedback.md")
    with pytest.raises(handoff.HandoffError) as refused:
        handoff.write_text(folder / "verify-feedback.md", "feedback\n")
    assert isinstance(refused.value, OSError)
    assert "handoff/verify-feedback.md" in str(refused.value)
    assert outside.read_text() == OUTSIDE


def test_a_link_is_not_appended_to_copied_into_or_read(folder, tmp_path):
    outside = linked(folder / "comments.md")
    with pytest.raises(handoff.HandoffError):
        handoff.append_text(folder / "comments.md", "more\n")
    log = tmp_path / "verify-1.log"
    log.write_text("the gate's log\n")
    with pytest.raises(handoff.HandoffError):
        handoff.copy_file(log, folder / "comments.md")
    with pytest.raises(handoff.HandoffError):
        handoff.read_text(folder / "comments.md")
    assert outside.read_text() == OUTSIDE


def test_a_fifo_is_refused_without_waiting_for_a_reader(folder):
    fifo = folder / "verify-feedback.md"
    os.mkfifo(fifo)
    raised = []

    def write():
        try:
            handoff.write_text(fifo, "feedback\n")
        except handoff.HandoffError as e:
            raised.append(e)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    writer.join(timeout=5)
    assert not writer.is_alive(), "the host hung on a FIFO"
    assert raised and "handoff/verify-feedback.md" in str(raised[0])


def test_a_hard_link_is_refused_before_anything_is_emptied(folder, tmp_path):
    other = tmp_path / "other.txt"
    other.write_text(OUTSIDE)
    os.link(other, folder / "criteria.md")
    with pytest.raises(handoff.HandoffError):
        handoff.write_text(folder / "criteria.md", "- [ ] x\n")
    assert other.read_text() == OUTSIDE, "truncated before the check refused it"


def test_plain_files_are_created_replaced_appended_to_and_read(folder, tmp_path):
    path = folder / "criteria.md"
    assert handoff.read_text(path) == "", "a missing file reads as empty"
    handoff.write_text(path, "first, and a longer line\n")
    assert path.read_text() == "first, and a longer line\n"
    handoff.write_text(path, "second\n")
    assert path.read_text() == "second\n", "replaced, nothing of the longer text left"
    handoff.append_text(path, "third\n")
    assert handoff.read_text(path) == "second\nthird\n"
    log = tmp_path / "verify-1.log"
    log.write_text("the gate's log\n")
    handoff.copy_file(log, folder / "verify.log")
    assert (folder / "verify.log").read_text() == "the gate's log\n"


@pytest.mark.parametrize("name", ["verify-feedback.md", "verify.log"])
def test_feedback_through_a_link_is_a_problem_outside_the_code(tmp_path, name):
    task = create_task(tmp_path, "demo", "goal", "")
    outside = linked(task.meta / "handoff" / name)
    log = tmp_path / "verify-1.log"
    log.write_text("the gate's log\n")
    result = gate.GateResult(log)
    result.missing_criteria = ["x"]
    feedback.write_feedback(task, result)
    assert f"handoff/{name}" in result.environment
    assert outside.read_text() == OUTSIDE


def test_a_review_through_a_link_is_refused(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    outside = linked(task.meta / "handoff" / "review-1.md")
    with pytest.raises(handoff.HandoffError):
        reviewing.keep(task, 1, "## Blocking\n\n## Not blocking\n")
    assert outside.read_text() == OUTSIDE


def test_a_plan_is_not_accepted_through_a_linked_checklist(tmp_path):
    task = create_task(tmp_path, "demo", "Reset view", PLAN)
    outside = linked(task.meta / "handoff" / gate.CRITERIA_FILE)
    with pytest.raises(gate.GateError, match="handoff/criteria.md"):
        gate.accept_plan(task)
    assert outside.read_text() == OUTSIDE
    assert not (task.meta / gate.ACCEPTED_PLAN).exists(), "not half accepted"


def at_review(tasks_dir):
    task = create_task(tasks_dir, "demo", "Reset view", PLAN)
    task.transition(State.CHECKPOINT_PLAN)
    supervisor.accept_plan(task, "plan accepted")
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)
    return task


def test_criteria_are_not_added_through_a_linked_checklist(tmp_path):
    task = at_review(tmp_path)
    accepted = (task.meta / gate.ACCEPTED_PLAN).read_text()
    outside = linked(task.meta / "handoff" / gate.CRITERIA_FILE)
    with pytest.raises(gate.GateError, match="handoff/criteria.md"):
        gate.add_criteria(task, ["works before a load"])
    assert outside.read_text() == OUTSIDE
    assert (task.meta / gate.ACCEPTED_PLAN).read_text() == accepted, "nothing half added"


def test_a_reply_is_not_appended_through_a_link(tmp_path):
    task = at_review(tmp_path)
    outside = linked(task.meta / "handoff" / "comments.md")
    with pytest.raises(gate.GateError, match="handoff/comments.md"):
        actions.reply(task, "The button does nothing on a fresh page.")
    assert outside.read_text() == OUTSIDE
    assert task.read_state().state is State.CHECKPOINT_FINAL, "the task stays with you"


def test_a_refused_reply_adds_no_criterion_and_a_retry_works(tmp_path):
    task = at_review(tmp_path)
    kept = {p: p.read_text() for p in (task.meta / gate.ACCEPTED_PLAN, task.plan_path)}
    checklist = (task.meta / "handoff" / gate.CRITERIA_FILE).read_text()
    outside = linked(task.meta / "handoff" / "comments.md")
    with pytest.raises(gate.GateError, match="handoff/comments.md"):
        actions.reply(task, "It fails offline.", ["works offline"])
    assert outside.read_text() == OUTSIDE
    assert {p: p.read_text() for p in kept} == kept, "no criterion half added"
    assert (task.meta / "handoff" / gate.CRITERIA_FILE).read_text() == checklist
    assert not [e for e in task.events() if e["type"] == "criteria_added"]
    assert task.read_state().state is State.CHECKPOINT_FINAL
    (task.meta / "handoff" / "comments.md").unlink()
    assert actions.reply(task, "It fails offline.", ["works offline"]) is State.IMPLEMENT
    assert "works offline" in (task.meta / "handoff" / "comments.md").read_text()


def in_time(call):
    """call's result, or a failure when it hangs (a FIFO opened for reading waits for a writer).
    An error call raised is raised here, as itself: it is not a hang."""
    done, failed = [], []

    def run():
        try:
            done.append(call())
        except BaseException as e:
            failed.append(e)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if failed:
        raise failed[0]
    assert done, "the view hung on a FIFO"
    return done[0]


@pytest.mark.parametrize("blocked", [True, False])
def test_the_panel_does_not_hang_on_a_fifo_for_feedback(tmp_path, blocked):
    from vivibox import panel

    task = at_review(tmp_path)
    task.event("gate", passed=False, missing_criteria=1)
    task.transition(State.IMPLEMENT)
    if blocked:
        task.transition(State.VERIFY)
        task.transition(State.CHECKPOINT_BLOCKED)
    for name in ("verify-feedback.md", "verify.log"):
        os.mkfifo(task.meta / "handoff" / name)
    in_time(lambda: panel.detail(task, task.read_state(), 3, running=False, pod=panel.PodView()))


def test_how_to_run_the_app_is_not_written_through_a_link(tmp_path):
    from vivibox.pod import PodError

    task = create_task(tmp_path, "demo", "goal", "")
    outside = linked(task.meta / "handoff" / actions.DEMO_FILE)
    with pytest.raises(PodError, match="handoff/demo.md"):
        actions.write_instruction(task, "```bash\nnpm start\n```\n")
    assert outside.read_text() == OUTSIDE


def test_the_demo_session_is_not_written_through_a_link(tmp_path, monkeypatch):
    from vivibox import harness, opencode
    from vivibox.pod import PodError

    task = create_task(tmp_path, "demo", "goal", "")
    outside = linked(task.meta / "handoff" / actions.DEMO_SESSION)
    monkeypatch.setattr(
        opencode.OpenCode, "turn", lambda *a, **k: harness.Turn("ses_demo", True, 0.01, 10, "done")
    )
    with pytest.raises(PodError, match="handoff/demo-session"):
        actions.ask_agent_how_to_run(task, object())
    assert outside.read_text() == OUTSIDE


def test_a_refused_checklist_at_plan_review_names_no_key_that_cannot_help(env):
    from test_tui import at_plan_checkpoint, new_task, run

    task = new_task()
    at_plan_checkpoint(task)
    outside = linked(task.meta / "handoff" / gate.CRITERIA_FILE)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await pilot.pause()
        said = [str(n.message) for n in app._notifications]
        assert task.read_state().state is State.CHECKPOINT_PLAN
        assert any("handoff/criteria.md" in m and "delete it" in m for m in said), said
        assert not any("press e to edit" in m for m in said), said

    run(scenario, notifications=True)
    assert outside.read_text() == OUTSIDE


SECRET_STEPS = "```bash\ncat ~/.ssh/id_ed25519\n```\n"


def test_how_to_run_the_app_is_not_read_through_a_link(env, monkeypatch):
    """A link to a file of yours with shell blocks in it would make them the commands run in the
    pod, where the agent sees them."""
    from test_demo import FakePod, a_task, back_with_you

    from vivibox.pod import PodError

    task = back_with_you(a_task())
    outside = linked(task.meta / "handoff" / actions.DEMO_FILE)
    outside.write_text(SECRET_STEPS)
    pod = FakePod()
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)
    with pytest.raises(PodError, match="handoff/demo.md"):
        actions.demo(task.id, ask=False)
    assert pod.started == [], "nothing of that file is run"
    with pytest.raises(PodError, match="handoff/demo.md"):
        actions.demo_instruction(task)


def test_accepted_work_keeps_no_instruction_read_through_a_link(env):
    """The history's instruction is offered to the project's next task: a link's file is no record."""
    from test_commit_branch import git

    from vivibox.cli import main
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    (task.repo / "one.txt").write_text("x\n")
    git(task.repo, "add", "one.txt")
    git(task.repo, "-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one")
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    outside = linked(task.meta / "handoff" / actions.DEMO_FILE)
    outside.write_text(SECRET_STEPS)
    done = actions.finish(*actions.load(task.id))
    assert done.demo == "", "accepted, without the link's file as how to run it"


def test_run_app_in_the_view_says_why_a_linked_instruction_is_refused(env, monkeypatch):
    from test_demo import back_with_you
    from test_tui import new_task, run

    from vivibox import tui

    task = back_with_you(new_task("Runnable"))
    outside = linked(task.meta / "handoff" / actions.DEMO_FILE)
    outside.write_text(SECRET_STEPS)
    called = []
    monkeypatch.setattr(tui.Vivibox, "run_demo", lambda self, task_id, **kw: called.append(kw))

    async def scenario(app, pilot):
        app.reload()
        app.action_demo()
        await pilot.pause()
        said = [str(n.message) for n in app._notifications]
        assert any("handoff/demo.md" in m for m in said), said
        assert called == [], "nothing run"
        assert app.is_running

    run(scenario, notifications=True)


def test_a_linked_demo_session_never_reaches_the_pod(tmp_path, monkeypatch):
    """The session goes to the pod as an argument the agent can read: a link's file must not."""
    from vivibox import harness, opencode
    from vivibox.pod import PodError

    task = create_task(tmp_path, "demo", "goal", "")
    outside = linked(task.meta / "handoff" / actions.DEMO_SESSION)
    outside.write_text("HOST-SECRET-TOKEN")
    sessions = []

    def turn(self, prompt, session="", title="", on_step=None):
        sessions.append(session)
        return harness.Turn("ses_demo", True, 0.01, 10, "done")

    monkeypatch.setattr(opencode.OpenCode, "turn", turn)
    with pytest.raises(PodError, match="handoff/demo-session"):
        actions.ask_agent_how_to_run(task, object(), reply="the local profile")
    assert sessions == [], "no turn with what the link points to"
    (task.meta / "handoff" / actions.DEMO_SESSION).unlink()
    os.mkfifo(task.meta / "handoff" / actions.DEMO_SESSION)

    def ask():
        try:
            actions.ask_agent_how_to_run(task, object(), reply="the local profile")
        except PodError as e:
            return e

    assert isinstance(in_time(ask), PodError) and sessions == []


# --- reads: what the agent hands over is never read through a link or waited on ---------------


def within(call):
    """call's result, or the error it raised; a failure when it hangs."""
    done = []

    def run():
        try:
            done.append(call())
        except Exception as e:
            done.append(e)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert done, "the host hung on a FIFO"
    return done[0]


def special(path: Path, kind: str) -> None:
    """path made a FIFO, a folder or a whiteout device (a char device 0:0, the one an unprivileged
    process can make, and so the one an agent in the pod can)."""
    path.unlink(missing_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "folder":
        path.mkdir()
    else:
        try:
            os.mknod(path, stat.S_IFCHR | 0o600, 0)
        except OSError as e:
            pytest.skip(f"no whiteout device on this filesystem: {e}")


@pytest.mark.parametrize("kind", ["fifo", "folder", "device"])
def test_a_special_file_is_refused_without_being_opened(folder, monkeypatch, kind):
    path = folder / "question.md"
    special(path, kind)
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real_open(p, *a, **k))
    refused = within(lambda: handoff.read_text(path))
    assert isinstance(refused, handoff.HandoffError) and "handoff/question.md" in str(refused)
    with pytest.raises(handoff.HandoffError, match="handoff/question.md"):
        handoff.write_text(path, "x\n")
    assert not [p for p in opened if p.endswith("question.md")], "refused on lstat, before any open"


def test_a_file_under_a_linked_folder_is_not_read_when_the_walk_starts_above_it(tmp_path):
    """The reviewer can put a link in place of its out folder: below the root, no part of the path
    is followed."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "review.md").write_text(OUTSIDE)
    (tmp_path / "out").symlink_to(elsewhere)
    with pytest.raises(handoff.HandoffError, match="out"):
        handoff.read_text(tmp_path / "out" / "review.md", root=tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "review.md").write_text("mine\n")
    assert handoff.read_text(tmp_path / "real" / "review.md", root=tmp_path) == "mine\n"
    assert handoff.read_text(tmp_path / "real" / "none.md", root=tmp_path) == ""


def planning(tmp_path):
    """A task whose planner left only the template: a question there is the planner's answer."""
    return create_task(
        tmp_path,
        "demo",
        "Reset view",
        PLAN.replace("clicking the button resets the camera", gate.PLACEHOLDER),
    )


def read_sites():
    """Each host read of a file the agent names in handoff/: what it reads, and the call that
    returns what reaches you, the agent or the pod from it."""
    from vivibox import logs, manual, proposal, sections

    return {
        "question.md": lambda task: supervisor.question(task),
        "verify-proposal.md": lambda task: proposal.proposed(task),
        "context.md": lambda task: manual.prompts(task, task.repo),
        "review-1.md": lambda task: [
            sections.reviewers_notes(task),
            [e.said for e in logs.entries(task, task.read_state(), False)[0]],
        ],
        "prepare.log": lambda task: [e.said for e in logs.entries(task, task.read_state(), False)[0]],
    }


@pytest.mark.parametrize(
    "name", ["question.md", "verify-proposal.md", "context.md", "review-1.md", "prepare.log"]
)
def test_a_linked_handoff_file_never_reaches_what_its_reader_returns(tmp_path, name):
    task = planning(tmp_path)
    outside = linked(task.meta / "handoff" / name)
    outside.write_text("## Blocking\n\n## Not blocking\n\n## Checked\n\n- HOST-SECRET\n")
    said = str(in_time(lambda: read_sites()[name](task)))
    assert "HOST-SECRET" not in said


@pytest.mark.parametrize(
    "name", ["question.md", "verify-proposal.md", "context.md", "review-1.md", "prepare.log", "prepare.exit"]
)
@pytest.mark.parametrize("kind", ["fifo", "device"])
def test_no_reader_of_a_handoff_file_waits_on_a_special_one(tmp_path, name, kind):
    from vivibox import prepare

    task = planning(tmp_path)
    special(task.meta / "handoff" / name, kind)
    sites = {**read_sites(), "prepare.exit": lambda task: prepare.ending(task)}
    in_time(lambda: sites[name](task))


def test_a_linked_exit_code_is_no_ending(tmp_path):
    from vivibox import prepare

    task = create_task(tmp_path, "demo", "goal", "")
    linked(task.meta / "handoff" / prepare.EXIT).write_text("0\n")
    assert prepare.ending(task) is None, "a file of yours is no record of the preparation"


def test_a_refused_question_stops_the_task_and_says_why(tmp_path):
    task = planning(tmp_path)
    linked(task.meta / "handoff" / supervisor.QUESTION).write_text("HOST-SECRET\n")
    said = supervisor.question(task)
    assert said and "handoff/question.md" in said and "HOST-SECRET" not in said, said


@pytest.mark.parametrize("kind", ["link", "fifo"])
@pytest.mark.parametrize("where", ["planner asks", "blocked", "manual plan"])
def test_the_details_say_why_a_linked_question_or_report_is_not_shown(tmp_path, where, kind):
    from vivibox import manual, panel

    task = planning(tmp_path)
    task.transition(State.CHECKPOINT_PLAN)
    name = supervisor.QUESTION
    if where == "blocked":
        task = at_review(tmp_path / "other")
        task.transition(State.IMPLEMENT)
        task.transition(State.VERIFY)
        task.transition(State.CHECKPOINT_BLOCKED)
    elif where == "manual plan":
        task.set_awaiting_plan(True)
        name = manual.CONTEXT
    if kind == "link":
        linked(task.meta / "handoff" / name).write_text("HOST-SECRET\n")
    else:
        special(task.meta / "handoff" / name, "fifo")
    shown = in_time(lambda: panel.detail(task, task.read_state(), 3, running=False, pod=panel.PodView()))
    assert "HOST-SECRET" not in shown
    assert f"handoff/{name}" in shown, shown


def test_a_review_through_a_link_shows_no_notes_of_it(tmp_path):
    from vivibox import sections

    task = create_task(tmp_path, "demo", "goal", "")
    linked(task.meta / "handoff" / "review-1.md").write_text(
        "## Blocking\n\n## Not blocking\n\n## Checked\n\n- ok\n"
    )
    shown = "\n".join(sections.reviewers_notes(task))
    assert "0 blocking" not in shown, "a refused review is no clean review"
    assert "handoff/review-1.md" in shown, shown


def test_l_names_a_refused_review_and_offers_no_pager_on_it(tmp_path):
    from vivibox import logs

    task = create_task(tmp_path, "demo", "goal", "")
    os.mkfifo(task.meta / "handoff" / "review-1.md")
    found = in_time(lambda: logs.entries(task, task.read_state(), False)[0])
    entry = next(e for e in found if e.label == "review-1.md")
    assert "a link or not a plain file" in entry.said and entry.command == []


@pytest.mark.parametrize("draft", ["a plan", "a plan acceptance refuses", "a FIFO"])
def test_a_linked_plan_draft_never_becomes_the_plan(tmp_path, draft):
    """A link to a file of yours that reads as a plan would be copied into plan.md, which the agent
    reads: nothing of it gets there, and you are told why."""
    from test_supervisor import DRAFT, FakeHarness, make

    task = planning(tmp_path)
    text = DRAFT.replace("health endpoint returns 200", "HOST-SECRET")
    if draft == "a plan acceptance refuses":
        text = text.replace("- [ ] HOST-SECRET", f"- [ ] {gate.PLACEHOLDER}\n\nHOST-SECRET")

    def link(t):
        if draft == "a FIFO":
            special(t.meta / "handoff" / "plan-draft.md", "fifo")
        else:
            linked(t.meta / "handoff" / "plan-draft.md").write_text(text)

    sup, notes = make(task, FakeHarness(task, [link]))
    in_time(sup.step)
    assert task.read_state().state is State.CHECKPOINT_PLAN
    assert "HOST-SECRET" not in task.plan_path.read_text()
    assert "handoff/plan-draft.md" in notes[-1], notes


@pytest.mark.parametrize("linked_part", ["review.md", "the out folder", "a FIFO"])
def test_a_linked_review_is_never_kept_for_the_writer(task_with_review, linked_part):
    from test_supervisor import BLOCKING

    sup, notes, out = task_with_review
    if linked_part == "review.md":
        linked(out / "review.md").write_text(BLOCKING.replace("asserts True", "HOST-SECRET"))
    elif linked_part == "a FIFO":
        special(out / "review.md", "fifo")
    else:
        elsewhere = out.parent / "elsewhere"
        out.rename(elsewhere)
        out.symlink_to(elsewhere)
        (elsewhere / "review.md").write_text(BLOCKING.replace("asserts True", "HOST-SECRET"))
    for _ in range(3):  # implement, verify, review
        in_time(sup.step)
    kept = (sup.task.meta / "handoff" / "review-1.md").read_text()
    assert "HOST-SECRET" not in kept
    assert sup.task.read_state().state is State.CHECKPOINT_FINAL, "as with no review: to you"
    problem = [e["data"]["problem"] for e in sup.task.events() if e["type"] == "review"]
    assert problem and "review.md" in problem[0], problem


@pytest.fixture
def task_with_review(tmp_path):
    from test_supervisor import reviewed

    task = create_task(tmp_path / "tasks", "demo", "Add health endpoint", PLAN)
    sup, notes, reviewer = reviewed(task, tmp_path, [])
    return sup, notes, reviewer.out


def gated(tmp_path):
    from conftest import make_repo

    from vivibox import repo
    from vivibox.risky import Approvals

    source = make_repo(tmp_path / "source")
    task = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    task.set_base_commit(repo.prepare(source, task.repo, task.id, task.meta))
    Approvals(task.meta, task.repo).approve()
    gate.accept_plan(task)
    (task.repo / "tests").mkdir()
    (task.repo / "tests" / "test_view.py").write_text("def test_reset():\n    assert True\n")
    from test_gate import git

    git(task.repo, "add", ".")
    git(task.repo, "commit", "-q", "-m", "Test the reset")
    return task


def test_the_gate_counts_a_linked_checklist_and_red_evidence_as_missing_and_says_why(tmp_path):
    from test_gate import FakePod

    task = gated(tmp_path)
    folder = task.meta / "handoff"
    (folder / gate.CRITERIA_FILE).unlink()
    missing = gate.run_gate(task, FakePod(), ["true"], [])
    linked(folder / gate.CRITERIA_FILE).write_text("- [x] clicking the button resets the camera\n")
    (folder / "red.md").symlink_to(tmp_path / "outside.txt")
    (tmp_path / "outside.txt").write_text("tests/test_view.py\n")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert result.missing_criteria == missing.missing_criteria == ["clicking the button resets the camera"]
    assert result.no_red_evidence == missing.no_red_evidence == ["tests/test_view.py"]
    assert not result.passed
    assert "handoff/criteria.md" in result.environment and "handoff/red.md" in result.environment
    assert gate.next_state(result, 0, 3) is State.CHECKPOINT_BLOCKED, "for you, no round spent"


def test_no_line_of_a_linked_checklist_is_quoted_back_to_the_agent(tmp_path):
    task = at_review(tmp_path)
    linked(task.meta / "handoff" / gate.CRITERIA_FILE).write_text(
        "- [x] clicking the button resets the camera HOST\n"
    )
    criterion = "clicking the button resets the camera"
    assert gate.missing_criteria(task) == [criterion]
    assert gate.reworded_criteria(task, [criterion]) == {}


def test_the_gate_does_not_wait_on_a_special_checklist_or_red_evidence(tmp_path):
    task = gated(tmp_path)
    folder = task.meta / "handoff"
    special(folder / gate.CRITERIA_FILE, "fifo")
    special(folder / "red.md", "fifo")
    criterion = "clicking the button resets the camera"
    assert in_time(lambda: gate.missing_criteria(task)) == [criterion]
    assert in_time(lambda: gate.reworded_criteria(task, [criterion])) == {}
    assert in_time(lambda: gate.red_evidence_missing(task, task.read_state().base_commit)) == [
        "tests/test_view.py"
    ]


def test_accepting_does_not_wait_on_a_special_file_and_keeps_nothing_through_a_link(env):
    from vivibox import review

    task = at_review(env / "tasks")
    folder = task.meta / "handoff"
    special(folder / gate.CRITERIA_FILE, "fifo")
    linked(folder / actions.DEMO_FILE).write_text("HOST-SECRET\n")
    special(folder / "review-1.md", "fifo")
    (folder / "review-2.md").write_text("## Blocking\n\n## Not blocking\n\n## Checked\n\n- ok\n")
    kept = in_time(lambda: review.archive(task))
    assert not (kept / gate.CRITERIA_FILE).exists() and not (kept / actions.DEMO_FILE).exists()
    assert not (kept / "review-1.md").exists()
    assert (kept / "review-2.md").read_text().endswith("- ok\n"), "the plain ones are kept"
    timeline = (kept / "timeline.txt").read_text()
    assert "HOST-SECRET" not in timeline
    for name in (gate.CRITERIA_FILE, actions.DEMO_FILE, "review-1.md"):
        assert f"handoff/{name}" in timeline, timeline


@pytest.mark.parametrize("kind", ["link", "fifo"])
def test_run_app_says_why_it_did_not_read_the_agents_question(tmp_path, monkeypatch, kind):
    from vivibox import harness, opencode
    from vivibox.pod import PodError

    task = create_task(tmp_path, "demo", "goal", "")
    folder = task.meta / "handoff"

    def turn(self, prompt, session="", title="", on_step=None):
        (folder / actions.DEMO_FILE).write_text("```bash\nnpm start\n```\n")
        if kind == "link":
            linked(folder / actions.DEMO_QUESTION).write_text("HOST-SECRET\n")
        else:
            special(folder / actions.DEMO_QUESTION, "fifo")
        return harness.Turn("ses_demo", True, 0.01, 10, "done")

    monkeypatch.setattr(opencode.OpenCode, "turn", turn)

    def ask():
        try:
            return actions.ask_agent_how_to_run(task, object())
        except PodError as e:
            return e

    said = in_time(ask)
    assert isinstance(said, PodError) and "handoff/demo-question.md" in str(said), said
    assert "HOST-SECRET" not in str(said)


# --- fix round 1 ------------------------------------------------------------------------------

REFUSED = "is a link or not a plain file, and vivibox does not read through one"


@pytest.mark.parametrize("stage", ["planning", "implementing"])
def test_a_refused_question_is_viviboxs_reason_not_the_agents_question(tmp_path, stage):
    """The words are vivibox's, and the way on is a key: r answers, which moves the file away."""
    from test_supervisor import FakeHarness, command_supervisor, make

    from vivibox import panel

    def link(t):
        linked(t.meta / "handoff" / supervisor.QUESTION).write_text("HOST-SECRET\n")

    task = planning(tmp_path)
    if stage == "planning":
        sup, notes = make(task, FakeHarness(task, [link]))
    else:
        sup, notes, _ = command_supervisor(task, FakeHarness(task, [link]))
    sup.step()
    path = task.meta / "handoff" / supervisor.QUESTION
    said = f"vivibox did not read the agent's question: {path} {REFUSED}"
    assert notes[-1] == f"{said}; press r to answer (vivibox reply {task.id})"
    shown = panel.detail(task, task.read_state(), 3, running=False, pod=panel.PodView())
    assert "The agent asks" not in shown and "HOST-SECRET" not in shown
    assert f"**vivibox did not read the agent's question:** {path} {REFUSED}." in shown, shown


def test_a_refused_proposal_is_named_at_the_command_checkpoint(tmp_path):
    from test_supervisor import FakeHarness, command_supervisor

    from vivibox import panel, proposal

    def link(t):
        linked(t.meta / "handoff" / proposal.PROPOSAL).write_text("cat ~/.ssh/id_ed25519\n")

    task = planning(tmp_path)
    sup, notes, kept = command_supervisor(task, FakeHarness(task, [link]))
    sup.step()
    path = task.meta / "handoff" / proposal.PROPOSAL
    assert task.read_state().state is State.CHECKPOINT_COMMAND and kept == []
    assert notes[-1] == f"no command came from the writer: {path} {REFUSED}; type one, or send it back"
    assert panel.command_to_review(task)[0] == f"**No command came from the writer:** {path} {REFUSED}."


def test_l_names_a_refused_preparation_log_and_offers_no_pager_on_it(tmp_path):
    from vivibox import logs, prepare

    task = create_task(tmp_path, "demo", "goal", "")
    os.mkfifo(task.meta / "handoff" / prepare.LOG)
    found = in_time(lambda: logs.entries(task, task.read_state(), False)[0])
    entry = next(e for e in found if e.label == prepare.LOG)
    assert entry.said == logs.NOT_READ and entry.command == [], "Enter never opens a FIFO"


def test_a_linked_out_folder_is_named_not_the_review_in_it(task_with_review):
    """The reviewer is told to fix the folder it linked: deleting review.md would change nothing."""
    from test_supervisor import BLOCKING

    sup, notes, out = task_with_review
    elsewhere = out.parent / "elsewhere"
    out.rename(elsewhere)
    out.symlink_to(elsewhere)
    (elsewhere / "review.md").write_text(BLOCKING)
    for _ in range(3):  # implement, verify, review
        in_time(sup.step)
    problem = [e["data"]["problem"] for e in sup.task.events() if e["type"] == "review"][0]
    assert problem.startswith(f"{out} is a link or not a folder"), problem
    assert f"{out} is a link or not a folder" in sup.reviewer.prompts[-1]


def test_a_review_vivibox_could_not_read_shows_no_counts(task_with_review):
    """Read as no notes, a review it never read would look like a clean one, in the details and l."""
    from vivibox import logs, sections

    sup, notes, out = task_with_review
    linked(out / "review.md").write_text("## Blocking\n\n## Not blocking\n\n## Checked\n\n- ok\n")
    for _ in range(3):  # implement, verify, review
        in_time(sup.step)
    shown = "\n".join(sections.reviewers_notes(sup.task))
    assert "0 blocking" not in shown and "#### Review 1: unreadable" in shown, shown
    assert f"{out / 'review.md'} {REFUSED}" in shown
    said = next(
        e.said for e in logs.entries(sup.task, sup.task.read_state(), False)[0] if e.label == "review-1.md"
    )
    assert said == f"review 1 · unreadable: review.md {REFUSED}; delete it and try again", said
