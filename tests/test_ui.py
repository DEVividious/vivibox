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


def test_a_finished_task_shows_its_split_or_its_total_from_before():
    assert ui.finished_cost({"cost": 0.59, "planning": 0.5}) == "$0.50 + $0.09"
    assert ui.finished_cost({"cost": 0.42}) == "$0.42"
