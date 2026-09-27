"""u and vivibox usage: how long each role and the verification took, per task."""

import json
from datetime import UTC, datetime, timedelta

from vivibox import usage
from vivibox.cli import main

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def event(when, type_, **data):
    return {"ts": at(when), "task": "demo-1", "type": type_, "data": data}


EVENTS = [
    event(0, "created", project="demo", goal="Goal"),
    event(10, "turn_started", state="plan", role="planner"),
    event(70, "turn", state="plan", role="planner", cost=0.01),
    event(100, "state", previous="checkpoint:plan", current="implement"),
    event(100, "turn_started", state="implement", role="writer"),
    event(400, "turn", state="implement", role="writer", cost=0.02),
    event(400, "state", previous="implement", current="verify"),
    event(520, "gate", passed=False, seconds=118.5),
    event(520, "turn_started", state="implement", role="writer"),
    event(580, "turn", state="implement", role="writer", cost=0.01),
    event(580, "state", previous="implement", current="verify"),
    event(640, "gate", passed=True),  # before gates said how long: from entering verify
    event(640, "turn_started", state="review", role="reviewer"),
    event(700, "turn", state="review", role="reviewer", cost=0.01),
    event(900, "state", previous="checkpoint:final", current="done"),
]


def test_a_task_adds_up_its_turns_per_role_its_verifications_and_its_whole_time():
    used = usage.of_events("demo-1", "demo", EVENTS, live=False, now=T0 + timedelta(hours=5))
    assert (used.plan, used.write, used.review) == (60, 360, 60)
    assert used.gate == 118.5 + 60
    assert used.total == 900, "until done, not until now"


def test_a_turn_under_way_counts_until_now_on_a_live_task():
    events = [*EVENTS[:5]]  # the writer's first turn started at 100 s and has not ended
    used = usage.of_events("demo-1", "demo", events, live=True, now=T0 + timedelta(seconds=250))
    assert used.write == 150 and used.total == 250


def test_turns_from_before_they_named_their_role_count_by_state():
    events = [event(0, "created"), event(5, "turn_started", state="plan"), event(35, "turn", state="plan")]
    assert usage.of_events("demo-1", "demo", events, live=False, now=T0).plan == 30


def test_durations_read_as_a_person_says_them():
    assert [usage.duration(s) for s in (0, 45, 60, 754, 3600, 3725, 90000)] == [
        "-", "45 s", "1 min", "12 min", "1 h 00", "1 h 02", "25 h 00",
    ]  # fmt: skip


def test_vivibox_usage_prints_a_row_per_task_and_json_for_a_note(env, capsys):
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    task.event("turn_started", state="plan", role="planner")
    task.event("turn", state="plan", role="planner", cost=0.01)
    capsys.readouterr()
    assert main(["usage"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["TASK", "PLAN", "WRITE", "REVIEW", "GATE", "TOTAL"]
    assert out.splitlines()[1].startswith("demo-1")
    assert main(["usage", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["task"] == "demo-1" and rows[0]["live"] is True
    assert set(rows[0]) >= {"plan", "write", "review", "gate", "total"}


def test_u_shows_the_times_per_task_and_esc_closes_it(env):
    from test_tui import new_task, run
    from ux import screen_text

    from vivibox.usage_view import Usage

    task = new_task()
    task.event("turn_started", state="plan", role="planner")
    task.event("turn", state="plan", role="planner", cost=0.01)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, Usage)
        text = screen_text(app)
        assert all(column in text for column in ("TASK", "PLAN", "WRITE", "REVIEW", "GATE", "TOTAL"))
        assert "demo-1" in text
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, Usage)

    run(scenario)


def test_help_names_u():
    from vivibox import dialogs

    assert "\n  u     " in dialogs.HELP
