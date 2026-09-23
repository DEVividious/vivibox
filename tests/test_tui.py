import asyncio
import subprocess
from pathlib import Path

import pytest
from conftest import make_repo
from textual.widgets import Input, Label, OptionList, Select, SelectionList, TextArea
from textual.widgets._footer import FooterKey

from vivibox import actions, gate, tui, ui
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
    # By number: sorted by name, demo-10 comes before demo-9.
    newest = max(int(p.name.removeprefix("demo-")) for p in tasks.iterdir() if p.name.startswith("demo-"))
    return find_task(tasks, f"demo-{newest}")


def at_plan_checkpoint(task):
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))


def run(scenario, size=(140, 40)):
    async def go():
        app = Vivibox()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(go())


def test_lists_tasks_waiting_for_you_first(env, monkeypatch):
    planning = new_task("Still planning")
    planning.event("started", model="m")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id == planning.id)
    waiting = new_task("Waiting")
    at_plan_checkpoint(waiting)

    async def scenario(app, pilot):
        app.reload()
        assert app.selected_id() == waiting.id, "the task waiting for you is on top"
        assert app.sub_title == "1 waiting for you · 1 working"
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


def fresh_project(env, name="clicker"):
    """A project from scratch, with no build of its own yet."""
    actions.setup_project(env / name, name, [], create=True)
    return name


def plan_with(task, header):
    task.plan_path.write_text(f"+++\n{header}\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] it works\n")
    task.transition(State.CHECKPOINT_PLAN)


def test_the_first_plan_shows_the_verification_it_sets_before_you_accept(env):
    """The command is kept for the project's next tasks, so it is said once, where you decide."""
    from vivibox.config import load_project

    fresh_project(env)
    task = actions.create("clicker", "A click counter page")
    plan_with(task, 'verify = ["npm ci && npm test"]')

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, tui.Confirm)
        assert "npm ci && npm test" in app.screen.question and "clicker" in app.screen.question
        await pilot.press("enter")
        await pilot.pause()
        assert task.read_state().state is State.IMPLEMENT

    run(scenario)
    assert load_project("clicker").verify == ["npm ci && npm test"]


def test_the_first_plan_may_say_there_is_no_build(env):
    from vivibox.config import load_project

    fresh_project(env, "notes")
    task = actions.create("notes", "Write the handbook")
    plan_with(task, "verify = false")

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, tui.Confirm) and "no build" in app.screen.question
        await pilot.press("escape")
        await pilot.pause()
        assert task.read_state().state is State.CHECKPOINT_PLAN, "not accepted: you said no"

    run(scenario)
    assert not load_project("notes").no_build


def test_e_on_a_project_row_picks_how_it_is_verified(env, tmp_path, monkeypatch):
    """The commands the build files name, no build, or one of your own; the file itself last."""
    from vivibox.config import load_project

    fresh_project(env, "notes")
    (env / "notes" / "package.json").write_text("{}")
    opened = []
    # The editor takes the terminal over (App.suspend), which Pilot cannot do: the call is checked.
    monkeypatch.setattr(tui.Vivibox, "edit_project_file", lambda self: opened.append(self.project_file()))

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("notes"))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, tui.ChooseVerify)
        shown = [str(app.screen.query_one(OptionList).get_option_at_index(i).prompt)
                 for i in range(app.screen.query_one(OptionList).option_count)]  # fmt: skip
        assert "npm ci && npm test" in shown[0] and "package.json" in shown[0]
        assert any("no build" in s for s in shown) and any("edit the project file" in s for s in shown)
        await pilot.press("enter")  # the first: what package.json names
        await pilot.pause()
        assert load_project("notes").verify == ["npm ci && npm test"]
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("down", "enter")  # no build
        await pilot.pause()
        assert load_project("notes").no_build and load_project("notes").verify == []
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#other", Input).value = "make check"
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert load_project("notes").verify == ["make check"]
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("end", "enter")  # the file itself
        await pilot.pause()
        assert opened == [env / "config" / "projects" / "notes.toml"]

    run(scenario)


def test_the_new_project_dialog_says_where_the_command_comes_from_and_lets_you_pick(
    env, tmp_path, monkeypatch
):
    """What the build file says is the default; what the CI definition runs is a pick away, so
    the verification here is the pipeline's, not a guess."""
    from vivibox.config import load_project

    repo = tmp_path / "shop"
    make_repo(repo)
    (repo / "mvnw").write_text("")
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  b:\n    steps:\n      - run: ./mvnw --batch-mode verify -Pit\n"
    )
    monkeypatch.chdir(repo)

    async def scenario(app, pilot):
        await pilot.press("i")
        await pilot.pause()
        shown = str(app.screen.query_one("#verify", Label).render())
        assert "bash mvnw -B verify" in shown and "from mvnw" in shown
        app.screen.query_one("#change").press()
        await pilot.pause()
        assert isinstance(app.screen, tui.ChooseVerify)
        options = app.screen.query_one(OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert "ci.yml" in labels[1] and not any("edit the project file" in s for s in labels), "no file yet"
        assert any("first plan" in s for s in labels), "or leave it to the plan, as before"
        await pilot.press("down", "enter")
        await pilot.pause()
        shown = str(app.screen.query_one("#verify", Label).render())
        assert "verify -Pit" in shown and "ci.yml" in shown
        app.screen.query_one("#create").press()
        await pilot.pause()

    run(scenario)
    assert load_project("shop").verify == ["bash mvnw --batch-mode verify -Pit"]


def test_a_project_from_scratch_can_be_set_up_with_no_build(env, tmp_path, monkeypatch):
    from vivibox.config import load_project

    fresh = tmp_path / "notes"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui, "browse_start", lambda: tmp_path)

    async def scenario(app, pilot):
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one("#browse").press()
        await pilot.pause()
        app.screen.query_one("#new-folder").press()
        await pilot.pause()
        await pilot.press(*"notes", "enter")
        await pilot.pause()
        assert "first plan" in str(app.screen.query_one("#verify", Label).render())
        app.screen.query_one("#change").press()
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        await pilot.press(*["down"] * labels.index(next(s for s in labels if "no build" in s)), "enter")
        await pilot.pause()
        assert "no build" in str(app.screen.query_one("#verify", Label).render())
        app.screen.query_one("#create").press()
        await pilot.pause()

    run(scenario)
    assert load_project("notes").no_build and (fresh / ".git").is_dir()


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
    monkeypatch.setattr(
        "vivibox.actions.start", lambda task_id, resume=False, on_step=None: started.append(task_id) or "m"
    )

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix login", "enter", *"More context")
        await pilot.press("down")
        assert app.screen.focused.id == "attach", "down on the last line moves on"
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
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")

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
        actions.Finished("demo-9", env, ui.Spend(0.3, 0.12), "Reject expired cards"),
        load_project("demo"),
        "abc1234567",
    )

    async def scenario(app, pilot):
        app.reload()
        assert rows(app) == ["demo", "demo-1", "demo-9"]
        await pilot.press("down", "enter")
        assert "Reject expired cards" in app.shown and "$0.30 + $0.12" in app.shown
        assert app.check_action("remove", ()) and not app.check_action("accept", ())
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, tui.DeleteTask) and app.screen.focused.id == "no", "Cancel first"
        await pilot.press("left", "enter")  # Delete
        await pilot.pause()
        assert rows(app) == ["demo", "demo-1"] and actions.history() == []
        await pilot.press("h")
        assert rows(app) == ["demo", "demo-1"], "hiding finished tasks leaves the live ones"

    run(scenario)


def test_a_project_can_be_set_up_from_the_view(env, tmp_path, monkeypatch):
    from vivibox.config import load_project

    fresh = tmp_path / "clicker"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tui, "browse_start", lambda: tmp_path)
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")

    async def scenario(app, pilot):
        await pilot.press("i")  # a project of its own, from anywhere in the view
        await pilot.pause()
        assert app.screen.where == tmp_path, "where you started vivibox, until you browse elsewhere"
        app.screen.query_one("#browse").press()
        await pilot.pause()
        assert isinstance(app.screen, tui.Browse)
        app.screen.query_one("#new-folder").press()  # a project from scratch: no folder yet
        await pilot.pause()
        await pilot.press(*"clicker", "enter")
        await pilot.pause()
        assert isinstance(app.screen, tui.NewProject) and app.screen.where == fresh and fresh.is_dir()
        assert app.screen.query_one("#name", Input).value == "clicker"
        app.screen.query_one("#create").press()
        await pilot.pause()
        assert app.screen.query_one("#goal"), "its first task follows right away"
        kind = app.screen.query_one("#kind-row")
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
        assert app.screen.query_one("#kind-row").display, "a project with code can have bugs in it"

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


def test_the_first_run_says_how_to_add_a_project_and_opens_nothing(env, tmp_path, monkeypatch):
    """No dialog you did not ask for: the view says what to press, and a task cannot be started
    before there is a project to put it in."""
    (env / "config" / "projects" / "demo.toml").unlink()
    monkeypatch.chdir(tmp_path)

    async def scenario(app, pilot):
        await pilot.pause()
        assert not isinstance(app.screen, NewProject)
        shown = str(app.query_one("#empty").render())
        assert "No projects yet" in shown and "Press i" in shown and not app.table.display
        assert not app.check_action("new", ()), "n is hidden with no project"
        assert not app.check_action("details", ()) and not app.check_action("toggle_done", ()), "nor d and h"
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, tui.NewTask)
        (env / "config" / "projects" / "demo.toml").write_text(
            f'repo = "{env / "repo"}"\nverify = ["true"]\n'
        )
        app.reload()
        await pilot.pause()
        assert app.check_action("new", ()) and app.table.display
        assert "no tasks · n creates one" in cell(app, 0, "GOAL"), "the project row says"

    run(scenario)


