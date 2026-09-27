"""Under the settings list, what the highlighted row does, in words: read while choosing, not in a
notification that is gone before it is read."""

from test_tui import AVAILABLE, new_task, run
from textual.widgets import Label, OptionList

from vivibox import settings
from vivibox.config import ORCHESTRATION_MODES


def labels(app) -> list[str]:
    """The rows as they read, without their markup."""
    from rich.text import Text

    options = app.screen.query_one("#rows", OptionList)
    return [
        Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
        for i in range(options.option_count)
    ]


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
            ("cost warning", "you are told once"),
            ("cost limit", "stops before its next turn"),
            ("verification timeout", "one verification command"),
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
        pick(app, "flow")
        await pilot.pause()
        now = ORCHESTRATION_MODES["planner_maker_checker"]
        lines = about(app).splitlines()
        assert lines[0].split()[:5] == now.label.split() and now.flow in lines[0], "the name and the flow"
        assert now.summary == lines[1] and now.tradeoff == lines[-1], "what it is first, what it costs last"
        assert any(line.startswith("Sessions") for line in lines), "facts, a line each"
        before = len(app._notifications)
        await pilot.press("enter")
        await pilot.pause()
        after = ORCHESTRATION_MODES["supervisor_worker"]
        assert after.label in about(app) and after.summary in about(app), "the mode it is on now"
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
        pick(app, "variables")
        await pilot.pause()
        assert "your shell" in about(app)

    run(scenario)


def test_a_change_shows_on_its_row_and_its_description_not_in_a_notification(env):
    """The row says the new value and the line under the list when it applies; a notification
    saying the same again, "rounds = 5", is one more thing to read before it goes."""
    from textual.widgets import Input

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("k")
        await pilot.pause()
        pick(app, "rounds")
        await pilot.pause()
        assert "next start" in about(app), "when a change applies is said before it is made"
        before = len(app._notifications)
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = "5"
        await pilot.press("enter")
        await pilot.pause()
        assert ["rounds", "5"] in [text.split() for text in labels(app)]
        assert len(app._notifications) == before
        pick(app, "desktop notifications")
        await pilot.press("enter")
        await pilot.pause()
        assert len(app._notifications) == before
        pick(app, "cost limit")
        await pilot.pause()
        assert "next turn" in about(app)

    run(scenario, notifications=True)


def test_a_projects_changes_show_on_their_rows_without_a_notification(env):
    from textual.widgets import Input

    new_task()

    async def scenario(app, pilot):
        app.push_screen(settings.ProjectSettings("demo"))
        await pilot.pause()
        pick(app, "java")
        await pilot.pause()
        assert "next start" in about(app)
        before = len(app._notifications)
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = "17"
        await pilot.press("enter")
        await pilot.pause()
        assert ["java", "17"] in [text.split() for text in labels(app)]
        assert len(app._notifications) == before

    run(scenario, notifications=True)
