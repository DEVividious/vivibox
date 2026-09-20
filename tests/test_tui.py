import asyncio
import subprocess

from textual.widgets import Input, Label

from vivibox import gate
from vivibox.cli import main
from vivibox.config import load_config, load_project
from vivibox.states import State
from vivibox.task import find_task
from vivibox.tui import CommitWork, NewProject, Vivibox, projects


def new_task(goal="Goal"):
    assert main(["new", "demo", goal, "--draft"]) == 0
    tasks = load_config().tasks_dir
    return find_task(tasks, sorted(p.name for p in tasks.iterdir() if p.name.startswith("demo-"))[-1])


def at_plan_checkpoint(task):
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))


def run(scenario):
    async def go():
        app = Vivibox()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(go())


def test_lists_tasks_waiting_for_you_first(env):
    new_task("Still planning")
    waiting = new_task("Waiting")
    at_plan_checkpoint(waiting)

    async def scenario(app, pilot):
        app.reload()
        assert app.selected_id() == waiting.id, "the task waiting for you is on top"
        assert app.sub_title == "1 waiting for you"
        assert app.check_action("accept", ()) and app.check_action("reply", ())
        assert not app.check_action("open_ide", ()), "keys that do nothing here stay hidden"
        await pilot.press("down")
        assert app.selected_id() == "demo-1" and not app.check_action("accept", ())

    run(scenario)


def test_accept_the_plan_with_a(env):
    task = new_task()
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        assert task.read_state().state is State.IMPLEMENT

    run(scenario)


def test_reply_sends_your_comment(env):
    task = new_task()
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("r")
        await pilot.press(*"Use Postgres")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert task.read_state().state is State.PLAN
        assert "Use Postgres" in (task.meta / "handoff" / "comments.md").read_text()

    run(scenario)


def test_accepting_the_work_offers_a_commit(env):
    source = load_project("demo").repo
    task = new_task()
    (task.repo / "one.txt").write_text("x\n")
    for args in (
        ["add", "one.txt"],
        ["-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one"],
    ):
        subprocess.run(["git", *args], cwd=task.repo, check=True, capture_output=True)
    at_plan_checkpoint(task)
    gate.accept_plan(task, ["true"])
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await pilot.press("right", "left", "enter")  # arrows move between the buttons
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, CommitWork)
        # The repository is on its main branch, so leaving it uncommitted is the focused choice.
        assert app.screen.focused.id == "later"
        await pilot.press("right")
        assert app.screen.focused.id == "commit"
        await pilot.press("enter")
        await pilot.pause()

    run(scenario)
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=source, capture_output=True, text=True)
    assert log.stdout.strip() == "Add one" and (source / "one.txt").exists()
    assert not task.root.exists()


def test_a_task_can_be_a_whole_ticket(env):
    from vivibox import actions

    ticket = "PAY-123: Reject expired cards\n\nCustomers report that...\n- keep the API\n- add tests\n"
    task = actions.create("demo", ticket)
    assert task.read_state().goal == "PAY-123: Reject expired cards"
    plan = task.plan_path.read_text()
    assert "Customers report that..." in plan and "- add tests" in plan


def test_new_task_dialog_takes_a_long_description(env, monkeypatch):
    started = []
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False: started.append(task_id) or "m")

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix login", "enter", *"More context")
        await pilot.press("down")
        assert app.screen.focused.id == "auto", "down on the last line moves on"
        await pilot.press("up")
        assert app.screen.focused.id == "goal"
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    task = find_task(load_config().tasks_dir, "demo-1")
    assert task.read_state().goal == "Fix login" and "More context" in task.plan_path.read_text()
    assert started == ["demo-1"]


def test_at_suggests_paths(env, tmp_path, monkeypatch):
    (tmp_path / "tickets").mkdir()
    (tmp_path / "tickets" / "PAY-1.md").write_text("details")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False: "m")

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix it, see @ti")
        suggestions = app.screen.query_one("#suggestions")
        assert suggestions.display and suggestions.get_option_at_index(0).prompt == "tickets/"
        await pilot.press("tab")  # into the folder: its files are suggested next
        assert suggestions.get_option_at_index(0).prompt == "tickets/PAY-1.md"
        await pilot.press("enter")
        assert not suggestions.display
        assert app.screen.query_one("#goal").text == "Fix it, see @tickets/PAY-1.md "
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    task = find_task(load_config().tasks_dir, "demo-1")
    assert "/task/context/PAY-1.md" in task.plan_path.read_text()


