"""A plan made in a chat or an agent's CLI names the modules the task changes, in a project
verified by its modules (ADR-0033, change of 2026-09-29): the prompt asks for a "Modules:" line,
the import puts it in the plan's header, and refuses a plan without it."""

import pytest

from vivibox import actions, manual, panel, ui
from vivibox.plan import PlanError, parse_plan
from vivibox.states import State

SCOPED = "mvn -B -pl {modules} -am verify"
ANSWER = (
    "Summary: Add subtract to the calculator\n"
    "{modules}"
    "\n# Goal\n\nSubtract.\n\n## Acceptance criteria\n\n"
    "- [ ] Every test added was seen failing on its own assertion\n"
    "- [ ] subtract(5, 3) returns 2\n"
)


def made(env, scoped: bool):
    if scoped:
        demo = env / "config" / "projects" / "demo.toml"
        demo.write_text(demo.read_text().replace('verify = ["true"]', f'verify = ["{SCOPED}"]'))
    task = actions.create("demo", "Add subtract", plan_in_cli=True)
    task.transition(State.CHECKPOINT_PLAN)
    task.set_awaiting_plan(True)
    return task


def test_the_prompt_asks_for_the_modules_only_in_a_project_verified_by_them(env):
    scoped = made(env, True)
    web, cli = manual.prompts(scoped, env / "repo", [SCOPED])
    for prompt in (web, cli):
        assert '"Modules: ' in prompt and "the directories this task changes" in prompt


def test_a_plain_project_is_not_asked_for_modules(env):
    task = made(env, False)
    web, cli = manual.prompts(task, env / "repo", ["true"])
    assert "Modules:" not in web and "Modules:" not in cli


@pytest.mark.parametrize(
    "line", ["Modules: core, app\n", "**Modules:** `core`, `app`\n", "Modules: core app\n"]
)
def test_the_modules_line_goes_into_the_plans_header(env, line):
    task = made(env, True)
    (task.meta / manual.ANSWER).write_text(ANSWER.format(modules=line))
    manual.import_answer(task)
    assert parse_plan(task.plan_path.read_text()).modules == ["core", "app"]


def test_a_plan_without_its_modules_is_refused_with_what_to_add(env):
    task = made(env, True)
    (task.meta / manual.ANSWER).write_text(ANSWER.format(modules=""))
    with pytest.raises(PlanError, match='"Modules: '):
        manual.import_answer(task)
    assert task.read_state().awaiting_plan, "still waiting for a plan it can take"


def test_the_modules_are_shown_where_the_plan_is_decided(env):
    task = made(env, True)
    (task.meta / manual.ANSWER).write_text(ANSWER.format(modules="Modules: core, app\n"))
    manual.import_answer(task)
    st = task.read_state()
    assert "Verified by modules: core, app" in panel.detail(task, st, 3, running=False)
    assert "Verified by modules: core, app" in ui.task_detail(
        task, lambda t: "0/2", 3, 0, ui.Style(False), False
    )
