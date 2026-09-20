import asyncio
import subprocess

from textual.widgets import Input, Label
from textual.widgets._footer import FooterKey

from vivibox import actions, gate, tui
from vivibox.cli import main
from vivibox.config import load_config, load_project
from vivibox.pod import Listener
from vivibox.states import State
from vivibox.task import find_task, now
from vivibox.tui import (
    CommitWork,
    NewProject,
    Vivibox,
    detail,
    finished_detail,
    projects,
)


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
        # Not "the glyph changed": the 0.1s timer can turn it a full ten frames between two reads
        # when the machine is busy, and land on the same one.
        before = app.frame
        app.spin()
        assert app.frame == before + 1, "the spinner advances"
        cell = str(table.get_cell("demo-1", app.status_column))
        assert any(c in cell for c in SPINNER) and "planning" in cell
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


def test_criteria_are_ticked_off_in_view_while_the_agent_works(env):
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task, load_project("demo").verify)
    st = task.transition(State.IMPLEMENT)
    assert "\u2610 it works" in detail(task, st, 3), "an open criterion"
    assert "gate has not run yet" in detail(task, st, 3).lower()
    reported = task.meta / "handoff" / gate.CRITERIA_FILE
    reported.write_text(reported.read_text().replace("- [ ]", "- [x]"))
    assert "\u2611 it works" in detail(task, st, 3), "the agent reports it met"


def test_a_finished_task_shows_what_it_was_accepted_for():
    entry = {
        "id": "demo-1",
        "project": "demo",
        "title": "Add health endpoint",
        "cost": 0.02,
        "commit": "abc1234567",
        "branch": "",
        "conflicts": [],
        "criteria": ["it works"],
        "finished": now(),
    }
    assert "\u2611 it works" in finished_detail(entry)
    del entry["criteria"]
    assert "not recorded" in finished_detail(entry), "tasks accepted before vivibox kept them"


def test_the_panel_tells_the_three_states_apart(env):
    task = new_task()
    st = task.read_state()

    up = tui.PodView("198.51.100.2", [Listener(5173, True)], demo=True)
    assert "[198.51.100.2:5173](http://198.51.100.2:5173)" in detail(task, st, 3, pod=up), "clickable"

    hidden = tui.PodView("198.51.100.2", [Listener(8000, False)], demo=True)
    assert "nothing outside can reach it" in detail(task, st, 3, pod=hidden)

    starting = tui.PodView("198.51.100.2", [], demo=True)
    assert "running, nothing listening yet" in detail(task, st, 3, pod=starting)

    crashed = tui.PodView("198.51.100.2", [], demo=False, log="Traceback…\nKeyError: 'gameweek'")
    text = detail(task, st, 3, pod=crashed)
    assert "It stopped." in text and "KeyError: 'gameweek'" in text, "why, not just that"

    idle = tui.PodView("198.51.100.2", [], demo=False)
    assert "demo not running" in detail(task, st, 3, pod=idle)
    assert "It stopped." not in detail(task, st, 3, pod=idle), "it was never started"


def test_stopping_is_offered_only_while_something_runs(env, monkeypatch):
    task = new_task()
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    # Pinned, or the refresh in the background would replace it with what a real pod says.
    view = [tui.PodView("198.51.100.2", [], demo=False)]
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: view[0] for i in ids})

    async def scenario(app, pilot):
        assert await until(pilot, lambda: not app.pod.demo)
        assert app.check_action("demo", ()) and not app.check_action("demo_stop", ())
        view[0] = tui.PodView("198.51.100.2", [Listener(8000, True)], demo=True)
        assert await until(pilot, lambda: app.pod.demo)
        assert app.check_action("demo_stop", ()), "stop appears once it is up"
        assert app.check_action("demo", ()), "and running it again restarts it"

    run(scenario)


def test_this_tasks_instruction_wins_over_everything_else(env):
    task = new_task("Goal")
    (task.repo / "compose.yaml").write_text("services: {}\n")
    project = load_project("demo")

    assert actions.demo_commands(project, task) == (["docker compose up"], "compose")
    actions.write_instruction(task, "Start it:\n\n```bash\nnpm run dev\n```\n")
    assert actions.demo_commands(project, task) == (["npm run dev"], "task")


def test_an_instruction_is_its_shell_blocks_in_order(env):
    text = (
        "Needs the database first.\n\n```bash\ndocker compose up -d db\n```\n\n"
        "Then the app, on 0.0.0.0 so you can reach it:\n\n```bash\n./gradlew bootRun\n```\n"
    )
    assert actions.instruction_commands(text) == ["docker compose up -d db", "./gradlew bootRun"]
    assert actions.instruction_commands("No commands here, only prose.") == []


def test_a_compose_file_is_the_repositorys_own_answer(tmp_path):
    from vivibox import init

    repo = tmp_path / "r"
    repo.mkdir()
    assert init.detect_demo(repo) == []
    (repo / "docker-compose.yml").write_text("services: {}\n")
    assert init.detect_demo(repo) == ["docker compose up"]


def test_an_accepted_task_leaves_its_instruction_for_the_next_one(env):
    """A record, not configuration: vivibox offers it and you say whether it still applies."""
    task = new_task("Goal")
    actions.write_instruction(task, "```bash\nnpm run dev\n```\n")
    done = actions.Finished("demo-9", env, 0.1, "Done", demo=actions.demo_instruction(task))
    actions.remember(done, load_project("demo"), "abc1234567")

    assert "npm run dev" in actions.demo_from_history("demo")
    assert actions.demo_from_history("other-project") == ""


