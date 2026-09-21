import asyncio
import subprocess

import pytest
from textual.widgets import Input, Label, Select, TextArea
from textual.widgets._footer import FooterKey

from vivibox import actions, gate, tui
from vivibox.cli import main
from vivibox.config import ConfigError, Role, load_config, load_project
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

OC = "opencode"
AVAILABLE = {"deepseek": ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]}


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
        assert app.screen_stack, "the view is still up, not crashed in a worker"

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
        assert app.screen_stack, "the view is still up, not crashed in a worker"

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


def test_a_pod_answer_arriving_after_you_quit_is_dropped(env):
    """Asking the pods runs docker in a thread, which can outlast the app. The answer then arrives
    for a view with no screen left and raised ScreenStackError, surfacing in whatever test happened
    to be running next."""
    new_task("Waiting")
    app = Vivibox()

    async def go():
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()

    asyncio.run(go())
    assert not app.screen_stack, "the app is gone; docker was still thinking"
    app.pods_answered({"demo-1": tui.PodView("198.51.100.2", [], demo=True)})


def test_m_puts_one_role_on_another_model_for_this_task_only(env):
    """A task going badly on a cheap model is worth finishing on a better one. The machine's
    config.toml is the default and is left alone; the choice belongs to the task."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.available = AVAILABLE
        app.reload()
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, tui.ChooseRole)
        assert [r[0] for r in app.screen.rows] == ["planner", "writer"], "both roles, named"

        await pilot.press("down", "enter")  # writer
        await pilot.pause()
        assert isinstance(app.screen, tui.ChooseModel)
        # A list of what you can run, not a field to type a model id into.
        assert app.screen.offered == [
            (OC, "m"),
            (OC, "deepseek/deepseek-v4-flash"),
            (OC, "deepseek/deepseek-v4-pro"),
        ]
        await pilot.press("down", "down", "enter")
        await pilot.pause()

    run(scenario)
    assert task.read_state().models == {"writer": "deepseek/deepseek-v4-pro"}
    assert task.read_state().harnesses == {}, "same harness as config.toml, so nothing to keep"
    assert load_config().roles["writer"].model == "m", "config.toml is not touched"


def test_m_can_hand_planning_to_you_for_one_task(env):
    task = new_task("Waiting")
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.available = AVAILABLE
        app.reload()
        await pilot.press("m", "enter")  # planner
        await pilot.pause()
        assert app.screen.offered[1] == ("manual", "")
        await pilot.press("down", "enter")
        await pilot.pause()

    run(scenario)
    assert actions.role_of(task, "planner") == Role("manual", "")


def test_the_first_choice_hands_the_role_back_to_the_config(env):
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    task.set_model("planner", "claude-opus-5")

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("m")
        await pilot.pause()
        assert app.screen.rows[0] == ("planner", "claude-opus-5", True), "shown as this task's own"
        await pilot.press("enter")  # planner
        await pilot.pause()
        await pilot.press("enter")  # the first entry: back to config.toml

    run(scenario)
    assert task.read_state().models == {}


def test_the_view_rebuilds_itself_only_when_something_moved(env, monkeypatch):
    """Refreshing every two seconds rebuilt the table and the panel whether or not anything had
    changed. Textual keeps several hundred objects per rebuild, so a session left open overnight
    reached 5 GB and a quarter of a core while the tasks sat still."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    monkeypatch.setattr(tui, "pod_views", lambda ids: {})
    built = []
    real = tui.Vivibox.fill_table
    monkeypatch.setattr(tui.Vivibox, "fill_table", lambda self, *a: built.append(1) or real(self, *a))

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        built.clear()
        for _ in range(20):
            app.reload()
            await pilot.pause()
        assert built == [], "nothing moved, so nothing is redrawn"

        task.transition(State.IMPLEMENT)
        app.reload()
        await pilot.pause()
        assert len(built) == 1, "a task that moved is drawn again"
        assert "review the plan" not in str(app.table.get_row(task.id)[1]), "the row followed it"

    run(scenario)


