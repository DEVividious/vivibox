import asyncio
import contextlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_repo
from rich.text import Text
from textual.widgets import Checkbox, Input, Label, OptionList, Select, SelectionList, TextArea
from textual.widgets._footer import FooterKey

from vivibox import (
    actions,
    app_support,
    box,
    browse,
    dialogs,
    gate,
    look,
    newtask,
    panel,
    providers_ui,
    settings,
    tui,
    ui,
    widgets,
)
from vivibox.cli import main
from vivibox.config import ConfigError, Role, load_config, load_project
from vivibox.dialogs import CommitWork, NewProject
from vivibox.panel import detail, finished_detail, projects
from vivibox.probe import Listener
from vivibox.states import State
from vivibox.task import find_task, now
from vivibox.tui import Vivibox
from vivibox.verify_ui import AskVerify

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


def run(scenario, size=(140, 40), *, notifications=False):
    """A scenario on the view, once the view's own start-up work is over: the thread that reads
    the models and the provider catalog at mount would otherwise finish during the scenario and
    overwrite what it set (app.available, app.catalog), as it did on a slow CI runner."""

    async def go():
        app = Vivibox()
        async with app.run_test(size=size, notifications=notifications) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(go())


def test_columns_follow_each_terminal_resize(env):
    from ux import screen_text

    new_task("Resize task")

    async def scenario(app, pilot):
        for width in (80, 160, 70, 200):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            visible = screen_text(app)
            assert ("CRITERIA" in visible) == (width >= 100)
            assert ("CREATED" in visible) == (width >= 130)
            assert "Resize task" in visible

    run(scenario, size=(80, 40))


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


def test_a_projects_tasks_keep_their_place_newest_first_whatever_they_do(env, monkeypatch):
    """A row that moves when its task changes state is a row you lose, or press a key on by
    mistake: the order is the tasks' numbers, newest first, the finished ones under the live ones.
    What waits for you says so by its colour, the project's count and where the cursor starts."""
    tasks = [new_task(f"Task {n}") for n in range(1, 11)]
    for task in tasks:
        task.set_paused(True)  # stopped, by you: nothing of theirs waits
    at_plan_checkpoint(tasks[2])
    tasks[2].set_paused(False)
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    kept = {"project": "demo", "cost": 0.1, "commit": "abc", "branch": "", "conflicts": [], "finished": now()}
    actions.history_path().write_text(
        json.dumps({"id": "demo-12", "title": "Accepted later", **kept})
        + "\n"
        + json.dumps({"id": "demo-11", "title": "Accepted last", **kept})
        + "\n"
    )
    live = [f"demo-{n}" for n in range(10, 0, -1)]

    async def scenario(app, pilot):
        app.reload()
        assert rows(app) == ["demo", *live, "demo-12", "demo-11"], "by number: demo-10 before demo-9"
        assert app.selected_id() == "demo-3", "the cursor starts on what waits for you"
        at_plan_checkpoint(tasks[6])
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo", *live, "demo-12", "demo-11"], "a task that starts to wait stays put"

    run(scenario)


def test_prepare_is_one_line_saved_with_enter_and_its_words_are_all_on_the_screen(env):
    """Its question and its hint used to end at the dialog's edge, "(ctrl+s saves)" with them, so
    nothing said how to save; Enter saves, like the other one-line rows."""
    from ux import screen_text

    project = env / "config" / "projects" / "demo.toml"
    project.write_text(project.read_text() + 'prepare = ["npm ci", "npm run build"]\n')

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("demo"))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, settings.Ask)
        assert app.screen.query_one(Input).value == "npm ci && npm run build"
        shown = " ".join(screen_text(app).replace("█", " ").replace("│", " ").split())
        assert "the writer's first turn waits for it" in shown and "Empty: nothing." in shown
        app.screen.query_one(Input).value = "bash mvnw -B install -DskipTests"
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("demo").prepare == ["bash mvnw -B install -DskipTests"]
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("demo").prepare == []

    run(scenario, size=(80, 24))


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


def test_a_plan_that_names_a_command_settles_nothing_for_the_project(env):
    """How the project is built is the writer's to propose and yours to accept on its own, after
    the work: accepting the plan asks nothing about it and keeps nothing."""
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
        assert not isinstance(app.screen, widgets.Confirm)
        assert task.read_state().state is State.IMPLEMENT

    run(scenario)
    assert load_project("clicker").verify == []


def test_a_plan_without_a_build_is_accepted_without_a_word_about_the_project(env):
    """Nothing is settled for the project, so there is nothing to confirm."""
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
        assert not isinstance(app.screen, widgets.Confirm)
        assert task.read_state().state is State.IMPLEMENT

    run(scenario)
    assert not load_project("notes").no_build and load_project("notes").verify == []


def test_e_asks_how_a_project_is_verified_as_one_line_or_the_writers_proposal(env, monkeypatch):
    """One field with the command as it is now, Enter saves it, as prepare does; the box leaves it
    to the next task's writer, who proposes the command it ran. No list of guesses."""
    from vivibox.config import load_project
    from vivibox.verify_ui import AskVerify

    fresh_project(env, "notes")
    (env / "notes" / "package.json").write_text("{}")

    async def open_it(app, pilot):
        app.table.move_cursor(row=rows(app).index("notes"))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, settings.ProjectSettings)
        await pilot.press("down", "enter")  # the second row: the verification
        await pilot.pause()
        assert isinstance(app.screen, AskVerify)

    async def scenario(app, pilot):
        app.reload()
        await open_it(app, pilot)
        assert app.screen.query_one(Input).value == "", "nothing guessed from package.json"
        assert app.screen.query_one(Checkbox).value, "empty: the writer proposes it"
        app.screen.query_one(Input).value = "npm ci && npm run check"
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("notes").verify == ["npm ci && npm run check"]
        assert isinstance(app.screen, settings.ProjectSettings), "back on the project's screen"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one(Input).value == "npm ci && npm run check"
        assert not app.screen.query_one(Checkbox).value
        app.screen.query_one(Checkbox).value = True
        await pilot.pause()
        assert load_project("notes").verify == [] and not load_project("notes").no_build
        assert isinstance(app.screen, settings.ProjectSettings), "the box is taken at once"

    run(scenario)
    path = env / "config" / "projects" / "notes.toml"
    path.write_text(path.read_text().replace("verify = []", "verify = false"))

    async def no_build(app, pilot):
        app.reload()
        await open_it(app, pilot)
        assert not app.screen.query_one(Checkbox).value
        assert "nothing to build" in " ".join(screen_text(app).replace("█", " ").split())
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("notes").no_build, "Enter on nothing leaves the file as it says"

    from ux import screen_text

    run(no_build)


def test_a_new_project_leaves_its_verification_to_you_or_to_the_first_writer(env, tmp_path, monkeypatch):
    """What the build files and the pipeline name is a note: the pipeline builds with more than
    they say, and a guess that builds the wrong thing passes."""
    from vivibox.config import load_project
    from vivibox.verify_ui import AskVerify

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
        assert str(app.screen.query_one("#verify", Label).render()) == actions.WRITER_PROPOSES
        notes = str(app.screen.query_one("#notes", Label).render())
        assert "mvnw runs: bash mvnw -B verify" in notes and "ci.yml runs" in notes
        app.screen.query_one("#change").press()
        await pilot.pause()
        assert isinstance(app.screen, AskVerify)
        app.screen.query_one(Input).value = "bash mvnw -B verify -Pit -f pom.xml"
        await pilot.press("enter")
        await pilot.pause()
        assert "-Pit -f pom.xml" in str(app.screen.query_one("#verify", Label).render())
        app.screen.query_one("#create").press()
        await pilot.pause()

    run(scenario)
    assert load_project("shop").verify == ["bash mvnw -B verify -Pit -f pom.xml"]


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
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await pilot.press("right", "left", "enter")  # arrows move between the buttons
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, CommitWork)
        from ux import screen_text

        # Where to commit comes first: the task started on main, so a branch of its own is offered.
        branch = app.screen.query_one("#branch", Select)
        assert app.screen.focused is branch and branch.value == "new"
        assert "feature/goal, a new branch" in screen_text(app)
        await pilot.press("enter")  # the list: the other place to commit it
        await pilot.pause()
        assert "main, where the task started (your main branch)" in screen_text(app)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, CommitWork) and branch.value == "new"
        message = app.screen.query_one("#message", TextArea)
        assert message.text == "Goal\n\n- Add one", "the task as the subject, the commit listed"
        message.focus()
        await pilot.press("end", *" now")
        await pilot.press("ctrl+s")  # a multi-line field: ctrl+s submits, like every other one
        await pilot.pause()

    run(scenario)
    git = lambda *a: subprocess.run(["git", *a], cwd=source, capture_output=True, text=True).stdout  # noqa: E731
    assert (
        git("log", "-1", "--format=%s%n%b").strip() == "Goal now\n- Add one" and (source / "one.txt").exists()
    )
    assert git("branch", "--show-current").strip() == "feature/goal", "the checkout stays on the new branch"
    assert git("log", "-1", "--format=%s", "main").strip() == "Initial commit"
    assert not task.root.exists()


def test_escape_on_an_open_list_closes_the_list_not_the_form(env):
    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        form = app.screen
        form.query_one("#goal", TextArea).text = "Half a ticket"
        kind = form.query_one("#kind", Select)
        kind.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert kind.expanded
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is form and not kind.expanded, "what you typed is still there"
        assert form.query_one("#goal", TextArea).text == "Half a ticket"
        await pilot.press("escape")  # and the next one closes the form, as always
        await pilot.pause()
        assert app.screen is not form

    run(scenario)


def test_the_work_can_be_committed_on_the_branch_the_task_started_on(env):
    source = load_project("demo").repo
    task = new_task()
    (task.repo / "one.txt").write_text("x\n")
    for args in (
        ["add", "one.txt"],
        ["-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one"],
    ):
        subprocess.run(["git", *args], cwd=task.repo, check=True, capture_output=True)
    at_plan_checkpoint(task)
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await pilot.press("right", "left", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen.query_one("#branch", Select).value = "start"
        await pilot.pause()
        app.screen.query_one("#commit").press()
        await pilot.pause()

    run(scenario)
    git = lambda *a: subprocess.run(["git", *a], cwd=source, capture_output=True, text=True).stdout  # noqa: E731
    assert git("branch", "--show-current").strip() == "main"
    assert git("log", "-1", "--format=%s").strip() == "Goal"
    assert "feature/goal" not in git("branch", "--list")


def done_with_a_proposal(env, command="npm ci && npm test"):
    """A task at review in a project with no command, its writer having proposed one."""
    from vivibox import proposal

    path = env / "config" / "projects" / "demo.toml"
    path.write_text(path.read_text().replace('verify = ["true"]', "verify = []"))
    task = new_task()
    (task.repo / "one.txt").write_text("x\n")
    for args in (
        ["add", "one.txt"],
        ["-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one"],
    ):
        subprocess.run(["git", *args], cwd=task.repo, check=True, capture_output=True)
    at_plan_checkpoint(task)
    gate.accept_plan(task)
    (task.meta / "handoff" / proposal.PROPOSAL).write_text(f"`{command}`\n")
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    return task


def at_command_checkpoint(env, command="npm ci && npm test"):
    """A task whose writer has just proposed the command, in a project with none."""
    import re

    from vivibox import proposal

    path = env / "config" / "projects" / "demo.toml"
    path.write_text(re.sub(r"^verify = .*$", "verify = []", path.read_text(), flags=re.MULTILINE))
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task)
    if command:
        (task.meta / "handoff" / proposal.PROPOSAL).write_text(f"`{command}`\n")
    for state in (State.IMPLEMENT, State.CHECKPOINT_COMMAND):
        task.transition(state)
    return task


