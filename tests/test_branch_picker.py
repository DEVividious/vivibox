import asyncio

from textual.widgets import Button, Input, OptionList, TextArea
from ux import screen_text

from vivibox import dialogs, repo
from vivibox.tui import Vivibox


def test_new_task_searches_thousands_of_branches_and_keeps_current_first(env, monkeypatch):
    choices = [("Current (main)", "HEAD")]
    choices += [
        (f"feature/ticket-{i}-checkout", f"refs/heads/feature/ticket-{i}-checkout") for i in range(3000)
    ]
    monkeypatch.setattr(repo, "branches", lambda source: choices)

    async def scenario():
        app = Vivibox()
        async with app.run_test(size=(80, 24)) as pilot:
            result = []
            app.push_screen(dialogs.NewTask("demo"), result.append)
            await pilot.pause()
            assert app.screen.query("#base-ref"), "new tasks need a branch field"
            button = app.screen.query_one("#base-ref", Button)
            assert "Current" in str(button.label)
            assert button.region.bottom < 24
            await pilot.click("#base-ref")
            await pilot.pause()
            assert "Current (main)" in screen_text(app)
            offered = app.screen.query_one(OptionList)
            assert offered.option_count <= 100, "a huge repo must not mount thousands of rows"
            app.screen.query_one(Input).value = "2917 check"
            await pilot.pause()
            assert "feature/ticket-2917-checkout" in screen_text(app)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, dialogs.NewTask)
            app.screen.query_one("#goal", TextArea).text = "Fix checkout"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert result[0]["base_ref"] == "refs/heads/feature/ticket-2917-checkout"

    asyncio.run(scenario())
