"""The planner asked instead of planning (the goal is met already, say): the task waits for your
answer, not for a plan that is still the template."""

from test_tui import new_task

from vivibox import actions, gate, panel, ui
from vivibox.config import load_project
from vivibox.states import State


def asked(env):
    task = new_task("Add a unit test for parse")
    (task.meta / "handoff" / "question.md").write_text("The requested unit test already exists.\n")
    task.transition(State.CHECKPOINT_PLAN, reason="question from the agent: the test exists")
    return task


def test_a_planners_question_shows_as_the_agent_asking_with_the_question_and_no_accept(env):
    task = asked(env)
    st = task.read_state()
    view = ui.view(task, st, True, 3)
    assert view.status == "agent asks"
    assert view.commands == (f'vivibox reply {task.id} "…"',), "nothing to accept: there is no plan"
    shown = panel.detail(task, st, 3, running=True)
    assert "The requested unit test already exists." in shown
    assert gate.PLACEHOLDER not in shown, "the template is not a plan to review"
    assert "`r`" in shown and "`a`" not in shown
    keys = panel.keys_for(task, st, running=True, busy=False, demo_running=False)
    assert keys["reply"] and keys["edit_plan"] and not keys["accept"]


def test_a_plan_you_write_yourself_is_reviewed_and_accepted_and_the_question_put_away(env):
    task = asked(env)
    task.plan_path.write_text(
        task.plan_path.read_text().replace(gate.PLACEHOLDER, "parse rejects an empty line")
    )
    st = task.read_state()
    assert ui.view(task, st, True, 3).status == "review the plan"
    assert panel.keys_for(task, st, running=True, busy=False, demo_running=False)["accept"]
    actions.accept_plan(task, load_project("demo"))
    assert not (task.meta / "handoff" / "question.md").exists(), "the writer's first turn would stop on it"
