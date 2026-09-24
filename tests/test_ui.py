from datetime import UTC, datetime, timedelta

from vivibox import ui
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = "+++\n+++\n\n# Goal\n\n{{goal}}\n"
plain = ui.Style(False)


def test_ago():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    at = lambda **kw: (now - timedelta(**kw)).isoformat()  # noqa: E731
    assert ui.ago(at(seconds=5), now) == "just now"
    assert ui.ago(at(minutes=7), now) == "7 min ago"
    assert ui.ago(at(hours=3, minutes=5), now) == "3 h ago"
    assert ui.ago(at(days=2), now) == "2 d ago"


def test_lasting_is_a_duration_not_a_moment():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    at = lambda **kw: (now - timedelta(**kw)).isoformat()  # noqa: E731
    assert ui.lasting(at(seconds=20), now) == ""
    assert ui.lasting(at(minutes=7), now) == "for 7 min"
    assert ui.lasting(at(hours=2), now) == "for 2 h"


def test_the_panel_says_how_long_implementing_has_taken(tmp_path):
    from datetime import UTC, datetime, timedelta

    task = create_task(tmp_path, "demo", "Goal", TEMPLATE)
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    st = task.read_state()
    st.updated = (datetime.now(UTC) - timedelta(minutes=7)).isoformat(timespec="milliseconds")
    task._write_state(st)
    assert ui.lasting(task.read_state().updated) == "for 7 min"


def test_shorten():
    assert ui.shorten("short", 10) == "short"
    assert ui.shorten("a long goal text", 8) == "a long…"


def test_shorten_does_not_cut_a_word_in_half():
    title = "Serve latest FPL dream team on a FastAPI page with cached data and offline fallback"
    assert ui.shorten(title, 72) == "Serve latest FPL dream team on a FastAPI page with cached data and…"


def test_shorten_cuts_mid_word_when_there_is_no_better_place():
    assert ui.shorten("Supercalifragilisticexpialidocious", 20) == "Supercalifragilisti…"
    assert ui.shorten("tiny wordsfollowedbyaverylongoneindeed", 30) == "tiny wordsfollowedbyaverylong…"


def test_style_is_plain_without_color():
    assert plain("x", "bold") == "x"
    assert ui.Style(True)("x", "bold") == "\033[1mx\033[0m"


def test_list_is_a_table_with_waiting_tasks_first(tmp_path):
    waiting = create_task(tmp_path, "demo", "Write the plan", TEMPLATE)
    waiting.transition(State.CHECKPOINT_PLAN)
    working = create_task(tmp_path, "demo", "Implement it", TEMPLATE)
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.IMPLEMENT):
        working.transition(s)
    working.event("turn", cost=0.25)
    stopped = create_task(tmp_path, "demo", "Paused one", TEMPLATE)
    stopped.set_paused(True)
    out = ui.task_list([stopped, working, waiting], lambda t: "0/1", 3, plain)
    lines = out.splitlines()
    assert lines[0].split() == [
        "TASK",
        "STATUS",
        "CRITERIA",
        "COST",
        "PLAN",
        "+",
        "IMPL",
        "CREATED",
        "UPDATED",
        "GOAL",
    ]
    assert lines[1].startswith("demo-1") and "review the plan" in lines[1]
    assert (
        lines[2].startswith("demo-2")
        and "implementing (attempt 2/3)" in lines[2]
        and "$0.00 + $0.25" in lines[2]
    )
    assert lines[3].startswith("demo-3") and "stopped" in lines[3]
    assert lines[1].index("0/1") == lines[3].index("0/1"), "columns line up"
    assert "\033[" not in out


def test_detail_shows_next_step_and_events(tmp_path):
    task = create_task(tmp_path, "demo", "Goal", TEMPLATE)
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(s)
    out = ui.task_detail(task, lambda t: "1/1", 3, 20, plain)
    assert "review the work" in out and "vivibox accept demo-1" in out
    assert "current=checkpoint:final" in out


def test_cost_is_split_into_planning_and_implementation(tmp_path):
    """The stronger model plans and the cheaper one writes; the split shows where the money goes.
    Answering your comments on the plan is planning; fixing, final replies and demos are not."""
    task = create_task(tmp_path, "demo", "goal", "")
    task.event("turn", state="plan", cost=0.40, tokens=1)
    task.event("turn", state="checkpoint:plan", cost=0.10, tokens=1)
    task.event("turn", state="implement", cost=0.06, tokens=1)
    task.event("turn", state="checkpoint:final", cost=0.02, tokens=1)
    task.event("turn", cost=0.01, tokens=1, kind="demo")
    assert ui.cost(task) == ui.Spend(0.5, 0.09)
    assert str(ui.cost(task)) == "$0.50 + $0.09"