def test_the_writers_command_is_a_decision_of_its_own_before_the_first_verification(env):
    """You see the command the task is about to be verified with and keep it for the project,
    once: the next task has it. a's field is e's, prefilled; leaving it to the writer is not a
    choice here, the writer just had its say."""
    from vivibox import actions

    task = at_command_checkpoint(env)

    async def scenario(app, pilot):
        app.reload()
        assert app.views[task.id].status == "review the command"
        assert "`a` keep it" in detail(task, task.read_state(), 3, True, None)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, AskVerify)
        assert app.screen.query_one(Input).value == "npm ci && npm test"
        assert "proposes" in app.screen.heading and not app.screen.query(Checkbox)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert load_project("demo").verify == ["npm ci && npm test"]
        assert task.read_state().state is State.VERIFY and actions.started[-1] == task.id

    run(scenario)


def test_no_command_from_the_writer_is_yours_to_type_or_to_ask_for(env):
    task = at_command_checkpoint(env, command="")

    async def scenario(app, pilot):
        app.reload()
        assert app.views[task.id].status == "review the command"
        assert "No command came from the writer" in detail(task, task.read_state(), 3, True, None)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, AskVerify) and app.screen.query_one(Input).value == ""
        assert "No command came from the writer" in app.screen.heading
        await pilot.press("enter")  # nothing typed: nothing decided
        await pilot.pause()
        assert task.read_state().state is State.CHECKPOINT_COMMAND
        assert not isinstance(app.screen, AskVerify)
        await pilot.press("r")
        await pilot.pause()
        app.screen.query_one(TextArea).text = "Write the command that builds and tests the project."
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert task.read_state().state is State.IMPLEMENT

    run(scenario)


def test_a_narrowed_command_is_shown_with_its_selection(env):
    task = at_command_checkpoint(env, "./mvnw -Dtest=PetTests test")

    async def scenario(app, pilot):
        app.reload()
        assert "narrowed to -Dtest=PetTests" in detail(task, task.read_state(), 3, True, None)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, AskVerify) and "narrowed to -Dtest=PetTests" in app.screen.heading

    run(scenario)


def test_a_task_with_nothing_to_build_is_said_so_when_it_is_made(env, monkeypatch):
    """A ticket to analyse, facts to gather: nothing to build, and you know it before the planner
    does. Build's second answer puts verify = false in the task's plan; the project keeps building
    the rest."""
    from vivibox import proposal

    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: task_id)

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#goal", TextArea).text = "Analyse PAY-123"
        build = app.screen.query_one("#no-build", Select)
        assert build.value is False, "building and testing is where the form starts"
        assert any("nothing to build" in str(label).lower() for label, _ in build._options)
        build.value = True
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    run(scenario)
    task = find_task(load_config().tasks_dir, "demo-1")
    assert "verify = false" in task.plan_path.read_text()
    at_plan_checkpoint(task)
    gate.accept_plan(task)
    assert proposal.nothing_to_build(task) and actions.verify_commands(task, load_project("demo")) == []
    assert load_project("demo").verify == ["true"], "the project builds its other tasks as ever"


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
    from vivibox.panel import SPINNER

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


def test_l_on_a_finished_task_reads_what_its_archive_kept(env):
    """Done, a task had no l: its logs were gone with its directory. Now they are kept, and l
    lists them, the timeline first; a task finished before that is said to have nothing."""
    from vivibox import actions, logs

    actions.remember(
        actions.Finished("demo-9", env, ui.Spend(0.3, 0.12), "Reject expired cards"),
        load_project("demo"),
        "abc1234567",
    )
    kept = actions.archive_path("demo-9")
    (kept / "log").mkdir(parents=True)
    (kept / "timeline.txt").write_text("# demo-9: Reject expired cards\n\n12:00:00  created\n")
    (kept / "log" / "verify-1-120000.log").write_text("$ npm test\n[exit 0 after 3 s]\n")
    (kept / "log" / "verify-2-130000.log").write_text("$ npm test\n[exit 1 after 2 s]\n")
    (kept / "review-1.md").write_text("## Blocking\n\n- a.py:1 — wrong\n\n## Not blocking\n")
    (kept / "log" / "supervisor.log").write_text("Supervising demo-9\n")
    (kept / "log" / "planner.log").write_text(
        "=== 12:00:00 planner · plan ===\n--- 12:01:00 · ok · $0.0100\n"
    )

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("demo-9"))
        await pilot.press("enter")
        await pilot.pause()
        assert app.check_action("show_log", ()) and "`l`" in app.shown
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, logs.ChooseLog)
        labels = [e.label for e in app.screen.found]
        assert labels == [
            "timeline",
            "planner.log",
            "verify-2-130000.log",
            "verify-1-120000.log",
            "review-1.md",
            "supervisor.log",
        ]
        assert any("1 blocking" in e.said for e in app.screen.found)
        await pilot.press("escape")
        await pilot.pause()

    run(scenario)
    assert logs.archived_entries(actions.archive_path("demo-8")) == [], "finished before logs were kept"


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
        assert isinstance(app.screen, dialogs.DeleteTask) and app.screen.focused.id == "no", "Cancel first"
        await pilot.press("left", "enter")  # Delete
        await pilot.pause()
        assert rows(app) == ["demo", "demo-1"] and actions.history() == []
        await pilot.press("h")
        assert rows(app) == ["demo", "demo-1"], "hiding finished tasks leaves the live ones"

    run(scenario)


def test_accepted_and_deleted_tasks_are_shown_or_hidden_separately_and_the_choice_is_kept(env):
    """h is for the tasks you accepted, H for the ones you deleted; the deleted ones start hidden,
    since they are the ones you rarely look at. The header says how many of each are out of sight,
    and both choices outlive the view, like a folded project."""
    task = new_task("Task")
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    kept = {"project": "demo", "cost": 0.1, "commit": "abc", "branch": "", "conflicts": [], "finished": now()}
    actions.history_path().write_text(
        json.dumps({"id": "demo-0", "title": "Accepted", **kept})
        + "\n"
        + json.dumps({"id": "demo-5", "title": "Thrown away", "deleted": "planning", **kept})
        + "\n"
    )

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo", task.id, "demo-0"], "deleted tasks start hidden"
        assert "1 deleted hidden" in app.sub_title and "done hidden" not in app.sub_title
        await pilot.press("H")
        await pilot.pause()
        assert rows(app) == ["demo", task.id, "demo-5", "demo-0"] and "hidden" not in app.sub_title
        await pilot.press("h")
        await pilot.pause()
        assert rows(app) == ["demo", task.id, "demo-5"] and "1 done hidden" in app.sub_title
        app.table.move_cursor(row=2)
        await pilot.press("d")
        await pilot.pause()
        assert "`H` to hide deleted tasks" in app.shown

    run(scenario)

    async def again(app, pilot):
        app.reload()
        await pilot.pause()
        assert rows(app) == ["demo", task.id, "demo-5"], "both choices are remembered"

    run(again)


def test_a_task_number_twice_in_the_history_is_listed_once_as_the_newest(env):
    """Older versions could leave one number in the history twice: a number used again, a task
    written down twice. The list keys its rows by number, and two rows under one key crashed it."""
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    kept = {"project": "demo", "cost": 0.1, "commit": "abc", "branch": "", "conflicts": [], "finished": now()}
    actions.history_path().write_text(
        json.dumps({"id": "demo-5", "title": "Older", "deleted": "planning", **kept})
        + "\n"
        + json.dumps({"id": "demo-5", "title": "Newer", "deleted": "planning", **kept})
        + "\n"
    )

    async def scenario(app, pilot):
        await pilot.press("H")
        await pilot.pause()
        assert rows(app) == ["demo", "demo-5"]
        assert cell(app, 1, "GOAL") == "Newer"

    run(scenario)


def test_a_forgotten_project_takes_its_history_with_it(env, tmp_path, monkeypatch):
    """Forgetting every project leaves the view as it starts, even with the deleted tasks shown:
    the history of a project that is gone is not what you came for, and H has nothing to toggle."""
    (env / "config" / "projects" / "demo.toml").unlink()
    monkeypatch.chdir(tmp_path)
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    kept = {"project": "gone", "cost": 0.1, "commit": "abc", "branch": "", "conflicts": [], "finished": now()}
    actions.history_path().write_text(
        json.dumps({"id": "gone-1", "title": "Accepted", **kept})
        + "\n"
        + json.dumps({"id": "gone-2", "title": "Thrown away", "deleted": "planning", **kept})
        + "\n"
    )
    panel.save_view(show_deleted=True, show_done=True)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert "No projects yet" in str(app.query_one("#empty").render()) and not app.table.display
        assert not app.check_action("toggle_deleted", ()) and not app.check_action("toggle_done", ())
        assert "hidden" not in app.sub_title

    run(scenario)


def test_the_list_that_comes_back_takes_the_arrows(env):
    """The list is hidden while there is nothing in it, which takes the focus away; when it is
    back, the arrows move in it again instead of nowhere."""
    project = env / "config" / "projects" / "demo.toml"
    saved = project.read_text()

    async def scenario(app, pilot):
        assert app.focused is app.table
        project.unlink()
        app.reload()
        await pilot.pause()
        assert not app.table.display
        project.write_text(saved)
        app.reload()
        await pilot.pause()
        assert app.table.display and app.focused is app.table

    run(scenario)


def test_a_task_whose_project_is_gone_keeps_its_row_but_not_the_project_keys(env):
    """A task outlives its project's file, so it is not orphaned off the screen; its project row has
    no file to edit, no repository to open and nothing to start a box in."""
    task = new_task("Task")
    (env / "config" / "projects" / "demo.toml").unlink()

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=0)
        await pilot.pause()
        assert rows(app) == ["demo", task.id] and app.on_project_row()
        for action in ("edit_project", "open_repo", "new_box"):
            assert not app.check_action(action, ()), action

    run(scenario)


def test_a_project_can_be_set_up_from_the_view(env, tmp_path, monkeypatch):
    from vivibox.config import load_project

    fresh = tmp_path / "clicker"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(browse, "browse_start", lambda: tmp_path)
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")

    async def scenario(app, pilot):
        await pilot.press("i")  # a project of its own, from anywhere in the view
        await pilot.pause()
        assert app.screen.where == tmp_path, "where you started vivibox, until you browse elsewhere"
        app.screen.query_one("#browse").press()
        await pilot.pause()
        assert isinstance(app.screen, browse.Browse)
        app.screen.query_one("#new-folder").press()  # a project from scratch: no folder yet
        await pilot.pause()
        await pilot.press(*"clicker", "enter")
        await pilot.pause()
        assert isinstance(app.screen, dialogs.NewProject) and app.screen.where == fresh and fresh.is_dir()
        assert app.screen.query_one("#name", Input).value == "clicker"
        from ux import screen_text

        assert "nothing; the writer builds what it needs" in screen_text(app), "an empty folder: no build"
        app.screen.query_one("#create").press()
        await pilot.pause()
        assert app.screen.query_one("#goal"), "its first task follows right away"
        kind = app.screen.query_one("#kind-row")
        assert not kind.display, "an empty project has nothing that could work wrong"

    run(scenario)
    project = load_project("clicker")
    assert project.repo == fresh and project.verify == [], "the first plan will set how to test it"
    assert (fresh / ".git").is_dir()