def test_a_spinner_shows_tasks_the_agent_is_working_on(env, monkeypatch):
    from vivibox.tui import SPINNER

    new_task("Busy")
    monkeypatch.setattr("vivibox.actions.supervisor_running", lambda task: True)

    async def scenario(app, pilot):
        app.reload()
        table = app.query_one("DataTable")
        first = str(table.get_cell("demo-1", app.status_column))
        app.spin()
        second = str(table.get_cell("demo-1", app.status_column))
        assert first != second and any(c in second for c in SPINNER) and "planning" in second
        assert "1 working" in app.sub_title

    run(scenario)


def test_details_open_on_request(env):
    task = new_task("Waiting")
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.reload()
        panel = app.query_one("#detail")
        assert panel.has_class("hidden"), "the list fills the screen until you ask for details"
        await pilot.press("enter")  # or d
        assert not panel.has_class("hidden") and "it works" in app.shown
        await pilot.press("d")
        assert panel.has_class("hidden")

    run(scenario)


def test_finished_tasks_are_listed_below_and_can_be_hidden(env):
    from vivibox import actions

    task = new_task("Waiting")
    at_plan_checkpoint(task)
    actions.remember(
        actions.Finished("demo-9", env, 0.42, "Reject expired cards"), load_project("demo"), "abc1234567"
    )

    async def scenario(app, pilot):
        app.reload()
        table = app.query_one("DataTable")
        assert [str(table.get_cell_at((r, 0))) for r in range(table.row_count)] == ["demo-1", "demo-9"]
        await pilot.press("down", "enter")
        assert "Reject expired cards" in app.shown and "$0.42" in app.shown
        assert app.check_action("remove", ()) and not app.check_action("accept", ())
        await pilot.press("x")  # forget it
        assert table.row_count == 1 and actions.history() == []
        await pilot.press("h")
        assert table.row_count == 1, "hiding finished tasks leaves the live ones"

    run(scenario)


def test_a_project_can_be_set_up_from_the_view(env, tmp_path, monkeypatch):
    from vivibox.config import load_project

    fresh = tmp_path / "clicker"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False: "m")

    async def scenario(app, pilot):
        await pilot.press("P")  # a project of its own, from anywhere in the view
        await pilot.pause()
        assert str(app.screen.query_one("#path", Input).value) == str(tmp_path)
        app.screen.query_one("#path", Input).value = str(fresh)
        await pilot.pause()
        assert app.screen.query_one("#name", Input).value == "clicker"
        await pilot.click("#create")
        await pilot.pause()
        assert app.screen.query_one("#goal"), "its first task follows right away"
        kind = app.screen.query_one("#kind")
        assert not kind.display, "an empty project has nothing that could work wrong"

    run(scenario)
    project = load_project("clicker")
    assert project.repo == fresh and project.verify == [], "the first plan will set how to test it"
    assert (fresh / ".git").is_dir()


def test_a_folder_that_is_already_a_project_leads_to_a_task(env, tmp_path, monkeypatch):
    from vivibox.config import load_project

    source = load_project("demo").repo
    (source / "app.py").write_text("print('hi')\n")
    for args in (["add", "app.py"], ["commit", "-qm", "Add app"]):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    monkeypatch.chdir(source)

    async def scenario(app, pilot):
        await pilot.press("i")
        await pilot.pause()
        assert "already a project: demo" in str(app.screen.query_one("#notes", Label).render())
        assert app.screen.query_one("#name", Input).disabled
        await pilot.click("#create")
        await pilot.pause()
        assert app.screen.query_one("#goal"), "a task in that project, not another project"
        assert app.screen.query_one("#kind").display, "a project with code can have bugs in it"

    run(scenario)


def test_a_project_whose_folder_is_gone_is_offered_for_removal(env, tmp_path, monkeypatch):
    from vivibox.tui import Confirm

    gone = env / "config" / "projects" / "gone.toml"
    gone.write_text(f'repo = "{tmp_path / "vanished"}"\nverify = ["true"]\n')
    monkeypatch.chdir(load_project("demo").repo)

    async def scenario(app, pilot):
        assert isinstance(app.screen, Confirm), "it says so instead of falling over"
        assert "is gone" in app.screen.question and "gone" in app.screen.question
        assert projects() == ["demo"], "and does not offer a project it cannot work in"
        await pilot.press("enter")  # forget it
        await pilot.pause()
        assert not gone.exists()
        assert (load_project("demo").repo / ".git").is_dir(), "the repositories are left alone"

    run(scenario)


def test_the_first_run_asks_for_a_project(env, tmp_path, monkeypatch):
    (env / "config" / "projects" / "demo.toml").unlink()
    monkeypatch.chdir(tmp_path)

    async def scenario(app, pilot):
        await pilot.pause()
        assert isinstance(app.screen, NewProject), "nothing to work on: set up a project first"
        await pilot.press("escape")
        await pilot.pause()
        assert not projects()

    run(scenario)