def test_criteria_are_ticked_off_in_view_while_the_agent_works(env):
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task, load_project("demo").verify)
    st = task.transition(State.IMPLEMENT)
    assert "\u2610 it works" in detail(task, st, 3), "an open criterion"
    assert "no verification yet" in detail(task, st, 3).lower()
    reported = task.meta / "handoff" / gate.CRITERIA_FILE
    reported.write_text(reported.read_text().replace("- [ ]", "- [x]"))
    assert "\u2611 it works" in detail(task, st, 3), "the agent reports it met"


def test_a_failed_gate_shows_what_the_build_said(env):
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task, load_project("demo").verify)
    st = task.transition(State.IMPLEMENT)
    handoff = task.meta / "handoff"
    (handoff / "verify-feedback.md").write_text("# Verification failed\n- Command failed: `mvn -B verify`\n")
    (handoff / "verify.log").write_text(
        "[INFO] Scanning\n[ERROR] ShopIT: permission denied\n[INFO] BUILD FAILURE\n"
    )
    task.event("gate", passed=True)
    assert "What the build said" not in detail(task, st, 3), "not after a gate that passed"
    task.event("gate", passed=False)
    shown = detail(task, st, 3)
    assert "Command failed" in shown and "[ERROR] ShopIT: permission denied" in shown
    assert f"Full log: `{handoff / 'verify.log'}`" in shown, "a path on your machine, not in the pod"
    st = task.transition(State.CHECKPOINT_BLOCKED)
    assert "[ERROR] ShopIT: permission denied" in detail(task, st, 3), "and when it keeps failing"


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


def test_running_the_app_waits_until_the_work_is_back_with_you(env, monkeypatch):
    """The agent builds and tests in the same working tree and pod; the app started beside it
    would fight it for the build output and the ports."""
    task = new_task()
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    view = [tui.PodView("198.51.100.2", [], demo=False)]
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: view[0] for i in ids})

    async def scenario(app, pilot):
        assert await until(pilot, lambda: not app.pod.demo)
        assert not app.check_action("demo", ()), "not while the agent implements"
        task.transition(State.VERIFY)
        app.reload()
        assert not app.check_action("demo", ()), "not while the gate verifies"
        task.transition(State.CHECKPOINT_FINAL)
        app.reload()
        assert app.check_action("demo", ()), "yours to run once the work is back with you"

    run(scenario)


def test_stopping_is_offered_only_while_something_runs(env, monkeypatch):
    task = new_task()
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)
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
    done = actions.Finished("demo-9", env, ui.Spend(0.0, 0.1), "Done", demo=actions.demo_instruction(task))
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
    done = actions.Finished("demo-9", env, ui.Spend(0.3, 0.12), "Reject expired cards", created=now())
    actions.remember(done, load_project("demo"), "abc1234567")
    actions.remember(
        actions.Finished("demo-8", env, ui.Spend(0.0, 0.1), "Older, before created was kept"),
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
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")

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
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")
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


def test_a_provider_imported_from_opencode_json_is_offered_to_the_writer(env, monkeypatch, tmp_path):
    """The company case: you plan in your own chat, and the writer runs on your employer's model,
    defined in an opencode.json you already use. Brought over under k, then picked in the task."""
    from vivibox import keys, providers

    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")
    monkeypatch.setattr("vivibox.actions.provider_models", lambda p: [])
    monkeypatch.setattr("vivibox.actions.models_cache", lambda: tmp_path / "models.json")
    monkeypatch.setenv("ACME_KEY", "acme-secret")
    source = tmp_path / "xdg" / "opencode" / "opencode.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"provider": {"acme": {"npm": "@ai-sdk/openai-compatible",'
        ' "options": {"baseURL": "https://ai.acme.example/v1", "apiKey": "{env:ACME_KEY}"},'
        ' "models": {"coder": {}, "chat": {}}}}}'
    )

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("k")
        await pilot.pause()
        app.screen.query_one("#import").press()
        await pilot.pause()
        assert isinstance(app.screen, tui.ImportSource)
        await pilot.press("enter")  # the configuration found where opencode keeps it
        await pilot.pause()
        assert isinstance(app.screen, tui.ChooseImport), "you see what comes before it comes"
        assert keys.list_keys() == {}, "nothing kept yet"
        app.screen.query_one("#import").press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, tui.ManageProviders)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix login")
        writer = app.screen.query_one("#role-writer", Select)
        assert (OC, "acme/coder") in [c for _, c in writer._options], "its models are on the list"
        writer.value = (OC, "acme/coder")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    assert keys.get_key("acme") == "acme-secret"
    assert providers.models()["acme"] == ["acme/coder", "acme/chat"]
    st = find_task(load_config().tasks_dir, "demo-1").read_state()
    assert st.models == {"writer": "acme/coder"}


def test_a_provider_added_by_name_and_key_is_stored(env, monkeypatch, tmp_path):
    from vivibox import keys

    monkeypatch.setattr("vivibox.actions.provider_models", lambda p: [f"{p}/big", f"{p}/small"])
    monkeypatch.setattr("vivibox.actions.models_cache", lambda: tmp_path / "models.json")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        app.catalog = CATALOG
        await pilot.press("k")
        await pilot.pause()
        app.screen.query_one("#add").press()
        await pilot.pause()
        await pilot.press(*"open")  # the search field has the focus
        await pilot.pause()
        assert app.screen.picked() == "openai", "the first match, picked as you type"
        app.screen.query_one("#key", Input).value = "sk-openai"
        app.screen.query_one("#add").press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        writer = app.screen.query_one("#role-writer", Select)
        assert (OC, "openai/big") in [c for _, c in writer._options], "the new models are on the list"
        assert not any(str(label).startswith("+") for label, _ in writer._options), "adding is under k"

    run(scenario)
    assert keys.get_key("openai") == "sk-openai"


def test_a_provider_that_lists_no_models_is_said_to_check_the_name(env, monkeypatch, tmp_path):
    """A typo in a provider's name is not an error to opencode: it lists nothing. Said at once."""
    monkeypatch.setattr("vivibox.actions.provider_models", lambda p: [])
    monkeypatch.setattr("vivibox.actions.models_cache", lambda: tmp_path / "models.json")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        app.catalog = CATALOG
        await pilot.press("k")
        await pilot.pause()
        app.screen.query_one("#add").press()
        await pilot.pause()
        await pilot.press(*"azure")
        app.screen.query_one("#key", Input).value = "sk-azure"
        app.screen.query_one("#add").press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any("no models for azure; check the name" in str(n.message) for n in app._notifications)

    run(scenario)


def test_n_without_a_provider_says_to_press_k(env):
    """The first run: no provider and the writer without a model. A dialog whose lists have nothing
    to pick would be a dead end, so n says where to go, as it does without a project."""
    (env / "config" / "config.toml").write_text(
        f'tasks_dir = "{env / "tasks"}"\n'
        '[roles.planner]\nharness = "manual"\nmodel = ""\n'
        '[roles.writer]\nharness = "opencode"\nmodel = ""\n'
    )

    async def scenario(app, pilot):
        app.available = {}
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, tui.NewTask)
        assert any("press k" in str(n.message) for n in app._notifications)

    run(scenario)


def test_d_shows_with_a_task_and_h_with_a_finished_one(env):
    new_task()

    async def scenario(app, pilot):
        await pilot.pause()
        assert app.check_action("details", ())
        assert not app.check_action("toggle_done", ()), "nothing finished yet"
        actions.history_path().parent.mkdir(parents=True, exist_ok=True)
        actions.history_path().write_text(
            '{"id": "demo-9", "title": "Old", "cost": 0, "finished": "2026-09-01T00:00:00+00:00"}\n'
        )
        app.reload()
        assert app.check_action("toggle_done", ())

    run(scenario)


CATALOG = [("anthropic", "Anthropic"), ("openai", "OpenAI"), ("deepseek", "DeepSeek"), ("azure", "Azure")]


def test_providers_are_found_by_name_or_id_and_a_typed_name_is_offered_too():
    assert [pid for pid, _ in tui.find_providers(CATALOG, "")] == ["anthropic", "openai", "deepseek", "azure"]
    assert [pid for pid, _ in tui.find_providers(CATALOG, "SEEK")] == ["deepseek", "seek"]
    assert [pid for pid, _ in tui.find_providers(CATALOG, "deepseek")] == ["deepseek"], (
        "no second of the same"
    )
    assert tui.find_providers([], "acme") == [("acme", "use “acme” as the provider's name")]


def test_arrows_in_the_search_walk_the_list(env):
    async def scenario(app, pilot):
        app.push_screen(tui.AddProvider(CATALOG))
        await pilot.pause()
        assert app.screen.picked() == "anthropic"
        await pilot.press("down", "down")
        assert app.screen.picked() == "deepseek"
        await pilot.press("up")
        assert app.screen.picked() == "openai"
        await pilot.press("enter")
        assert app.screen.focused is app.screen.query_one("#key"), "Enter goes on to the key"

    run(scenario)