def test_i_asks_what_to_prepare_a_new_tasks_clone_with_and_suggests_it(env, tmp_path, monkeypatch):
    """After i and n at once, the first task's writer started on a clone nobody had built, and
    the person built it by hand in the agent's window: i asks for prepare next to the
    verification, with the build files' suggestion, which Change… edits."""
    from ux import screen_text

    from vivibox.config import load_project

    maven = tmp_path / "api"
    make_repo(maven)
    (maven / "mvnw").write_text("")
    (maven / "pom.xml").write_text("<project/>")
    monkeypatch.chdir(maven)
    monkeypatch.setattr("vivibox.actions.start", lambda task_id, resume=False, on_step=None: "m")

    async def scenario(app, pilot):
        await pilot.press("i")
        await pilot.pause()
        assert "Preparation" in screen_text(app) and "bash mvnw -B install -DskipTests" in screen_text(app)
        app.screen.query_one("#change-prepare").press()
        await pilot.pause()
        field = app.screen.query_one("#value", Input)
        assert field.value == "bash mvnw -B install -DskipTests"
        field.value = "bash mvnw -B -q install -DskipTests"
        await pilot.press("enter")
        await pilot.pause()
        assert "bash mvnw -B -q install -DskipTests" in screen_text(app)
        app.screen.query_one("#create").press()
        await pilot.pause()
        assert app.screen.query_one("#goal"), "its first task follows right away"

    run(scenario)
    project = load_project("api")
    assert project.prepare == ["bash mvnw -B -q install -DskipTests"]


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
    from vivibox.widgets import Confirm

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
        assert not isinstance(app.screen, newtask.NewTask)
        (env / "config" / "projects" / "demo.toml").write_text(
            f'repo = "{env / "repo"}"\nverify = ["true"]\n'
        )
        app.reload()
        await pilot.pause()
        assert app.check_action("new", ()) and app.table.display
        assert "no tasks yet  n new task" in cell(app, 0, "GOAL"), "the project row says"

    run(scenario)


def test_criteria_are_ticked_off_in_view_while_the_agent_works(env):
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task)
    st = task.transition(State.IMPLEMENT)
    assert "\u2610 it works" in detail(task, st, 3), "an open criterion"
    assert "no verification yet" in detail(task, st, 3).lower()
    reported = task.meta / "handoff" / gate.CRITERIA_FILE
    reported.write_text(reported.read_text().replace("- [ ]", "- [x]"))
    assert "\u2611 it works" in detail(task, st, 3), "the agent reports it met"


def test_a_failed_gate_shows_what_the_build_said(env):
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task)
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


def test_the_build_files_a_task_leaves_behind_are_pointed_out(env):
    """A new product's first task makes the build; the project runs nothing yet, and the panel
    says which file names the command and where to pick it."""
    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task)
    st = task.transition(State.IMPLEMENT)
    task.event("gate", passed=True, build_files=[["npm ci && npm test", "package.json"]])
    shown = detail(task, st, 3)
    assert "package.json" in shown and "`npm ci && npm test`" in shown and "`e`" in shown
    task.event("gate", passed=True)
    assert "package.json" not in detail(task, st, 3), "only while the last gate saw them"


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

    up = panel.PodView("198.51.100.2", [Listener(5173, True)], demo=True)
    assert "[198.51.100.2:5173](http://198.51.100.2:5173)" in detail(task, st, 3, pod=up), "clickable"

    hidden = panel.PodView("198.51.100.2", [Listener(8000, False)], demo=True)
    assert "nothing outside can reach it" in detail(task, st, 3, pod=hidden)

    starting = panel.PodView("198.51.100.2", [], demo=True)
    assert "running, nothing listening yet" in detail(task, st, 3, pod=starting)

    crashed = panel.PodView("198.51.100.2", [], demo=False, log="Traceback…\nKeyError: 'gameweek'")
    text = detail(task, st, 3, pod=crashed)
    assert "It stopped." in text and "KeyError: 'gameweek'" in text, "why, not just that"

    idle = panel.PodView("198.51.100.2", [], demo=False)
    assert "demo not running" in detail(task, st, 3, pod=idle)
    assert "It stopped." not in detail(task, st, 3, pod=idle), "it was never started"


def test_running_the_app_waits_until_the_work_is_back_with_you(env, monkeypatch):
    """The agent builds and tests in the same working tree and pod; the app started beside it
    would fight it for the build output and the ports."""
    task = new_task()
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    view = [panel.PodView("198.51.100.2", [], demo=False)]
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
    view = [panel.PodView("198.51.100.2", [], demo=False)]
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: view[0] for i in ids})

    async def scenario(app, pilot):
        assert await until(pilot, lambda: not app.pod.demo)
        assert app.check_action("demo", ()) and not app.check_action("demo_stop", ())
        view[0] = panel.PodView("198.51.100.2", [Listener(8000, True)], demo=True)
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
    answer = [panel.PodView("198.51.100.2", [], demo=False)]
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: answer[0] for i in ids})

    def cell(app):
        return cell_of(app, task.id, "APP")

    async def scenario(app, pilot):
        assert await until(pilot, lambda: cell(app) == "·"), "nothing started yet"
        answer[0] = panel.PodView("198.51.100.2", [Listener(8000, True)], demo=True)
        assert await until(pilot, lambda: "live" in cell(app)), "it is serving"
        assert "8000" not in cell(app), "the address belongs in the panel, where all of it fits"
        answer[0] = panel.PodView("198.51.100.2", [Listener(5173, True), Listener(8000, True)], demo=True)
        assert await until(pilot, lambda: "×2" in cell(app)), "a front end and a back end both up"
        answer[0] = panel.PodView("198.51.100.2", [Listener(8000, False)], demo=True)
        assert await until(pilot, lambda: "local" in cell(app)), "bound to localhost, never coming"
        answer[0] = panel.PodView("198.51.100.2", [], demo=True)
        assert await until(pilot, lambda: "starting" in cell(app)), "up, but no port yet"
        answer[0] = panel.PodView("198.51.100.2", [], demo=False, log="Error: exploded")
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


def test_w_appears_once_the_agent_has_a_conversation_without_leaving_the_view(env, monkeypatch):
    """A session is recorded after the start, without moving the task's state: the list looked the
    same, so the footer was never drawn again and w came only with a new vivibox."""
    task = implementing("Watched")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        assert "watch" not in keys(app), "no conversation to look at yet"
        assert "enter_box" not in keys(app), "w is never a shell in a task's pod, only in a box"
        task.set_session("writer", "ses_1")
        app.reload()
        await pilot.pause()
        await pilot.pause()
        assert "watch" in keys(app)

    run(scenario)


def test_the_list_says_when_you_asked_for_a_task_not_only_when_it_last_moved(env):
    task = new_task("Waiting")

    async def scenario(app, pilot):
        app.reload()
        # Not ui.ago() recomputed here: that races the minute boundary and says nothing extra.
        assert cell_of(app, task.id, "CREATED") == "now", "CREATED, and the task was made a moment ago"
        assert cell_of(app, task.id, "UPDATED") == "now", "UPDATED"

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
        assert cell_of(app, "demo-9", "CREATED") == "now"
        assert cell_of(app, "demo-8", "CREATED") == "·"

    run(scenario)


