"""What a task that waits for you is called follows what the host took in, never what lies in a
folder the agent writes: a question.md vivibox refused is not the agent's question, and an
empty one asks nothing, in the list, the status, the panel and the supervisor alike."""

import pytest
from test_handoff import REFUSED, linked, planning
from test_supervisor import FakeHarness, command_supervisor, gate_result, make

from vivibox import actions, panel, supervisor, ui
from vivibox.states import State

NOT_READ = "question not read"


def refused_question(tmp_path, stage: str):
    """A task stopped on a question.md that is a link out of the handoff, while planning or
    implementing; the supervisor never read through it."""

    def link(t):
        linked(t.meta / "handoff" / supervisor.QUESTION).write_text("HOST-SECRET\n")

    task = planning(tmp_path)
    if stage == "planning":
        sup, _ = make(task, FakeHarness(task, [link]))
    else:
        sup, _, _ = command_supervisor(task, FakeHarness(task, [link]))
    sup.step()
    return task


@pytest.mark.parametrize("stage", ["planning", "implementing"])
def test_a_refused_question_is_not_called_the_agents(tmp_path, stage):
    task = refused_question(tmp_path, stage)
    st = task.read_state()
    seen = ui.view(task, st, True, 3)
    assert seen.status == NOT_READ, "the host took no question in: none is said to be asked"
    assert f'vivibox reply {task.id} "…"' in seen.commands, "answering moves the file out of the way"
    listed = ui.task_list([task], lambda t: "0/1", 3, ui.Style(False), running=lambda t: True)
    assert NOT_READ in listed and "agent asks" not in listed
    shown = ui.task_detail(task, lambda t: "0/1", 3, 3, ui.Style(False), True)
    assert NOT_READ in shown.splitlines()[0] and "agent asks" not in shown
    assert "HOST-SECRET" not in shown


def test_status_says_why_in_viviboxs_words_and_names_no_file_to_open(tmp_path):
    task = refused_question(tmp_path, "implementing")
    shown = ui.task_detail(task, lambda t: "0/1", 3, 0, ui.Style(False), True)
    path = task.meta / "handoff" / supervisor.QUESTION
    why = next(line for line in shown.splitlines() if line.startswith("Why"))
    assert why == f"Why   vivibox did not read the agent's question: {path} {REFUSED}"


@pytest.mark.parametrize("stage", ["planning", "implementing"])
def test_the_panel_of_a_refused_question_offers_the_answer_and_says_whose_words(tmp_path, stage):
    task = refused_question(tmp_path, stage)
    st = task.read_state()
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert f"### {task.id} · {NOT_READ}" in shown
    assert "**Next:** `r` answer" in shown
    assert "**vivibox did not read the agent's question:**" in shown and "The agent asks" not in shown


def blocked_on_a_failed_verification(task, question: str | None):
    """A task whose verification failed with no round left, and question.md as given."""
    sup, notes, _ = command_supervisor(task, FakeHarness(task), results=[gate_result(passed=False)] * 3)
    sup.project_verify, sup.max_rounds = ["true"], 0
    sup.step()  # implement
    sup.step()  # verify: red, no round left
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    if question is not None:
        (task.meta / "handoff" / supervisor.QUESTION).write_text(question)
    return sup, notes


@pytest.mark.parametrize("empty", ["", "\n", "  \n\n"])
def test_an_empty_question_asks_nothing(tmp_path, empty):
    """The file is there and says nothing: the task is blocked on its verification, as the panel
    says, not on a question nobody can read."""
    task = planning(tmp_path)
    blocked_on_a_failed_verification(task, empty)
    st = task.read_state()
    assert ui.view(task, st, True, 3).status == "verification failed 1×"
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert "**Verification keeps failing.**" in shown and "`r` answer" not in shown
    status = ui.task_detail(task, lambda t: "0/1", 3, 0, ui.Style(False), True)
    assert "question.md" not in status, "Why names what the verification found, not an empty file"


def test_a_question_the_host_read_is_the_agents(tmp_path):
    task = planning(tmp_path)
    blocked_on_a_failed_verification(task, "Which port?\n")
    st = task.read_state()
    assert ui.view(task, st, True, 3).status == "agent asks"
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert "**Next:** `r` answer" in shown and "Which port?" in shown
    status = ui.task_detail(task, lambda t: "0/1", 3, 0, ui.Style(False), True)
    assert f"Why   {task.meta / 'handoff' / supervisor.QUESTION}" in status


@pytest.mark.parametrize("empty", ["", "\n"])
def test_an_empty_question_put_back_after_g_stops_the_task_with_a_reason(tmp_path, empty):
    """g on a blocked task puts question.md away and, the build failing again, back: an empty one
    left by the agent ended the supervisor's step with an IndexError. The task waits for you,
    with what is true, and no round is spent."""
    task = planning(tmp_path)
    sup, notes = blocked_on_a_failed_verification(task, empty)
    actions.verify_again(task)
    sup.step()  # the verification again: red again, and the question it held goes back
    st = task.read_state()
    assert (st.state, st.paused, st.problem) == (State.CHECKPOINT_BLOCKED, False, "")
    assert notes[-1] == (
        "verification still failing; the agent's question stands: (its question.md is empty)"
    )
    assert ui.view(task, st, True, 3).status == "verification failed 2×"


def test_a_question_put_back_after_g_is_quoted(tmp_path):
    task = planning(tmp_path)
    sup, notes = blocked_on_a_failed_verification(task, "Which port?\nThe plan names two.\n")
    actions.verify_again(task)
    sup.step()
    assert notes[-1] == "verification still failing; the agent's question stands: Which port?"
    assert ui.view(task, task.read_state(), True, 3).status == "agent asks"