def test_an_import_lists_what_it_brings_and_you_decide_on_what_you_have(env, tmp_path, monkeypatch):
    """Nothing merged behind your back: providers and MCP servers in two groups, the new ones ticked,
    one that differs from yours unticked for you to decide, one you have already not to be picked,
    and the rest of the file named as left behind."""
    from vivibox import keys, providers

    keys.set_key("deepseek", "sk-mine")
    monkeypatch.setenv("ACME_KEY", "acme-secret")
    source = env / "xdg" / "opencode" / "opencode.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"agent": {}, "provider": {'
        '"acme": {"options": {"baseURL": "https://ai.acme.example/v1", "apiKey": "{env:ACME_KEY}"},'
        ' "models": {"coder": {}}},'
        '"deepseek": {"options": {"apiKey": "sk-theirs"}}},'
        ' "mcp": {"company": {"type": "remote", "url": "https://mcp.acme.example"},'
        ' "tools": {"type": "local", "command": ["npx", "tools-mcp"]}}}'
    )
    providers.bring_over([f for f in providers.read_opencode(source).found if f.name == "tools"])

    async def scenario(app, pilot):
        app.import_opencode(lambda names: None)
        await pilot.pause()
        await pilot.press("enter")  # the configuration found where opencode keeps it
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, tui.ChooseImport)

        def rows(kind):
            found = screen.query_one(f"#found-{kind}", SelectionList)
            options = [found.get_option_at_index(i) for i in range(found.option_count)]
            return [(str(o.prompt), o.value in found.selected, o.disabled) for o in options]

        acme, deepseek = rows("provider")
        assert "acme  1 model, key from $ACME_KEY" in acme[0] and acme[1:] == (True, False), "new: ticked"
        assert "differs from yours: tick to overwrite" in deepseek[0] and deepseek[1:] == (False, False)
        company, tools = rows("mcp")
        assert "remote https://mcp.acme.example" in company[0] and company[1:] == (True, False)
        assert "same as yours" in tools[0] and tools[1:] == (False, True), "nothing to decide"
        assert "not for vivibox: agent" in " ".join(str(w.render()) for w in screen.query(Label))
        screen.query_one("#import").press()
        await pilot.pause()

    run(scenario)
    assert keys.get_key("deepseek") == "sk-mine", "yours kept: you did not tick it"
    assert keys.get_key("acme") == "acme-secret"
    assert list(providers.load()) == ["acme"] and sorted(providers.load_mcp()) == ["company", "tools"]


def test_a_file_is_judged_before_you_pick_it(env, tmp_path):
    good = tmp_path / "opencode.json"
    good.write_text('{"provider": {"acme": {"models": {"m": {}}}}, "mcp": {"s": {"type": "local"}}}')
    other = tmp_path / "package.json"
    other.write_text('{"name": "x"}')
    broken = tmp_path / "broken.json"
    broken.write_text('{"provider": {')
    assert tui.judge(good) == (True, "opencode configuration: 1 provider, 1 MCP server")
    assert tui.judge(other) == (
        False,
        "JSON, but not an opencode configuration with providers or MCP servers",
    )
    ok, said = tui.judge(broken)
    assert not ok and said.startswith("broken: not JSON")


def test_the_browser_shows_only_what_can_be_picked(tmp_path):
    for name in ("a.json", "b.jsonc", "notes.txt", "img.png"):
        (tmp_path / name).write_text("{}")
    for name in ("src", ".config", "node_modules", ".git"):
        (tmp_path / name).mkdir()
    shown = {
        mode: {p.name for p in tmp_path.iterdir() if tui.shows(mode, p)} for mode in ("json", "folder", "any")
    }
    assert shown["json"] == {"a.json", "b.jsonc", "src", ".config"}
    assert shown["folder"] == {"src", ".config"}
    assert shown["any"] == {"a.json", "b.jsonc", "notes.txt", "img.png", "src", ".config"}


def test_browsing_picks_an_opencode_configuration_and_not_another_json(env, tmp_path, monkeypatch):
    from textual.widgets import DirectoryTree

    good = tmp_path / "opencode.json"
    good.write_text('{"provider": {"acme": {"models": {"m": {}}}}}')
    other = tmp_path / "package.json"
    other.write_text('{"name": "x"}')
    picked = []

    async def scenario(app, pilot):
        app.push_screen(tui.Browse("json", "Find it"), picked.append)
        await pilot.pause()
        browser = app.screen
        tree = browser.query_one("#tree", tui.PathTree)
        browser.file_picked(DirectoryTree.FileSelected(tree.root, other))
        await pilot.pause()
        assert app.screen is browser, "not an opencode configuration: not picked"
        browser.file_picked(DirectoryTree.FileSelected(tree.root, good))
        await pilot.pause()

    run(scenario)
    assert picked == [good]


def test_the_project_is_a_list_even_with_one_project(env):
    """The same dialog with one project as with five."""

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        project = app.screen.query_one("#project", Select)
        assert project.value == "demo" and projects() == ["demo"]

    run(scenario)


def test_k_lists_providers_and_mcp_and_manage_turns_them_off_or_removes_them(env):
    from vivibox import keys, providers

    keys.set_key("deepseek", "sk-mine")
    keys.set_key("openai", "sk-other")

    async def scenario(app, pilot):
        await pilot.press("k")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, tui.ManageProviders)
        assert [(r[1], r[3]) for r in screen.rows] == [("deepseek", True), ("openai", True), ("serena", True)]
        assert "auto: on for a project with 100+ source files" in screen.rows[2][2]
        screen.query_one("#manage").press()
        await pilot.pause()
        manage = app.screen
        assert isinstance(manage, tui.ManageItems)
        assert [r[1] for r in manage.rows] == ["deepseek", "openai"], "Serena has its mode, not a tick"
        manage.query_one("#items", SelectionList).deselect(1)  # openai off
        manage.query_one("#serena-mode", Select).value = "off"
        manage.query_one("#save").press()
        await pilot.pause()
        assert [(r[1], r[3]) for r in app.screen.rows] == [
            ("deepseek", True),
            ("openai", False),
            ("serena", False),
        ]
        app.screen.query_one("#manage").press()
        await pilot.pause()
        app.screen.query_one("#items", SelectionList).highlighted = 0
        app.screen.query_one("#remove").press()
        await pilot.pause()
        await pilot.press("enter")  # confirm
        await pilot.pause()

    run(scenario)
    assert "deepseek" not in keys.list_keys() and keys.get_key("openai") == "sk-other"
    assert not providers.enabled(providers.PROVIDER, "openai") and providers.serena_mode() == "off"


def test_the_panel_says_whether_the_task_got_serena_and_why(env):
    from vivibox import opencode

    task = new_task()
    opencode.prepare(task, "deepseek/deepseek-v4-flash", ["true"])
    shown = detail(task, task.read_state(), 3, running=False, pod=tui.PodView())
    assert "*Serena: off, 0 source files (fewer than 100)*" in shown
    opencode.prepare(task, "deepseek/deepseek-v4-flash", ["true"])
    assert sum(e["type"] == "serena" for e in task.events()) == 1, "said once, not at every start"


def test_the_view_says_your_opencode_configuration_can_be_brought_over(env, monkeypatch):
    config = env / "xdg" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"provider": {"acme": {"options": {"baseURL": "https://x"}, "models": {"m": {}}}}}')
    said = []

    async def scenario(app, pilot):
        monkeypatch.setattr(app, "notify", lambda text, **kw: said.append(text))
        app.hint_opencode()

    run(scenario)
    assert any("Press k to bring its providers over" in t for t in said)


def test_a_file_found_by_browsing_goes_on_to_the_import(env, tmp_path):
    """The whole way, not the browser alone: a file picked there is the import's source. Returning
    dismiss() from the browser's callback made Textual await it inside a message handler."""
    from textual.widgets import DirectoryTree

    good = tmp_path / "opencode.json"
    good.write_text('{"provider": {"acme": {"models": {"m": {}}}}}')
    chosen = []

    async def scenario(app, pilot):
        app.push_screen(tui.ImportSource([]), chosen.append)
        await pilot.pause()
        await pilot.press("enter")  # Browse…
        await pilot.pause()
        browser = app.screen
        assert isinstance(browser, tui.Browse)
        browser.post_message(DirectoryTree.FileSelected(browser.query_one("#tree", tui.PathTree).root, good))
        await pilot.pause()
        await pilot.pause()

    run(scenario)
    assert chosen == [good]


def test_the_import_from_adding_a_provider_comes_back_with_what_came(env, tmp_path, monkeypatch):
    source = env / "xdg" / "opencode" / "opencode.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"provider": {"acme": {"models": {"m": {}}}}}')
    added = []

    async def scenario(app, pilot):
        app.push_screen(tui.AddProvider([]), added.append)
        await pilot.pause()
        app.screen.query_one("#import").press()
        await pilot.pause()
        await pilot.press("enter")  # the configuration found
        await pilot.pause()
        app.screen.query_one("#import").press()  # import what is ticked
        await pilot.pause()
        await pilot.pause()

    run(scenario)
    assert added == [["acme"]]


