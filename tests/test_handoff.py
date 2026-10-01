"""The host's files in handoff/, which the agent's pod mounts read-write: the agent names every file
there, so a link or a special file in place of one is never written or read through."""

import os
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