def test_the_running_turn_counts_in_the_cost_and_the_panel_says_when_it_last_stepped(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    task.event("turn", state="plan", cost=0.40, tokens=1)
    task.transition(State.CHECKPOINT_PLAN)
    task.transition(State.IMPLEMENT)
    task.set_live_turn(0.03, 300, 2)
    assert ui.cost(task) == ui.Spend(0.4, 0.03), "the running turn is implementing"
    shown = ui.task_detail(task, lambda t: 0, 3, 0, lambda text, _style: text)
    assert "last step just now" in shown
    task.clear_live_turn()
    assert ui.cost(task) == ui.Spend(0.4, 0.0) and task.live_turn() is None


def test_a_finished_task_shows_its_split_or_its_total_from_before():
    assert ui.finished_cost({"cost": 0.59, "planning": 0.5}) == "$0.50 + $0.09"
    assert ui.finished_cost({"cost": 0.42}) == "$0.42"


def test_times_are_on_your_clock_not_in_utc(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Etc/GMT-5")  # five hours ahead of UTC, whatever the season
    time.tzset()
    try:
        assert ui.clock("2026-01-01T10:00:00.000+00:00") == "15:00:00"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_blocked_task_says_why_and_never_sends_you_back_to_status(tmp_path):
    task = create_task(tmp_path, "demo", "Migrate the scheduler", TEMPLATE)
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.CHECKPOINT_BLOCKED):
        task.transition(s)
    feedback = task.meta / "handoff" / "verify-feedback.md"
    feedback.write_text("# Verification failed\n")
    shown = ui.task_detail(task, lambda t: "0/1", 3, 5, plain)
    assert f"vivibox status {task.id}" not in shown, "that is the command you just ran"
    assert f'vivibox reply {task.id} "…"' in shown
    assert f"Why   {feedback}" in shown
    question = task.meta / "handoff" / "question.md"
    question.write_text("Which scheduler?\n")
    assert f"Why   {question}" in ui.task_detail(task, lambda t: "0/1", 3, 5, plain)


def test_a_deleted_task_says_what_it_was_doing_in_words():
    assert ui.when_deleted("implement") == "while implementing"
    assert ui.when_deleted("checkpoint:final") == "while it waited for you to review the work"
    assert ui.when_deleted("something older") == ""


def started(tmp_path, *states):
    task = create_task(tmp_path, "demo", "Goal", TEMPLATE)
    for s in states:
        task.transition(s)
    task.event("started", model="m")
    return task


def seen(task, running):
    return ui.view(task, task.read_state(), running, 3)


def test_a_task_that_will_not_move_without_you_waits_for_you(tmp_path):
    draft = create_task(tmp_path, "demo", "Goal", TEMPLATE)
    assert (seen(draft, False).status, seen(draft, False).group) == ("not started", "Waiting for you")
    assert seen(draft, False).commands == (f"vivibox start {draft.id}",)

    dead = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT)
    assert (seen(dead, False).status, seen(dead, False).group) == ("not running", "Waiting for you")
    assert (seen(dead, True).status, seen(dead, True).group) == ("implementing", "Working")

    failed = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT)
    failed.set_paused(True, problem="agent turn failed: 429 Too Many Requests")
    for running in (True, False):  # the supervisor outlives its own error
        v = seen(failed, running)
        assert (v.status, v.group, v.problem) == (
            "agent turn failed",
            "Waiting for you",
            "429 Too Many Requests",
        )

    yours = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT)
    yours.set_paused(True)
    assert (seen(yours, False).status, seen(yours, False).group) == ("stopped", "Stopped")


def test_blocked_says_which_of_its_two_reasons(tmp_path):
    task = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.IMPLEMENT)
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_BLOCKED)
    assert seen(task, True).status == "verification failed 2×"
    (task.meta / "handoff" / "question.md").write_text("Which scheduler?\n")
    assert seen(task, True).status == "agent asks"


def test_decisions_come_first_then_failures_then_the_rest(tmp_path):
    working = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT)
    failed = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT)
    failed.set_paused(True, problem="could not start: REPO_TOKEN not set")
    decision = started(tmp_path, State.CHECKPOINT_PLAN)
    out = ui.task_list([working, failed, decision], lambda t: "0/1", 3, plain, running=lambda t: True)
    rows = [line.split()[0] for line in out.splitlines()[1:]]
    assert rows == [decision.id, failed.id, working.id]
    assert "could not start" in out


def test_blocked_by_the_environment_says_so(tmp_path):
    task = started(tmp_path, State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY)
    task.event("gate", passed=False, environment="Cannot connect to the Docker daemon")
    task.transition(State.CHECKPOINT_BLOCKED, reason="verification could not run")
    assert seen(task, True).status == "verification could not run", (
        "not the agent's failure, and no attempt spent"
    )


def test_the_reviewers_turns_are_a_cost_of_their_own(tmp_path):
    """Another prompt and another model: the review is the third figure, shown only once a task
    has one, and a finished task keeps it apart in its history line."""
    task = create_task(tmp_path, "demo", "goal", "")
    task.event("turn", state="plan", role="planner", cost=0.40, tokens=1)
    task.event("turn", state="implement", role="writer", cost=0.06, tokens=1)
    assert ui.cost(task) == ui.Spend(0.4, 0.06) and ui.cost(task).review == 0.0
    assert str(ui.cost(task)) == "$0.40 + $0.06"
    task.event("turn", state="review", role="reviewer", cost=0.05, tokens=1)
    spent = ui.cost(task)
    assert spent == ui.Spend(0.4, 0.06, 0.05) and spent.total == 0.51
    assert str(spent) == "$0.40 + $0.06 + $0.05"
    task.transition(State.CHECKPOINT_PLAN)
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    task.transition(State.REVIEW)
    task.set_live_turn(0.02, 100, 1)
    assert ui.cost(task).review == 0.07, "the reviewer's running turn counts as review"
    assert ui.finished_cost({"cost": 0.51, "planning": 0.4, "review": 0.05}) == "$0.40 + $0.06 + $0.05"
    assert ui.activity(task.read_state(), 3) == "reviewing"
