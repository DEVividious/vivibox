"""The review in an agent's CLI (ADR-0035): Supervisor ⇄ Worker planned in the CLI has the same
conversation review each round; vivibox waits for its review as it waits for the plan."""

import io
import json

import pytest
from test_supervisor import BLOCKING, CLEAN, FakeHarness, accepted, gate_result, make

from vivibox import actions, manual, orchestration, prompts, reviewing, waiting
from vivibox.config import ConfigError, Role
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = '+++\nmode = "code-only"\n+++\n\n# Goal\n\n{{goal}}\n\n## Acceptance criteria\n\n- [ ] it works\n'


@pytest.fixture
def task(tmp_path):
    return create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)


def in_cli(task, tmp_path):
    """A task planned in the CLI, at its first implementing turn, on Supervisor ⇄ Worker."""
    accepted(task)
    task.set_plan_in_cli(True)
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)] * 3)
    sup.mode, sup.planner, sup.max_rounds = orchestration.MODES["supervisor_worker"], manual.Manual(), 2
    copy = tmp_path / "review-copy"
    copy.mkdir()
    sup.ports.prepare_review = lambda: copy
    return sup, notes, copy


def to_review(sup, task):
    sup.step()  # implement
    sup.step()  # verify: green
    assert task.read_state().state is State.REVIEW


def test_a_green_gate_waits_for_the_review_from_the_cli_without_a_turn(task, tmp_path):
    sup, notes, copy = in_cli(task, tmp_path)
    to_review(sup, task)
    sup.step()  # asks: the prompt, and no turn
    assert sup.harness.prompts[-1] != prompts.REVIEW_PROMPT
    st = task.read_state()
    assert st.state is State.REVIEW and st.awaiting_review
    asked = reviewing.cli_prompt(task)
    assert prompts.REVIEW_RULES in asked and f"git -C {copy} diff --cached" in asked
    assert "/task/" not in asked and "run nothing" in asked
    assert f"vivibox review {task.id} --import" in asked
    assert notes[-1].startswith("review in your CLI"), "said where it waits"
    assert sup.step() is False, "still waiting, and asked once"
    assert [e["type"] for e in task.events()].count("review_asked") == 1


def test_blocking_notes_from_the_cli_go_back_to_the_writer_and_a_clean_one_ends_the_rounds(task, tmp_path):
    sup, notes, _ = in_cli(task, tmp_path)
    to_review(sup, task)
    sup.step()
    reviewing.import_answer(task, BLOCKING)
    assert sup.step()
    st = task.read_state()
    assert (st.state, st.rounds, st.reviews, st.awaiting_review) == (State.IMPLEMENT, 1, 1, False)
    assert (task.meta / "handoff" / "review-1.md").read_text() == BLOCKING
    sup.step()  # the writer, with the notes
    assert sup.harness.prompts[-1] == prompts.REVIEW_FIX_PROMPT
    sup.step()  # verify: green
    (task.meta / "handoff" / "review-1-reply.md").write_text("It does prove it.")
    sup.step()  # asks again
    assert "review-1-reply.md" in reviewing.cli_prompt(task), "the writer's answer to the last round"
    reviewing.import_answer(task, CLEAN)
    sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and st.reviews == 2
    assert "review 2: no blocking notes" in notes[-1]


def test_an_import_that_is_not_a_review_is_refused_and_nothing_moves(task, tmp_path):
    sup, _, _ = in_cli(task, tmp_path)
    to_review(sup, task)
    sup.step()
    with pytest.raises(reviewing.ReviewError, match="## Checked"):
        reviewing.import_answer(task, "## Blocking\n\n## Not blocking\n")
    assert sup.step() is False and task.read_state().awaiting_review


def test_nothing_to_import_when_no_review_is_asked(task):
    with pytest.raises(reviewing.ReviewError, match="not waiting for a review"):
        reviewing.import_answer(task, CLEAN)
    with pytest.raises(reviewing.ReviewError, match="not waiting for a review"):
        reviewing.cli_prompt(task)


def test_supervisor_worker_takes_a_planner_in_the_cli_only():
    """A chat in a browser cannot read the work; the CLI that planned can, in its conversation."""
    you = Role(manual.NAME, "")
    assert "planner that runs on a model" in orchestration.problem("supervisor_worker", you)
    assert orchestration.problem("supervisor_worker", you, plan_in_cli=True) == ""
    assert "can write" in orchestration.problem("single_agent", you, plan_in_cli=True)


def test_new_plan_in_cli_takes_supervisor_worker(env):
    made = actions.create("demo", "Add health endpoint", orchestration="supervisor_worker", plan_in_cli=True)
    assert made.read_state().plan_in_cli
    with pytest.raises(ConfigError, match="planner that runs on a model"):
        actions.create(
            "demo",
            "Add health endpoint",
            orchestration="supervisor_worker",
            roles={"planner": ("manual", "")},
        )


