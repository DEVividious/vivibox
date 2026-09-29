"""Money spent on API keys and what a subscription's use would have cost stay two sums (ADR-0036):
a total that added them would be neither."""

from vivibox import ui
from vivibox.task import create_task


def test_a_turn_on_a_subscription_is_not_money_spent(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    task.event("turn", state="plan", metered=True, cost=0.40, tokens=1)
    task.event("turn", state="plan", metered=False, cost=0.30, tokens=1)
    task.event("turn", state="implement", metered=False, cost=0.05, tokens=1)
    spent = ui.cost(task)
    assert spent.planning == 0.40 and spent.implementation == 0.0 and spent.total == 0.40
    assert spent.subscription == {"planning": 0.30, "implementation": 0.05}
    assert spent.subscription_total == 0.35
    # Turns from before metered was kept were all spent on keys.
    task.event("turn", state="implement", cost=0.06, tokens=1)
    assert ui.cost(task).implementation == 0.06


def test_the_cli_s_use_is_a_subscription_sum_by_stage(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    task.event("cli_usage", stage="planning", cost=0.41)
    task.event("cli_usage", stage="review", cost=0.10)
    task.event("cli_usage", stage="review", cost=0.05)
    task.event("cli_usage", stage="conversation", cost=0.20)
    spent = ui.cost(task)
    assert spent.total == 0.0 and not spent, "nothing spent on keys"
    assert spent.subscription == {"planning": 0.41, "review": 0.15, "conversation": 0.20}
    assert spent.subscription_total == 0.76 and not spent.subscription_unknown
    # A model with no price: its tokens were used, the sum is not the whole of it.
    task.event("cli_usage", stage="review", cost=None)
    assert ui.cost(task).subscription_unknown


def test_money_spent_reads_as_before_with_the_subscription_beside_it(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    task.event("turn", state="plan", cost=0.40, tokens=1)
    task.event("cli_usage", stage="review", cost=0.10)
    assert str(ui.cost(task)) == "$0.40 + $0.00"
    assert ui.with_subscription(ui.cost(task)) == "$0.40 + $0.00 · $0.10 sub"
    assert ui.cost_cells(ui.cost(task)) == ("$0.40", "$0.00", "$0.10 sub")


def test_a_finished_task_keeps_its_subscription_sum_apart(tmp_path):
    entry = {"cost": 0.51, "planning": 0.4, "review": 0.05, "subscription": {"planning": 0.41}}
    spent = ui.finished_spend(entry)
    assert spent.total == 0.51 and spent.subscription == {"planning": 0.41}
    assert ui.finished_spend({"cost": 0.51, "planning": 0.4}).subscription == {}
    assert ui.history_subscription(spent) == {"subscription": {"planning": 0.41}}
    assert ui.history_subscription(ui.Spend(0.4, 0.1)) == {}


def test_a_removed_task_s_history_line_keeps_the_subscription_sum(env):
    from vivibox import actions
    from vivibox.config import load_project

    task = actions.create("demo", "Add health endpoint")
    task.event("cli_usage", stage="planning", cost=0.41)
    actions.remember_removed(task, load_project("demo"))
    assert actions.history()[0]["subscription"] == {"planning": 0.41}