@pytest.mark.parametrize("height", [20, 24, 30, 50])
def test_a_new_task_can_be_filled_in_on_a_short_terminal(env, height):
    """Every field and button reachable however few rows the terminal has: the fields scroll, the
    buttons stay in view. On a laptop's terminal they had been cut off below the screen."""

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        for wid in ("#project", "#goal", "#plan", "#role-planner", "#role-writer", "#create", "#cancel"):
            widget = app.screen.query_one(wid)
            widget.focus()
            await pilot.pause()
            await pilot.pause()
            r = widget.region
            assert r.height > 0 and r.y >= 1 and r.y + r.height <= height - 1, f"{wid} out of view: {r}"
        buttons = app.screen.query_one("#create").region
        writer = app.screen.query_one("#role-writer").region
        assert not buttons.overlaps(writer), "nothing hidden behind the buttons"

    run(scenario, size=(100, height))


def with_code(name: str) -> None:
    """A file committed in the project, so it is not empty and the kind of task is asked."""
    repo = load_project(name).repo
    (repo / "app.py").write_text("print('hi')\n")
    for args in (["add", "app.py"], ["commit", "-qm", "Add app"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.mark.parametrize("size", [(146, 38), (100, 30), (80, 24)])
def test_the_new_task_dialog_shows_every_field_at_once(env, size):
    """A form (§4): labels on the left, every field on the screen without scrolling, the dialog no
    wider than the terminal. On a short terminal the description gives way, down to one line."""
    from ux import screen_text

    second_project(env)
    with_code("demo")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        dialog = app.screen
        assert dialog.query_one(tui.Fields).max_scroll_y == 0, "nothing to scroll: every field is in view"
        assert dialog.query_one(".dialog").region.width <= size[0], "the dialog fits the terminal"
        shown = screen_text(app)
        for word in ("Project", "Kind", "Task", "Attach…", "@path", "Plan", "Planner", "Writer", "Create",
                     "Cancel", "ctrl+s"):  # fmt: skip
            assert word in shown, f"{word} not on the screen at {size}"
        assert dialog.query_one("#goal").region.height >= 3, "room for at least a line of the description"

    run(scenario, size=size)


@pytest.mark.parametrize(("size", "apart"), [((146, 38), 2), ((100, 30), 2), ((80, 24), 1)])
def test_lists_in_a_group_stand_a_row_apart_unless_the_terminal_is_short(env, size, apart):
    """A blank row between the lists of a group keeps them from reading as one block (§4). On a
    short terminal the rows between them go before the description shrinks below three lines."""
    with_code("demo")  # the kind is asked too: the whole form, as on a project with code

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        dialog = app.screen
        planner, writer = dialog.query_one("#role-planner").region, dialog.query_one("#role-writer").region
        assert writer.y - planner.y == apart, f"Planner at {planner.y}, Writer at {writer.y}"
        assert dialog.query_one(tui.Fields).max_scroll_y == 0, "still nothing to scroll"
        assert dialog.query_one("#goal").region.height >= 3

    run(scenario, size=size)


def test_attach_stands_with_the_description_and_create_is_the_only_primary_button(env):
    """Attach fills the description, so it stands under it, in the task's group, not among the
    lists; the buttons that close the dialog are Create and Cancel, and Create alone is primary."""
    from textual.widgets import Button

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        dialog = app.screen
        task = dialog.query_one("#task")
        goal, attach = task.query_one("#goal"), task.query_one("#attach")
        assert attach.region.y >= goal.region.y + goal.region.height, "Attach is under the description"
        assert {s.id for s in task.query(Select)} == {"project", "kind"}, "planning's lists are elsewhere"
        assert [b.id for b in dialog.query(Button) if b.variant == "primary"] == ["create"]
        assert [b.id for b in dialog.query(".buttons Button")] == ["create", "cancel"]

    run(scenario)


def test_the_project_list_ends_with_setting_up_another_project(env):
    """Like "+ add a provider…" in the lists of models: what is not on the list is set up from it."""
    from ux import screen_text

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        project = app.screen.query_one("#project", Select)
        project.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "+ set up another project…" in screen_text(app)
        project.value = tui.NEW_PROJECT
        await pilot.pause()
        assert isinstance(app.screen, NewProject)

    run(scenario)


def test_tab_walks_the_new_task_form_from_the_description_down(env):
    """The description first, as the project comes from the selected row; then down the form, and
    round to the project and the kind."""
    expected = ["goal", "attach", "plan", "role-planner", "role-writer", "create", "cancel",
                "project", "kind", "goal"]  # fmt: skip
    with_code("demo")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        order = []
        for _ in expected:
            order.append(app.focused.id)
            await pilot.press("tab")
            await pilot.pause()
        assert order == expected

    run(scenario)


def test_starting_the_demo_shows_in_the_status_like_a_working_agent(env, monkeypatch):
    """Starting a server takes a while; the row says so, with the spinner, until it is up or not."""
    import threading

    task = new_task()
    release = threading.Event()

    def slow_demo(task_id, ask=True, reply=""):
        release.wait(5)
        return actions.Demo(commands=["npm run dev"], starting=True)

    monkeypatch.setattr(actions, "demo", slow_demo)

    async def scenario(app, pilot):
        worker = app.run_demo(task.id)
        for _ in range(20):
            await pilot.pause(0.05)
            if task.id in app.starting:
                break
        cell = str(app.table.get_cell(task.id, app.status_column))
        assert "starting the demo" in cell and app.busy(task.read_state()), "shown as at work"
        release.set()
        await worker.wait()
        await pilot.pause()
        assert task.id not in app.starting
        assert "starting the demo" not in str(app.table.get_cell(task.id, app.status_column))

    run(scenario)


def test_stopping_and_starting_show_in_the_status_until_done(env, monkeypatch):
    """Taking a pod down or up takes a while; the row says what is going on, with the spinner."""
    import threading

    task = new_task()
    release = threading.Event()

    def slow_stop(t):
        release.wait(5)
        t.set_paused(True)

    def slow_start(task_id, resume=False, on_step=None):
        release.wait(5)
        return "m"

    monkeypatch.setattr(actions, "stop", slow_stop)
    monkeypatch.setattr(actions, "start", slow_start)

    async def scenario(app, pilot):
        for step, doing, after in ((app.stop, "stopping…", "stopped"), (app.start, "starting…", None)):
            release.clear()
            worker = step(task.id)
            for _ in range(20):
                await pilot.pause(0.05)
                if task.id in app.starting:
                    break
            cell = str(app.table.get_cell(task.id, app.status_column))
            assert doing in cell and app.busy(task.read_state()), "shown as at work"
            assert not app.check_action("start_task", ()) and not app.check_action("stop_task", ())
            release.set()
            await worker.wait()
            await pilot.pause()
            cell = str(app.table.get_cell(task.id, app.status_column))
            assert task.id not in app.starting and doing not in cell
            if after:
                assert after in cell

    run(scenario)


def test_starting_says_which_step_it_is_at(env, monkeypatch):
    import threading

    task = new_task()
    release = threading.Event()

    def slow_start(task_id, resume=False, on_step=lambda step: None):
        on_step("starting the pod…")
        release.wait(5)
        return "m"

    monkeypatch.setattr(actions, "start", slow_start)

    async def scenario(app, pilot):
        worker = app.start(task.id)
        for _ in range(20):
            await pilot.pause(0.05)
            if app.starting.get(task.id) == "starting the pod…":
                break
        assert "starting the pod…" in str(app.table.get_cell(task.id, app.status_column))
        release.set()
        await worker.wait()

    run(scenario)


def test_the_view_says_what_it_stopped_to_run_the_app(env, monkeypatch):
    from vivibox.pod import Listener

    task = new_task()
    opened = []
    left = ["python -m http.server 8000 (port 8000)"]
    heard = [Listener(8000, True)]
    result = actions.Demo(["python -m http.server 8000"], "task", "198.51.100.2", heard, stopped=left)
    monkeypatch.setattr(actions, "demo", lambda task_id, ask=True, reply="", wait=40: result)
    monkeypatch.setattr(tui.Vivibox, "open_url", lambda self, url: opened.append(url))

    async def scenario(app, pilot):
        app.reload()
        await app.run_demo(task.id).wait()
        await pilot.pause()
        assert opened == ["http://198.51.100.2:8000"]
        said = [str(n.message) for n in app._notifications]
        assert any(f"Stopped what the agent left running: {left[0]}" in s for s in said)

    run(scenario)


def test_the_view_leaves_at_once_when_a_step_still_waits_on_docker(monkeypatch, capsys):
    """Textual runs thread workers in the loop's default executor, and asyncio waits for them at
    the end: a pod start that hung held the window until Ctrl-C, which showed a traceback. Nothing
    is lost by leaving: the pod and the supervisor are processes of their own."""
    import threading
    import time

    stuck = threading.Event()
    executor = tui.LeavingExecutor()
    executor.submit(stuck.wait, 5)
    began = time.monotonic()
    executor.shutdown(wait=True)
    assert time.monotonic() - began < 1, "asyncio's wait for the executor returns at once"
    assert executor.unfinished() == 1

    left = []
    monkeypatch.setattr(tui.Vivibox, "run", lambda self: setattr(self, "executor", executor))
    monkeypatch.setattr(tui.os, "_exit", lambda code: left.append(code))
    assert tui.run() == 0 and left == [0]
    assert "still finishing in the background" in capsys.readouterr().out
    stuck.set()


def test_a_deleted_task_stays_in_the_history_without_its_files(env, monkeypatch):
    task = new_task("Try the other approach")
    task.event("turn", state="plan", cost=0.2, tokens=1)
    task.event("turn", state="implement", cost=0.05, tokens=1)
    monkeypatch.setattr(actions, "task_pod", lambda task_id: type("P", (), {"remove": lambda self: None})())
    actions.remove(task, load_project("demo"))
    assert not task.root.exists()
    entry = actions.history()[0]
    assert (entry["id"], entry["title"], entry["deleted"]) == ("demo-1", "Try the other approach", "plan")
    assert ui.finished_cost(entry) == "$0.20 + $0.05"
    assert actions.used_numbers(load_project("demo")) == 1, "its number is not given to the next task"
    assert actions.demo_from_history("demo") == ""

    async def scenario(app, pilot):
        await pilot.pause()
        assert "deleted" in str(app.table.get_cell("demo-1", app.status_column))
        await pilot.press("d")
        await pilot.pause()
        assert "### demo-1 · deleted" in app.shown and "its files and its work went with it" in app.shown

    run(scenario)


def test_the_view_starts_over_when_the_last_project_goes(env, monkeypatch):
    task = new_task()

    async def scenario(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        assert not app.panel.has_class("hidden")
        import shutil

        shutil.rmtree(task.root)
        app.reload()
        await pilot.pause()
        assert not app.panel.has_class("hidden"), "the project is still there to look at"
        (env / "config" / "projects" / "demo.toml").unlink()
        app.reload()
        await pilot.pause()
        assert app.panel.has_class("hidden") and not app.check_action("details", ())
        assert app.query_one("#empty").display

    run(scenario)


def test_deleting_a_task_says_what_goes_and_what_stays_and_cancel_comes_first(env, monkeypatch):
    task = new_task("Try the other approach")
    task.event("turn", state="plan", cost=0.2, tokens=1)
    monkeypatch.setattr(actions, "task_pod", lambda task_id: type("P", (), {"remove": lambda self: None})())

    async def scenario(app, pilot):
        await pilot.press("x")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, tui.DeleteTask)
        said = " ".join(str(w.render()) for w in dialog.query(Label))
        assert "Delete demo-1?" in said and "Try the other approach" in said and "$0.20 + $0.00" in said
        assert "Deleted:" in said and "none of its work reaches your repository" in said
        assert "Kept:" in said and "a line in the history" in said
        await pilot.press("enter")  # Cancel has the focus: Enter out of habit deletes nothing
        await pilot.pause()
        assert task.root.exists()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("left", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    assert not task.root.exists() and actions.history()[0]["deleted"] == "plan"


def test_an_imported_serena_is_greyed_and_says_it_comes_with_vivibox(env, tmp_path):
    from vivibox import providers

    path = tmp_path / "opencode.json"
    path.write_text('{"mcp": {"serena": {"type": "local", "command": ["serena", "start-mcp-server"]}}}')
    (found,) = providers.read_opencode(path, env={}).found
    label = tui.import_label(found)
    assert label.startswith("[dim]") and "comes with vivibox; set its mode in Manage" in label


def test_a_folder_is_described_before_you_pick_it(env, tmp_path):
    from vivibox.config import load_project

    empty = tmp_path / "empty"
    empty.mkdir()
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "notes.txt").write_text("x")
    demo = load_project("demo").repo
    assert tui.folder_verdict(empty) == (True, "an empty folder: a new project starts here")
    assert tui.folder_verdict(loose) == (True, "a folder without git: a repository starts here")
    assert tui.folder_verdict(demo) == (True, "already the project demo")


def test_attach_puts_the_picked_file_in_the_description(env, tmp_path, monkeypatch):
    ticket = tmp_path / "ticket.md"
    ticket.write_text("the ticket")
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix it, see ")
        dialog = app.screen
        dialog.query_one("#attach").press()
        await pilot.pause()
        assert isinstance(app.screen, tui.Browse) and app.screen.mode == "any"
        app.screen.dismiss(ticket)
        await pilot.pause()
        assert dialog.query_one("#goal").text == f"Fix it, see @{ticket} "

    run(scenario)


def test_the_folder_browser_opens_with_right_and_picks_with_enter(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "work" / "shop").mkdir(parents=True)
    monkeypatch.setattr(tui, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(tui.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        tree = app.screen.query_one("#tree", tui.PathTree)
        await pilot.press("down")  # work
        await pilot.pause()
        assert tree.cursor_node.data.path == home / "work"
        await pilot.press("right")  # open it
        await pilot.pause()
        await pilot.pause()
        assert tree.cursor_node.is_expanded
        await pilot.press("down", "enter")  # shop
        await pilot.pause()

    run(scenario)
    assert picked == [home / "work" / "shop"]


def test_a_click_marks_a_folder_and_select_or_new_folder_act_on_it(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    (home / "zoo").mkdir()
    monkeypatch.setattr(tui, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(tui.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        browser = app.screen
        tree = browser.query_one("#tree", tui.PathTree)
        await pilot.click("#tree", offset=(8, 1))  # the "work" line
        await pilot.pause()
        assert app.screen is browser, "a click picks nothing"
        assert tree.cursor_node.data.path == home / "work"
        browser.query_one("#new-folder").press()
        await pilot.pause()
        await pilot.press(*"shop", "enter")
        await pilot.pause()

    run(scenario)
    assert picked == [home / "work" / "shop"] and (home / "work" / "shop").is_dir(), "made where you clicked"


def test_select_picks_the_marked_folder(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setattr(tui, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(tui.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        await pilot.click("#tree", offset=(8, 1))
        await pilot.pause()
        app.screen.query_one("#select").press()
        await pilot.pause()

    run(scenario)
    assert picked == [home / "work"]


def test_a_double_click_picks(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    monkeypatch.setattr(tui, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(tui.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        await pilot.click("#tree", offset=(8, 1), times=2)
        await pilot.pause()

    run(scenario)
    assert picked == [home / "work"]


def test_the_panel_shows_times_on_your_clock(env, monkeypatch):
    import time

    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task, load_project("demo").verify)
    st = task.transition(State.IMPLEMENT)
    task.event("gate", passed=False)
    stamp = task.events()[-1]["ts"]
    monkeypatch.setenv("TZ", "Etc/GMT-5")
    time.tzset()
    try:
        assert f"Verification failed at {ui.clock(stamp)}." in detail(task, st, 3)
        assert ui.clock(stamp) != stamp[11:19], "five hours from UTC"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_task_never_started_does_not_claim_it_was_interrupted(env):
    task = new_task()
    shown = detail(task, task.read_state(), 3, running=False)
    assert "Not started yet" in shown and "`s` start" in shown
    assert "goes on from where it was" not in shown
    task.event("started", model="m")
    assert "goes on from where it was" in detail(task, task.read_state(), 3, running=False)


def test_only_a_destructive_question_has_a_red_button(env, monkeypatch):
    task = new_task()
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("s")
        assert app.screen.query_one("#yes").variant == "error", "stopping interrupts work"
        await pilot.press("escape")
        app.push_screen(tui.Confirm("Accept the work?", "Accept"))
        await pilot.pause()
        assert app.screen.query_one("#yes").variant == "primary", "accepting is not a warning"

    run(scenario)


def test_an_empty_reply_stays_open_and_says_so(env):
    task = new_task()
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("r")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, tui.Reply), "nothing was sent, so nothing closed"
        assert any("Write a comment" in str(n.message) for n in app._notifications)
        assert task.read_state().state is State.CHECKPOINT_PLAN
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, tui.Reply), "Escape still leaves"

    run(scenario)


def test_running_the_app_is_called_that(env):
    labels = {b.action: b.description for b in Vivibox.BINDINGS}
    assert labels["demo"] == "Run app" and labels["demo_stop"] == "Stop app"


def test_a_deleted_task_in_the_history_reads_as_words():
    entry = {"id": "demo-1", "project": "demo", "title": "Try it", "cost": 0.1, "planning": 0.0,
             "finished": now(), "deleted": "implement"}  # fmt: skip
    shown = finished_detail(entry)
    assert "Deleted while implementing;" in shown and "Deleted at" not in shown


def implementing(goal="Goal"):
    task = new_task(goal)
    at_plan_checkpoint(task)
    gate.accept_plan(task, load_project("demo").verify)
    task.transition(State.IMPLEMENT)
    task.event("started", model="m")
    return task


def test_a_failed_task_looks_failed_and_s_starts_it_again(env, monkeypatch):
    from ux import screen_text

    stopped = implementing("Stopped by me")
    stopped.set_paused(True)
    failed = implementing("Rate limited")
    failed.set_paused(True, problem="agent turn failed: 429 Too Many Requests")
    # The supervisor outlives its own error, which is what used to put Stop in the footer.
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id == failed.id)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert app.selected_id() == failed.id, "it needs you, so it is on top"
        assert app.sub_title == "1 waiting for you"
        shown = screen_text(app)
        assert "agent turn failed" in shown and "stopped" in shown, "two rows, two different words"
        assert app.check_action("start_task", ()) and not app.check_action("stop_task", ())
        await pilot.press("d")
        await pilot.pause()
        shown = screen_text(app)
        assert "429 Too Many Requests" in shown and "Next: s try again" in shown
        await pilot.press("s")
        await app.workers.wait_for_complete()
        assert actions.started == [failed.id]

    run(scenario)


def test_the_panel_calls_a_task_what_the_list_calls_it(env):
    task = implementing()
    task.set_paused(True)
    assert detail(task, task.read_state(), 3, running=False).startswith(f"### {task.id} · stopped")
    draft = new_task()
    assert detail(draft, draft.read_state(), 3, running=False).startswith(f"### {draft.id} · not started")


def test_accepting_a_plan_starts_a_task_nobody_is_working_on(env):
    task = new_task()
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert task.read_state().state is State.IMPLEMENT
        assert actions.started == [task.id], "after a reboot no supervisor is left to carry on"

    run(scenario)


def test_a_reply_starts_a_task_nobody_is_working_on_and_leaves_a_running_one(env, monkeypatch):
    dead, alive = new_task("Dead"), new_task("Alive")
    for task in (dead, alive):
        at_plan_checkpoint(task)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id == alive.id)

    async def scenario(app, pilot):
        app.reload()
        for task in (dead, alive):
            app.table.move_cursor(row=rows(app).index(task.id))
            await pilot.pause()
            await pilot.press("r")
            await pilot.press(*"Again")
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
        assert actions.started == [dead.id]

    run(scenario)


def test_w_is_only_named_when_there_is_an_agent_to_watch(env):
    task = implementing()
    st = task.read_state()
    assert "`w`" not in detail(task, st, 3, running=True), "no session yet: the footer has no w"
    task.set_session("writer", "ses_1")
    assert "Look at the agent with `w`" in detail(task, task.read_state(), 3, running=True)
    assert "`w`" not in detail(task, task.read_state(), 3, running=False)


def blocked_on_verification(goal="Goal"):
    task = implementing(goal)
    task.transition(State.VERIFY)
    task.event("gate", passed=False)
    (task.meta / "handoff" / "verify-feedback.md").write_text(
        "# Verification failed\n\n- Command failed: `mvn`\n"
    )
    (task.meta / "handoff" / "verify.log").write_text("[ERROR] boom\n")
    task.transition(State.CHECKPOINT_BLOCKED, reason="verification still failing")
    return task


def test_g_verifies_a_blocked_task_again_and_is_offered_only_there(env):
    task = blocked_on_verification()
    other = new_task()
    at_plan_checkpoint(other)

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(other.id))
        await pilot.pause()
        assert not app.check_action("verify_again", ()), "nothing to verify at the plan"
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        assert app.check_action("verify_again", ())
        await pilot.press("g")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert task.read_state().state is State.VERIFY
        assert actions.started == [task.id], "nobody was running it"
        assert any("Verifying" in str(n.message) for n in app._notifications)

    run(scenario)


def test_g_is_offered_on_a_blocked_task_with_a_question_too(env):
    """The agent asks about the environment more often than the gate recognises one: once you
    have fixed what it names, g verifies again without a turn, and the question is put away."""
    task = blocked_on_verification()
    (task.meta / "handoff" / "question.md").write_text("The image cannot be pulled: x509\n")
    shown = detail(task, task.read_state(), 3, running=False)
    next_line = next(line for line in shown.splitlines() if line.startswith("**Next:**"))
    assert "`r` answer" in next_line and "`g` verify again" in next_line

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        assert app.check_action("verify_again", ())
        await pilot.press("g")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert task.read_state().state is State.VERIFY
        assert not (task.meta / "handoff" / "question.md").exists()

    run(scenario)


def test_the_next_step_comes_first_in_the_panel(env):
    task = blocked_on_verification()
    shown = detail(task, task.read_state(), 3, running=True)
    lines = [line for line in shown.splitlines() if line.strip()]
    assert lines[2].startswith("**Next:**"), lines[:3]
    assert "`g` verify again" in lines[2] and "`r`" in lines[2]
    assert lines[2].index("`g`") < shown.index("What the build said")


def test_w_looks_at_the_verification_once_its_log_is_there(env, monkeypatch):
    """Not at the agent: nothing happens in its conversation while the gate builds."""
    task = implementing()
    task.set_session("writer", "ses_1")
    task.transition(State.VERIFY)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    shown = detail(task, task.read_state(), 3, running=True)
    assert "`w`" not in shown and "wait for the verification" in shown, "no log yet: nothing to look at"
    (task.meta / "log" / "verify-1-120000.log").write_text("# fresh clone of commit abc\n")
    shown = detail(task, task.read_state(), 3, running=True)
    next_line = next(line for line in shown.splitlines() if line.startswith("**Next:**"))
    assert "`w` look at it as it runs" in next_line and "`l`" in next_line
    assert "Look at the agent" not in shown

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert app.check_action("watch", ())

    run(scenario)


def test_w_asks_which_conversation_when_the_task_has_two(env, monkeypatch):
    """The planner's conversation stays readable once the writer is at work; with both there, you
    pick. With one, w opens it without asking."""
    task = implementing()
    task.set_session("writer", "ses_w")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    watched = []
    monkeypatch.setattr(tui.Vivibox, "watch", lambda self, task_id, role="": watched.append((task_id, role)))

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert watched == [(task.id, "")], "one conversation: no question"
        task.set_session("planner", "ses_p")
        app.reload()
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, tui.ChooseSession)
        options = app.screen.query_one(OptionList)
        shown = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert "writer" in shown[0] and "at work" in shown[0], "the one at work first"
        assert "planner" in shown[1] and "finished" in shown[1]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert watched[-1] == (task.id, "planner")

    run(scenario)


def test_the_farewell_tmux_prints_is_wiped_when_its_session_ended(capsys):
    """tmux says [exited] when the session ends under the client, and that line stays on the
    terminal you come back to after quitting vivibox."""
    tui.after_window(session_gone=True)
    assert capsys.readouterr().out == "\x1b[1A\x1b[2K"
    tui.after_window(session_gone=False)
    assert capsys.readouterr().out == ""


def test_a_running_verification_is_shown_as_running(env, monkeypatch):
    task = implementing()
    task.transition(State.VERIFY)
    log = task.meta / "log" / "verify-1-120000.log"
    log.write_text(
        "# fresh clone of commit abc\n\n$ mvn -B verify\n[INFO] Scanning\n[INFO] Compiling 12 files\n"
    )
    shown = detail(task, task.read_state(), 3, running=True)
    assert "**Verification running**" in shown and "`mvn -B verify`" in shown
    assert "[INFO] Compiling 12 files" in shown and "No verification yet" not in shown
    assert f"`{log}`" in shown, "where the whole log is"


def test_l_opens_the_newest_verification_log_in_the_pager(env, monkeypatch):
    # The pager takes the terminal over (App.suspend), which Pilot cannot do; the command is tested.
    task = blocked_on_verification()
    (task.meta / "log" / "verify-1-120000.log").write_text("old\n")
    newest = task.meta / "log" / "verify-2-130000.log"
    newest.write_text("new\n")
    monkeypatch.setenv("PAGER", "less -R")
    assert tui.newest_log(task) == newest
    assert tui.pager_command(newest) == ["less", "-R", str(newest)]
    # Where the trouble is: at the end. While the verification runs: following it as it is written.
    assert tui.pager_command(newest, at_end=True) == ["less", "-R", "+G", str(newest)]
    assert tui.pager_command(newest, follow=True) == ["less", "-R", "+F", str(newest)]
    monkeypatch.setenv("PAGER", "more")
    assert tui.pager_command(newest, follow=True) == ["more", str(newest)], "only less knows the flags"
    monkeypatch.setenv("PAGER", "less -R")
    assert tui.log_command(task, task.read_state(), running=False) == ["less", "-R", "+G", str(newest)]
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    assert tui.log_command(task, task.read_state(), running=True) == ["less", "-R", "+F", str(newest)]
    assert tui.log_command(task, task.read_state(), running=False)[2] == "+G", "nothing is being written"

    async def scenario(app, pilot):
        app.reload()
        assert app.check_action("show_log", ())

    run(scenario)


def test_l_is_offered_only_when_there_is_a_log(env):
    task = new_task()

    async def scenario(app, pilot):
        app.reload()
        assert not app.check_action("show_log", ())
        (task.meta / "log" / "supervisor.log").write_text("Supervising\n")
        assert app.check_action("show_log", ())

    run(scenario)


def test_the_view_says_when_vivibox_changed_on_disk(env, monkeypatch):
    from vivibox import code

    task = new_task()
    now = ["v1"]
    monkeypatch.setattr(code, "signature", lambda: now[0])

    async def scenario(app, pilot):
        app.reload()
        assert "changed on disk" not in app.sub_title
        now[0] = "v2"
        app.code_checked = 0.0  # the view looks every ten seconds; the test does not wait
        app.reload()
        await pilot.pause()
        assert "vivibox changed on disk: quit and start it again" in app.sub_title
        assert sum("changed on disk" in str(n.message) for n in app._notifications) == 1
        app.reload()
        assert sum("changed on disk" in str(n.message) for n in app._notifications) == 1, "said once"

    run(scenario)
    assert task.root.exists()


def test_the_panel_says_when_a_supervisor_runs_older_code(env, monkeypatch):
    from vivibox import code

    task = implementing()
    (task.meta / code.RECORD).write_text("v1\n")
    monkeypatch.setattr(code, "signature", lambda: "v2")
    shown = detail(task, task.read_state(), 3, running=True)
    assert "runs an older vivibox" in shown and "`s`" in shown
    assert "older vivibox" not in detail(task, task.read_state(), 3, running=False), "nothing runs"
    monkeypatch.setattr(code, "signature", lambda: "v1")
    assert "older vivibox" not in detail(task, task.read_state(), 3, running=True)


# --- project rows -------------------------------------------------------------------------------


def rows(app) -> list[str]:
    """The list's rows by key: a project's name, or a task's id."""
    return [str(key.value).removeprefix(tui.PROJECT_ROW) for key in app.table.rows]


def cell(app, row: int, column: str) -> str:
    """A cell by the column's name: which columns there are depends on the terminal's width."""
    names = [c.label.plain for c in app.table.columns.values()]
    return str(app.table.get_cell_at((row, names.index(column))))


def second_project(env, name="shop"):
    from conftest import make_repo

    repo = make_repo(env / name)
    (env / "config" / "projects" / f"{name}.toml").write_text(f'repo = "{repo}"\nverify = ["true"]\n')
    return repo


def test_tasks_are_listed_under_their_project_and_the_first_waiting_one_is_selected(env, monkeypatch):
    from ux import screen_text

    second_project(env)
    working = implementing("Working on it")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id == working.id)
    waiting = new_task("Waiting for you")
    at_plan_checkpoint(waiting)
    assert main(["new", "shop", "Shop task", "--draft"]) == 0

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo", waiting.id, working.id, "shop", "shop-1"], "projects, tasks under them"
        assert app.selected_id() == waiting.id, "the cursor starts on the first task waiting for you"
        shown = screen_text(app)
        assert "1 waiting for you · 1 working" in shown, "the project row sums up its tasks"
        assert "not started" in shown

    run(scenario)


def test_a_project_with_no_tasks_is_a_row_that_says_so(env):
    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo"] and app.table.display
        assert "no tasks · n creates one" in cell(app, 0, "GOAL")
        assert app.check_action("new", ()) and app.check_action("details", ())
        await pilot.press("d")
        await pilot.pause()
        assert "demo" in app.shown and "verify" in app.shown, "the project's file, in the panel"

    run(scenario)


def test_a_collapsed_project_keeps_saying_what_waits_and_stays_collapsed(env):
    waiting = new_task("Waiting")
    at_plan_checkpoint(waiting)
    new_task("Other")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        assert rows(app) == ["demo"], "collapsed: its tasks are folded away"
        assert "2 waiting for you" in cell(app, 0, "STATUS"), "but not out of sight"
        assert app.sub_title.startswith("2 waiting for you")
        app.reload()
        assert rows(app) == ["demo"], "a refresh does not unfold it"
        await pilot.press("enter")
        await pilot.pause()
        assert rows(app) == ["demo", waiting.id, "demo-2"]

    run(scenario)
    assert tui.load_collapsed() == set()


def test_collapsing_is_remembered_across_views(env):
    new_task("Task")

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()

    run(scenario)

    async def again(app, pilot):
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo"]

    run(again)


def test_a_project_row_offers_project_actions_and_a_task_row_task_actions(env, monkeypatch):
    task = new_task("Task")
    at_plan_checkpoint(task)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert app.selected_id() == task.id
        assert app.check_action("accept", ()) and app.check_action("edit_plan", ())
        assert not app.check_action("edit_project", ()) and not app.check_action("forget_project", ())
        app.table.move_cursor(row=0)
        await pilot.pause()
        assert app.selected_project() == "demo"
        assert not app.check_action("accept", ()) and not app.check_action("edit_plan", ())
        assert app.check_action("edit_project", ()) and app.check_action("new", ())
        assert not app.check_action("forget_project", ()), "it has a task; delete that first"
        # The editor takes the terminal over (App.suspend), which Pilot cannot do; the file is checked.
        assert app.project_file() == env / "config" / "projects" / "demo.toml"

    run(scenario)


def test_n_on_a_project_row_starts_a_task_in_that_project(env):
    second_project(env)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=1)
        await pilot.pause()
        assert app.selected_project() == "shop"
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, tui.NewTask)
        assert app.screen.query_one("#project", Select).value == "shop"

    run(scenario)


def test_a_project_without_tasks_can_be_forgotten_from_its_row(env):
    second_project(env)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=1)
        await pilot.pause()
        assert app.check_action("forget_project", ())
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, tui.DeleteTask)
        await pilot.press("left", "enter")  # Cancel has the focus; left is Forget
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert projects() == ["demo"]
        assert (env / "shop").exists(), "the repository stays"

    run(scenario)


def test_a_project_row_says_when_a_variable_it_passes_is_missing(env, monkeypatch):
    project = env / "config" / "projects" / "demo.toml"
    project.write_text(project.read_text() + 'pass_env = ["REPO_TOKEN"]\n')
    monkeypatch.delenv("REPO_TOKEN", raising=False)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert "REPO_TOKEN not set in this shell" in cell(app, 0, "STATUS")
        monkeypatch.setenv("REPO_TOKEN", "x")
        app.reload()
        assert "not set" not in cell(app, 0, "STATUS")

    run(scenario)


def test_finished_tasks_sit_under_their_project(env):
    task = new_task("Task")
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    actions.history_path().write_text(
        '{"id": "demo-0", "project": "demo", "title": "Old one", "cost": 0.1, "commit": "abc", "branch": "",'
        ' "conflicts": [], "finished": "' + now() + '"}\n'
    )

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo", task.id, "demo-0"]
        await pilot.press("h")
        await pilot.pause()
        assert rows(app) == ["demo", task.id]

    run(scenario)


# --- the footer, help, and a narrow terminal ---------------------------------------------------


def test_the_footer_shows_decisions_first_and_keeps_the_rest_under_help(env):
    task = blocked_on_verification()

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        shown = [str(key.key_display) for key in app.query(FooterKey)]
        assert shown[:2] == ["r", "g"], "your decisions come first"
        for hidden in ("i", "h", "k"):
            assert hidden not in shown, f"{hidden} is under ? Help"
        assert shown.index("?") == shown.index("q") - 1
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, tui.Help)
        text = str(app.screen.query_one("#help").render())
        for key, what in (
            ("i", "set up a project"),
            ("k", "Providers & MCP"),
            ("h", "finished"),
            ("g", "verif"),
        ):
            assert key in text and what.lower() in text.lower()

    run(scenario)