async def until(pilot, check, tries: int = 60):
    """The pod is asked in a worker, and an exclusive worker may be replaced by the next refresh;
    what matters is that the answer lands, not which run delivered it."""
    for _ in range(tries):
        if check():
            return True
        await asyncio.sleep(0.05)
        await pilot.pause()
    return False


def test_the_row_says_whether_that_task_is_serving_anything(env, monkeypatch):
    """On the row itself, without opening the panel: the list is what you look at."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    answer = [tui.PodView("198.51.100.2", [], demo=False)]
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: answer[0] for i in ids})

    def cell(app):
        return str(app.table.get_row(task.id)[2])

    async def scenario(app, pilot):
        assert await until(pilot, lambda: cell(app) == "-"), "nothing started yet"
        answer[0] = tui.PodView("198.51.100.2", [Listener(8000, True)], demo=True)
        assert await until(pilot, lambda: "live" in cell(app)), "it is serving"
        assert "8000" not in cell(app), "the address belongs in the panel, where all of it fits"
        answer[0] = tui.PodView("198.51.100.2", [Listener(5173, True), Listener(8000, True)], demo=True)
        assert await until(pilot, lambda: "×2" in cell(app)), "a front end and a back end both up"
        answer[0] = tui.PodView("198.51.100.2", [Listener(8000, False)], demo=True)
        assert await until(pilot, lambda: "local" in cell(app)), "bound to localhost, never coming"
        answer[0] = tui.PodView("198.51.100.2", [], demo=True)
        assert await until(pilot, lambda: "starting" in cell(app)), "up, but no port yet"
        answer[0] = tui.PodView("198.51.100.2", [], demo=False, log="Error: exploded")
        assert await until(pilot, lambda: "stopped" in cell(app)), "and when it dies"
        assert app.is_running

    run(scenario)


def keys(app) -> set[str]:
    """The actions the footer is showing right now."""
    return {key.action for key in app.query(FooterKey)}


def test_the_footer_shows_your_keys_while_the_terminal_has_no_focus(env):
    """Textual's Footer stops redrawing when the app is blurred, so the keys of a task that starts
    waiting for you never appear until you click back into the terminal."""
    task = new_task("Waiting")

    async def scenario(app, pilot):
        app.app_focus = False
        app.reload()
        await pilot.pause()
        await pilot.pause()
        assert "accept" not in keys(app), "nothing to accept while it is still planning"
        at_plan_checkpoint(task)
        app.reload()
        await pilot.pause()
        await pilot.pause()
        assert "accept" in keys(app), "the plan checkpoint offers Accept without a click first"

    run(scenario)


def test_the_list_says_when_you_asked_for_a_task_not_only_when_it_last_moved(env):
    task = new_task("Waiting")

    async def scenario(app, pilot):
        app.reload()
        row = app.table.get_row(task.id)
        # Not ui.ago() recomputed here: that races the minute boundary and says nothing extra.
        assert row[5] == "just now", "CREATED, and the task was made a moment ago"
        assert row[6] == "just now", "UPDATED"
        assert [str(c.label) for c in app.table.columns.values()][5] == "CREATED"

    run(scenario)


def test_a_finished_task_keeps_when_you_asked_for_it(env):
    """A task you accepted still says when you asked for it; one accepted before vivibox recorded
    that says so with a dash instead of pretending."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    done = actions.Finished("demo-9", env, 0.42, "Reject expired cards", created=now())
    actions.remember(done, load_project("demo"), "abc1234567")
    actions.remember(
        actions.Finished("demo-8", env, 0.1, "Older, before created was kept"),
        load_project("demo"),
        "def4567890",
    )

    async def scenario(app, pilot):
        app.show_done = True
        app.reload()
        assert app.table.get_row("demo-9")[5] == "just now"
        assert app.table.get_row("demo-8")[5] == "-"

    run(scenario)


def test_the_view_follows_the_pod_on_its_own(env, monkeypatch):
    """The refresh asks the pods and puts the answer on screen without being told to, and the
    answer arrives on the event loop: a slip there takes the whole app down."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    down = tui.PodView("198.51.100.2", [], demo=False)
    up = tui.PodView("198.51.100.2", [Listener(8000, True)], demo=True)
    answer = [down]
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: answer[0] for i in ids})

    async def scenario(app, pilot):
        await pilot.press("enter")
        assert await until(pilot, lambda: app.pod == down), "the first answer lands"
        answer[0] = up  # only a later refresh can see this; the selection never changes again
        assert await until(pilot, lambda: app.pod == up), "asked again on its own"
        assert await until(pilot, lambda: "198.51.100.2:8000" in app.shown), "and the panel says so"
        await pilot.pause()
        assert app.is_running

    run(scenario)


def test_a_task_with_no_pod_claims_nothing(env):
    task = new_task("Waiting")
    assert "Pod" not in detail(task, task.read_state(), 3, pod=tui.PodView())


def test_running_it_does_not_stop_to_ask_permission_to_work_out_how(env, monkeypatch):
    """Pressing the key that means 'run it' when nothing says how used to open a confirmation, and
    there is no second answer you could give it: you already said run it."""
    task = new_task("Runnable")
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    monkeypatch.setattr(tui.actions, "demo_from_history", lambda name: "")
    called: list[dict] = []
    monkeypatch.setattr(tui.Vivibox, "run_demo", lambda self, task_id, **kw: called.append(kw))

    async def scenario(app, pilot):
        app.action_demo()
        await pilot.pause()
        assert called == [{"ask": True}], "it asks the agent straight away"
        assert not app.screen_stack[1:], "and puts no question in your way"

    run(scenario)
