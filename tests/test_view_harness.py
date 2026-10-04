"""How the tests drive the view (conftest: at_rest, threads_done, the close as on quit): each
stands in for something of Textual's test harness that made a test pass or fail by how busy the
machine was. Each case here plants the moment that used to come only under load, so it fails
every time without its stand-in."""

import asyncio
import threading

from test_tui import new_task, run
from textual.app import App
from textual.widgets import TextArea

from vivibox import newtask, tui
from vivibox.tui import Vivibox


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


def test_no_refresh_runs_while_a_tests_view_is_taken_down(env, monkeypatch):
    """The view's refresh falls due while run_test removes its screens: planted here, right
    after they are gone. A view that exited is not called back by its timers; the refresh would
    refill a list whose screen is no longer there."""
    new_task()
    due = []
    close_all = App._close_all

    async def close_all_then_the_refresh_falls_due(app):
        await close_all(app)
        for timer in list(app._timers):
            if timer._callback == app.reload:
                due.append(timer)
                await timer._tick(next_timer=0.0, count=1)

    monkeypatch.setattr(App, "_close_all", close_all_then_the_refresh_falls_due)

    async def scenario(app, pilot):
        app.drawn = ()  # something changed since the list was drawn, as while an agent works

    run(scenario)
    assert len(due) == 1, "the refresh timer was there to fall due"


def test_waiting_for_the_views_threads_takes_a_cancelled_worker_as_ended_and_waits_for_its_thread(
    env, monkeypatch
):
    """A refresh that arrives while the view looks at the pods replaces that look: the earlier
    worker is cancelled, its thread still on its way. Planted here: the first look held until
    the second is asked for."""
    task = new_task()
    held, release = threading.Event(), threading.Event()
    looked = []

    def slow(ids):
        held.set()
        release.wait(10)
        looked.append(ids)
        return {}

    async def scenario(app, pilot):
        monkeypatch.setattr(tui, "pod_views", slow)
        first = app.look_at_pods([task.id])
        assert await asyncio.to_thread(held.wait, 10)
        app.look_at_pods([task.id])
        assert first.is_cancelled
        release.set()
        await app.workers.wait_for_complete()
        assert len(looked) == 2 and app.executor.unfinished() == 0, "both threads are back"

    run(scenario)


def test_a_worker_that_failed_still_fails_the_wait(env, monkeypatch):
    import pytest
    from textual.worker import WorkerFailed

    async def go():
        app = Vivibox()
        async with app.run_test():
            worker = app.run_worker(lambda: 1 / 0, thread=True, exit_on_error=False)
            with pytest.raises(WorkerFailed):
                await app.workers.wait_for_complete()
            assert isinstance(worker.error, ZeroDivisionError)

    asyncio.run(go())
