"""How the tests drive the view (conftest: at_rest): it stands in for something of Textual's
test harness that made a test pass or fail by how busy the machine was. The case here plants the
moment that used to come only under load, so it fails every time without the stand-in."""

import asyncio

from test_tui import run
from textual.widgets import TextArea

from vivibox import newtask


def test_a_key_and_a_pause_wait_for_the_view_however_little_cpu_the_process_got(env, monkeypatch):
    """Textual's own wait takes the app for idle when the process used next to no CPU while it
    slept 20 ms. Planted here: no CPU used as far as that wait can tell, and its sleep over at
    once, which is what a process the machine has no time for sees. The dialog must still be
    whole after the key that opens it, and every letter in its field."""
    import textual._wait

    monkeypatch.setattr(textual._wait, "process_time", lambda: 0.0)
    monkeypatch.setattr(textual._wait, "sleep", lambda seconds: asyncio.sleep(0))

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, newtask.NewTask), "the key reached the view"
        assert app.screen.focused is not None and app.screen.focused.id == "goal", "and the dialog is whole"
        await pilot.press(*"Fix login")
        assert app.screen.query_one("#goal", TextArea).text == "Fix login"

    run(scenario)
