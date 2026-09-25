from vivibox import timeline
from vivibox.states import State
from vivibox.task import create_task


def test_the_timeline_reads_the_events_as_a_person_would(tmp_path):
    task = create_task(tmp_path, "demo", "Add health endpoint", "")
    task.event("started", model="deepseek/deepseek-v4-flash")
    task.event("turn_started", state="plan", role="planner")
    task.event("turn", state="plan", role="planner", ok=True, cost=0.0512, tokens=3400)
    task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
    task.transition(State.PLAN, reason="your reply")
    task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
    task.transition(State.IMPLEMENT)
    task.event(
        "gate", iteration=1, passed=False, failed_commands=["npm test"], missing_criteria=2, environment="",
        log="verify-1-120000.log", risky_changes=[],
    )  # fmt: skip
    task.event("turn", state="implement", role="writer", ok=False, cost=0.0, tokens=0, error="rate limited")
    task.set_paused(True, problem="agent turn failed: rate limited")
    task.set_paused(False)
    task.event("gate", iteration=2, passed=True, failed_commands=[], log="verify-2-130000.log")
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL, reason="verification passed")
    task.event("risky_approved", paths=[".idea/x.xml"])

    what = [text for _, text in timeline.entries(task)]
    assert what[:3] == [
        "created: Add health endpoint",
        "started on deepseek/deepseek-v4-flash",
        "planner turn: $0.05, 3400 tokens, 0 s",
    ]
    assert "you: your reply → planning" in what and "→ review the plan (plan ready)" in what
    assert not any("checkpoint:" in text for text in what), "states in the list's words, not raw"
    assert "verification 1: failed, `npm test`; 2 criteria not met (verify-1-120000.log)" in what, (
        "what the gate found, in words"
    )
    assert "writer turn: $0.00, 0 tokens; failed: rate limited" in what
    assert "stopped: agent turn failed: rate limited" in what and "started again" in what
    assert "verification 2: passed (verify-2-130000.log)" in what
    assert what[-1] == "you: risky files approved: .idea/x.xml"
    assert all(len(when) == 8 and when[2] == ":" for when, _ in timeline.entries(task)), "on your clock"
    assert timeline.latest(task, 2) == timeline.lines(task)[-2:]
    assert timeline.write(task).read_text().startswith("# demo-1: Add health endpoint\n\n")


def test_the_cost_warning_and_the_limit_read_as_money(tmp_path):
    task = create_task(tmp_path, "demo", "Add health endpoint", "")
    task.event("cost_warning", spent=1.5, warning=1.0)
    task.set_paused(True, problem="cost limit reached: $2.10 of $2.00")
    said = [what for _, what in timeline.entries(task)]
    assert "cost $1.50, past the warning of $1.00" in said
    assert "stopped: cost limit reached: $2.10 of $2.00" in said


def test_the_preparation_says_what_runs_and_how_it_ended(tmp_path):
    task = create_task(tmp_path, "demo", "Add health endpoint", "")
    task.event("prepare_started", commands=["bash mvnw -B install -DskipTests"])
    task.event("prepared", ok=False, code=1)
    said = [what for _, what in timeline.entries(task)]
    assert "preparing: bash mvnw -B install -DskipTests" in said
    assert "preparation failed, exit 1; its output is under l" in said
    task.event("prepared", ok=True, code=0)
    assert [what for _, what in timeline.entries(task)][-1] == "prepared"
