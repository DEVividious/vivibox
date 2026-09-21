from pathlib import Path

import pytest

from vivibox import gate, supervisor
from vivibox.opencode import Turn
from vivibox.risky import Change
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = '+++\nmode = "code-only"\n+++\n\n# Goal\n\n{{goal}}\n\n## Acceptance criteria\n\n- [ ] x\n'
DRAFT = (
    '+++\nmode = "code-only"\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] health endpoint returns 200\n'
)


class FakeHarness:
    name = "opencode"

    def __init__(self, task, actions=()):
        self.task, self.actions, self.prompts = task, list(actions), []

    def turn(self, prompt, session="", title=""):
        self.prompts.append(prompt)
        if self.actions:
            self.actions.pop(0)(self.task)
        return Turn("ses_1", True, 0.01, 100, "done")


def gate_result(passed=True, risky=()):
    r = gate.GateResult(Path("/dev/null"))
    if not passed:
        r.missing_criteria = ["x"]
    r.risky = [Change(p, "changed") for p in risky]
    return r


@pytest.fixture
def task(tmp_path):
    return create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)


def make(task, harness, results=(), risky=()):
    results = list(results)
    notes = []
    sup = supervisor.Supervisor(
        task,
        harness,
        run_gate=lambda t: results.pop(0),
        risky_changes=lambda: [Change(p, "changed") for p in risky],
        max_iterations=2,
        project_verify=["true"],
        notify=lambda _id, msg, kind="": notes.append(msg),
    )
    return sup, notes


def write_draft(task):
    (task.meta / "handoff" / "plan-draft.md").write_text(DRAFT)


def test_plan_turn_copies_draft_and_stops_at_checkpoint(task):
    harness = FakeHarness(task, [write_draft])
    sup, notes = make(task, harness)
    assert sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_PLAN and st.sessions == {"opencode": "ses_1"}
    assert "health endpoint returns 200" in task.plan_path.read_text()
    assert harness.prompts == [supervisor.PLAN_PROMPT] and notes == ["plan ready for review"]
    assert not sup.step(), "waits for you at a checkpoint"


def test_a_plan_you_review_has_no_notes_left_for_the_agent(task):
    def draft_with_notes(t):
        (t.meta / "handoff" / "plan-draft.md").write_text(
            DRAFT.replace("# Goal", "# Goal\n\n<!-- Why it happens, found in the code. -->")
        )

    sup, _ = make(task, FakeHarness(task, [draft_with_notes]))
    assert sup.step()
    assert "<!--" not in task.plan_path.read_text()


def test_auto_plan_goes_straight_to_implementation(task):
    task.set_auto_plan(True)
    harness = FakeHarness(task, [write_draft])
    sup, notes = make(task, harness)
    sup.step()
    st = task.read_state()
    assert st.state is State.IMPLEMENT and notes == []
    assert (task.meta / gate.ACCEPTED_PLAN).exists()
    sup.step()
    assert harness.prompts[-1] == supervisor.IMPLEMENT_PROMPT


def test_auto_plan_still_stops_without_real_criteria(task):
    task.set_auto_plan(True)
    placeholder = DRAFT.replace("health endpoint returns 200", gate.PLACEHOLDER)
    write = lambda t: (t.meta / "handoff" / "plan-draft.md").write_text(placeholder)  # noqa: E731
    sup, notes = make(task, FakeHarness(task, [write]))
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN and "not accepted automatically" in notes[0]


def test_auto_plan_stops_for_risky_changes(task):
    task.set_auto_plan(True)
    sup, _ = make(task, FakeHarness(task, [write_draft]), risky=["build.gradle"])
    sup.step()
    assert task.read_state().state is State.APPROVAL_RISKY


def test_invalid_draft_still_stops_for_you(task):
    sup, notes = make(task, FakeHarness(task))
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN and "no valid plan draft" in notes[0]


def test_question_during_implementation_blocks(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    ask = lambda t: (t.meta / "handoff" / "question.md").write_text("Which database?")  # noqa: E731
    sup, notes = make(task, FakeHarness(task, [ask]))
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED and "Which database?" in notes[0]


def test_failed_gate_sends_feedback_then_blocks_after_limit(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    harness = FakeHarness(task)
    sup, notes = make(task, harness, results=[gate_result(False), gate_result(False)])
    sup.step()  # implement
    sup.step()  # verify: fails, iteration 1 of 2
    assert task.read_state().state is State.IMPLEMENT
    assert (task.meta / "handoff" / "verify-feedback.md").exists()
    sup.step()  # implement with feedback
    assert harness.prompts[-1] == supervisor.FEEDBACK_PROMPT
    sup.step()  # verify: fails again at the limit
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    assert notes[-1] == "verification still failing after 2 attempts", "attempts, as the list calls them"


def test_your_reply_to_a_blocked_task_gives_the_agent_a_whole_new_budget(task):
    from vivibox import actions, ui

    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    sup, _ = make(task, FakeHarness(task), results=[gate_result(False)] * 3)
    for _ in range(4):  # implement, fail, implement, fail at the limit of 2
        sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    actions.reply(task, "Docker works again; switch the integration tests back on")
    st = task.read_state()
    assert (st.state, st.iteration) == (State.IMPLEMENT, 1), "attempt 1, not 2, after your reply"
    assert "attempt" not in ui.activity(st, 2)
    sup.step()  # implement
    sup.step()  # verify fails: the first of the new two
    assert task.read_state().state is State.IMPLEMENT and task.read_state().iteration == 2


def test_passing_gate_reaches_final_checkpoint(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)])
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert notes == [f"ready for your review: vivibox review {task.id}"]