def test_the_project_summary_says_what_waits_but_does_not_widen_status(env):
    waiting = new_task("Waiting")
    at_plan_checkpoint(waiting)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert "1 waiting for you" in cell(app, 0, "GOAL") and cell(app, 0, "STATUS").strip() == ""
        app.table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        assert "1 waiting for you" in cell(app, 0, "STATUS"), "collapsed: the row says it"

    run(scenario)


def test_a_narrow_terminal_shows_task_status_and_goal(env):
    task = new_task("Reject expired cards at checkout, and log every rejection with its reason")

    async def scenario(app, pilot):
        from ux import screen_text

        app.reload()
        await pilot.pause()
        shown = screen_text(app)
        assert [c.label.plain for c in app.table.columns.values()] == ["TASK", "STATUS", "GOAL"]
        assert "Reject expired cards" in shown and "not started" in shown
        assert "…" in cell(app, 1, "GOAL"), "the goal is cut to fit, not scrolled off"
        for key in ("a", "r", "p", "g"):
            assert key in tui.DECISION_KEYS
        assert task.id in shown

    run(scenario, size=(80, 24))


def test_a_wide_terminal_has_every_column(env):
    new_task()

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert len(app.table.columns) == 8

    run(scenario, size=(140, 40))


def test_the_new_task_dialog_asks_one_question_about_the_plan(env, monkeypatch):
    created = []
    monkeypatch.setattr(actions, "create", lambda project, goal, **kw: created.append(kw) or new_task(goal))

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        plan = app.screen.query_one("#plan", Select)
        assert plan.value == "review", "stopping for your review is the default"
        assert not app.screen.query("#auto") and not app.screen.query("#draft"), "one question, not two boxes"
        plan.value = "auto"
        app.screen.query_one("#goal", TextArea).text = "Goal"
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert created and created[0]["auto"] is True

    run(scenario)