def test_criteria_ticked_during_a_turn_show_up(env, monkeypatch):
    """The agent ticks criteria while it works, and watching them fill in is how you see a long
    turn progressing. Skipping the redraw when the task's state has not moved froze the column for
    the whole of an implementation, which is exactly when it has something to say."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    gate.accept_plan(task, ["true"])
    task.transition(State.IMPLEMENT)
    monkeypatch.setattr(tui, "pod_views", lambda ids: {})
    checklist = task.meta / "handoff" / gate.CRITERIA_FILE

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert str(app.table.get_row(task.id)[3]) == "0/2", "nothing ticked yet"

        checklist.write_text(checklist.read_text().replace("- [ ] it works", "- [x] it works"))
        app.reload()
        await pilot.pause()
        assert str(app.table.get_row(task.id)[3]) == "1/2", "the agent ticked one while working"

    run(scenario)


def test_a_plan_from_your_own_chat_goes_in_through_the_view(env, monkeypatch):
    """A manual planner: c puts the prompt in your clipboard, and the answer you bring back is read,
    counted and offered for acceptance in one step. An answer that is not a plan leaves the task
    waiting and puts the message that asks your chat to fix it in the clipboard instead."""
    from vivibox import manual

    cfg = env / "config" / "config.toml"
    cfg.write_text(
        cfg.read_text().replace(
            '[roles.planner]\nharness = "opencode"\nmodel = "m"', '[roles.planner]\nharness = "manual"'
        )
    )
    task = new_task("Health")
    task.transition(State.CHECKPOINT_PLAN)
    task.set_awaiting_plan(True)
    (task.meta / manual.PROMPT).write_text("the browser prompt")
    (task.meta / manual.PROMPT_CLI).write_text("the cli prompt")
    copied = []
    monkeypatch.setattr(Vivibox, "to_clipboard", lambda self, text: copied.append(text) or "test")

    async def scenario(app, pilot):
        app.reload()
        # Nothing to accept yet, and nobody a reply would reach: the keys that would say otherwise hide.
        assert not app.check_action("accept", ()) and not app.check_action("reply", ())
        assert app.check_action("copy_prompt", ()) and app.check_action("edit_plan", ())
        await pilot.press("c")
        await pilot.press("C")
        assert copied == ["the browser prompt", "the cli prompt"]

        actions.answer_path(task).write_text("Sure, which framework?")
        app.bring_in_plan(task)
        await pilot.pause()
        assert task.read_state().awaiting_plan and "````markdown" in copied[-1]

        plan = task.plan_path.read_text().replace(gate.PLACEHOLDER, "GET /health returns 200")
        actions.answer_path(task).write_text(f"Final:\n\n````markdown\n{plan}````\n")
        app.bring_in_plan(task)
        await pilot.pause()
        assert "2 criteria" in str(app.screen.query_one(Label).render())
        await pilot.click("#yes")
        await pilot.pause()
        assert task.read_state().state is State.IMPLEMENT

    run(scenario)


def test_the_panel_shows_what_the_browser_prompt_sends_about_the_repository(env):
    """An agent wrote it and it goes to another provider with the prompt, so you see it before
    you copy, not after."""
    from vivibox import manual

    task = new_task("Health")
    task.transition(State.CHECKPOINT_PLAN)
    task.set_awaiting_plan(True)
    shown = detail(task, task.read_state(), 3, running=True, pod=tui.PodView())
    assert "Nothing: this is a new project." in shown
    (task.meta / "handoff" / manual.CONTEXT).write_text("Express 4, tests with vitest.\n")
    shown = detail(task, task.read_state(), 3, running=True, pod=tui.PodView())
    assert "Express 4, tests with vitest." in shown and "Health" in shown


def test_a_new_task_can_run_a_role_on_another_model(env, monkeypatch):
    """Chosen when the task is made, from a list of what you can run, not only with m afterwards:
    the first turn is the one that most often decides which model a task deserves. A role left on
    config.toml's choice keeps following config.toml."""
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False: "m")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix login")
        writer = app.screen.query_one("#role-writer", Select)
        assert writer.value == (OC, "m"), "config.toml's choice, ready to keep or change"
        writer.value = (OC, "deepseek/deepseek-v4-pro")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    st = find_task(load_config().tasks_dir, "demo-1").read_state()
    assert st.models == {"writer": "deepseek/deepseek-v4-pro"} and st.harnesses == {}


def test_a_planner_you_plan_with_can_be_given_a_model_for_one_task(env, monkeypatch):
    """The other way round too: config.toml says you plan, and this one task plans on DeepSeek."""
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False: "m")
    cfg = env / "config" / "config.toml"
    cfg.write_text(
        cfg.read_text().replace(
            '[roles.planner]\nharness = "opencode"\nmodel = "m"', '[roles.planner]\nharness = "manual"'
        )
    )

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix login")
        planner = app.screen.query_one("#role-planner", Select)
        assert planner.value == ("manual", "")
        planner.value = (OC, "deepseek/deepseek-v4-pro")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    task = find_task(load_config().tasks_dir, "demo-1")
    assert actions.role_of(task, "planner") == Role(OC, "deepseek/deepseek-v4-pro")
    with pytest.raises(ConfigError, match="only the planner"):
        actions.create("demo", "Fix login", roles={"writer": ("manual", "")})


def test_the_choice_config_toml_makes_is_called_the_default():
    planning_yourself = ("manual", "")
    assert actions.choice_label(planning_yourself, planning_yourself) == "you, in your own chat  (default)"
    assert (
        actions.choice_label((OC, "deepseek/deepseek-v4-pro"), planning_yourself)
        == "deepseek/deepseek-v4-pro"
    )


def test_sending_work_back_can_add_criteria(env):
    task = new_task("Reset view")
    at_plan_checkpoint(task)
    actions.accept_plan(task, load_project("demo"))
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, tui.ReplyWithCriteria)
        app.screen.query_one("#comment", TextArea).text = "Nothing happens on a fresh page."
        app.screen.query_one("#criteria", TextArea).text = "works before a load\n\nkeeps the zoom\n"
        await pilot.press("ctrl+s")
        await pilot.pause()

    run(scenario)
    assert task.read_state().state is State.IMPLEMENT
    assert gate.missing_criteria(task)[-2:] == ["works before a load", "keeps the zoom"]