def test_the_view_follows_the_pod_on_its_own(env, monkeypatch):
    """The refresh asks the pods and puts the answer on screen without being told to, and the
    answer arrives on the event loop: a slip there takes the whole app down."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    down = panel.PodView("198.51.100.2", [], demo=False)
    up = panel.PodView("198.51.100.2", [Listener(8000, True)], demo=True)
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
    assert "Pod" not in detail(task, task.read_state(), 3, pod=panel.PodView())


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
    app.pods_answered({"demo-1": panel.PodView("198.51.100.2", [], demo=True)})


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
        assert isinstance(app.screen, dialogs.ChooseRole)
        assert [r[0] for r in app.screen.rows] == ["planner", "writer"], "both roles, named"

        await pilot.press("down", "enter")  # writer
        await pilot.pause()
        assert isinstance(app.screen, dialogs.ChooseModel)
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


def test_the_running_turns_cost_and_last_step_show_in_the_row_and_the_panel(env, monkeypatch):
    """A long turn used to be a frozen row: the cost came with the turn event at its end, and
    nothing said the agent was still at work. Now the row's cost grows step by step and UPDATED
    is when the agent last finished a step; the panel says "last step … ago"."""
    task = new_task()
    task.event("started", model="m")
    task.event("turn", state="plan", cost=0.10, tokens=1)
    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id == task.id)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert str(app.table.get_cell(task.id, app.plan_column)) == "$0.10"
        assert str(app.table.get_cell(task.id, app.impl_column)) == "$0.00"
        task.set_live_turn(0.04, 400, 3)
        app.reload()
        await pilot.pause()
        assert str(app.table.get_cell(task.id, app.impl_column)) == "$0.04", "the turn so far"
        assert str(app.table.get_cell(task.id, app.updated_column)) == "now"
        await pilot.press("d")
        await pilot.pause()
        assert "last step just now" in app.shown
        task.clear_live_turn()
        app.reload()
        await pilot.pause()
        assert str(app.table.get_cell(task.id, app.impl_column)) == "$0.00"
        assert "last step" not in app.shown

    run(scenario)


def test_criteria_ticked_during_a_turn_show_up(env, monkeypatch):
    """The agent ticks criteria while it works, and watching them fill in is how you see a long
    turn progressing. Skipping the redraw when the task's state has not moved froze the column for
    the whole of an implementation, which is exactly when it has something to say."""
    task = new_task("Waiting")
    at_plan_checkpoint(task)
    gate.accept_plan(task)
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
    shown = detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    assert "Nothing: this is a new project." in shown
    (task.meta / "handoff" / manual.CONTEXT).write_text("Express 4, tests with vitest.\n")
    shown = detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
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
        assert isinstance(app.screen, dialogs.ReplyWithCriteria)
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
    monkeypatch.setattr("vivibox.roles.provider_models", lambda p: [])
    monkeypatch.setattr("vivibox.roles.models_cache", lambda: tmp_path / "models.json")
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
        await pilot.press("enter")  # the settings' first row: Providers & MCP
        await pilot.pause()
        app.screen.query_one("#import").press()
        await pilot.pause()
        assert isinstance(app.screen, providers_ui.ImportSource)
        await pilot.press("enter")  # the configuration found where opencode keeps it
        await pilot.pause()
        assert isinstance(app.screen, providers_ui.ChooseImport), "you see what comes before it comes"
        assert keys.list_keys() == {}, "nothing kept yet"
        app.screen.query_one("#import").press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, providers_ui.ManageProviders)
        await pilot.press("escape", "escape")  # Providers & MCP, then the settings
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

    monkeypatch.setattr("vivibox.roles.provider_models", lambda p: [f"{p}/big", f"{p}/small"])
    monkeypatch.setattr("vivibox.roles.models_cache", lambda: tmp_path / "models.json")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        app.catalog = CATALOG
        await pilot.press("k")
        await pilot.pause()
        await pilot.press("enter")  # the settings' first row: Providers & MCP
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
        await pilot.press("escape", "escape")  # Providers & MCP, then the settings
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
    monkeypatch.setattr("vivibox.roles.provider_models", lambda p: [])
    monkeypatch.setattr("vivibox.roles.models_cache", lambda: tmp_path / "models.json")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        app.catalog = CATALOG
        await pilot.press("k")
        await pilot.pause()
        await pilot.press("enter")  # the settings' first row: Providers & MCP
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
        assert not isinstance(app.screen, newtask.NewTask)
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
            '{"id": "demo-9", "project": "demo", "title": "Old", "cost": 0,'
            ' "finished": "2026-09-01T00:00:00+00:00"}\n'
        )
        app.reload()
        assert app.check_action("toggle_done", ())

    run(scenario)


CATALOG = [("anthropic", "Anthropic"), ("openai", "OpenAI"), ("deepseek", "DeepSeek"), ("azure", "Azure")]


def test_providers_are_found_by_name_or_id_and_a_typed_name_is_offered_too():
    assert [pid for pid, _ in providers_ui.find_providers(CATALOG, "")] == [
        "anthropic",
        "openai",
        "deepseek",
        "azure",
    ]
    assert [pid for pid, _ in providers_ui.find_providers(CATALOG, "SEEK")] == ["deepseek", "seek"]
    assert [pid for pid, _ in providers_ui.find_providers(CATALOG, "deepseek")] == ["deepseek"], (
        "no second of the same"
    )
    assert providers_ui.find_providers([], "acme") == [("acme", "use “acme” as the provider's name")]


def test_arrows_in_the_search_walk_the_list(env):
    async def scenario(app, pilot):
        app.push_screen(providers_ui.AddProvider(CATALOG))
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
        assert isinstance(screen, providers_ui.ChooseImport)

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
    assert browse.judge(good) == (True, "opencode configuration: 1 provider, 1 MCP server")
    assert browse.judge(other) == (
        False,
        "JSON, but not an opencode configuration with providers or MCP servers",
    )
    ok, said = browse.judge(broken)
    assert not ok and said.startswith("broken: not JSON")


def test_the_browser_shows_only_what_can_be_picked(tmp_path):
    for name in ("a.json", "b.jsonc", "notes.txt", "img.png"):
        (tmp_path / name).write_text("{}")
    for name in ("src", ".config", "node_modules", ".git"):
        (tmp_path / name).mkdir()
    shown = {
        mode: {p.name for p in tmp_path.iterdir() if browse.shows(mode, p)}
        for mode in ("json", "folder", "any")
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
        app.push_screen(browse.Browse("json", "Find it"), picked.append)
        await pilot.pause()
        browser = app.screen
        tree = browser.query_one("#tree", browse.PathTree)
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


def test_k_opens_the_settings_and_each_row_writes_its_own_key(env, monkeypatch):
    """One screen for what changes often: providers first, the roles' defaults, the review, the
    limits; the machine's own settings shown, not edited. Each row writes its key alone, and the
    file's comments, its manual, stay."""
    from vivibox import ide

    config = env / "config" / "config.toml"
    config.write_text(
        '# The manual.\ntasks_dir = "' + str(env / "tasks") + '"\n\n[limits]\n# Kept.\nmax_rounds = 3\n\n'
        '[roles.planner]\nharness = "manual"\nmodel = ""\n\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n\n'
        '[review]\n# ide = "idea {path}"\n'
    )
    monkeypatch.setattr(ide, "candidates", lambda: [ide.Editor("VS Code", "code {path}")])

    def labels(app) -> list[str]:
        options = app.screen.query_one("#rows", OptionList)
        # As they read: without their markup.
        return [
            Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
            for i in range(options.option_count)
        ]

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("k")
        await pilot.pause()
        assert isinstance(app.screen, settings.Settings)
        shown = labels(app)
        assert "PROVIDERS & MCP" in shown[0] and "serena" in shown[1], "providers first, as before"
        assert any("code {path} (found here)" in row for row in shown), "o's editor, found, not chosen yet"
        assert any("tasks folder" in row for row in shown) and any("network pool" in row for row in shown)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, providers_ui.ManageProviders), "the first row is the old k"
        await pilot.press("escape")
        await pilot.pause()
        # The writer's default model.
        writer = next(i for i, row in enumerate(shown) if row.strip().startswith("writer"))
        app.screen.query_one("#rows", OptionList).highlighted = writer
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, dialogs.ChooseModel)
        await pilot.press("down", "enter")  # the first of deepseek's
        await pilot.pause()
        text = config.read_text()
        assert (
            'model = "deepseek/deepseek-v4-flash"' in text and "# The manual." in text and "# Kept." in text
        )
        assert "deepseek-v4-flash" in labels(app)[writer], "the row says so at once"
        # The editor for o, under "Review copy"; the notifications under a heading of their own.
        assert any("REVIEW COPY" in row for row in shown) and any("NOTIFICATIONS" in row for row in shown)
        assert not any("editor for o" in row for row in shown), "the row names the tool, not the key"
        editor = next(i for i, row in enumerate(labels(app)) if "IDE / text editor (o)" in row)
        app.screen.query_one("#rows", OptionList).highlighted = editor
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, dialogs.ChooseEditor)
        await pilot.press("enter")
        await pilot.pause()
        text = config.read_text()
        assert 'ide = "code {path}"' in text and "# ide" not in text and text.count("[review]") == 1
        # Notifications: a toggle, no dialog.
        notify = next(i for i, row in enumerate(labels(app)) if "desktop notifications" in row)
        app.screen.query_one("#rows", OptionList).highlighted = notify
        await pilot.press("enter")
        await pilot.pause()
        assert "desktop = false" in config.read_text() and "off" in labels(app)[notify]
        # A limit: one line, checked.
        limit = next(i for i, row in enumerate(labels(app)) if row.strip().startswith("rounds"))
        app.screen.query_one("#rows", OptionList).highlighted = limit
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, settings.Ask)
        app.screen.query_one(Input).value = "0"
        await pilot.press("enter")
        await pilot.pause()
        assert "max_rounds = 3" in config.read_text(), "0 is refused"
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = "5"
        await pilot.press("enter")
        await pilot.pause()
        text = config.read_text()
        assert "[limits]\n# Kept.\nmax_rounds = 5\n" in text and app.config.max_rounds == 5
        # The verification's time limit: minutes to read, minutes or seconds to type.
        timeout = next(i for i, row in enumerate(labels(app)) if "verification timeout" in row)
        assert "30 min" in labels(app)[timeout] and "1800" not in labels(app)[timeout]
        app.screen.query_one("#rows", OptionList).highlighted = timeout
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = "45m"
        await pilot.press("enter")
        await pilot.pause()
        assert "verify_timeout = 2700" in config.read_text() and "45 min" in labels(app)[timeout]
        app.screen.query_one("#rows", OptionList).highlighted = timeout
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one(Input).value = "45"
        await pilot.press("enter")
        await pilot.pause()
        assert "verify_timeout = 45" in config.read_text() and "45 s" in labels(app)[timeout]
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, settings.Settings)

    run(scenario)


def test_e_opens_the_projects_screen_and_each_row_writes_its_own_key(env, monkeypatch):
    """What belongs to the project sits on its row: how it is verified and run, its JDK, what its
    build needs from your shell, its editor; the rest is in the file, the last row."""
    from vivibox import ide

    monkeypatch.setattr(ide, "candidates", lambda: [ide.Editor("VS Code", "code {path}")])
    path = env / "config" / "projects" / "demo.toml"
    path.write_text(path.read_text() + '# Mine.\ndemo = []\njava = ""\npass_env = []\n')
    opened = []
    monkeypatch.setattr(tui.Vivibox, "edit_project_file", lambda self: opened.append(self.project_file()))

    def labels(app) -> list[str]:
        options = app.screen.query_one("#rows", OptionList)
        # As they read: without their markup.
        return [
            Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
            for i in range(options.option_count)
        ]

    async def go_to(app, pilot, what: str):
        row = next(i for i, text in enumerate(labels(app)) if what in text)
        app.screen.query_one("#rows", OptionList).highlighted = row
        await pilot.press("enter")
        await pilot.pause()
        return row

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("demo"))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, settings.ProjectSettings)
        assert labels(app)[0].strip().startswith("preparation"), "what a new task does first, first"
        assert labels(app)[1].strip().startswith("verification") and "true" in labels(app)[1]
        await go_to(app, pilot, "run app")
        assert isinstance(app.screen, settings.AskLines)
        app.screen.query_one(TextArea).text = "npm install\nnpm start\n"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert load_project("demo").demo == ["npm install", "npm start"] and "# Mine." in path.read_text()
        await go_to(app, pilot, "java")
        app.screen.query_one(Input).value = "17"
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("demo").java == "17"
        row = await go_to(app, pilot, "variables")
        app.screen.query_one(Input).value = "PATH"
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("demo").pass_env == [], "a variable vivibox sets itself is refused"
        await go_to(app, pilot, "variables")
        app.screen.query_one(Input).value = "NPM_TOKEN, REPO_TOKEN"
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("demo").pass_env == ["NPM_TOKEN", "REPO_TOKEN"]
        assert "NPM_TOKEN, REPO_TOKEN" in labels(app)[row]
        await go_to(app, pilot, "IDE / text editor (o)")
        assert isinstance(app.screen, dialogs.ChooseEditor)
        await pilot.press("down", "enter")  # after "the one in config.toml": VS Code
        await pilot.pause()
        assert load_project("demo").ide == "code {path}"
        await go_to(app, pilot, "project file")
        assert opened == [path]

    run(scenario)


def test_o_opens_with_the_editor_the_repository_points_at(env, monkeypatch):
    """No editor chosen: a repository with .idea opens in the JetBrains one found here, one with
    .vscode in VS Code, any other in the first editor found; ? says which."""
    from vivibox import ide

    found = [ide.Editor("VS Code", "code {path}"), ide.Editor("IntelliJ IDEA", "idea {path}")]
    monkeypatch.setattr(ide, "candidates", lambda: found)
    project = load_project("demo")
    assert actions.editor_command(load_config(), project) == "code {path}", "the first found"
    (project.repo / ".idea").mkdir()
    assert actions.editor_command(load_config(), project) == "idea {path}"
    assert ide.default_for(project.repo, []) == ""

    from ux import screen_text

    async def scenario(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        app.screen.query_one(".help-body").scroll_end(animate=False)
        await pilot.pause()
        note = " ".join(screen_text(app).replace("│", " ").split())
        assert "o opens with code {path}" in note and "k changes it" in note

    run(scenario)


def test_a_configured_model_the_provider_no_longer_offers_is_marked(env):
    """config.toml's own choice is always on the list, so a retired model stays visible; it says
    so, in the settings row and in the picker, so the person picks its successor."""
    config = env / "config" / "config.toml"
    config.write_text(config.read_text().replace('model = "m"', 'model = "deepseek/deepseek-v4-flash"'))
    app_available = {"deepseek": ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]}

    async def scenario(app, pilot):
        app.available = app_available
        await pilot.press("k")
        await pilot.pause()
        options = app.screen.query_one("#rows", OptionList)
        rows = [
            Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
            for i in range(options.option_count)
        ]
        writer = next(i for i, row in enumerate(rows) if row.strip().startswith("writer"))
        assert "not offered now" in rows[writer]
        options.highlighted = writer
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen.query_one(OptionList)
        labels = [str(picker.get_option_at_index(i).prompt) for i in range(picker.option_count)]
        assert labels[0].startswith("deepseek/deepseek-v4-flash") and "not offered" in labels[0]
        assert not any("not offered" in label for label in labels[1:]), "the provider's own are fine"

    run(scenario)


def test_k_lists_providers_and_mcp_and_manage_turns_them_off_or_removes_them(env):
    from vivibox import keys, providers

    keys.set_key("deepseek", "sk-mine")
    keys.set_key("openai", "sk-other")

    async def scenario(app, pilot):
        await pilot.press("k")
        await pilot.pause()
        await pilot.press("enter")  # the settings' first row: Providers & MCP
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, providers_ui.ManageProviders)
        assert [(r[1], r[3]) for r in screen.rows] == [("deepseek", True), ("openai", True), ("serena", True)]
        assert "auto: on for a project with 100+ source files" in screen.rows[2][2]
        screen.query_one("#manage").press()
        await pilot.pause()
        manage = app.screen
        assert isinstance(manage, providers_ui.ManageItems)
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
    shown = detail(task, task.read_state(), 3, running=False, pod=panel.PodView())
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
        app.push_screen(providers_ui.ImportSource([]), chosen.append)
        await pilot.pause()
        await pilot.press("enter")  # Browse…
        await pilot.pause()
        browser = app.screen
        assert isinstance(browser, browse.Browse)
        browser.post_message(
            DirectoryTree.FileSelected(browser.query_one("#tree", browse.PathTree).root, good)
        )
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
        app.push_screen(providers_ui.AddProvider([]), added.append)
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


