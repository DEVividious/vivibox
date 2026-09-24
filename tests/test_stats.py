import json

from vivibox import stats
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.task import find_task


def events_of(task_id, *entries):
    """Events as events.jsonl holds them, in the order given."""
    return [{"ts": f"2026-09-2{i}T10:00:00+00:00", "task": task_id, "type": kind, "data": data}
            for i, (kind, data) in enumerate(entries)]  # fmt: skip


def test_the_numbers_match_a_count_by_hand():
    """Two tasks: one passed at its second run after a failed command and a criterion not met,
    the other never passed and had a failed and a retried turn."""
    first = events_of(
        "a-1",
        ("created", {}),
        ("turn", {"state": "plan", "role": "planner", "ok": True, "cost": 0.4, "tokens": 4000}),
        ("turn", {"state": "implement", "role": "writer", "ok": True, "cost": 0.1, "tokens": 2000}),
        ("gate", {"passed": False, "failed_commands": ["npm test"], "missing_criteria": 1}),
        ("turn", {"state": "implement", "role": "writer", "ok": True, "cost": 0.3, "tokens": 6000}),
        ("gate", {"passed": True, "failed_commands": []}),
    )
    second = events_of(
        "a-2",
        ("created", {}),
        ("turn_retry", {"wait": 30, "error": "429"}),
        ("turn", {"state": "plan", "role": "planner", "ok": False, "cost": 0.0, "tokens": 0, "error": "x"}),
        ("gate", {"passed": False, "failed_commands": ["npm test"], "environment": "docker down"}),
        ("turn", {"cost": 0.05, "tokens": 500, "kind": "demo"}),
    )
    third = events_of(
        "a-3",
        ("created", {}),
        ("turn", {"state": "plan", "role": "planner", "ok": True, "cost": 0.2, "tokens": 1000}),
    )

    found = stats.collect([(first, False), (second, True), (third, True)])
    assert (found.tasks, found.live) == (3, 2)
    assert found.iterations == [2] and found.never_passed == 1, "a task with no gate run counts in neither"
    assert (found.gates, found.gates_passed) == (3, 1)
    assert found.reasons == {"a command failed": 2, "criteria not met": 1, "outside the code": 1}
    assert (found.turns, found.failed_turns, found.retries) == (6, 1, 1)
    assert found.cost == 1.05
    as_dict = stats.as_dict(found)
    assert as_dict["iterations_to_pass"] == {
        "median": 2,
        "max": 2,
        "tasks_passed": 1,
        "tasks_never_passed": 1,
    }
    assert as_dict["per_turn"]["by_role"] == [
        {"role": "demo", "turns": 1, "cost": 0.05, "tokens": 500},
        {"role": "planner", "turns": 3, "cost": 0.2, "tokens": 1666},
        {"role": "writer", "turns": 2, "cost": 0.2, "tokens": 4000},
    ]
    text = stats.report(found)
    assert text.startswith(
        "Tasks: 3 (2 live, 1 finished)\n"
        "Verification runs to pass: median 2, max 2 (1 tasks passed, 1 never did)\n"
    )
    assert "Turns: 6, 1 failed (16%), 1 retried, $1.05\n" in text
    assert "  a command failed  2\n" in text and "Verification: 3 runs, 1 passed" in text
    assert stats.report(stats.Stats()) == "No events yet: nothing to count.\n"
    assert stats.collect([(first, False)], since="2026-09-24").turns == 1, "since leaves the earlier out"


def test_stats_reads_live_tasks_and_the_archive(env, capsys):
    from vivibox import actions
    from vivibox.config import load_project

    assert main(["new", "demo", "One", "--draft"]) == 0
    one = find_task(load_config().tasks_dir, "demo-1")
    one.event("turn", state="plan", role="planner", ok=True, cost=0.5, tokens=100)
    assert main(["new", "demo", "Two", "--draft"]) == 0
    two = find_task(load_config().tasks_dir, "demo-2")
    two.event("turn", state="plan", role="planner", ok=True, cost=0.25, tokens=100)
    actions.remove(two, load_project("demo"))  # deleted: its events live on in the archive
    capsys.readouterr()
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Tasks: 2 (1 live, 1 finished)\n") and "$0.75" in out
    assert main(["stats", "--json", "--project", "other"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == 0
    assert main(["stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["turns"] == {
        "count": 2,
        "failed": 0,
        "retried": 0,
        "cost": 0.75,
    }