def test_final_checkpoint_prepares_the_review(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)])
    sup.prepare_review = lambda: Path("/srv/vivibox/demo-1/demo")
    sup.step()
    assert notes == ["ready for your review in /srv/vivibox/demo-1/demo"]


def test_failed_review_preparation_still_reaches_the_checkpoint(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)])

    def fail():
        raise RuntimeError("local changes")

    sup.prepare_review = fail
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert "local changes" in notes[0] and "vivibox review demo-1" in notes[0]


def test_notification_buttons_run_their_command(monkeypatch, capsys, tmp_path):
    ran = tmp_path / "ran"
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/notify-send")

    def fake_run(command, **kw):
        if command[0] == "notify-send":
            assert "--action=accept=Accept plan" in command
            return type("P", (), {"stdout": "accept\n", "returncode": 0})()
        ran.write_text(" ".join(command))
        return type("P", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    action = supervisor.Action("accept", "Accept plan", ["vivibox", "accept", "demo-1"])
    supervisor._await_click("demo-1", ["notify-send", "--action=accept=Accept plan"], [action])
    assert ran.read_text() == "vivibox accept demo-1"


def test_risky_changes_hold_back_checkpoints(task):
    sup, notes = make(task, FakeHarness(task, [write_draft]), risky=["pom.xml"])
    sup.step()
    assert task.read_state().state is State.APPROVAL_RISKY
    assert task.events()[-1]["data"]["then"] == "checkpoint:plan"


def test_failed_turn_pauses_the_task(task):
    class Broken(FakeHarness):
        def turn(self, prompt, session="", title=""):
            return Turn("", False, 0, 0, "", "APIError: invalid key")

    sup, notes = make(task, Broken(task))
    sup.step()
    assert task.read_state().paused and "invalid key" in notes[0]
    assert not sup.step()


def test_next_prompt_survives_a_failed_turn(task):
    class Broken(FakeHarness):
        def turn(self, prompt, session="", title=""):
            self.prompts.append(prompt)
            return Turn("", False, 0, 0, "", "network down")

    supervisor.set_next_prompt(task, "custom")
    harness = Broken(task)
    sup, _ = make(task, harness)
    sup.step()
    assert supervisor.next_prompt(task, "default") == "custom", "kept for the resumed turn"
    task.set_paused(False)
    sup.harness = FakeHarness(task, [write_draft])
    sup.step()
    assert sup.harness.prompts == ["custom"]
    assert supervisor.next_prompt(task, "default") == "default"


def test_desktop_notifications_follow_the_setting(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(supervisor.subprocess, "run", lambda cmd, check: calls.append(cmd))
    supervisor.notify("demo-1", "plan ready", desktop=False)
    assert calls == [] and "plan ready" in capsys.readouterr().out
    supervisor.notify("demo-1", "plan ready", desktop=True)
    assert calls and calls[0][0] == "notify-send"


def test_the_plans_summary_becomes_the_task_title(task):
    draft = DRAFT.replace("+++\n", '+++\nsummary = "Reject expired cards in the validator"\n', 1)
    write = lambda t: (t.meta / "handoff" / "plan-draft.md").write_text(draft)  # noqa: E731
    sup, _ = make(task, FakeHarness(task, [write]))
    sup.step()
    assert task.read_state().goal == "Reject expired cards in the validator"


class StartsSessions(FakeHarness):
    """A harness that can make its session before the turn, as opencode's server can."""

    def __init__(self, task, actions=()):
        super().__init__(task, actions)
        self.seen_during_turn = None

    def start_session(self, title):
        self.title = title
        return "ses_early"

    def turn(self, prompt, session="", title=""):
        self.seen_during_turn = (session, self.task.read_state().sessions)
        return super().turn(prompt, session, title)


def test_the_first_turn_can_be_watched_while_it_runs(task):
    """The session used to be known only when the turn returned, so for the whole first turn --
    the one you most want to see -- watching said the agent was not working, and your window of
    the agent opened only once there was nothing left to watch."""
    harness = StartsSessions(task, [write_draft])
    opened = []
    sup, _ = make(task, harness)
    sup.session_started = lambda st: opened.append(dict(st.sessions))
    sup.step()
    assert harness.seen_during_turn == ("ses_early", {"opencode": "ses_early"}), "recorded before it ran"
    assert opened == [{"opencode": "ses_early"}]
    assert harness.title == "demo-1: Add health endpoint"


def test_a_session_that_could_not_be_made_leaves_the_turn_to_make_its_own(task):
    class Refuses(FakeHarness):
        def start_session(self, title):
            raise supervisor.HarnessError("server down")

    harness = Refuses(task, [write_draft])
    sup, _ = make(task, harness)
    sup.step()
    assert task.read_state().sessions == {"opencode": "ses_1"}
    assert any(e["type"] == "session_not_started" for e in task.events())
