"""A review is of the commit the verification passed. When the clone changed under it (the
commits moved, or the task added what blocks the clone), the review is abandoned and the work
goes back to the verification, for no round of the writer's. Something that changes the clone
after every verification would make that a loop nobody is told of, each turn of it a paid
review: the second abandonment in a row, with no turn of the writer's and no decision of yours
between, stops the task for you, and no further review is started."""

from pathlib import Path

import pytest
from test_supervisor import CLEAN, gate_result, reviewed, task  # noqa: F401 (task: the fixture)

from vivibox import actions, gate, panel, ui
from vivibox import repo as r
from vivibox.states import State

VERIFIED = "c" * 40
MOVED = "d" * 40


def green(count: int = 40) -> list:
    return [gate.GateResult(Path("/dev/null"), commit=VERIFIED) for _ in range(count)]


def changing_after_each_review(sup, how: str) -> None:
    """The clone as a process left in the pod leaves it: as verified when a review starts, and
    changed by the time the review is done (new commits, or a worktree added)."""
    asked = []

    def head() -> str:
        asked.append(len(sup.reviewer.prompts))
        if len(asked) % 2 == 1:  # as a round starts
            return VERIFIED
        if how == "the commits move":
            return MOVED
        raise r.Blocked("the task added a worktree to its clone and is blocked until it is out: …", [])

    sup.ports.head = head


def loop(task, tmp_path, how: str):  # noqa: F811
    sup, notes, reviewer = reviewed(task, tmp_path, [CLEAN] * 20, results=green())
    changing_after_each_review(sup, how)
    return sup, notes, reviewer


TRIGGERS = ["the commits move", "the clone is blocked"]


@pytest.mark.parametrize("how", TRIGGERS)
def test_a_clone_that_changes_after_every_review_stops_the_task_after_two(task, tmp_path, how):  # noqa: F811
    sup, notes, reviewer = loop(task, tmp_path, how)
    for _ in range(16):
        sup.step()
    st = task.read_state()
    assert len(reviewer.prompts) == 2, "two reviews were paid for, and no third"
    assert (st.state, st.rounds, st.paused) == (State.CHECKPOINT_BLOCKED, 0, False)
    assert ui.view(task, st, True, 3).status == "review could not finish"
    why = (
        "commits changed since the verification (ccccccc → ddddddd)"
        if how == "the commits move"
        else "the task's clone is blocked by what it added"
    )
    said = (
        f"no review could be finished on what was verified, twice in a row with no turn of the "
        f"writer's between: {why}. Whatever changes the clone after the writer's turn (something it "
        "left running in the pod, or work under w) has to end first: reply, and the writer ends what "
        "it left; or stop the pod and start it again (s); then verify again (g). No review is started "
        "until then"
    )
    assert notes[-1] == f"the review could not finish: {said}"
    assert ui.environment_problem(task) == said, "the same under Why, after the notification is gone"


@pytest.mark.parametrize("how", TRIGGERS)
def test_the_first_abandonment_goes_back_to_the_verification_as_before(task, tmp_path, how):  # noqa: F811
    sup, notes, reviewer = loop(task, tmp_path, how)
    for _ in range(3):  # implement, verify, review
        sup.step()
    st = task.read_state()
    assert (st.state, st.rounds) == (State.VERIFY, 0) and len(reviewer.prompts) == 1
    assert not [n for n in notes if "could not finish" in n]


def test_the_panel_and_the_keys_of_a_review_that_could_not_finish(task, tmp_path):  # noqa: F811
    sup, _, _ = loop(task, tmp_path, "the commits move")
    for _ in range(16):
        sup.step()
    st = task.read_state()
    shown = panel.detail(task, st, 3, running=False, pod=panel.PodView())
    assert f"### {task.id} · review could not finish" in shown
    assert "**The review could not finish:**" in shown and "Verification could not run" not in shown
    assert "**Next:** `r` tell the writer · `g` verify again, once what changes the clone has ended" in shown
    offered = ui.view(task, st, True, 3).commands
    assert f'vivibox reply {task.id} "…"' in offered and f"vivibox verify-again {task.id}" in offered


def test_your_g_gives_it_two_more_and_no_more(task, tmp_path):  # noqa: F811
    sup, notes, reviewer = loop(task, tmp_path, "the commits move")
    for _ in range(16):
        sup.step()
    actions.verify_again(task)
    for _ in range(16):
        sup.step()
    assert len(reviewer.prompts) == 4 and task.read_state().state is State.CHECKPOINT_BLOCKED
    assert len([n for n in notes if n.startswith("the review could not finish")]) == 2


def test_your_reply_sends_the_writer_and_the_count_starts_again(task, tmp_path):  # noqa: F811
    sup, _, reviewer = loop(task, tmp_path, "the commits move")
    for _ in range(16):
        sup.step()
    assert actions.reply(task, "end the watcher you left running") is State.IMPLEMENT
    for _ in range(3):  # the writer's turn, the verification, one review
        sup.step()
    assert len(sup.harness.prompts) == 2 and len(reviewer.prompts) == 3
    assert task.read_state().state is State.VERIFY, "abandoned once since the writer's turn: verified again"


def test_abandonments_with_a_turn_of_the_writers_between_are_not_in_a_row(task, tmp_path):  # noqa: F811
    """Commits that moved once, a red verification and the writer's fix, commits that moved once
    more: each is the first since a turn of the writer's."""
    results = [*green(1), gate_result(False), *green(10)]
    sup, notes, reviewer = reviewed(task, tmp_path, [CLEAN] * 5, results=results, max_rounds=3)
    heads = [MOVED]
    sup.ports.head = lambda: heads[0]
    for _ in range(3):  # implement, verify (green), review: the commits moved
        sup.step()
    assert task.read_state().state is State.VERIFY
    sup.step()  # verify: red, so the writer has a turn
    sup.step()  # the fix
    sup.step()  # verify: green
    sup.step()  # review: the commits moved again
    st = task.read_state()
    assert st.state is State.VERIFY and reviewer.prompts == [], "the second, and not in a row"
    assert not [n for n in notes if "could not finish" in n]


def test_commits_that_moved_before_each_review_stop_it_with_no_review_paid(task, tmp_path):  # noqa: F811
    sup, notes, reviewer = reviewed(task, tmp_path, [CLEAN] * 5, results=green())
    sup.ports.head = lambda: MOVED
    for _ in range(10):
        sup.step()
    assert reviewer.prompts == [] and task.read_state().state is State.CHECKPOINT_BLOCKED
    assert notes[-1].startswith("the review could not finish: no review could be finished")


def test_the_count_is_the_hosts_record_which_the_pod_only_reads(task, tmp_path):  # noqa: F811
    """What is counted is in the task's event log, written by the host alone: nothing the writer
    does in its clone or its handoff starts the count again but a turn it is given."""
    sup, _, reviewer = loop(task, tmp_path, "the commits move")
    for _ in range(4):  # implement, verify, review (abandoned), verify
        sup.step()
    for name in ("question.md", "verify-feedback.md", "review-1-reply.md"):
        (task.meta / "handoff" / name).write_text("")  # whatever the writer leaves there
    (task.meta / "handoff" / "question.md").unlink()
    for _ in range(12):
        sup.step()
    assert len(reviewer.prompts) == 2 and task.read_state().state is State.CHECKPOINT_BLOCKED
    abandoned = [e for e in task.events() if e["type"] == "review_abandoned"]
    assert len(abandoned) == 2
