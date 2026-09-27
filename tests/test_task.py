import json

import pytest

from vivibox.states import State, TransitionError
from vivibox.task import create_task, find_task, list_tasks


def test_create_numbers_tasks_per_project(tmp_path):
    a = create_task(tmp_path, "shop", "first", "{{goal}}")
    b = create_task(tmp_path, "shop", "second", "{{goal}}")
    c = create_task(tmp_path, "blog", "other", "{{goal}}")
    assert (a.id, b.id, c.id) == ("shop-1", "shop-2", "blog-1")
    assert a.plan_path.read_text() == "first"
    assert {t.id for t in list_tasks(tmp_path)} == {"shop-1", "shop-2", "blog-1"}


def test_new_task_starts_in_plan_with_event(tmp_path):
    task = create_task(tmp_path, "shop", "goal", "")
    st = task.read_state()
    assert st.state is State.PLAN and (st.iteration, st.rounds) == (0, 0) and not st.paused
    assert [e["type"] for e in task.events()] == ["created"]
    assert (task.meta / "handoff").is_dir() and (task.meta / "log").is_dir()


def test_transitions_are_logged_and_count_iterations(tmp_path):
    task = create_task(tmp_path, "shop", "goal", "")
    for target in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.IMPLEMENT, State.VERIFY):
        task.transition(target)
    task.transition(State.CHECKPOINT_FINAL, reason="green")
    st = task.read_state()
    assert (st.iteration, st.rounds) == (2, 1), "two verifications, one fix turn"
    last = task.events()[-1]
    assert last["type"] == "state"
    assert last["data"] == {"previous": "verify", "current": "checkpoint:final", "reason": "green"}


def test_illegal_transition_changes_nothing(tmp_path):
    task = create_task(tmp_path, "shop", "goal", "")
    with pytest.raises(TransitionError):
        task.transition(State.DONE)
    assert task.read_state().state is State.PLAN
    assert len(task.events()) == 1


def test_done_requires_final_checkpoint(tmp_path):
    task = create_task(tmp_path, "shop", "goal", "")
    for target in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.APPROVAL_RISKY):
        task.transition(target)
    with pytest.raises(TransitionError):
        task.transition(State.DONE)
    task.transition(State.CHECKPOINT_FINAL)
    task.transition(State.DONE)


@pytest.mark.parametrize("task_id", ["../x", "shop", "shop-1/../../etc"])
def test_find_rejects_unknown_or_unsafe_ids(tmp_path, task_id):
    with pytest.raises(KeyError):
        find_task(tmp_path, task_id)


def test_state_from_a_newer_version_still_reads(tmp_path):
    task = create_task(tmp_path, "demo", "Goal", "+++\n+++\n")
    path = task.meta / "state.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "added_later": 1}))
    assert task.read_state().goal == "Goal"


def test_task_numbers_are_not_reused(tmp_path):
    import shutil

    first = create_task(tmp_path, "shop", "first", "{{goal}}")
    shutil.rmtree(first.root)
    assert create_task(tmp_path, "shop", "second", "{{goal}}").id == "shop-2"


def test_sessions_are_kept_one_per_role(tmp_path):
    """A planner and a writer on the same harness (two models through opencode) would otherwise
    share one conversation, and a reviewer would review its own writing."""
    task = create_task(tmp_path, "shop", "goal", "")
    task.set_session("writer", "ses_abc")
    task.set_session("planner", "ses_def")
    st = task.read_state()
    assert st.sessions == {"writer": "ses_abc", "planner": "ses_def"}
    task.set_session("writer", "")
    assert task.read_state().sessions == {"planner": "ses_def"}, "cleared, not emptied"


def test_a_task_with_sessions_per_harness_keeps_them_under_their_roles(tmp_path):
    """Until roles owned sessions, a session was kept under its harness's name: opencode's was
    the writer's (the only one that could write), claude-code's the planner's."""
    task = create_task(tmp_path, "shop", "goal", "")
    path = task.meta / "state.json"
    data = json.loads(path.read_text())
    data["sessions"] = {"opencode": "ses_abc", "claude-code": "fdcccc7a-1a4d"}
    path.write_text(json.dumps(data))
    assert task.read_state().sessions == {"writer": "ses_abc", "planner": "fdcccc7a-1a4d"}


def test_a_task_started_before_roles_keeps_its_conversation(tmp_path):
    """Every session written until now was opencode's, under a plain 'session' key. Losing it would
    start a fresh conversation on a task already half done, with only its files to go on."""
    task = create_task(tmp_path, "shop", "goal", "")
    path = task.meta / "state.json"
    data = json.loads(path.read_text())
    data.pop("sessions", None)
    data["session"] = "ses_from_before"
    path.write_text(json.dumps(data))
    assert task.read_state().sessions == {"writer": "ses_from_before"}


def test_a_task_can_run_a_role_on_another_model(tmp_path):
    """A task going badly on a cheap model is worth finishing on a better one, and a throwaway one
    is not worth the good model at all. The choice belongs to the task, not to the machine."""
    task = create_task(tmp_path, "shop", "goal", "")
    assert task.read_state().models == {}, "the config decides until you say otherwise"
    task.set_model("planner", "claude-opus-5")
    assert task.read_state().models == {"planner": "claude-opus-5"}
    task.set_model("planner", "")
    assert task.read_state().models == {}, "cleared, so the config decides again"


def test_a_blocked_task_can_go_back_to_verification_without_the_agent():
    from vivibox.states import check_transition

    check_transition(State.CHECKPOINT_BLOCKED, State.VERIFY)