def test_a_step_that_outlives_the_view_has_nobody_to_tell(env):
    """A thread worker can finish after the view is gone (a docker command, a start): its loop is
    closed, so telling the view anything is dropped, without an error in the thread and without a
    coroutine Textual would leave unawaited, which ended every run of these tests with a warning."""
    import threading

    kept = []

    async def scenario(app, pilot):
        kept.append(app)

    run(scenario)
    app, outcome = kept[0], []

    def late() -> None:
        try:
            outcome.append(app.call_from_thread(app.notify, "too late"))
        except RuntimeError as e:
            outcome.append(e)

    thread = threading.Thread(target=late)
    thread.start()
    thread.join()
    assert outcome == [None], f"dropped quietly, not {outcome}"


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
        assert dialog.query_one(widgets.Fields).max_scroll_y == 0, "nothing to scroll: every field is in view"
        assert dialog.query_one(".dialog").region.width <= size[0], "the dialog fits the terminal"
        shown = screen_text(app)
        for word in ("Project", "Kind", "Goal", "Attach…", "@path", "Plan", "Planner", "Writer", "Create",
                     "Cancel", "ctrl+s"):  # fmt: skip
            assert word in shown, f"{word} not on the screen at {size}"
        assert dialog.query_one("#goal").region.height >= 3, "room for at least a line of the description"

    run(scenario, size=size)


@pytest.mark.parametrize("size", [(80, 24), (100, 30)])
def test_with_a_reviewer_the_whole_new_task_form_fits_a_short_terminal(env, size):
    """With a reviewer's row as well, Orchestration and Rounds were a scroll away at 80×24: the
    blank row between the two groups goes on a short terminal too, after the headings and the
    blank rows between the lists."""
    from ux import screen_text

    with_reviewer(env)
    with_code("demo")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        dialog = app.screen
        assert dialog.query_one(widgets.Fields).max_scroll_y == 0, "nothing to scroll"
        shown = screen_text(app)
        for word in ("Kind", "Branch", "Build", "Attach…", "Reviewer", "Flow", "Fix rounds", "Create"):
            assert word in shown, f"{word} not on the screen at {size}"
        assert dialog.query_one("#goal").region.height >= 3

    run(scenario, size=size)


@pytest.mark.parametrize(("size", "headed"), [((120, 40), True), ((100, 30), False), ((80, 24), False)])
def test_the_groups_have_headings_where_there_is_room_and_the_roles_stand_in_working_order(env, size, headed):
    """TASK and WORKFLOW head the two sections on a tall terminal and go first on a short one (§4);
    the flow first, then its agents as they work, planner, writer, reviewer."""
    from ux import screen_text

    with_reviewer(env)

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        shown = screen_text(app)
        assert ("WORKFLOW" in shown) is headed, shown
        rows = [app.screen.query_one(f"#role-{r}").region.y for r in ("planner", "writer", "reviewer")]
        assert rows == sorted(rows), rows
        assert app.screen.query_one("#orchestration").region.y < rows[0], "the flow first"
        assert app.screen.query_one(widgets.Fields).max_scroll_y == 0, "every field in view"

    run(scenario, size=size)


@pytest.mark.parametrize("size", [(146, 38), (100, 30), (80, 24)])
def test_the_rows_of_a_section_stand_one_under_another_at_every_size(env, size):
    """With no band on a list, lists stand one under another without reading as one block (§4):
    the form's height is the same whatever the terminal, and a short one shrinks the description,
    never below three lines."""
    apart = 1
    with_code("demo")  # the kind is asked too: the whole form, as on a project with code

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        dialog = app.screen
        planner, writer = dialog.query_one("#role-planner").region, dialog.query_one("#role-writer").region
        assert writer.y - planner.y == apart, f"Planner at {planner.y}, Writer at {writer.y}"
        assert dialog.query_one(widgets.Fields).max_scroll_y == 0, "still nothing to scroll"
        assert dialog.query_one("#goal").region.height >= 3

    run(scenario, size=size)


@pytest.mark.parametrize("size", [(146, 38), (80, 24)])
def test_every_field_of_the_new_task_form_starts_in_one_column_and_looks_like_one(env, size):
    """Kind, Branch and Build stood a column apart, one under another, and read as overlapping;
    Rounds and Attach, with no band of their own, read as text rather than as a field and a
    button."""
    with_code("demo")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        dialog = app.screen
        fields = ["#project", "#kind", "#base-ref", "#no-build", "#plan", "#orchestration"]
        columns = {name: dialog.query_one(name).region.x for name in fields}
        assert len(set(columns.values())) == 1, columns
        branch, build = dialog.query_one("#base-ref").region, dialog.query_one("#no-build").region
        assert branch.right == dialog.query_one("#kind").region.right, "the Branch field is as wide as a list"
        assert build.x == branch.x and build.right == branch.right, "Build is a list as wide too"
        # What you type in and what you press has a band; a list is its value and its mark.
        page = dialog.query_one(".dialog").styles.background
        for name in ("#max-rounds", "#attach"):
            band = dialog.query_one(name).styles.background
            assert band.a > 0 and band != page, f"{name} has no band of its own"

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
        lists = {s.id for s in task.query(Select)}
        assert lists == {"project", "kind", "no-build"}, "the workflow's lists are elsewhere"
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
        project.value = dialogs.NEW_PROJECT
        await pilot.pause()
        assert isinstance(app.screen, NewProject)

    run(scenario)


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
def test_an_open_list_in_the_new_task_form_is_framed_apart_from_the_rows_under_it(env, size):
    """Open, the plan's list ended right above the writer's row, and the writer's model read as
    one more of its options: the open list is framed, its last option above the frame's end."""
    from ux import screen_text

    with_code("demo")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#plan", Select).focus()
        await pilot.press("enter")
        await pilot.pause()
        lines = screen_text(app).splitlines()
        last = next(i for i, line in enumerate(lines) if "--draft" in line)
        assert "╰" in lines[last + 1], "the frame closes under the last option"
        top = max(i for i, line in enumerate(lines[:last]) if "╭" in line)  # the dialog is framed too
        assert top < last and "Stop for my review" in lines[top + 1]

    run(scenario, size=size)


def test_tab_walks_the_new_task_form_from_the_description_down(env):
    """The description first, as the project comes from the selected row; then down the form, and
    round to the project and the kind."""
    expected = ["goal", "attach", "plan", "orchestration", "role-planner", "role-writer", "max-rounds",
                "create", "cancel", "project", "kind", "base-ref", "no-build", "goal"]  # fmt: skip
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

    def slow_stop(t, force=False):
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


def test_S_stops_by_force_even_while_a_stop_hangs(env, monkeypatch):
    """s takes the pod down and waits for it; when the container ignores the stop or the supervisor
    hangs, the row says "stopping…" for good and s is off. S is on whenever the task is not done,
    asks once, and kills; the hanging step is left to finish on its own."""
    import threading

    task = new_task()
    task.event("started", model="m")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    release = threading.Event()
    stops = []

    def stop(t, force=False):  # what actions.stop does, with the stop hanging until released
        stops.append(force)
        if force:
            t.set_paused(True, problem="stopped by force")
            return
        release.wait(5)
        if not t.read_state().paused:
            t.set_paused(True)

    monkeypatch.setattr(actions, "stop", stop)

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert app.check_action("stop_task", ()) and app.check_action("force_stop", ())
        hung = app.stop(task.id)
        for _ in range(20):
            await pilot.pause(0.05)
            if task.id in app.starting:
                break
        assert not app.check_action("stop_task", ()), "a stop is under way"
        assert app.check_action("force_stop", ()), "and S is the way out of it"
        await pilot.press("S")
        await pilot.pause()
        assert isinstance(app.screen, widgets.Confirm), "asked once, like s"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause(0.05)
            if len(stops) == 2:
                break
        release.set()
        await hung.wait()
        await pilot.pause()
        assert stops == [False, True]
        assert "stopped by force" in str(app.table.get_cell(task.id, app.status_column))
        assert app.check_action("start_task", ()), "and s starts it again"

    run(scenario)


def test_the_view_says_what_it_stopped_to_run_the_app(env, monkeypatch):
    from vivibox.probe import Listener

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


def test_the_view_leaves_at_once_when_a_step_still_waits_on_docker(env, monkeypatch, capsys):
    """Textual runs thread workers in the loop's default executor, and asyncio waits for them at
    the end: a pod start that hung held the window until Ctrl-C, which showed a traceback. Nothing
    is lost by leaving: the pod and the supervisor are processes of their own."""
    import threading
    import time

    stuck = threading.Event()
    executor = app_support.LeavingExecutor()
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
        app.show_deleted = True  # hidden to start with; H shows them
        app.reload()
        app.table.move_cursor(row=1)
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
        assert isinstance(dialog, dialogs.DeleteTask)
        said = " ".join(str(w.render()) for w in dialog.query(Label))
        assert "Delete demo-1?" in str(dialog.query_one(".dialog").border_title), "the question is the title"
        assert "Try the other approach" in said and "$0.20 + $0.00" in said
        assert "Deleted" in said and "none of its work reaches your repository" in said
        assert "Kept" in said and "a line in the history" in said
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
    label = providers_ui.import_label(found)
    assert label.startswith(f"[{look.MUTED}]") and "comes with vivibox; set its mode in Manage" in label


def test_a_folder_is_described_before_you_pick_it(env, tmp_path):
    from vivibox.config import load_project

    empty = tmp_path / "empty"
    empty.mkdir()
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "notes.txt").write_text("x")
    demo = load_project("demo").repo
    assert browse.folder_verdict(empty) == (True, "an empty folder: a new project starts here")
    assert browse.folder_verdict(loose) == (True, "a folder without git: a repository starts here")
    assert browse.folder_verdict(demo) == (True, "already the project demo")


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
        assert isinstance(app.screen, browse.Browse) and app.screen.mode == "any"
        app.screen.dismiss(ticket)
        await pilot.pause()
        assert dialog.query_one("#goal").text == f"Fix it, see @{ticket} "

    run(scenario)


