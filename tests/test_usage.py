"""u and vivibox usage: how long each role and the verification took, per task."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from vivibox import resources, usage
from vivibox.cli import main

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    """Docker's figures are measured by a test that asks for them; the rest never reach Docker."""

    def unavailable(command):
        raise resources.Unavailable("not in a test")

    monkeypatch.setattr(resources, "default_run", unavailable)


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
    assert out.splitlines()[0].split() == [
        "TASK",
        "PLAN",
        "WRITE",
        "REVIEW",
        "GATE",
        "TOTAL",
        "CPU",
        "RAM",
        "DISK",
    ]
    assert out.splitlines()[1].startswith("demo-1")
    assert main(["usage", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["tasks"]
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


def test_u_measures_the_pods_only_while_it_is_open(env, monkeypatch):
    from test_tui import new_task, run
    from ux import screen_text

    from vivibox import resources
    from vivibox.usage_view import Usage

    task = new_task()
    sampled = []

    def sample(roots, run=None, problems=None, shared_caches=None):
        sampled.append(sorted(roots))
        return {
            task.id: resources.Resources(
                [resources.Container("agent", 12.5, 2**30, 2**34)], {"docker": 2 * 10**9}, 0
            )
        }

    monkeypatch.setattr(resources, "sample", sample)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert not sampled, "the list never waits for Docker"
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert sampled == [[task.id]]
        text = screen_text(app)
        assert all(column in text for column in ("CPU", "RAM", "DISK")) and "12%" in text and "2.0 GB" in text
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, Usage)
        app.reload()
        await pilot.pause()
        assert len(sampled) == 1

    run(scenario)


def test_vivibox_usage_json_has_each_containers_figures(env, capsys, monkeypatch):
    from vivibox import resources

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    monkeypatch.setattr(
        resources,
        "sample",
        lambda roots, run=None, problems=None, shared_caches=None: {
            "demo-1": resources.Resources(
                [resources.Container("agent", 12.5, 2**30, 2**34)], {"docker": 2 * 10**9}, 4096
            )
        },
    )
    capsys.readouterr()
    assert main(["usage", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["tasks"][0]
    assert row["resources"]["containers"] == [
        {"role": "agent", "cpu": 12.5, "memory": 2**30, "memory_limit": 2**34}
    ]
    assert row["resources"]["volumes"] == {"docker": 2 * 10**9} and row["resources"]["files"] == 4096


def test_a_pod_that_is_down_has_no_cpu_or_memory_only_disk():
    used = usage.Usage("demo-1", "demo", live=True, now=resources.Resources([], {"docker": 2 * 10**9}, 0))
    assert usage.cells(used)[-3:] == ["-", "-", "2.0 GB"]


def test_u_says_why_the_pods_figures_are_dashes_when_docker_does_not_answer(env, monkeypatch):
    """Dashes alone read as pods doing nothing; with Docker down the screen says so, in a line of
    its own above the note, cut to the dialog's width."""
    from test_tui import new_task, run
    from ux import screen_text

    from vivibox import resources
    from vivibox.usage_view import Usage

    new_task()

    def sample(roots, run=None, problems=None, shared_caches=None):
        if problems is not None:
            problems.append("Docker did not answer: Cannot connect to the Docker daemon")
        return {}

    monkeypatch.setattr(resources, "sample", sample)

    async def scenario(app, pilot):
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, Usage)
        text = " ".join(screen_text(app).split())
        assert "CPU, RAM and DISK not measured: Docker did not answer: Cannot connect" in text

    run(scenario)


def shared_cache_docker(command):
    if command[:3] == ["docker", "system", "df"]:
        return json.dumps({"Volumes": [{"Name": "vivibox-cache-yarn", "Size": "100MB"}]})
    return ""


def test_usage_reports_shared_caches_without_tasks_and_with_a_project_filter(env, capsys, monkeypatch):
    monkeypatch.setattr(resources, "default_run", shared_cache_docker)
    assert main(["usage", "--project", "demo"]) == 0
    text = capsys.readouterr().out
    assert "No tasks yet" in text
    assert "Shared caches (all projects): 100 MB" in text and "yarn: 100 MB" in text
    assert main(["usage", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"tasks": [], "shared_caches": {"yarn": 100_000_000}}


def test_u_shows_shared_caches_separately_on_a_small_terminal(env, monkeypatch):
    from test_tui import run
    from ux import screen_text

    monkeypatch.setattr(resources, "default_run", shared_cache_docker)

    async def scenario(app, pilot):
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = " ".join(screen_text(app).split())
        assert "Shared caches (all projects): 100 MB" in text
        assert "yarn: 100 MB" in text and "No tasks yet" in text

    run(scenario, size=(80, 24))


def test_u_keeps_the_whole_cache_breakdown_visible_at_80_columns(env, monkeypatch):
    from test_tui import new_task, run
    from ux import screen_text

    from vivibox.pod import CACHES

    new_task()

    def run_docker(command):
        if command[:3] == ["docker", "system", "df"]:
            return json.dumps(
                {"Volumes": [{"Name": f"vivibox-cache-{name}", "Size": "1GB"} for name in CACHES]}
            )
        return ""

    monkeypatch.setattr(resources, "default_run", run_docker)

    async def scenario(app, pilot):
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = " ".join(screen_text(app).split())
        assert "demo-1" in text and "Shared caches (all projects): 11 GB" in text
        for name in CACHES:
            assert f"{name}: 1.0 GB" in text
        assert "Esc closes" in text

    run(scenario, size=(80, 24))


def test_missing_docker_does_not_report_an_empty_cache(env, capsys):
    assert main(["usage", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["shared_caches"] is None
    assert "Docker did not answer" in captured.err