def test_stopping_at_a_checkpoint_is_called_stopping_the_pod(env, monkeypatch):
    task = new_task()
    at_plan_checkpoint(task)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert app.check_action("stop_pod", ()) and not app.check_action("stop_task", ())
        assert "stop_pod" in keys(app) and "stop_task" not in keys(app)

    run(scenario)


def test_at_suggests_the_projects_files_and_the_view_notes_uncommitted_ones(env, tmp_path, monkeypatch):
    import subprocess

    repo = env / "repo"
    (repo / "src").mkdir()
    (repo / "src" / "Order.java").write_text("1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Add"], cwd=repo, check=True)
    (repo / "src" / "Order.java").write_text("2\n")
    monkeypatch.chdir(tmp_path)

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix @sr")
        suggestions = app.screen.query_one("#suggestions")
        assert suggestions.display and suggestions.get_option_at_index(0).prompt == "src/"
        await pilot.press("tab", "enter")
        assert app.screen.query_one("#goal").text == "Fix @src/Order.java "
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any("uncommitted changes" in str(n.message) for n in app._notifications)

    run(scenario)
    task = find_task(load_config().tasks_dir, "demo-1")
    assert f"{task.repo}/src/Order.java" in task.plan_path.read_text()
    assert "uncommitted changes" in detail(task, task.read_state(), 3, running=False), "kept with the task"


