"""Under the settings list, what the highlighted row does, in words: read while choosing, not in a
notification that is gone before it is read."""

from test_tui import AVAILABLE, new_task, run
from textual.widgets import Label, OptionList

from vivibox import settings
from vivibox.config import ORCHESTRATION_MODES


def labels(app) -> list[str]:
    options = app.screen.query_one("#rows", OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def pick(app, name: str) -> None:
    index = next(i for i, text in enumerate(labels(app)) if text.strip().startswith(name))
    app.screen.query_one("#rows", OptionList).highlighted = index


def about(app) -> str:
    return str(app.screen.query_one("#about", Label).render())


def test_the_highlighted_setting_says_what_it_does_under_the_list(env):
    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("k")
        await pilot.pause()
        for name, said in (
            ("rounds", "One round is one fix turn of the writer"),
            ("cost_warning", "you are told once"),
            ("cost_limit", "stops before its next turn"),
            ("verification gate timeout", "one verification command"),
            ("planner", "writes the plan you accept"),
            ("ntfy events", "decisions"),
        ):
            pick(app, name)
            await pilot.pause()
            assert said in about(app), (name, about(app))

    run(scenario)


def test_orchestration_describes_the_mode_it_is_on_and_enter_moves_on_without_a_notification(env):
    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("k")
        await pilot.pause()
        pick(app, "orchestration")
        await pilot.pause()
        now = ORCHESTRATION_MODES["planner_maker_checker"]
        assert now.label in about(app) and now.when in about(app) and now.tradeoff in about(app)
        assert "P planner" in about(app), "the legend of the symbols"
        before = len(app._notifications)
        await pilot.press("enter")
        await pilot.pause()
        after = ORCHESTRATION_MODES["supervisor_worker"]
        assert after.label in about(app) and after.when in about(app), "the mode it is on now"
        assert len(app._notifications) == before, "the description is under the list, not in a toast"

    run(scenario, notifications=True)


def test_a_projects_settings_say_what_they_do_too(env):
    new_task()

    async def scenario(app, pilot):
        app.push_screen(settings.ProjectSettings("demo"))
        await pilot.pause()
        pick(app, "java")
        await pilot.pause()
        assert "JDK" in about(app)
        pick(app, "pass_env")
        await pilot.pause()
        assert "your shell" in about(app)

    run(scenario)
