import os
import time
from importlib.resources import files

import pytest

from vivibox import actions, gate, manual, supervisor, ui
from vivibox.config import load_config, load_project
from vivibox.harness import Harness, HarnessError, Turn
from vivibox.plan import PlanError, parse_plan
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = (
    '+++\nmode = "code-only"\nverify = []\n+++\n\n# Goal\n\n{{goal}}\n\n## Acceptance criteria\n\n'
    f"- [ ] {gate.PLACEHOLDER}\n"
)
PLAN = (
    '+++\nmode = "code-only"\nsummary = "Add a health endpoint"\nverify = ["npm test"]\n+++\n\n'
    "# Goal\n\nHealth.\n\n## Approach\n\n```js\napp.get('/health')\n```\n\n"
    "## Acceptance criteria\n\n- [ ] GET /health returns 200\n"
)


class Writer(Harness):
    """The writer's harness, which reports on the repository for a chat that cannot see it."""

    name = "opencode"

    def __init__(self, task):
        self.task, self.prompts = task, []

    def turn(self, prompt, session="", title="", on_step=None):
        self.prompts.append(prompt)
        (self.task.meta / "handoff" / manual.CONTEXT).write_text("Express 4, tests with vitest.")
        return Turn("ses_1", True, 0.01, 100, "done")


@pytest.fixture
def task(tmp_path):
    return create_task(tmp_path / "tasks", "demo", "Add health endpoint", TEMPLATE)


def make(task, writer, source):
    notes = []
    ports = supervisor.Ports(
        run_gate=lambda t: None,
        risky_changes=lambda: [],
        notify=lambda _id, msg, kind="": notes.append(msg),
    )
    sup = supervisor.Supervisor(task, writer, ports, max_iterations=2, planner=manual.Manual(), source=source)
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
    assert [p.endswith(manual.RECON_PROMPT) for p in writer.prompts] == [True], (
        "the writer reports; the planner runs nothing"
    )
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


# How Claude Code printed the plan of the first real run, and how it was copied from the terminal:
# indented, headings without their '#', and the header described in a line of prose.
TERMINAL = """  Summary: Add a Reset view button to the 3D preview

  Context
  - The camera starts at (90, -120, 90).

  Approach
  1. Extract the orbit maths into orbit.js.

     No DOM.

  Acceptance criteria
  - [ ] after rotate then reset, the camera is back at (90, -120, 90)
  - [ ] clicking the button calls resetView once

  Out of scope
  - Zoom

  Header: kind feature, mode code-only, verify = ["npm ci && npm test"]. Plain npm test fails here.
"""


def test_a_plan_copied_from_a_terminal_is_read_with_the_tasks_own_header(task):
    """The chat decides the summary, the sections and the criteria; kind, mode and the rest were
    decided when the task was made. Asking a chat to reproduce a TOML header got it back as prose."""
    real = (files("vivibox") / "templates" / "plan.md").read_text()
    task.plan_path.write_text(real.replace("{{kind}}", "bug").replace("{{goal}}", "Add health endpoint"))
    task.set_awaiting_plan(True)
    (task.meta / manual.ANSWER).write_text(TERMINAL)
    assert manual.import_answer(task) == "Add a Reset view button to the 3D preview"
    plan = parse_plan(task.plan_path.read_text())
    assert plan.kind == "bug", "the header is the task's, not rebuilt from defaults"
    assert plan.verify == ["npm ci && npm test"]
    assert [c.text for c in plan.criteria] == [
        "after rotate then reset, the camera is back at (90, -120, 90)",
        "clicking the button calls resetView once",
    ]
    text = task.plan_path.read_text()
    # The goal you gave, which the chat left out, and headings where the template has them.
    assert "# Goal\n\nAdd health endpoint" in text and "## Approach" in text and "Header:" not in text


def test_a_verify_line_is_taken_as_the_command(task):
    task.set_awaiting_plan(True)
    (task.meta / manual.ANSWER).write_text(
        "Summary: x\nVerify: `npm test`\n\n## Acceptance criteria\n\n- [ ] it works\n"
    )
    manual.import_answer(task)
    assert parse_plan(task.plan_path.read_text()).verify == ["npm test"]


