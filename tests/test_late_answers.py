"""What a thread of the view brings back after what asked for it is gone: a dialog closed before
git or opencode answered, the view left while a step was under way. Nothing is left to show the
answer on, so it is dropped; it once looked a field up in a dialog that had none any more, and the
view ended with a traceback (seen when the machine was busy: the answer came as the dialog closed).
"""

import asyncio

from test_tui import run
from textual.widgets import Button, Label, OptionList

from vivibox import newtask, providers_ui
from vivibox.branches import BranchPicker
from vivibox.config import load_project
from vivibox.tui import Vivibox


async def closed(pilot, dialog) -> None:
    """Esc, and the dialog taken down whole: off the view's screens, its fields removed."""
    await pilot.press("escape")
    for _ in range(100):
        if not dialog.is_attached:
            return
        await pilot.pause()
    raise AssertionError("the dialog did not close")


def test_the_checkouts_branch_arriving_after_the_new_task_dialog_closed_is_dropped(env):
    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, newtask.NewTask)
        await closed(pilot, dialog)
        dialog.current_branch_loaded("demo", "main")  # as its thread does, through call_from_thread

    run(scenario)


def test_the_answer_still_names_the_branch_while_the_dialog_is_open(env):
    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        dialog.current_branch_loaded("demo", "topic")
        assert "Current (topic)" in str(dialog.query_one("#base-ref", Button).label)

    run(scenario)


def test_the_branches_arriving_after_the_picker_closed_are_dropped(env):
    async def scenario(app, pilot):
        dialog = BranchPicker(load_project("demo").repo)
        app.push_screen(dialog)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert dialog.ready and dialog.query_one(OptionList).option_count == 1, "listed while it is open"
        await closed(pilot, dialog)
        dialog.loaded([("Current (main)", "HEAD"), ("topic", "refs/heads/topic")])
        dialog.failed("repository unavailable")
        assert len(dialog.choices) == 1, "what it listed is what it closed with"

    run(scenario)


def test_the_providers_arriving_after_the_dialog_closed_are_kept_for_the_next_one(env):
    """The list of providers opencode knows is the view's, read once: an answer that comes after
    the dialog closed still saves the next dialog the wait."""

    async def scenario(app, pilot):
        dialog = providers_ui.AddProvider([])
        app.push_screen(dialog)
        await pilot.pause()
        assert dialog.query_one("#problem", Label) is not None
        await closed(pilot, dialog)
        dialog.loaded([("deepseek", "DeepSeek")])
        assert app.catalog == [("deepseek", "DeepSeek")]

    run(scenario)


def test_a_thread_that_reports_once_the_view_has_closed_is_not_run(env):
    """A step that outlives the view (a start that took longer than the window was open) calls
    back into a view whose screens are gone, while its loop still runs: the call is dropped, as
    it is once the loop is closed."""
    late = []

    async def go():
        app = Vivibox()
        async with app.run_test() as pilot:
            await pilot.pause()
            await asyncio.to_thread(app.call_from_thread, late.append, "while it runs")
        await asyncio.to_thread(app.call_from_thread, late.append, "after it closed")

    asyncio.run(go())
    assert late == ["while it runs"]


def test_a_task_deleted_while_the_list_is_refreshed_is_gone_at_the_next_refresh(env, monkeypatch):
    """The refresh lists the tasks, then reads each one's files; a task whose files go in between
    (accepted in a shell or from a notification, removed by a step of the view still under way)
    raised FileNotFoundError out of the two-second timer, and the view ended with a traceback.
    Planted here: the task is deleted after the refresh listed it."""
    import shutil

    from test_tui import new_task, rows

    from vivibox import tui

    task = new_task("Accepted elsewhere")
    projects = tui.projects

    def deleted_meanwhile():
        shutil.rmtree(task.root, ignore_errors=True)
        return projects()

    async def scenario(app, pilot):
        assert rows(app) == ["demo", task.id]
        task.event("turn", state="plan", cost=0.1, tokens=1)  # something to draw again
        with monkeypatch.context() as during:
            during.setattr(tui, "projects", deleted_meanwhile)
            app.reload()
        await pilot.pause()
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo"], "listed as it is now"

    run(scenario)


def test_a_step_that_finishes_while_the_unfinished_are_counted_does_not_raise():
    """The executor's threads take themselves off its set of steps at work as they finish, while
    the view counts the unfinished ones on its own thread: counting over the set itself raised
    "Set changed size during iteration" (seen under load). Planted here: a step that finishes,
    and takes another with it, at the moment it is asked whether it is done."""
    from vivibox.app_support import LeavingExecutor

    executor = LeavingExecutor()

    class Step:
        def __init__(self, finishes=()):
            self.finishes = finishes

        def done(self):
            for step in self.finishes:
                executor.at_work.discard(step)
            return False

    others = [Step() for _ in range(8)]
    executor.at_work.update(others)
    executor.at_work.add(Step(finishes=others))
    assert executor.unfinished() >= 1
    executor.shutdown()