def test_the_folder_browser_opens_with_right_and_picks_with_enter(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "work" / "shop").mkdir(parents=True)
    monkeypatch.setattr(browse, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(browse.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        tree = app.screen.query_one("#tree", browse.PathTree)
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
    monkeypatch.setattr(browse, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(browse.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        browser = app.screen
        tree = browser.query_one("#tree", browse.PathTree)
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
    monkeypatch.setattr(browse, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(browse.Browse("folder", "Pick"), picked.append)
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
    monkeypatch.setattr(browse, "browse_start", lambda: home)
    picked = []

    async def scenario(app, pilot):
        app.push_screen(browse.Browse("folder", "Pick"), picked.append)
        await pilot.pause()
        await pilot.click("#tree", offset=(8, 1), times=2)
        await pilot.pause()

    run(scenario)
    assert picked == [home / "work"]


def test_the_panel_shows_times_on_your_clock(env, monkeypatch):
    import time

    task = new_task()
    at_plan_checkpoint(task)
    gate.accept_plan(task)
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
        app.push_screen(widgets.Confirm("Accept the work?", "Accept"))
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
        assert isinstance(app.screen, dialogs.Reply), "nothing was sent, so nothing closed"
        assert any("Write a comment" in str(n.message) for n in app._notifications)
        assert task.read_state().state is State.CHECKPOINT_PLAN
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, dialogs.Reply), "Escape still leaves"

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
    gate.accept_plan(task)
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


def test_w_while_verifying_offers_the_verification_first_and_the_agents_after(env, monkeypatch):
    """Pressing w during a verification went straight to its log; the writer's and the planner's
    conversations were out of reach until it ended. Now it asks, the verification first."""
    task = implementing()
    task.set_session("writer", "ses_w")
    task.set_session("planner", "ses_p")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    watched = []
    monkeypatch.setattr(tui.Vivibox, "watch", lambda self, task_id, role="": watched.append((task_id, role)))
    task.transition(State.VERIFY)
    (task.meta / "log" / "verify-1-120000.log").write_text("# fresh clone\n")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, dialogs.ChooseSession)
        options = app.screen.query_one(OptionList)
        shown = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert shown[0].startswith("verification") and "running" in shown[0]
        assert shown[1].startswith("writer") and shown[2].startswith("planner")
        await pilot.press("down", "enter")
        await pilot.pause()
        assert watched[-1] == (task.id, "writer")
        await pilot.press("w")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert watched[-1] == (task.id, actions.VERIFICATION)

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
        assert isinstance(app.screen, dialogs.ChooseSession)
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
    panel.after_window(session_gone=True)
    assert capsys.readouterr().out == "\x1b[1A\x1b[2K"
    panel.after_window(session_gone=False)
    assert capsys.readouterr().out == ""


def test_what_each_key_may_do_in_each_state(env):
    """One table of the task's keys per state, checked as a table: the place a wrong key hides."""
    from dataclasses import replace

    from vivibox.panel import keys_for

    task = new_task()

    def keys(state, running=False, paused=False, awaiting_plan=False, busy=False, demo=False, box=False):
        st = replace(task.read_state(), state=state, paused=paused, awaiting_plan=awaiting_plan, box=box)
        return {key for key, allowed in keys_for(task, st, running, busy, demo).items() if allowed}

    plan = keys(State.CHECKPOINT_PLAN)
    assert {"accept", "reply", "edit_plan", "models", "start_task", "remove"} <= plan
    assert not {"verify_again", "open_ide", "show_diff", "demo", "stop_task"} & plan
    assert "accept" not in keys(State.CHECKPOINT_PLAN, awaiting_plan=True), "no plan of yours in yet"
    blocked = keys(State.CHECKPOINT_BLOCKED, running=True)
    assert {"verify_again", "reply", "stop_pod"} <= blocked and not {"stop_task", "start_task"} & blocked
    final = keys(State.CHECKPOINT_FINAL)
    assert {"accept", "open_ide", "show_diff", "demo", "reply"} <= final and "demo_stop" not in final
    assert "demo_stop" in keys(State.CHECKPOINT_FINAL, demo=True)
    implementing = keys(State.IMPLEMENT, running=True)
    assert "stop_task" in implementing and not {"stop_pod", "demo", "edit_plan", "start_task"} & implementing
    assert "start_task" in keys(State.IMPLEMENT, running=False), "nobody is working on it"
    assert "start_task" not in keys(State.IMPLEMENT, running=False, busy=True), "not twice"
    assert "edit_plan" in keys(State.PLAN, running=False) and "edit_plan" not in keys(
        State.PLAN, running=True
    )
    done = keys(State.DONE)
    assert "remove" in done and not {"models", "start_task", "accept"} & done
    box = keys(State.IMPLEMENT, running=True, box=True)
    assert {"enter_box", "accept", "stop_task", "demo", "remove"} <= box
    assert not {"reply", "models", "watch", "verify_again", "start_task"} & box, "a box has no agent"
    assert "start_task" in keys(State.IMPLEMENT, paused=True, box=True)


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
    assert panel.newest_log(task) == newest
    assert panel.pager_command(newest) == ["less", "-R", str(newest)]
    # Where the trouble is: at the end. While the verification runs: following it as it is written.
    assert panel.pager_command(newest, at_end=True) == ["less", "-R", "+G", str(newest)]
    assert panel.pager_command(newest, follow=True) == ["less", "-R", "+F", str(newest)]
    monkeypatch.setenv("PAGER", "more")
    assert panel.pager_command(newest, follow=True) == ["more", str(newest)], "only less knows the flags"
    monkeypatch.setenv("PAGER", "less -R")
    assert panel.log_command(task, task.read_state(), running=False) == ["less", "-R", "+G", str(newest)]
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    assert panel.log_command(task, task.read_state(), running=True) == ["less", "-R", "+F", str(newest)]
    assert panel.log_command(task, task.read_state(), running=False)[2] == "+G", "nothing is being written"

    async def scenario(app, pilot):
        app.reload()
        assert app.check_action("show_log", ())

    run(scenario)


def test_l_always_has_the_timeline_and_picks_among_the_logs(env, monkeypatch):
    """A fresh task has its timeline to read, so l is always on. With more to read, l picks:
    the timeline, the verification logs newest first with what ran and how it went, the
    supervisor's log last; during a verification the cursor starts on its log."""
    from vivibox import logs

    task = new_task()
    opened = []
    monkeypatch.setattr(tui.Vivibox, "read_log", lambda self, command: opened.append(command))
    monkeypatch.setenv("PAGER", "less")

    async def scenario(app, pilot):
        app.reload()
        assert app.check_action("show_log", ())
        await pilot.press("l")
        await pilot.pause()
        assert opened == [["less", "+G", str(task.meta / "log" / "timeline.txt")]], "one entry opens at once"
        assert "created: Goal" in (task.meta / "log" / "timeline.txt").read_text()
        (task.meta / "log" / "verify-1-120000.log").write_text(
            "# started 12:00:00\n$ npm test\nFAIL a\n[exit 1 after 3 s]\n\n# summary\n"
        )
        newest = task.meta / "log" / "verify-2-130000.log"
        newest.write_text("# started 13:00:00\n$ npm test\n[exit 0 after 2 s]\n\n# summary\n")
        (task.meta / "log" / "supervisor.log").write_text("Supervising\n")
        (task.meta / "log" / "writer.log").write_text(
            "=== 12:00:00 writer · implement ===\nPrompt: Go\n\nDone.\n--- 12:01:00 · ok · $0.0100\n\n"
            "=== 12:02:00 writer · implement ===\nPrompt: Fix\n\nFixed.\n--- 12:03:00 · ok · $0.0200\n\n"
        )
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, logs.ChooseLog)
        labels = [e.label for e in app.screen.found]
        assert labels == [
            "timeline",
            "writer.log",
            "verify-2-130000.log",
            "verify-1-120000.log",
            "supervisor.log",
        ]
        assert app.screen.found[1].said == "the writer's conversation · 2 turns · $0.03"
        assert app.screen.found[2].said == "attempt 2 · `npm test` · passed · 2 s · 5 lines"
        assert app.screen.found[3].said == "attempt 1 · `npm test` · failed · 3 s · 6 lines"
        assert app.screen.query_one(OptionList).highlighted == 0, "the timeline first, nothing running"
        await pilot.press("down", "enter")
        await pilot.pause()
        assert opened[-1] == ["less", "+G", str(task.meta / "log" / "writer.log")], "at its newest turn"

    run(scenario)

    at_plan_checkpoint(task)
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)

    async def verifying(app, pilot):
        app.reload()
        await pilot.press("l")
        await pilot.pause()
        assert app.screen.query_one(OptionList).highlighted == 2, "the log being written"
        await pilot.press("enter")
        await pilot.pause()
        assert opened[-1] == ["less", "+F", str(task.meta / "log" / "verify-2-130000.log")], "followed"

    run(verifying)


def test_the_panel_at_implementing_says_what_happened_lately(env, monkeypatch):
    task = implementing("Goal")
    task.event("turn_started", state="implement", role="writer")
    task.event("turn", state="implement", role="writer", ok=True, cost=0.05, tokens=500)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        assert "**Lately**" in app.shown and "writer turn: $0.05, 500 tokens" in app.shown
        assert "`l` reads the whole timeline." in app.shown

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
    return [str(key.value).removeprefix(panel.PROJECT_ROW) for key in app.table.rows]


def cell_of(app, key: str, column: str) -> str:
    """A task's cell by the row's key and the column's name, as the text it shows."""
    names = [c.label.plain for c in app.table.columns.values()]
    return str(app.table.get_row(key)[names.index(column)])


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
        assert "no tasks yet  n new task" in cell(app, 0, "GOAL")
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
        assert rows(app) == ["demo", "demo-2", waiting.id], "newest first"

    run(scenario)
    assert panel.load_view().get("collapsed", []) == []


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
        assert isinstance(app.screen, newtask.NewTask)
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
        assert isinstance(app.screen, dialogs.DeleteTask)
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
        for hidden in ("h", "k"):
            assert hidden not in shown, f"{hidden} is under ? Help"
        assert shown.index("i") == shown.index("n") + 1, "i stands after n, on every row"
        assert shown.index("?") == shown.index("q") - 1
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, dialogs.Help)
        # What the help shows, key by key: its keys stand in a column of their own.
        shown = {keys: said for _, found in dialogs.help_sections() for keys, said in found}
        for key, what in (
            ("i", "set up a project"),
            ("k", "settings: providers & MCP"),
            ("h", "accepted"),
            ("H", "deleted"),
            ("g", "verif"),
        ):
            assert what.lower() in shown[key].lower(), key

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
            assert key in panel.DECISION_KEYS
        assert task.id in shown

    run(scenario, size=(80, 24))


def test_a_wide_terminal_has_every_column(env):
    new_task()

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert len(app.table.columns) == 10, "PLAN and IMPL apart; REVIEW, as the default mode reviews"

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
        assert "`f`" in panel.detail(task, task.read_state(), 3, running=True)

    run(scenario)
    command = [*panel.git_diff(task, load_project("demo")), "--stat"]
    out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    assert "Health.java" in out, command


def test_the_diff_is_paged_on_its_own_screen(monkeypatch):
    """git sets LESS=FRX when it is unset, and -X keeps less on the terminal's main screen: the
    diff scrolled among what the terminal showed before vivibox, and stayed there after q. With
    LESS set to R alone, less takes the alternate screen like the pager under l does, and gives
    the view back whole. Your own LESS is your choice and stays."""
    monkeypatch.delenv("LESS", raising=False)
    assert panel.git_env()["LESS"] == "R"
    monkeypatch.setenv("LESS", "-RX")
    assert panel.git_env()["LESS"] == "-RX"


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
    assert "`x` to delete it from the history" in shown


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
        assert isinstance(app.screen, dialogs.DeleteTask)
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
        assert isinstance(app.screen, widgets.Confirm), "asked once, on start"
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
        assert not isinstance(app.screen, widgets.Confirm)

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
    monkeypatch.setattr(box, "start_box", lambda task_id: None)
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
    monkeypatch.setattr(box, "commit_in_box", lambda task: None)
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


def test_every_dialog_opens_in_the_middle_of_the_screen(env, tmp_path, monkeypatch):
    """Whatever the dialog, it sits in the middle: the rule is on ModalScreen, not on a list of
    names a new dialog can be left off."""
    import inspect

    from textual.screen import ModalScreen

    fresh_project(env, "notes")
    (env / "notes" / "package.json").write_text("{}")

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("notes"))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, settings.ProjectSettings)
        assert app.screen.styles.align == ("center", "middle"), "the project's screen from e, in the middle"
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, AskVerify)
        assert app.screen.styles.align == ("center", "middle"), "the verification from it, in the middle"

    run(scenario)
    stylesheet = Path(tui.__file__).with_name(tui.Vivibox.CSS_PATH).read_text()
    named = [
        name
        for module in (dialogs, browse, providers_ui, widgets, settings)
        for name, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, ModalScreen) and cls.__module__ == module.__name__ and name in stylesheet
    ]
    assert not named, f"dialogs named in the CSS instead of one rule on ModalScreen: {named}"


