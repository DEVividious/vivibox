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


# --- the status reads no more of the agent's file than it needs -------------------------------------


def test_a_question_that_is_not_text_does_not_end_the_list(tmp_path):
    """The list asks what every blocked task waits on at each refresh: bytes that are no UTF-8
    in one task's question.md must not end it for all of them."""
    task = planning(tmp_path)
    blocked_on_a_failed_verification(task, None)
    (task.meta / "handoff" / supervisor.QUESTION).write_bytes(b"\xff\xfe which port?\n")
    assert ui.view(task, task.read_state(), True, 3).status == "agent asks"
    listed = ui.task_list([task], lambda t: "0/1", 3, ui.Style(False), running=lambda t: True)
    assert "agent asks" in listed


def test_the_status_reads_a_large_question_no_further_than_its_first_words(tmp_path):
    from test_clone_read import bytes_read

    task = planning(tmp_path)
    blocked_on_a_failed_verification(task, None)
    size = 64 << 20
    (task.meta / "handoff" / supervisor.QUESTION).write_bytes(b"Which port?\n" + b"x" * size)
    st = task.read_state()
    before = bytes_read()
    assert ui.view(task, st, True, 3).status == "agent asks"
    assert bytes_read() - before < size // 8, "a status is asked for at every refresh of the list"


# --- one answer to "is there a question, and what does it say" -------------------------------------

MiB = 1 << 20
# What the agent may leave as question.md, and what every reader takes it for: the supervisor,
# the list, vivibox status and the panel ask one function (supervisor.read_question).
NOTHING_TO_SEE = {
    "a megabyte of white space": b" \n" * (MiB // 2),
    "more than a megabyte of white space": b"\n" * (2 * MiB),
    "a byte order mark alone": "\ufeff".encode(),
    "zero-width spaces": "\u200b\u200b\n".encode(),
    "NUL bytes": b"\0" * 64,
    "a line separator": "\u2028".encode(),
    # Letters to Unicode, and drawn as nothing: a question of them alone is none (R43).
    "Hangul fillers": "\u3164\u115f\u1160\uffa0\n".encode(),
    "variation selectors and a grapheme joiner": "\ufe0f\u034f".encode(),
}
SOMETHING = {
    "bytes that are no UTF-8": (b"\xff\xfe why?\n", "\\xff\\xfe why?"),
    "a question after white space": (b" \n" * 1000 + b"Which port?\n", "Which port?"),
    "a question after a byte order mark": ("\ufeffWhich port?\n".encode(), "\ufeffWhich port?"),
}


@pytest.mark.parametrize("shape", NOTHING_TO_SEE)
def test_a_file_with_nothing_to_see_is_no_question_to_anyone(tmp_path, shape):
    task = planning(tmp_path)
    sup, notes = blocked_on_a_failed_verification(task, None)
    (task.meta / "handoff" / supervisor.QUESTION).write_bytes(NOTHING_TO_SEE[shape])
    st = task.read_state()
    assert supervisor.read_question(task) == ("", "") and supervisor.question(task) is None
    assert ui.asked(task) == ("", "") and sup._asks() == ""
    assert ui.view(task, st, True, 3).status == "verification failed 1×"
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert "**Verification keeps failing.**" in shown and "The agent asks" not in shown
    actions.verify_again(task)
    sup.step()  # red again: what g put away goes back, and says nothing
    assert notes[-1] == "verification still failing; the agent's question stands: (its question.md is empty)"


@pytest.mark.parametrize("shape", SOMETHING)
def test_a_file_with_something_to_see_is_the_same_question_to_everyone(tmp_path, shape):
    written, text = SOMETHING[shape]
    task = planning(tmp_path)
    sup, notes = blocked_on_a_failed_verification(task, None)
    (task.meta / "handoff" / supervisor.QUESTION).write_bytes(written)
    st = task.read_state()
    assert supervisor.read_question(task) == (text, "")
    assert ui.asked(task) == ("agent asks", "") and ui.view(task, st, True, 3).status == "agent asks"
    assert sup._asks() == f"question from the agent: {text}"
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert "**The agent asks:**" in shown and text.lstrip("\ufeff") in shown
    actions.verify_again(task)
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED and not task.read_state().paused
    assert notes[-1].startswith("verification still failing; the agent's question stands: ")
    assert notes[-1].endswith(text.lstrip("\ufeff"))


def test_bytes_that_are_no_text_end_neither_the_open_panel_nor_the_supervisor(tmp_path):
    """They were read strictly: the panel raised UnicodeDecodeError at every refresh, and the
    supervisor stopped on that error's text after the writer's turn. They are shown as codes."""
    task = planning(tmp_path)

    def asks(t):
        (t.meta / "handoff" / supervisor.QUESTION).write_bytes(b"\xff\xfe why?\n")

    sup, notes, _ = command_supervisor(task, FakeHarness(task, [asks]))
    sup.step()  # the writer's turn ends on its question
    st = task.read_state()
    assert (st.state, st.paused, st.problem) == (State.CHECKPOINT_BLOCKED, False, "")
    assert notes[-1] == "question from the agent: \\xff\\xfe why?"
    assert "\\xff\\xfe why?" in panel.detail(task, st, 3, running=False, pod=panel.PodView())


def test_a_question_longer_than_is_read_is_cut_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "QUESTION_LIMIT", 100)
    task = planning(tmp_path)
    blocked_on_a_failed_verification(task, "Which port? " + "x" * 500 + "\n")
    text, why = supervisor.read_question(task)
    assert why == "" and text.startswith("Which port? xxx")
    assert text.endswith("\n\n(question.md goes on: vivibox reads its first 100 characters)")
    assert len(text) < 200


