"""What an agent's CLI session used on a task is shown apart from money spent on keys, in its own
colour and with a `sub` mark that reads without colour (ADR-0036)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from ux import screen_text

from vivibox import actions, dialogs, look, stats, ui, usage
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.panel import detail, finished_detail
from vivibox.table import TaskTable
from vivibox.task import find_task, now
from vivibox.tui import Vivibox


def new_task(goal="Goal"):
    assert main(["new", "demo", goal, "--draft"]) == 0
    tasks = load_config().tasks_dir
    newest = max(int(p.name.removeprefix("demo-")) for p in tasks.iterdir() if p.name.startswith("demo-"))
    return find_task(tasks, f"demo-{newest}")


def run(scenario, size=(140, 40)):
    async def go():
        app = Vivibox()
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(go())


def planned_in_cli(task):
    task.event("cli_usage", stage="planning", model="claude-opus-5-5", tokens={"output": 1000}, cost=0.41)
    task.event("turn", state="implement", role="writer", cost=0.06, tokens=1)
    return task


def test_a_sum_used_on_a_subscription_says_so_without_colour():
    assert ui.sub_money(0.41) == "$0.41 sub"
    # A model with no list price used too: the figure is short of the whole.
    assert ui.sub_money(0.41, unknown=True) == "≥$0.41 sub"
    assert ui.sub_money(0.0, unknown=True) == "? sub"


def test_plan_and_review_done_in_the_cli_show_its_figure_in_their_cells(tmp_path):
    from vivibox.task import create_task

    task = planned_in_cli(create_task(tmp_path, "demo", "goal", ""))
    assert ui.cost_cells(ui.cost(task)) == ("$0.41 sub", "$0.06", "-")
    task.event("cli_usage", stage="review", model="claude-opus-5-5", tokens={}, cost=0.15)
    task.event("cli_usage", stage="conversation", model="claude-opus-5-5", tokens={}, cost=0.20)
    assert ui.cost_cells(ui.cost(task)) == ("$0.41 sub", "$0.06", "$0.15 sub")
    task.event("cli_usage", stage="review", cost=None, problem="transcript not read")
    assert ui.cost_cells(ui.cost(task))[2] == "≥$0.15 sub"
    # The list colours those cells apart; the figures from keys keep theirs.
    plan, impl, review = TaskTable.cost_cells(ui.cost(task))
    assert look.SUBSCRIPTION in plan and look.SUBSCRIPTION in review and look.SUBSCRIPTION not in impl


def test_a_task_with_nothing_on_keys_shows_its_subscription_total_as_its_cost(tmp_path):
    from vivibox.task import create_task

    task = create_task(tmp_path, "demo", "goal", "")
    task.event("cli_usage", stage="planning", cost=0.41)
    spent = ui.cost(task)
    assert ui.cost_cells(spent) == ("$0.41 sub", "-", "-")
    assert look.SUBSCRIPTION in TaskTable.total_cell(spent)
    assert TaskTable.total_cell(ui.Spend()) == TaskTable.total_cell(ui.Spend(0.0, 0.0))


def test_the_subscription_has_a_colour_of_its_own():
    others = {look.ACCENT, look.WORKING, look.WAITING, look.SUCCESS, look.ERROR, look.SECONDARY, look.MUTED}
    assert look.SUBSCRIPTION not in others and look.THEME.variables["text-subscription"] == look.SUBSCRIPTION


def test_the_details_have_a_cost_section_with_both_sums_and_the_split(env):
    task = new_task()
    assert "#### Cost" not in detail(task, task.read_state(), 3, running=False), "keys alone: Roles says it"
    planned_in_cli(task)
    task.event("cli_usage", stage="review", model="claude-opus-5-5", tokens={"output": 500}, cost=0.15)
    task.event(
        "cli_usage", stage="conversation", model="claude-opus-5-5", tokens={"input": 200_000}, cost=0.20
    )
    shown = detail(task, task.read_state(), 3, running=False)
    section = shown[shown.index("#### Cost") :]
    assert "- **API keys** · $0.06" in section
    assert "- **Subscription** · $0.76 sub · 201.5k tokens" in section
    assert "planning $0.41, review $0.15, conversation $0.20" in section
    assert "$0.06 so far · $0.76 sub" in shown, "the header line has both"


def test_status_and_a_finished_task_name_both_sums(env):
    task = planned_in_cli(new_task())
    shown = ui.task_detail(task, lambda t: "0/0", 2, 0, ui.Style(False), running=False)
    assert "$0.00 + $0.06 · $0.41 sub" in shown
    entry = {"id": "demo-9", "project": "demo", "title": "T", "cost": 0.06, "planning": 0.0,
             "subscription": {"planning": 0.41}, "commit": "a", "branch": "", "conflicts": [],
             "finished": now(), "created": now()}  # fmt: skip
    assert "$0.00 + $0.06 · $0.41 sub" in finished_detail(entry)


def test_the_header_says_today_s_subscription_use_apart(env):
    task = planned_in_cli(new_task())
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    today = now()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="milliseconds")
    entries = [
        {"id": "demo-8", "project": "demo", "title": "Today", "cost": 0.12, "subscription": {"review": 0.10},
         "commit": "a", "branch": "", "conflicts": [], "finished": today, "created": today},
        {"id": "demo-7", "project": "demo", "title": "Old", "cost": 0.5, "subscription": {"planning": 9.0},
         "commit": "b", "branch": "", "conflicts": [], "finished": yesterday, "created": yesterday},
    ]  # fmt: skip
    actions.history_path().write_text("".join(json.dumps(e) + "\n" for e in entries))
    assert task

    async def scenario(app, pilot):
        app.reload()
        assert app.sub_title.endswith("$0.18 today · $0.51 sub today"), app.sub_title

    run(scenario)


def test_the_list_shows_the_cli_s_figure_at_full_width(env):
    planned_in_cli(new_task())

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert "$0.41 sub" in screen_text(app)

    run(scenario, size=(180, 30))


def test_u_and_usage_have_both_sums(env):
    task = planned_in_cli(new_task())
    [row] = usage.gather(finished=False)
    assert (row.cost, row.subscription) == (0.06, 0.41)
    assert "COST" in usage.COLUMNS and "SUB" in usage.COLUMNS
    cells = dict(zip(usage.COLUMNS, usage.cells(row), strict=True))
    assert (cells["COST"], cells["SUB"]) == ("$0.06", "$0.41 sub")
    assert usage.as_dicts([row])[0]["subscription"] == 0.41
    task.event("cli_usage", stage="review", cost=None)
    [row] = usage.gather(finished=False)
    assert dict(zip(usage.COLUMNS, usage.cells(row), strict=True))["SUB"] == "≥$0.41 sub"


def test_stats_count_the_subscription_apart(env):
    task = planned_in_cli(new_task())
    found = stats.collect([(task.events(), True)])
    assert found.cost == 0.06 and found.subscription == 0.41
    assert "Subscription (agent CLI, at list prices): $0.41 sub" in stats.report(found)
    assert stats.as_dict(found)["subscription"] == {"cost": 0.41, "unpriced": 0}


def test_the_help_says_what_sub_means():
    shown = " ".join(str(part) for heading, rows in dialogs.help_sections() for part in (heading, *rows))
    assert "sub" in shown and "subscription" in shown