def test_k_sets_the_ntfy_topic_its_server_and_what_goes_there(env):
    """Three rows under notifications: the topic, a name the app on your phone subscribes to; the
    server, ntfy.sh unless yours; and what goes there, a toggle. A whole address pasted as the
    topic is taken apart into the two."""
    config = env / "config" / "config.toml"
    config.write_text(
        'tasks_dir = "' + str(env / "tasks") + '"\n\n[roles.planner]\nharness = "manual"\nmodel = ""\n\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n\n[notifications]\n# Kept.\ndesktop = true\n'
    )

    def labels(app) -> list[str]:
        options = app.screen.query_one("#rows", OptionList)
        # As they read: without their markup.
        return [
            Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
            for i in range(options.option_count)
        ]

    async def answer(app, pilot, row: str, value: str) -> None:
        index = next(i for i, text in enumerate(labels(app)) if row in text)
        app.screen.query_one("#rows", OptionList).highlighted = index
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, settings.Ask)
        app.screen.query_one(Input).value = value
        await pilot.press("enter")
        await pilot.pause()

    def row(app, name: str) -> str:
        return next(text for text in labels(app) if name in text)

    async def scenario(app, pilot):
        await pilot.press("k")
        await pilot.pause()
        assert "off" in row(app, "ntfy topic") and "https://ntfy.sh" in row(app, "ntfy server")
        await answer(app, pilot, "ntfy topic", "not a name!")
        assert "ntfy =" not in config.read_text(), "refused: a topic is a name"
        await answer(app, pilot, "ntfy topic", "a-name-nobody-guesses")
        text = config.read_text()
        assert 'ntfy = "a-name-nobody-guesses"' in text and "# Kept." in text
        assert "a-name-nobody-guesses" in row(app, "ntfy topic")
        await answer(app, pilot, "ntfy topic", "https://ntfy.example/other")
        text = config.read_text()
        assert 'ntfy = "other"' in text and 'ntfy_server = "https://ntfy.example"' in text, (
            "pasted, taken apart"
        )
        assert "ntfy.example" in row(app, "ntfy server")
        await answer(app, pilot, "ntfy server", "ntfy.sh")
        assert 'ntfy_server = "https://ntfy.example"' in config.read_text(), "refused: an address"
        events = next(i for i, text in enumerate(labels(app)) if "ntfy events" in text)
        assert "decisions" in labels(app)[events]
        app.screen.query_one("#rows", OptionList).highlighted = events
        await pilot.press("enter")
        await pilot.pause()
        assert 'ntfy_events = "all"' in config.read_text() and "all" in labels(app)[events]
        assert app.config.ntfy_events == "all", "the running view reads the file again"

    run(scenario)


def test_k_sets_the_cost_limits_in_dollars(env):
    """Two rows under limits, none unless set: dollars a task may cost before you are told, and
    before it stops. 0 is none."""
    config = env / "config" / "config.toml"
    config.write_text(
        'tasks_dir = "' + str(env / "tasks") + '"\n\n[limits]\n# Kept.\nmax_iterations = 3\n\n'
        '[roles.planner]\nharness = "manual"\nmodel = ""\n\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )

    def labels(app) -> list[str]:
        options = app.screen.query_one("#rows", OptionList)
        # As they read: without their markup.
        return [
            Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
            for i in range(options.option_count)
        ]

    async def answer(app, pilot, row: str, value: str) -> None:
        index = next(i for i, text in enumerate(labels(app)) if row in text)
        app.screen.query_one("#rows", OptionList).highlighted = index
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, settings.Ask)
        app.screen.query_one(Input).value = value
        await pilot.press("enter")
        await pilot.pause()

    def row(app, name: str) -> str:
        return next(text for text in labels(app) if name in text)

    async def scenario(app, pilot):
        await pilot.press("k")
        await pilot.pause()
        assert "none" in row(app, "cost limit") and "none" in row(app, "cost warning")
        await answer(app, pilot, "cost limit", "two")
        assert "cost_limit =" not in config.read_text(), "refused: dollars"
        await answer(app, pilot, "cost limit", "2.5")
        text = config.read_text()
        assert "cost_limit = 2.5" in text and "# Kept." in text and "$2.50" in row(app, "cost limit")
        await answer(app, pilot, "cost warning", "1")
        assert "cost_warning = 1" in config.read_text() and "$1.00" in row(app, "cost warning")
        await answer(app, pilot, "cost limit", "0")
        assert "cost_limit = 0" in config.read_text() and "none" in row(app, "cost limit")

    run(scenario)


def test_the_settings_list_grows_with_the_terminal_and_fits_a_short_one(env):
    """A tall terminal shows every row at once instead of a fixed twenty and a scrollbar; a short
    one keeps the whole dialog, its hint included, on the screen."""
    config = env / "config" / "config.toml"
    config.write_text(
        'tasks_dir = "' + str(env / "tasks") + '"\n\n[roles.planner]\nharness = "manual"\nmodel = ""\n\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )

    async def tall(app, pilot):
        await pilot.press("k")
        await pilot.pause()
        rows = app.screen.query_one("#rows", OptionList)
        assert rows.option_count >= 20, "as many rows as the old fixed height held, or more"
        assert rows.size.height >= rows.option_count, f"{rows.size.height} lines for {rows.option_count} rows"
        assert rows.max_scroll_y == 0, "nothing to scroll on a tall terminal"

    async def short(app, pilot):
        await pilot.press("k")
        await pilot.pause()
        dialog = app.screen.query_one(".dialog")
        assert dialog.region.height <= app.size.height, "the dialog fits"
        about = app.screen.query_one("#about")
        assert about.region.y + about.region.height <= app.size.height, "what the row does is on the screen"
        assert app.screen.query_one("#rows", OptionList).max_scroll_y > 0, "the rows scroll instead"

    run(tall, size=(140, 60))
    run(short, size=(140, 24))


# --- the reviewer in the view ------------------------------------------------------------------


def with_reviewer(env):
    config = env / "config" / "config.toml"
    config.write_text(config.read_text() + '[roles.reviewer]\nharness = "opencode"\nmodel = "other/strong"\n')


def with_mode(env, mode: str):
    from vivibox import configfile

    configfile.set_value(env / "config" / "config.toml", "agent_orchestration_mode", mode)


def test_the_cost_is_three_columns_and_review_shows_only_where_someone_reviews(env, monkeypatch):
    """PLAN, IMPL and REVIEW, one figure each, instead of a sum to read in one cell; the third
    column only where the mode has someone other than the writer review, so a list of a mode
    without one looks as it did."""
    with_mode(env, "planner_executor")
    task = implementing()
    task.event("turn", state="plan", role="planner", cost=0.10, tokens=1)
    task.event("turn", state="implement", role="writer", cost=0.04, tokens=1)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: False)

    async def without(app, pilot):
        app.reload()
        await pilot.pause()
        assert app.columns == (
            "TASK",
            "STATUS",
            "APP",
            "CRITERIA",
            "PLAN",
            "IMPL",
            "CREATED",
            "UPDATED",
            "GOAL",
        )
        assert str(app.table.get_cell(task.id, app.plan_column)) == "$0.10"
        assert str(app.table.get_cell(task.id, app.impl_column)) == "$0.04"

    run(without)
    with_mode(env, "planner_maker_checker")
    task.event("turn", state="review", role="reviewer", cost=0.02, tokens=1)

    async def with_(app, pilot):
        app.reload()
        await pilot.pause()
        assert "REVIEW" in app.columns and app.columns.index("REVIEW") == app.columns.index("IMPL") + 1
        assert str(app.table.get_cell(task.id, app.review_column)) == "$0.02"
        assert str(app.table.get_cell(task.id, app.impl_column)) == "$0.04", (
            "the review is not implementation"
        )
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        app.action_details()  # a second app in one test gets no keys from the pilot
        assert "- **Reviewer** · m (the writer's) · 1 turn · $0.02" in app.shown, (
            "no role of its own: the writer's"
        )
        assert "- **Total** · 3 turns · $0.16" in app.shown

    run(with_)


def test_a_finished_task_keeps_its_review_cost_apart(env):
    from vivibox.review import history_path

    with_reviewer(env)
    entry = {"id": "demo-9", "project": "demo", "title": "Done one", "cost": 0.16, "planning": 0.1,
             "review": 0.02, "created": now(), "finished": now(), "commit": "abc"}  # fmt: skip
    history_path().parent.mkdir(parents=True, exist_ok=True)
    history_path().write_text(json.dumps(entry) + "\n")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert str(app.table.get_cell("demo-9", app.plan_column)) == "$0.10"
        assert str(app.table.get_cell("demo-9", app.impl_column)) == "$0.04"
        assert str(app.table.get_cell("demo-9", app.review_column)) == "$0.02"

    run(scenario)


def test_the_final_checkpoint_shows_the_reviewers_notes_and_l_opens_them(env, monkeypatch):
    from vivibox import logs, reviewing

    task = implementing()
    task.transition(State.VERIFY)
    task.transition(State.REVIEW)
    reviewing.keep(task, 1, "# Review 1\n\n## Blocking\n\n- [ ] a.py:1 — wrong\n\n## Not blocking\n")
    task.event("review", round=1, blocking=1, not_blocking=0, problem="")
    task.set_reviews(1)
    task.transition(State.CHECKPOINT_FINAL, reason="review 1: 1 blocking note")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    monkeypatch.setattr(actions, "changed_files", lambda task, project: "a.py | 1 +")
    monkeypatch.setattr(tui.Vivibox, "read_log", lambda self, command: None)
    monkeypatch.setenv("PAGER", "less")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.press("d")
        await pilot.pause()
        assert "#### Review 1: 1 blocking, 0 not blocking" in app.shown and "a.py:1 — wrong" in app.shown
        await pilot.press("l")
        await pilot.pause()
        assert isinstance(app.screen, logs.ChooseLog)
        labels = [(e.label, e.said) for e in app.screen.found]
        assert ("review-1.md", "review 1 · 1 blocking · 0 not blocking") in labels

    run(scenario)


def test_n_asks_how_the_task_is_orchestrated_and_the_reviewers_model_follows(env, monkeypatch):
    """Flow heads the workflow, on config.toml's mode; each option says how many sessions and
    what review; a model row per agent of the flow, named as the flow names it; the help under
    the fields draws the flow. What the form sends names the mode and the rounds."""
    with_reviewer(env)
    calls = []

    def create(project, goal, **kw):
        calls.append(kw)
        return new_task(goal)  # goes through the same create again, so the first call is the view's

    monkeypatch.setattr(actions, "create", create)
    monkeypatch.setattr(actions, "start", lambda task_id, resume=False, on_step=None: "m")

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"Add divide")
        mode = app.screen.query_one("#orchestration", Select)
        reviewer = app.screen.query_one("#role-reviewer", Select)
        assert mode.value == "planner_maker_checker", "config.toml's mode, ready to keep or change"
        labels = [str(t) for t, _ in mode._options]
        names = ["Single agent", "Planner → Executor", "Planner → Writer → Reviewer", "Supervisor ⇄ Worker"]
        assert [label.split("  ")[0] for label in labels] == names, "names that say how the agents work"
        assert all("session" in label.split("  ")[1] for label in labels), "and beside each, to compare"
        assert not mode.tooltip, "no floating paragraph"
        assert reviewer.parent.display and reviewer.value == (OC, "other/strong")

        def agents() -> list[str]:
            """The model rows shown, by what the flow calls them."""
            return [
                str(row.query_one(".key", Label).render())
                for row in app.screen.query(".agent")
                if row.display
            ]

        help_ = app.screen.query_one(widgets.ContextHelp)
        mode.focus()  # the flow's details are the help's while Flow has focus
        await pilot.pause()
        # A row per agent of the flow, as the flow names it; the diagram in the help says where
        # the verification runs.
        for name, shown, flow in (
            ("planner_maker_checker", ["Planner", "Writer", "Reviewer"], "P → W → Gate → R ⇄ W"),
            ("single_agent", ["Agent"], "P+W+R → Gate"),
            ("planner_executor", ["Planner", "Executor"], "P → W+R → Gate"),
            ("supervisor_worker", ["Supervisor", "Worker"], "P → W → Gate → (P+R) ⇄ W"),
        ):
            mode.value = name
            await pilot.pause()
            assert agents() == shown, name
            assert help_.said.diagram == flow, name
        assert help_.said.facts[1:4] == (
            ("Supervisor", "m"),
            ("Shared", "Planner + Reviewer"),
            ("Worker", "m"),
        )
        app.screen.query_one("#role-planner", Select).value = (OC, "other/strong")
        await pilot.pause()
        assert help_.said.facts[1] == ("Supervisor", "other/strong"), "the models picked"
        # What one fix round is, said by Fix rounds' help, by the flow.
        app.screen.query_one("#max-rounds").focus()
        await pilot.pause()
        assert "supervisor's blocking notes" in help_.said.summary
        mode.value = "planner_maker_checker"
        await pilot.pause()
        assert reviewer.parent.display
        mode.value = "planner_executor"
        app.screen.query_one("#max-rounds", Input).value = "5"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls[0]["orchestration"] == "planner_executor" and calls[0]["max_rounds"] == 5
        assert "reviewer" not in calls[0]["roles"]

    run(scenario)