def test_what_is_read_of_a_question_is_bounded_whoever_asks(tmp_path):
    from test_clone_read import bytes_read

    task = planning(tmp_path)
    blocked_on_a_failed_verification(task, None)
    size = 64 << 20
    (task.meta / "handoff" / supervisor.QUESTION).write_bytes(b"Which port?\n" + b"x" * size)
    before = bytes_read()
    supervisor.read_question(task)
    panel.detail(task, task.read_state(), 3, running=False, pod=panel.PodView())
    assert bytes_read() - before < size // 8


@pytest.mark.parametrize("empty", ["", " \n", "﻿"])
def test_an_empty_question_at_the_plan_checkpoint_is_a_plan_to_review(tmp_path, empty):
    """The planner left a question.md with nothing in it and no plan: there is no question to
    answer, so the checkpoint is the plan's, with its own keys, not `agent asks` over nothing."""
    task = planning(tmp_path)
    (task.meta / "handoff" / supervisor.QUESTION).write_text(empty)
    task.transition(State.CHECKPOINT_PLAN)
    st = task.read_state()
    assert not ui.planner_asks(task, st)
    seen = ui.view(task, st, True, 3)
    assert seen.status == "review the plan" and f"vivibox accept {task.id}" in seen.commands
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert "`a` accept the plan" in shown and "instead of a plan" not in shown


def test_why_of_a_task_with_an_empty_question_is_what_stopped_the_verification(tmp_path):
    """With a question the file is why; with an empty one, what kept the verification from
    running is, and not the path of a feedback file."""
    task = planning(tmp_path)
    sup, _ = blocked_on_a_failed_verification(task, "")
    task.event("gate", passed=False, environment="Cannot connect to the Docker daemon")
    (task.meta / "handoff" / "verify-feedback.md").write_text("# Verification failed\n")
    status = ui.task_detail(task, lambda t: "0/1", 3, 0, ui.Style(False), True)
    why = [line for line in status.splitlines() if line.startswith("Why")]
    assert why == ["Why   Cannot connect to the Docker daemon"]
