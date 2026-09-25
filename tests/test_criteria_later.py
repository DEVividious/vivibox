"""Acceptance criteria added after the plan was accepted: found at review, held by the gate."""

import pytest

from vivibox import actions, gate, supervisor
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.states import State
from vivibox.task import create_task, find_task

PLAN = """+++
mode = "code-only"
verify = ["true"]
+++

# Goal

Reset view.

## Acceptance criteria

- [ ] clicking the button resets the camera
- [ ] a drag after a reset starts from the start,
      not from where the camera was

## Out of scope

- Zoom
"""


def at_review(tasks_dir):
    task = create_task(tasks_dir, "demo", "Reset view", PLAN)
    task.transition(State.CHECKPOINT_PLAN)
    supervisor.accept_plan(task, "plan accepted")
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)
    return task


def tick_all(task):
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    path.write_text(path.read_text().replace("- [ ]", "- [x]"))


def test_a_criterion_added_at_review_is_held_by_the_gate(tmp_path):
    """In the olkatter run a bug found at review went back as a comment; the agent added tests,
    but the checklist stayed at 20 and nothing held the work to what you had found."""
    task = at_review(tmp_path)
    tick_all(task)
    assert gate.missing_criteria(task) == []
    added = gate.add_criteria(task, ["- [ ] the button works  before an image is loaded", ""])
    assert added == ["the button works before an image is loaded"], "one line, your checkbox dropped"
    assert gate.missing_criteria(task) == ["the button works before an image is loaded"]
    for path in (task.meta / gate.ACCEPTED_PLAN, task.plan_path):
        text = path.read_text()
        # At the end of the criteria, after the wrapped one, not after "Out of scope".
        assert text.index("- [ ] the button works") < text.index("## Out of scope")
        assert text.index("not from where the camera was") < text.index("- [ ] the button works")
    tick_all(task)
    assert gate.missing_criteria(task) == []
    assert task.events()[-1]["data"] == {"criteria": ["the button works before an image is loaded"]}


def test_a_criterion_is_added_once(tmp_path):
    task = at_review(tmp_path)
    with pytest.raises(gate.GateError, match="already a criterion"):
        gate.add_criteria(task, ["clicking the button resets the camera"])
    with pytest.raises(gate.GateError, match="already a criterion"):
        gate.add_criteria(task, ["x", "x"])
    assert len(gate.missing_criteria(task)) == 2, "nothing written when one is refused"


def test_before_acceptance_criteria_belong_in_the_plan(tmp_path):
    task = create_task(tmp_path, "demo", "Reset view", PLAN)
    with pytest.raises(gate.GateError, match="not accepted"):
        gate.add_criteria(task, ["x"])


def test_a_reply_at_review_can_bring_criteria(tmp_path):
    task = at_review(tmp_path)
    assert (
        actions.reply(task, "The button does nothing on a fresh page.", ["works before a load"])
        is State.IMPLEMENT
    )
    comments = (task.meta / "handoff" / "comments.md").read_text()
    assert "does nothing on a fresh page" in comments and "- works before a load" in comments
    assert "works before a load" in gate.missing_criteria(task)


def test_criteria_alone_are_reply_enough(tmp_path):
    task = at_review(tmp_path)
    actions.reply(task, "", ["works before a load"])
    assert "New acceptance criteria" in (task.meta / "handoff" / "comments.md").read_text()


def test_criteria_wait_for_the_work_to_come_back(tmp_path):
    """While the agent works it edits the checklist itself, and before acceptance there is only the
    plan: adding then would race the agent or skip your review of the plan."""
    task = create_task(tmp_path, "demo", "Reset view", PLAN)
    task.transition(State.CHECKPOINT_PLAN)
    with pytest.raises(gate.GateError, match="comes back to you"):
        actions.reply(task, "more", ["x"])
    assert task.read_state().state is State.CHECKPOINT_PLAN


def test_the_command_line_adds_criteria_too(env, capsys):
    tasks = load_config().tasks_dir
    task = at_review(tasks)
    assert (
        main(["reply", task.id, "--criterion", "works before a load", "--criterion", "keeps the zoom"]) == 0
    )
    assert "with 2 new criteria" in capsys.readouterr().out
    assert gate.missing_criteria(find_task(tasks, task.id))[-2:] == ["works before a load", "keeps the zoom"]