def test_without_a_reviewer_n_still_asks_how_the_task_is_orchestrated(env, monkeypatch):
    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        assert app.screen.query("#orchestration") and not app.screen.query("#role-reviewer")

    run(scenario)


def test_k_adds_a_reviewer_and_sets_the_orchestration_and_the_rounds(env):
    config = env / "config" / "config.toml"

    def labels(app) -> list[str]:
        options = app.screen.query_one("#rows", OptionList)
        # As they read: without their markup.
        return [
            Text.from_markup(str(options.get_option_at_index(i).prompt)).plain
            for i in range(options.option_count)
        ]

    def row(app, name: str) -> str:
        return next(text for text in labels(app) if text.strip().startswith(name))

    def pick(app, name: str) -> None:
        index = next(i for i, text in enumerate(labels(app)) if text.strip().startswith(name))
        app.screen.query_one("#rows", OptionList).highlighted = index

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("k")
        await pilot.pause()
        assert "the writer's model" in row(app, "reviewer") and not any(
            "review mode" in r for r in labels(app)
        )
        assert "Planner → Writer → Reviewer" in row(app, "flow")
        pick(app, "reviewer")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, dialogs.ChooseModel)
        await pilot.press("enter")  # the first model offered
        await pilot.pause()
        text = config.read_text()
        assert "[roles.reviewer]" in text and 'model = "deepseek/deepseek-v4-flash"' in text
        assert "\nmode = " not in text.split("[roles.reviewer]")[1], "no review mode any more"
        assert "deepseek-v4-flash" in row(app, "reviewer")
        pick(app, "flow")
        await pilot.press("enter")
        await pilot.pause()
        text = config.read_text()
        assert 'agent_orchestration_mode = "supervisor_worker"' in text, "the next mode, at the top level"
        assert text.index("agent_orchestration_mode") < text.index("["), "before any table"
        assert "Supervisor ⇄ Worker" in row(app, "flow")
        pick(app, "rounds")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, settings.Ask)
        app.screen.query_one(Input).value = "4"
        await pilot.press("enter")
        await pilot.pause()
        assert "max_rounds = 4" in config.read_text() and "4" in row(app, "rounds")

    run(scenario)


def test_ctrl_c_in_a_pager_stops_the_pager_not_the_view():
    """Ctrl-C reaches every process on the terminal. In less +F it stops following, and it must
    not also end the view waiting behind the pager once you press q."""
    import sys

    script = (
        "from vivibox.app_support import in_terminal\n"
        "in_terminal(['sh', '-c', 'kill -INT $PPID; exit 0'])\n"
        "print('still here')\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert done.stdout == "still here\n", done.stderr[-300:]


def test_ctrl_q_in_the_view_does_not_quit_it(env):
    """Ctrl-q is the way back from the agent's window; Textual's default made it quit the view,
    so a press a second late, or after a w that opened nothing, closed vivibox without a word."""

    async def scenario(app, pilot):
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running, "the view is still here"
        assert any("q quits" in str(n.message) for n in app._notifications), "and says how to leave"

    run(scenario)


def test_the_agents_window_opens_from_inside_tmux_too(env, monkeypatch):
    """A view started in tmux carries $TMUX; tmux then refuses to attach the agent's session and
    w shows nothing. The hand-off leaves $TMUX behind: the agent's server is another one."""
    from vivibox import actions, tui

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    assert "TMUX" not in actions.outside_tmux() and actions.outside_tmux()["PATH"]
    ran = {}
    monkeypatch.setattr(
        actions, "attach_command", lambda task_id, role="": ["tmux", "-L", "vivibox", "attach"]
    )
    monkeypatch.setattr(actions, "tmux_has", lambda target: True)
    monkeypatch.setattr(tui.subprocess, "run", lambda command, **kw: ran.update(command=command, **kw))
    monkeypatch.setattr(tui.Vivibox, "suspend", lambda self: contextlib.nullcontext())
    tmux_calls = []
    monkeypatch.setattr(
        actions,
        "tmux",
        lambda *a, **kw: tmux_calls.append(list(a)) or subprocess.CompletedProcess(a, 0, "", ""),
    )

    async def scenario(app, pilot):
        app.watch("demo-1")
        assert ran["command"][:2] == ["tmux", "-L"] and "TMUX" not in ran["env"]
        assert ["kill-session", "-t", "vivibox-demo-1"] in tmux_calls, (
            "closed on leaving: nothing renders for nobody"
        )

    run(scenario)


def test_projects_keep_their_order_whatever_their_tasks_do(env, monkeypatch):
    """A project that jumped to the top when a task of its started to wait moved every row under
    the cursor, and a key pressed then landed on another task. Projects are listed by name; what
    waits for you says so by its colour, the header's count and where the cursor starts."""
    fresh_project(env, "zulu")
    fresh_project(env, "alpha")
    quiet = new_task("Quiet")
    quiet.set_paused(True)
    assert main(["new", "zulu", "Waiting in zulu", "--draft"]) == 0
    waiting = find_task(load_config().tasks_dir, "zulu-1")
    at_plan_checkpoint(waiting)

    async def scenario(app, pilot):
        app.reload()
        assert rows(app) == ["alpha", "demo", "demo-1", "zulu", "zulu-1"], "by name, waiting or not"
        assert app.selected_id() == "zulu-1", "the cursor starts on what waits for you"
        at_plan_checkpoint(quiet)
        quiet.set_paused(False)
        app.reload()
        await pilot.pause()
        assert rows(app) == ["alpha", "demo", "demo-1", "zulu", "zulu-1"], (
            "a project that starts to wait stays put"
        )

    run(scenario)


def test_an_unticked_box_shows_no_mark(env):
    """Textual draws the toggle's X in a darker shade of its own background when it is off, and
    in the view's colours that shade was visible: the box of a new task looked ticked, and
    "Nothing to build" looked chosen. Off, the mark has the colour of its box; on, it is seen.
    The box left in the view is the verification's "let the writer find the command"."""

    async def scenario(app, pilot):
        app.push_screen(AskVerify("demo", ["make test"]))
        await pilot.pause()
        box = app.screen.query_one(Checkbox)
        assert not box.value
        off = box.get_component_rich_style("toggle--button")
        assert off.color == off.bgcolor, "an unticked box shows no mark"
        app.pop_screen()
        app.push_screen(AskVerify("demo", []))  # ticking dismisses; with no command it opens ticked
        await pilot.pause()
        box = app.screen.query_one(Checkbox)
        assert box.value
        on = box.get_component_rich_style("toggle--button")
        assert on.color != on.bgcolor, "a ticked box shows its mark"

    run(scenario)


def test_i_is_offered_on_every_row_and_the_header_has_no_palette_icon(env):
    """Setting up a project was under ? only; a person with one project and no memory of the
    empty screen's hint had no way to find it. The header's icon opened Textual's command
    palette, which vivibox does not use."""
    from textual.widgets._header import HeaderIcon

    task = new_task()

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("demo"))
        await pilot.pause()
        assert "new_project" in keys(app), "on a project's row"
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        assert "new_project" in keys(app), "and on a task's"
        assert not app.query(HeaderIcon), "no header with the palette's icon: the bar is vivibox's"

    run(scenario)


def test_the_header_says_what_today_cost(env):
    """The limits are in dollars, and every row shows its own figure; the day's sum was nowhere."""
    task = new_task()
    task.event("turn", state="plan", ok=True, cost=0.05, tokens=1000, error="")
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    today = now()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="milliseconds")
    entries = [
        {"id": "demo-8", "project": "demo", "title": "Today", "cost": 0.12, "commit": "a", "branch": "",
         "conflicts": [], "finished": today, "created": today},
        {"id": "demo-7", "project": "demo", "title": "Yesterday", "cost": 0.5, "commit": "b", "branch": "",
         "conflicts": [], "finished": yesterday, "created": yesterday},
    ]  # fmt: skip
    actions.history_path().write_text("".join(json.dumps(e) + "\n" for e in entries))

    async def scenario(app, pilot):
        app.reload()
        assert app.sub_title.endswith("$0.17 today"), app.sub_title
        assert "0.5" not in app.sub_title, "yesterday's is history"

    run(scenario)


def test_the_header_says_first_what_would_keep_every_task_from_starting(env, monkeypatch):
    """Docker down, or a provider without a key, showed only once a task failed to start, in a
    message gone in seconds. The header says it before you press n."""
    from vivibox import keys, roles

    path = env / "config" / "config.toml"
    path.write_text(path.read_text().replace('model = "m"', 'model = "deepseek/deepseek-v4-flash"'))
    config = load_config()
    assert roles.provider_keys(config) == ["deepseek"]
    assert roles.machine_problem(config, docker_ok=True) == "no key for deepseek: press k"
    keys.set_key("deepseek", "sk-test")
    assert roles.machine_problem(config, docker_ok=True) == "", "whole now"
    assert roles.machine_problem(config, docker_ok=False).startswith("Docker is not running")
    from vivibox import probe

    monkeypatch.setattr(probe, "docker_running", lambda: False)

    async def scenario(app, pilot):
        app.reload()
        await app.workers.wait_for_complete()
        app.reload()
        await pilot.pause()
        assert app.sub_title.startswith("Docker is not running: no task can start · "), app.sub_title
        # The warning in red and bold, not in the dim the rest of the header has: it was grey
        # and read as one more count.
        shown = app.format_title(app.title, app.sub_title)
        note = "Docker is not running: no task can start"
        styles = [str(span.style) for span in shown.spans if shown.plain[span.start : span.end] == note]
        assert styles and all("red" in style and "bold" in style for style in styles), shown.spans

    run(scenario)


@pytest.mark.parametrize("missing", ["name", "email", "both"])
def test_n_without_git_identity_stays_on_list_and_explains_commands(env, monkeypatch, missing):
    from ux import screen_text

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("name", "email") if missing == "both" else (missing,):
        subprocess.run(["git", "config", "--unset", f"user.{key}"], cwd=env / "repo", check=True)

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(app.screen, newtask.NewTask)
        said = screen_text(app)
        assert "Git identity is missing" in said
        assert "git config --global user.name" in said
        assert "git config --global user.email" in said
        assert "without --global" in said
        assert not load_config().tasks_dir.exists()

    run(scenario, notifications=True)


@pytest.mark.parametrize("local", [False, True])
def test_n_accepts_a_complete_local_or_global_git_identity(env, monkeypatch, local):
    if local:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    else:
        for key in ("name", "email"):
            subprocess.run(["git", "config", "--unset", f"user.{key}"], cwd=env / "repo", check=True)

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, newtask.NewTask)

    run(scenario, notifications=True)


def test_new_task_rechecks_identity_if_it_disappears_while_form_is_open(env, monkeypatch):
    from ux import screen_text

    async def scenario(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, newtask.NewTask)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        subprocess.run(["git", "config", "--unset", "user.email"], cwd=env / "repo", check=True)
        app.screen.query_one("#goal", TextArea).load_text("Do something")
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not load_config().tasks_dir.exists()
        assert "Git identity is missing" in screen_text(app)

    run(scenario, notifications=True)