def test_at_finds_a_file_deep_in_the_projects_tree(env, tmp_path, monkeypatch):
    orders = env / "repo" / "src" / "main" / "java" / "com" / "acme" / "orders"
    orders.mkdir(parents=True)
    (orders / "OrderService.java").write_text("class OrderService {}\n")
    subprocess.run(["git", "add", "."], cwd=env / "repo", check=True)
    subprocess.run(["git", "commit", "-qm", "Add"], cwd=env / "repo", check=True)
    monkeypatch.chdir(tmp_path)

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Fix @OrderSer")
        suggestions = app.screen.query_one("#suggestions")
        assert suggestions.display
        assert suggestions.get_option_at_index(0).prompt == "src/main/java/com/acme/orders/OrderService.java"
        await pilot.press("enter")
        assert app.screen.query_one("#goal").text == "Fix @src/main/java/com/acme/orders/OrderService.java "

    run(scenario)


# --- reviewing: the diff, the plan, the archive --------------------------------------------------


def final(goal="Goal"):
    task = implementing(goal)
    (task.repo / "Health.java").write_text("class Health {}\n")
    for args in (
        ["add", "Health.java"],
        ["-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add"],
    ):
        subprocess.run(["git", *args], cwd=task.repo, check=True, capture_output=True)
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)
    actions.prepare_review(task, load_project("demo"))
    return task