def test_wait_ends_on_a_review_for_the_cli_and_says_how_to_get_it(env, capsys, monkeypatch):
    from vivibox.cli import main

    made = actions.create("demo", "Add health endpoint", orchestration="supervisor_worker", plan_in_cli=True)
    st = made.read_state()
    st.state, st.awaiting_review = State.REVIEW, True
    made._write_state(st)
    assert waiting.reason(made.read_state(), True) == "review"
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    assert main(["wait", made.id, "--json"]) == 0
    [shown] = json.loads(capsys.readouterr().out)["tasks"]
    assert shown["reason"] == "review" and shown["next"][0] == f"vivibox review {made.id} --prompt"


def test_the_command_line_gives_the_prompt_and_takes_the_review(env, capsys, monkeypatch):
    from vivibox.cli import main

    made = actions.create("demo", "Add health endpoint", orchestration="supervisor_worker", plan_in_cli=True)
    st = made.read_state()
    st.state, st.awaiting_review = State.REVIEW, True
    made._write_state(st)
    (made.meta / reviewing.CLI_PROMPT).write_text("the review prompt")
    assert main(["review", made.id, "--prompt"]) == 0 and capsys.readouterr().out == "the review prompt"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    (made.meta / reviewing.CLI_ANSWER).write_text("## Blocking\n\n## Not blocking\n")
    assert main(["review", made.id, "--import"]) == 1
    assert "## Checked" in capsys.readouterr().err
    (made.meta / reviewing.CLI_ANSWER).write_text(CLEAN)
    assert main(["review", made.id, "--import"]) == 0
    assert "no blocking notes" in capsys.readouterr().out
    assert (made.meta / reviewing.CLI_REVIEW).read_text() == CLEAN


def test_info_offers_supervisor_worker_to_a_planner_in_the_cli(env, capsys):
    from vivibox.cli import main

    assert main(["info", "--json", str(env / "repo")]) == 0
    flows = {f["name"]: f for f in json.loads(capsys.readouterr().out)["flows"]}
    assert flows["supervisor_worker"]["plan_in_cli"] and not flows["single_agent"]["plan_in_cli"]


def test_the_ask_is_a_notification_without_the_open_in_ide_button(task, tmp_path):
    """The work is not yet yours to open; the round is the CLI's."""
    sup, _, _ = in_cli(task, tmp_path)
    said = []
    sup.ports.notify = lambda task_id, message, kind="": said.append((message, kind))
    to_review(sup, task)
    sup.step()
    assert said[-1][0].startswith("review in your CLI") and said[-1][1] == ""


def test_the_view_says_the_review_is_the_clis_and_how_to_get_past_a_closed_one(env, task, tmp_path):
    from vivibox import panel, ui

    sup, _, _ = in_cli(task, tmp_path)
    to_review(sup, task)
    sup.step()
    st = task.read_state()
    seen = ui.view(task, st, True, 3)
    assert seen.status == "review in your CLI" and seen.group == "Working"
    shown = panel.detail(task, st, 3, running=True)
    assert "Review in your agent's CLI" in shown and f"vivibox review {task.id} --prompt" in shown
    assert "`m`" in shown, "the way out when the CLI is gone"


def test_a_supervisor_put_on_a_model_reviews_the_round_itself(task, tmp_path):
    """m, then a start: the task waited for the CLI; its new supervisor has a model, and runs it."""
    from test_supervisor import FakeReviewer

    sup, _, _ = in_cli(task, tmp_path)
    to_review(sup, task)
    sup.step()
    assert task.read_state().awaiting_review
    out = task.meta / "review"
    out.mkdir(exist_ok=True)
    sup.planner = FakeReviewer(task, out, [CLEAN])
    sup.step()  # the flag is the old supervisor's: taken down
    sup.step()  # and the model reviews
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and not st.awaiting_review and len(sup.planner.prompts) == 1


def test_import_does_not_wait_on_a_stdin_that_never_ends(env, task, tmp_path, monkeypatch, capsys):
    """Claude Code's shell gives a command an open socket for stdin: review --import read it and
    hung, as plan import had."""
    import socket

    from vivibox.cli import main

    sup, _, _ = in_cli(task, tmp_path)
    to_review(sup, task)
    sup.step()
    monkeypatch.setattr(actions, "load", lambda task_id: (task, None))
    (task.meta / reviewing.CLI_ANSWER).write_text(CLEAN)
    ours, theirs = socket.socketpair()
    theirs.settimeout(2)  # read by mistake, the test fails in two seconds instead of hanging
    monkeypatch.setattr("sys.stdin", theirs.makefile("r"))
    try:
        assert main(["review", task.id, "--import"]) == 0
    finally:
        ours.close()
        theirs.close()
    assert "no blocking notes" in capsys.readouterr().out
