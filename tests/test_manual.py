import pytest

from vivibox import actions, gate, manual, supervisor, ui
from vivibox.config import load_project
from vivibox.opencode import HarnessError, Turn
from vivibox.plan import PlanError
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = (
    '+++\nmode = "code-only"\nverify = []\n+++\n\n# Goal\n\n{{goal}}\n\n## Acceptance criteria\n\n- [ ] x\n'
)
PLAN = (
    '+++\nmode = "code-only"\nsummary = "Add a health endpoint"\nverify = ["npm test"]\n+++\n\n'
    "# Goal\n\nHealth.\n\n## Approach\n\n```js\napp.get('/health')\n```\n\n"
    "## Acceptance criteria\n\n- [ ] GET /health returns 200\n"
)


class Writer:
    """The writer's harness, which reports on the repository for a chat that cannot see it."""

    name = "opencode"

    def __init__(self, task):
        self.task, self.prompts = task, []

    def turn(self, prompt, session="", title=""):
        self.prompts.append(prompt)
        (self.task.meta / "handoff" / manual.CONTEXT).write_text("Express 4, tests with vitest.")
        return Turn("ses_1", True, 0.01, 100, "done")


@pytest.fixture
def task(tmp_path):
    return create_task(tmp_path / "tasks", "demo", "Add health endpoint", TEMPLATE)


def make(task, writer, source):
    notes = []
    sup = supervisor.Supervisor(
        task,
        writer,
        run_gate=lambda t: None,
        risky_changes=lambda: [],
        max_iterations=2,
        notify=lambda _id, msg, kind="": notes.append(msg),
        planner=manual.Manual(),
        source=source,
    )
    return sup, notes


def test_a_new_project_goes_straight_to_you(task, tmp_path):
    """Nothing to report on in an empty repository, so no turn is spent: the goal is the brief."""
    task.repo.mkdir(parents=True, exist_ok=True)
    (task.repo / ".git").mkdir()
    writer = Writer(task)
    sup, notes = make(task, writer, tmp_path / "checkout")
    assert sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_PLAN and st.awaiting_plan
    assert writer.prompts == []
    assert "Nothing: this is a new project." in (task.meta / manual.PROMPT).read_text()
    assert "vivibox plan prompt" in notes[0]
    # The list says what is wanted: a plan from you, not a verdict on one that does not exist.
    assert ui.activity(st, 3) == "plan it yourself"
    assert ui.next_commands(st)[0] == f"vivibox plan prompt {st.id}"


def test_a_chat_that_cannot_see_the_repository_is_told_what_is_in_it(task, tmp_path):
    task.repo.mkdir(parents=True, exist_ok=True)
    (task.repo / "package.json").write_text("{}")
    writer = Writer(task)
    source = tmp_path / "checkout"
    sup, _ = make(task, writer, source)
    sup.step()
    assert writer.prompts == [manual.RECON_PROMPT], "the writer reports; the planner runs nothing"
    web = (task.meta / manual.PROMPT).read_text()
    assert "Express 4, tests with vitest." in web and "Add health endpoint" in web
    cli = (task.meta / manual.PROMPT_CLI).read_text()
    # Your checkout, not the clone: an agent has written to the clone, and a CLI started there
    # would load the settings and hooks it left.
    assert f"The repository is {source}" in cli and str(task.repo) not in cli
    # The answer goes where the agent can read but not write, so what you open is what your chat wrote.
    assert str(task.meta / manual.ANSWER) in cli and "/handoff/" not in str(task.meta / manual.ANSWER)


def test_the_manual_planner_runs_no_turns():
    with pytest.raises(HarnessError):
        manual.Manual().turn("go")


def test_the_plan_is_found_in_what_you_paste():
    """A chat puts words around its block, and the plan quotes code in blocks of its own: a fence
    of three backticks inside must not end the fence of four around it."""
    pasted = f"Here is the final plan:\n\n````markdown\n{PLAN}````\n\nGood luck!\n"
    assert manual.extract(pasted) == PLAN
    assert manual.extract(PLAN) == PLAN, "a copy button gives the block's content alone"


def test_an_answer_that_is_not_a_plan_leaves_the_task_waiting(task):
    task.set_awaiting_plan(True)
    (task.meta / manual.ANSWER).write_text("Sure! What stack do you use?")
    with pytest.raises(PlanError):
        manual.import_answer(task)
    assert task.read_state().awaiting_plan
    assert "````markdown" in manual.repair_prompt("no header"), "the fix goes back to the same chat"


def test_a_plan_brought_in_is_yours_to_accept(task):
    task.set_awaiting_plan(True)
    (task.meta / manual.ANSWER).write_text(f"````markdown\n{PLAN}````\n")
    assert manual.import_answer(task) == "Add a health endpoint"
    st = task.read_state()
    assert not st.awaiting_plan and st.goal == "Add a health endpoint"
    assert "GET /health returns 200" in task.plan_path.read_text()


@pytest.fixture
def manual_env(env):
    cfg = env / "config" / "config.toml"
    cfg.write_text(
        cfg.read_text().replace(
            '[roles.planner]\nharness = "opencode"\nmodel = "m"', '[roles.planner]\nharness = "manual"'
        )
    )
    t = create_task(env / "tasks", "demo", "Add health endpoint", TEMPLATE)
    t.transition(State.CHECKPOINT_PLAN)
    t.set_awaiting_plan(True)
    (t.meta / manual.PROMPT).write_text("prompt")
    return t


def test_nothing_to_accept_before_your_plan_is_in(manual_env):
    with pytest.raises(gate.GateError, match="vivibox plan import"):
        actions.accept_plan(manual_env, load_project("demo"))


def test_a_reply_has_nobody_to_go_to(manual_env):
    """The planner is a chat of yours. Sending the comment to the plan state would start a turn
    that nothing runs, so it is refused with where the comment belongs."""
    with pytest.raises(gate.GateError, match="your own chat"):
        actions.reply(manual_env, "use Fastify")
    assert manual_env.read_state().state is State.CHECKPOINT_PLAN


def test_the_whole_way_from_answer_to_implementation(manual_env):
    summary = actions.import_plan(manual_env, f"Final:\n````markdown\n{PLAN}````\n")
    assert summary == "Add a health endpoint"
    actions.accept_plan(manual_env, load_project("demo"))
    assert manual_env.read_state().state is State.IMPLEMENT


def test_the_command_line_takes_the_answer_a_cli_wrote(manual_env, capsys, monkeypatch):
    """A CLI in your checkout writes its answer to the answer file itself, so import needs no
    argument. What is not a plan is refused with the message to give the same chat."""
    from vivibox.cli import main

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert main(["plan", "prompt", manual_env.id]) == 0 and capsys.readouterr().out == "prompt"
    actions.answer_path(manual_env).write_text("I need to know the framework first.")
    assert main(["plan", "import", manual_env.id]) == 1
    assert "````markdown" in capsys.readouterr().err
    actions.answer_path(manual_env).write_text(PLAN)
    assert main(["plan", "import", manual_env.id]) == 0
    assert f"vivibox accept {manual_env.id}" in capsys.readouterr().out