def test_f_shows_the_work_as_a_diff_and_is_offered_when_the_work_is_ready(env):
    task = final()
    other = new_task()

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(other.id))
        await pilot.pause()
        assert not app.check_action("show_diff", ())
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        assert app.check_action("show_diff", ())
        # git pages by itself and takes the terminal over (App.suspend); the command is tested.
        base = task.read_state().base_commit
        assert app.diff_command() == [
            "git", "-C", str(load_project("demo").repo), "diff", f"{base}...refs/vivibox/{task.id}"
        ]  # fmt: skip
        assert "`f`" in tui.detail(task, task.read_state(), 3, running=True)

    run(scenario)
    command = [*tui.git_diff(task, load_project("demo")), "--stat"]
    out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    assert "Health.java" in out, command


def test_the_plan_stays_readable_after_it_is_accepted(env):
    task = implementing()
    plan = task.meta / gate.ACCEPTED_PLAN
    plan.write_text(
        plan.read_text().replace(
            "## Acceptance criteria", "## Approach\n\nUse a filter.\n\n## Acceptance criteria"
        )
    )
    shown = detail(task, task.read_state(), 3, running=True)
    assert "Use a filter." in shown and shown.index("Acceptance criteria") < shown.index("Use a filter.")


def test_a_finished_task_shows_its_plan_from_the_archive(env):
    entry = {"id": "demo-1", "project": "demo", "title": "Add health", "cost": 0.02, "commit": "abc1234567",
             "branch": "", "conflicts": [], "criteria": ["it works"], "finished": now()}  # fmt: skip
    kept = actions.archive_path("demo-1")
    kept.mkdir(parents=True)
    (kept / "plan.accepted.md").write_text(
        '+++\nkind = "feature"\n+++\n\n# Goal\n\nAdd health\n\n## Approach\n\nA filter.\n'
    )
    shown = finished_detail(entry)
    assert "A filter." in shown and str(kept) in shown
    assert "Press `x` to delete it from the history" in shown


def test_forgetting_a_finished_task_says_the_archive_goes_too(env):
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    actions.history_path().write_text(
        '{"id": "demo-0", "project": "demo", "title": "Old", "cost": 0.1, "commit": "abc", "branch": "",'
        ' "conflicts": [], "finished": "' + now() + '"}\n'
    )
    kept = actions.archive_path("demo-0")
    kept.mkdir(parents=True)
    (kept / "events.jsonl").write_text("")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=rows(app).index("demo-0"))
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, tui.DeleteTask)
        assert "archive" in str(app.screen.goes)
        await pilot.press("left", "enter")
        await pilot.pause()
        assert not kept.exists()

    run(scenario)


# --- after a reboot, and when a task starts to wait ---------------------------------------------


def test_the_view_offers_to_start_the_tasks_that_were_running_before(env, monkeypatch):
    dead = implementing("Was running")
    parked = implementing("Stopped by me")
    parked.set_paused(True)
    draft = new_task("Never started")

    async def scenario(app, pilot):
        await pilot.pause()
        assert isinstance(app.screen, tui.Confirm), "asked once, on start"
        assert dead.id in app.screen.question and parked.id not in app.screen.question
        assert draft.id not in app.screen.question
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert actions.started == [dead.id]

    run(scenario)


def test_the_view_does_not_ask_when_nothing_was_running(env):
    new_task("Never started")

    async def scenario(app, pilot):
        await pilot.pause()
        assert not isinstance(app.screen, tui.Confirm)

    run(scenario)


def test_a_task_that_starts_to_wait_rings_the_bell_and_counts_in_the_title(env, monkeypatch):
    task = implementing()
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    rang = []

    async def scenario(app, pilot):
        monkeypatch.setattr(app, "bell", lambda: rang.append(1))
        app.reload()
        await pilot.pause()
        assert app.title == "vivibox" and not rang
        task.transition(State.VERIFY)
        task.transition(State.CHECKPOINT_FINAL)
        app.reload()
        await pilot.pause()
        assert rang == [1] and app.title == "vivibox (1)"
        app.reload()
        assert rang == [1], "once per task that starts to wait, not every refresh"

    run(scenario)


def test_a_verification_the_environment_stopped_says_so_and_offers_g_first(env):
    task = implementing("Goal")
    task.transition(State.VERIFY)
    task.event("gate", passed=False, environment="Cannot connect to the Docker daemon")
    (task.meta / "handoff" / "verify-feedback.md").write_text(
        "# Verification failed\n\n- Verification could not run: Cannot connect to the Docker daemon.\n"
    )
    task.transition(State.CHECKPOINT_BLOCKED, reason="verification could not run")
    shown = detail(task, task.read_state(), 3, running=True)
    lines = [line for line in shown.splitlines() if line.strip()]
    assert lines[2].startswith("**Next:**") and lines[2].index("`g`") < lines[2].index("`r`")
    assert "Verification could not run" in shown and "Cannot connect to the Docker daemon" in shown
    assert "keeps failing" not in shown


def test_removed_tests_are_shown_with_the_work(env):
    task = implementing("Goal")
    task.transition(State.VERIFY)
    task.event(
        "gate",
        passed=True,
        removed_tests=2,
        removed=["test/a.test.js: test('x')", "test/a.test.js: test('y')"],
    )
    task.transition(State.CHECKPOINT_FINAL)
    shown = detail(task, task.read_state(), 3, running=True)
    assert "2 tests removed" in shown and "test('x')" in shown


def test_the_plans_verify_command_is_shown_before_you_accept_it(env):
    """For a new project the plan's command becomes the project's for good once you accept, so
    you see it where you decide."""
    (Path(load_config().tasks_dir).parent / "config" / "projects" / "fresh.toml").write_text(
        f'repo = "{env / "repo"}"\nverify = []\n'
    )
    assert main(["new", "fresh", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "fresh-1")
    at_plan_checkpoint(task)
    task.plan_path.write_text(task.plan_path.read_text().replace("verify = []", 'verify = ["npm test"]'))
    shown = detail(task, task.read_state(), 3, running=True)
    assert "`npm test`" in shown and "becomes the project's" in shown
    demo = new_task()
    at_plan_checkpoint(demo)
    assert "becomes the project's" not in detail(demo, demo.read_state(), 3, running=True), "demo has its own"


# --- the box ---------------------------------------------------------------------------------


@pytest.fixture
def box_env(env, monkeypatch):
    monkeypatch.setattr(actions, "start_box", lambda task_id: None)
    monkeypatch.setattr(actions, "box_shell_command", lambda task_id: ["true"])
    return env


def test_b_on_a_project_row_opens_a_box_and_w_enters_it(box_env):
    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=0)
        await pilot.pause()
        assert app.check_action("new_box", ())
        await pilot.press("b")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert rows(app) == ["demo", "demo-1"] and app.selected_id() == "demo-1"
        assert "box open" in cell(app, 1, "STATUS") and "⠼" not in cell(app, 1, "STATUS"), (
            "no spinner: nothing runs"
        )
        assert app.check_action("enter_box", ()) and app.check_action("accept", ())
        assert not app.check_action("reply", ()) and not app.check_action("new_box", ())
        assert not app.check_action("watch", ()) and not app.check_action("edit_plan", ())
        shown = detail(*app.selected(), 3, running=False)
        assert "`w`" in shown and "`a`" in shown and "Box" in shown

    run(scenario)


def test_a_on_an_open_box_closes_it_for_review(box_env, monkeypatch):
    monkeypatch.setattr(actions, "commit_in_box", lambda task: None)
    task = actions.open_box("demo")
    (task.repo / "idea.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=task.repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Idea"],
        cwd=task.repo,
        check=True,
    )

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert task.read_state().state is State.CHECKPOINT_FINAL
        assert app.check_action("show_diff", ()) and app.check_action("accept", ())

    run(scenario)


def test_the_panel_says_how_long_the_agent_has_been_implementing(env):
    from datetime import UTC, datetime, timedelta

    task = implementing()
    st = task.read_state()
    st.updated = (datetime.now(UTC) - timedelta(minutes=7)).isoformat(timespec="milliseconds")
    task._write_state(st)
    shown = detail(task, task.read_state(), 3, running=True)
    assert "**Implementing** for 7 min" in shown