def test_the_chat_is_asked_for_a_command_only_when_the_project_has_none(task, tmp_path):
    """A planning chat asked where red.md goes and which command to use: vivibox decides both,
    and the prompt says so rather than leaving the chat to guess or ask."""
    web, cli = manual.prompts(task, tmp_path)
    assert "Verify:" in web and "Verify:" in cli
    web, _ = manual.prompts(task, tmp_path, ["npm ci", "npm test"])
    assert "Verify:" not in web and "`npm ci && npm test`" in web and "red.md" in web
    assert "+++" not in web, "the header is vivibox's; the chat is not asked for it"
    # It asked what UI it was talking to: the prompt says who reads the plan and that nobody can ask.
    assert "carries it out alone" in web and "carries it out alone" in cli
    assert web.index("# What is in the repository") < web.index("# When the plan is final"), "asked last"


def test_a_task_on_another_providers_model_gets_that_providers_key(env):
    """The keys came from config.toml alone, so a task moved to another provider's model started
    without the key it needed."""
    cfg = env / "config" / "config.toml"
    cfg.write_text(cfg.read_text().replace('model = "m"', 'model = "deepseek/deepseek-v4-flash"'))
    t = actions.create("demo", "Fix login", roles={"writer": ("opencode", "anthropic/claude-sonnet-5")})
    assert actions.provider_keys(load_config(), t) == ["deepseek", "anthropic"]
    assert actions.provider_keys(load_config()) == ["deepseek"]


def test_the_command_line_chooses_a_model_too(env, capsys):
    from vivibox.cli import main

    assert main(["new", "demo", "Fix login", "--draft", "--model", "writer=deepseek/deepseek-v4-pro"]) == 0
    tasks = load_config().tasks_dir
    assert actions.load("demo-1")[0].read_state().models == {"writer": "deepseek/deepseek-v4-pro"}
    assert main(["new", "demo", "Fix login", "--draft", "--model", "writer"]) == 1
    assert "role=model" in capsys.readouterr().err and tasks.is_dir()


def waiting_for_you(task, tmp_path):
    """A task at its plan checkpoint, prompt written a minute ago."""
    task.repo.mkdir(parents=True, exist_ok=True)
    sup, notes = make(task, Writer(task), tmp_path)
    sup.step()
    past = time.time() - 60
    os.utime(task.meta / manual.PROMPT, (past, past))
    return sup, notes


def answer(task, text, age):
    path = task.meta / manual.ANSWER
    path.write_text(text)
    when = time.time() - age
    os.utime(path, (when, when))


def test_a_plan_a_cli_writes_moves_the_task_on_by_itself(task, tmp_path):
    """The CLI writes the answer file, and nothing told vivibox: the view sat at "plan it
    yourself" with the plan already there, until you happened to press e."""
    sup, notes = waiting_for_you(task, tmp_path)
    answer(task, PLAN, age=5)
    assert not sup.step(), "still your turn: the plan is yours to accept"
    st = task.read_state()
    assert not st.awaiting_plan and st.goal == "Add a health endpoint"
    assert notes[-1] == "your plan is in, 1 criteria; review and accept it"


def test_an_answer_older_than_the_prompt_is_not_taken(task, tmp_path):
    """One left from an earlier round must not stand in for the plan you are writing now."""
    sup, notes = waiting_for_you(task, tmp_path)
    answer(task, PLAN, age=120)
    sup.step()
    assert task.read_state().awaiting_plan and len(notes) == 1


def test_a_file_still_being_written_is_left_alone(task, tmp_path):
    sup, _ = waiting_for_you(task, tmp_path)
    answer(task, PLAN, age=0)
    sup.step()
    assert task.read_state().awaiting_plan
    answer(task, PLAN, age=5)
    sup.step()
    assert not task.read_state().awaiting_plan


def test_an_answer_that_is_not_a_plan_is_reported_once(task, tmp_path):
    sup, notes = waiting_for_you(task, tmp_path)
    answer(task, "Let me look at the viewer first.", age=5)
    sup.step()
    sup.step()
    assert task.read_state().awaiting_plan
    assert sum("not readable yet" in n for n in notes) == 1


def test_a_chat_is_told_when_the_agent_wrote_no_report(task, tmp_path):
    task.repo.mkdir()
    (task.repo / "app.js").write_text("x")
    web, _ = manual.prompts(task, tmp_path / "src")
    assert "wrote no report" in web and "new project" not in web, "an empty report is not a new project"
