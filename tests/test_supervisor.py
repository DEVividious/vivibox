from pathlib import Path

import pytest

from vivibox import brief, gate, supervisor, ui
from vivibox.harness import Harness, Turn
from vivibox.risky import Change
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = (
    '+++\nmode = "code-only"\n+++\n\n# Goal\n\n{{goal}}\n\n## Acceptance criteria\n\n'
    f"- [ ] {gate.PLACEHOLDER}\n"
)
DRAFT = (
    '+++\nmode = "code-only"\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] health endpoint returns 200\n'
)


class FakeHarness(Harness):
    name = "opencode"

    def __init__(self, task, actions=()):
        self.task, self.actions, self.prompts = task, list(actions), []

    def turn(self, prompt, session="", title="", on_step=None):
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
    ports = supervisor.Ports(
        run_gate=lambda t: results.pop(0),
        risky_changes=lambda: [Change(p, "changed") for p in risky],
        notify=lambda _id, msg, kind="": notes.append(msg),
        sleep=lambda seconds: None,  # a retry's wait, not waited in tests
    )
    sup = supervisor.Supervisor(task, harness, ports, max_iterations=2, project_verify=["true"])
    return sup, notes


def write_draft(task):
    (task.meta / "handoff" / "plan-draft.md").write_text(DRAFT)


def test_plan_turn_copies_draft_and_stops_at_checkpoint(task):
    harness = FakeHarness(task, [write_draft])
    sup, notes = make(task, harness)
    assert sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_PLAN and st.sessions == {"writer": "ses_1"}
    assert "health endpoint returns 200" in task.plan_path.read_text()
    assert harness.prompts[0].endswith(supervisor.PLAN_PROMPT) and notes == ["plan ready for review"]
    assert not sup.step(), "waits for you at a checkpoint"


YOUR_PLAN = (
    '+++\nmode = "code-only"\nsummary = "Add a health endpoint"\n+++\n\n# Goal\n\nHealth.\n\n'
    "## Acceptance criteria\n\n- [ ] GET /health returns 200\n"
)


def test_a_plan_you_finished_yourself_goes_straight_to_review(task):
    """--draft, then the plan written under e: starting the task asks nobody to write it again."""
    task.plan_path.write_text(YOUR_PLAN)
    harness = FakeHarness(task)
    sup, notes = make(task, harness)
    assert sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_PLAN and st.goal == "Add a health endpoint"
    assert harness.prompts == [] and notes == ["your plan is ready for review"]
    assert "GET /health returns 200" in task.plan_path.read_text()


def test_a_plan_you_finished_yourself_needs_no_chat_when_you_are_the_planner(task):
    from vivibox import manual

    task.plan_path.write_text(YOUR_PLAN)
    harness = FakeHarness(task)
    sup, notes = make(task, harness)
    sup.planner = manual.Manual()
    assert sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_PLAN and not st.awaiting_plan, "yours to accept, not to plan again"
    assert harness.prompts == [], "no turn spent describing the repository for a chat"
    assert notes == ["your plan is ready for review"]


def test_a_plan_with_the_placeholder_left_in_goes_to_the_planner(task):
    harness = FakeHarness(task, [write_draft])
    sup, notes = make(task, harness)
    assert sup.step()
    assert len(harness.prompts) == 1 and notes == ["plan ready for review"]


def test_after_your_reply_the_planner_revises_even_a_finished_plan(task):
    task.plan_path.write_text(YOUR_PLAN)
    supervisor.set_next_prompt(task, supervisor.PLAN_COMMENT_PROMPT)
    harness = FakeHarness(task, [write_draft])
    sup, _ = make(task, harness)
    assert sup.step()
    assert len(harness.prompts) == 1 and harness.prompts[0].endswith(supervisor.PLAN_COMMENT_PROMPT)


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
    assert task.read_state().state is State.CHECKPOINT_PLAN and "placeholder" in notes[0]


def test_auto_plan_stops_for_risky_changes(task):
    task.set_auto_plan(True)
    sup, _ = make(task, FakeHarness(task, [write_draft]), risky=["build.gradle"])
    sup.step()
    assert task.read_state().state is State.APPROVAL_RISKY


def test_invalid_draft_still_stops_for_you(task):
    sup, notes = make(task, FakeHarness(task))
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN and "no valid plan draft" in notes[0]


def test_the_writer_waits_for_the_projects_preparation(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    harness, waits, slept = FakeHarness(task), [True, False], []
    ports = supervisor.Ports(
        run_gate=lambda t: gate_result(False),
        risky_changes=lambda: [],
        notify=lambda _id, msg, kind="": None,
        sleep=slept.append,
        preparing=lambda: waits.pop(0) if waits else False,
    )
    sup = supervisor.Supervisor(
        task, harness, ports, max_iterations=2, project_verify=["true"], prepared=["mvn -B install"]
    )
    assert sup.step()
    assert harness.prompts == [] and slept == [supervisor.PREPARE_POLL]
    assert task.read_state().state is State.IMPLEMENT
    sup.step()
    assert len(harness.prompts) == 1 and harness.prompts[0].endswith(supervisor.IMPLEMENT_PROMPT)
    assert "`mvn -B install`" in harness.prompts[0] and "/task/handoff/prepare.log" in harness.prompts[0]
    sup.step()  # the verification fails: the feedback turn is not told again
    sup.step()
    assert "prepare.log" not in harness.prompts[-1] and harness.prompts[-1] == supervisor.FEEDBACK_PROMPT


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
    assert notes == [f"work ready for your review: vivibox review {task.id}"]


def test_final_checkpoint_prepares_the_review(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)])
    sup.ports.prepare_review = lambda: Path("/srv/vivibox/demo-1/demo")
    sup.step()
    assert notes == ["work ready for your review in /srv/vivibox/demo-1/demo"]


def test_failed_review_preparation_still_reaches_the_checkpoint(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)])

    def fail():
        raise RuntimeError("local changes")

    sup.ports.prepare_review = fail
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
        def turn(self, prompt, session="", title="", on_step=None):
            return Turn("", False, 0, 0, "", "APIError: invalid key")

    waits = []
    sup, notes = make(task, Broken(task))
    sup.ports.sleep = waits.append
    sup.step()
    assert task.read_state().paused and "invalid key" in notes[0]
    assert waits == [], "a key that is wrong does not pass with time: no retry"
    assert not sup.step()


def test_next_prompt_survives_a_failed_turn(task):
    class Broken(FakeHarness):
        def turn(self, prompt, session="", title="", on_step=None):
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
    assert len(sup.harness.prompts) == 1 and sup.harness.prompts[0].endswith("custom")
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

    def turn(self, prompt, session="", title="", on_step=None):
        self.seen_during_turn = (session, self.task.read_state().sessions)
        return super().turn(prompt, session, title)


def test_the_first_turn_can_be_watched_while_it_runs(task):
    """The session used to be known only when the turn returned, so for the whole first turn --
    the one you most want to see -- watching said the agent was not working, and your window of
    the agent opened only once there was nothing left to watch."""
    harness = StartsSessions(task, [write_draft])
    opened = []
    sup, _ = make(task, harness)
    sup.ports.session_started = lambda st: opened.append(dict(st.sessions))
    sup.step()
    assert harness.seen_during_turn == ("ses_early", {"writer": "ses_early"}), "recorded before it ran"
    assert opened == [{"writer": "ses_early"}]
    assert harness.title == "demo-1: Add health endpoint"


def test_a_session_that_could_not_be_made_leaves_the_turn_to_make_its_own(task):
    class Refuses(FakeHarness):
        def start_session(self, title):
            raise supervisor.HarnessError("server down")

    harness = Refuses(task, [write_draft])
    sup, _ = make(task, harness)
    sup.step()
    assert task.read_state().sessions == {"writer": "ses_1"}
    assert any(e["type"] == "session_not_started" for e in task.events())


def test_a_failed_turn_leaves_its_reason_with_the_task(task):
    class Broken(FakeHarness):
        def turn(self, prompt, session="", title="", on_step=None):
            return Turn("", False, 0, 0, "", "429 Too Many Requests")

    sup, _ = make(task, Broken(task))
    sup.step()
    assert task.read_state().problem == "agent turn failed: 429 Too Many Requests"
    task.set_paused(False)
    assert task.read_state().problem == "", "starting again is the end of it"


def test_an_error_in_the_supervisor_leaves_its_reason_with_the_task(task):
    class Explodes(FakeHarness):
        def turn(self, prompt, session="", title="", on_step=None):
            raise RuntimeError("network vivibox-demo-1 not found")

    class Enough(Exception):
        pass

    def leave(st):
        raise Enough

    sup, notes = make(task, Explodes(task))
    with pytest.raises(Enough):
        sup.run(poll=0, on_step=leave)
    st = task.read_state()
    assert st.paused and st.problem == "stopped on an error: network vivibox-demo-1 not found"
    assert "network vivibox-demo-1 not found" in notes[-1]


def blocked(task, harness):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    task.transition(State.CHECKPOINT_BLOCKED, reason="verification still failing")
    (task.meta / "handoff" / "verify-feedback.md").write_text("# Verification failed\n")


def test_verifying_again_runs_the_gate_without_a_turn_of_the_agent(task):
    from vivibox import actions

    harness = FakeHarness(task)
    blocked(task, harness)
    actions.verify_again(task)
    st = task.read_state()
    assert st.state is State.VERIFY and st.iteration == 2, "no new attempt: nothing was written"
    assert task.events()[-1]["data"]["reason"] == "verify again"
    sup, notes = make(task, harness, results=[gate_result(True)])
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_FINAL and harness.prompts == []


def test_verifying_again_that_fails_again_waits_for_you_with_fresh_feedback(task):
    from vivibox import actions

    harness = FakeHarness(task)
    blocked(task, harness)
    actions.verify_again(task)
    sup, notes = make(task, harness, results=[gate_result(False)])
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED and harness.prompts == []
    assert "still failing" in notes[-1]


def test_only_a_blocked_task_can_be_verified_again(task):
    from vivibox import actions, gate

    task.transition(State.CHECKPOINT_PLAN)
    with pytest.raises(gate.GateError, match="verified again"):
        actions.verify_again(task)


def test_a_draft_the_gate_would_refuse_gets_one_repair_turn(task):
    placeholder = DRAFT.replace("health endpoint returns 200", gate.PLACEHOLDER)
    write_placeholder = lambda t: (t.meta / "handoff" / "plan-draft.md").write_text(placeholder)  # noqa: E731
    harness = FakeHarness(task, [write_placeholder, write_draft])
    sup, notes = make(task, harness)
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN and notes == ["plan ready for review"]
    assert "health endpoint returns 200" in task.plan_path.read_text()
    assert len(harness.prompts) == 2 and "placeholder" in harness.prompts[1], "told what is wrong"


def test_a_draft_still_refused_after_the_repair_turn_stops_for_you(task):
    placeholder = DRAFT.replace("health endpoint returns 200", gate.PLACEHOLDER)
    write_placeholder = lambda t: (t.meta / "handoff" / "plan-draft.md").write_text(placeholder)  # noqa: E731
    harness = FakeHarness(task, [write_placeholder, write_placeholder, write_draft])
    sup, notes = make(task, harness)
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_PLAN
    assert len(harness.prompts) == 2, "one repair turn, not a loop"
    assert "placeholder" in notes[0] and "reply" in notes[0]


def test_accepting_the_plan_puts_an_old_question_away(task):
    task.plan_path.write_text(DRAFT)
    task.transition(State.CHECKPOINT_PLAN)
    handoff = task.meta / "handoff"
    (handoff / supervisor.QUESTION).write_text("Which database?")
    supervisor.accept_plan(task, "plan accepted")
    assert not (handoff / supervisor.QUESTION).exists(), "or it would block the first turn as a new question"
    assert any(p.name.startswith("question-answered-") for p in handoff.iterdir())


def test_resuming_repeats_the_states_own_prompt():
    own = {State.PLAN: supervisor.PLAN_PROMPT, State.IMPLEMENT: supervisor.IMPLEMENT_PROMPT}
    for state, prompt in own.items():
        resumed = supervisor.resume_prompt(state)
        assert resumed.startswith("You were interrupted") and resumed.endswith(prompt)


def test_a_broken_environment_stops_the_task_without_using_an_attempt(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    broken = gate.GateResult(Path("/dev/null"))
    broken.commands = [gate.CommandResult("mvn -B verify", False, 1.0, "Cannot connect to the Docker daemon")]
    broken.environment = "Cannot connect to the Docker daemon"
    harness = FakeHarness(task)
    sup, notes = make(task, harness, results=[broken])
    sup.step()  # implement
    sup.step()  # verify
    st = task.read_state()
    assert (st.state, st.iteration) == (State.CHECKPOINT_BLOCKED, 1), "the first attempt, still"
    assert "could not run" in notes[-1] and "Docker" in notes[-1] and "g" in notes[-1]
    assert not (task.meta / supervisor.NEXT_PROMPT).exists(), (
        "no feedback turn for something the agent cannot fix"
    )
    assert "Verification could not run" in (task.meta / "handoff" / "verify-feedback.md").read_text()


def test_a_planner_and_a_writer_on_one_harness_have_a_conversation_each(task):
    class Named(FakeHarness):
        def __init__(self, task, actions=(), session="ses_x"):
            super().__init__(task, actions)
            self.session, self.sessions_seen = session, []

        def turn(self, prompt, session="", title="", on_step=None):
            self.sessions_seen.append(session)
            super().turn(prompt, session, title)
            return Turn(self.session, True, 0.01, 100, "done")

    planner = Named(task, [write_draft], session="ses_planner")
    writer = Named(task, session="ses_writer")
    sup, _ = make(task, writer)
    sup.planner = planner
    sup.step()  # plan
    task.transition(State.IMPLEMENT)
    sup.step()  # implement
    assert task.read_state().sessions == {"planner": "ses_planner", "writer": "ses_writer"}
    assert writer.sessions_seen == [""], "the writer's first turn is a new conversation, not the planner's"
    assert [e["data"]["role"] for e in task.events() if e["type"] == "turn"] == ["planner", "writer"]


def test_the_first_turn_of_a_role_opens_with_its_brief(task):
    harness = FakeHarness(task, [write_draft])
    sup, _ = make(task, harness)
    sup.step()  # plan: the writer plans too when there is no planner
    task.transition(State.IMPLEMENT)
    sup.step()  # implement, in the same conversation
    first, second = harness.prompts
    assert first.startswith(brief.role_text("writer")) and first.endswith(supervisor.PLAN_PROMPT)
    assert second == supervisor.IMPLEMENT_PROMPT, "said once per conversation, not once per turn"


def test_the_review_message_counts_removed_tests(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    result = gate_result(True)
    result.removed_tests = ["test/a.test.js: test('x')", "test/a.test.js: test('y')"]
    sup, notes = make(task, FakeHarness(task), results=[result])
    sup.step()
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_FINAL and "2 tests removed" in notes[-1]


def test_verifying_again_puts_the_agents_question_away(task):
    """The agent asked about the environment and you fixed it: the question is answered by the
    build running, and left in place it would block the next turn as a new one."""
    from vivibox import actions, supervisor

    harness = FakeHarness(task)
    blocked(task, harness)
    (task.meta / "handoff" / "question.md").write_text("Docker cannot pull images here: x509\n")
    actions.verify_again(task)
    assert supervisor.question(task) is None
    assert list((task.meta / "handoff").glob("question-answered-*.md")), "kept for the record"


def test_the_writer_is_asked_for_the_command_only_when_the_project_has_none(task):
    """It builds the project while it works, so it knows what builds and tests it; not when the
    project has a command, nor when the task was made with nothing to build."""

    def first_implement_prompt(project_verify, plan_header=""):
        t = create_task(
            task.root.parent / f"t{len(project_verify)}{len(plan_header)}", "demo", "Goal", TEMPLATE
        )
        t.plan_path.write_text(DRAFT.replace("+++\n", f"+++\n{plan_header}\n", 1))
        t.transition(State.CHECKPOINT_PLAN)
        supervisor.accept_plan(t, "plan accepted")
        harness = FakeHarness(t)
        ports = supervisor.Ports(run_gate=lambda _: gate_result(), risky_changes=lambda: [])
        supervisor.Supervisor(t, harness, ports, max_iterations=2, project_verify=project_verify).step()
        return harness.prompts[0]

    asked = first_implement_prompt([])
    assert supervisor.PROPOSE_PREFIX + supervisor.IMPLEMENT_PROMPT in asked
    assert "/task/handoff/verify-proposal.md" in asked
    assert supervisor.PROPOSE_PREFIX not in first_implement_prompt(["npm test"])
    assert supervisor.PROPOSE_PREFIX not in first_implement_prompt([], "verify = false")


def test_a_turn_leaves_its_running_cost_with_the_task_while_it_runs(task):
    """During a turn the task holds what it has cost so far, for the view to add; when the turn
    ends the turn event is the record and the running figure goes. The turn's start is an event too."""
    seen = []

    class Streaming(FakeHarness):
        def turn(self, prompt, session="", title="", on_step=None):
            on_step(0.02, 150, 1)
            seen.append(self.task.live_turn())
            on_step(0.05, 400, 2)
            seen.append(self.task.live_turn())
            return Turn("ses_1", True, 0.05, 400, "done")

    write_draft(task)
    sup, _ = make(task, Streaming(task))
    sup.step()
    assert [(s["cost"], s["tokens"], s["steps"]) for s in seen] == [(0.02, 150, 1), (0.05, 400, 2)]
    assert all(s["at"] for s in seen) and task.live_turn() is None, "gone with the turn"
    kinds = [e["type"] for e in task.events()]
    assert kinds.index("turn_started") < kinds.index("turn")
    started = next(e for e in task.events() if e["type"] == "turn_started")
    assert started["data"]["state"] == "plan" and started["data"]["role"]


class Flaky(FakeHarness):
    """Fails with what it is given, once per failure, then plans."""

    def __init__(self, task, failures):
        super().__init__(task)
        self.failures = list(failures)

    def turn(self, prompt, session="", title="", on_step=None):
        self.prompts.append(prompt)
        if self.failures:
            return Turn("", False, 0, 0, "", self.failures.pop(0))
        write_draft(self.task)
        return Turn("ses_1", True, 0.01, 100, "done")


def test_a_turn_is_tried_again_after_an_error_that_passes_with_time(task):
    """A provider that is busy or a network that hiccups is not a reason to stop a task and wait
    for you: the turn is tried again, half a minute, a minute, then two minutes later, and the
    timeline says so. The fourth failure in a row is a failure like any other."""
    waits = []
    harness = Flaky(task, ["429 Too Many Requests", "503 Service Unavailable: overloaded"])
    sup, notes = make(task, harness)
    sup.ports.sleep = waits.append
    sup.step()
    assert waits == [30, 60] and len(harness.prompts) == 3
    assert not task.read_state().paused and task.read_state().state is State.CHECKPOINT_PLAN
    retries = [e["data"] for e in task.events() if e["type"] == "turn_retry"]
    assert [r["wait"] for r in retries] == [30, 60] and "429" in retries[0]["error"]
    assert sum(e["type"] == "turn" for e in task.events()) == 1, "the turn that went through"
    assert any("trying again in 30 s" in n for n in notes)


def test_a_turn_that_keeps_failing_with_time_is_given_up_after_three_retries(task):
    waits = []
    harness = Flaky(task, ["connection reset by peer"] * 5)
    sup, _ = make(task, harness)
    sup.ports.sleep = waits.append
    sup.step()
    assert waits == [30, 60, 120] and len(harness.prompts) == 4
    st = task.read_state()
    assert st.paused and st.problem == "agent turn failed: connection reset by peer"


def test_every_tool_keeps_the_harness_contract():
    """The supervisor reads the same attributes and calls the same methods on every tool, so it
    never asks one what it can do: the base class answers for a tool that has no session ahead of
    a turn (""), is metered, and is not you."""
    import inspect

    from vivibox import claudecode, manual, opencode

    for tool in (opencode.OpenCode, claudecode.ClaudeCode, manual.Manual):
        assert issubclass(tool, Harness), tool
    plain = FakeHarness(None)
    assert plain.start_session("t") == "" and plain.metered and not plain.manual
    assert manual.Manual().manual and not opencode.OpenCode.manual
    source = inspect.getsource(supervisor)
    assert "hasattr(" not in source and "getattr(" not in source, "the contract says it all"


def priced(task, harness, results=(), **limits):
    sup, notes = make(task, harness, results)
    for name, value in limits.items():
        setattr(sup, name, value)
    return sup, notes


def test_the_cost_limit_stops_the_task_before_the_writers_next_turn(task):
    """Checked before a turn is paid for, not in the middle of one: a turn may run past the limit
    by its own cost, and the next one does not start. The task waits for you with the figures on
    its row; raising the limit and s takes it on."""
    harness = FakeHarness(task, [write_draft])  # every turn of the fake costs $0.01
    sup, notes = priced(task, harness, cost_limit=0.025)
    sup.step()  # the plan: $0.01
    task.transition(State.IMPLEMENT)  # your acceptance
    assert sup.step() and turns(task) == 2, "the first implement turn: $0.02, under the limit"
    task.transition(State.IMPLEMENT)  # as a failed verification would
    assert sup.step() and turns(task) == 3, "the second: $0.03, the turn that crosses the limit runs"
    task.transition(State.IMPLEMENT)
    assert not sup.step() and turns(task) == 3, "the next does not"
    st = task.read_state()
    assert st.paused and st.problem == "cost limit reached: $0.03 of $0.03"
    assert notes[-1] == "cost limit reached: $0.03 of $0.03; raise limits.cost_limit under k, then s"
    assert ui.view(task, st, True, 3).status == "cost limit reached", "the row says so"
    sup.cost_limit = 0.05  # raised under k; s clears the pause as it does after any problem
    task.set_paused(False)
    task.set_problem("")
    assert sup.step() and turns(task) == 4


def test_the_cost_warning_is_said_once_and_the_task_goes_on(task):
    harness = FakeHarness(task, [write_draft])
    sup, notes = priced(task, harness, cost_warning=0.015)
    sup.step()  # the plan: $0.01
    task.transition(State.IMPLEMENT)  # your acceptance
    sup.step()  # $0.02 after it, past the warning
    for _ in range(2):
        task.transition(State.IMPLEMENT)  # as a failed verification would
        assert sup.step(), "goes on"
    warned = [n for n in notes if "warning" in n]
    assert warned == ["cost $0.02, past the warning of $0.01"], "once, not at every turn"
    assert [e for e in task.events() if e["type"] == "cost_warning"], "and on the timeline"
    assert turns(task) == 4 and not task.read_state().paused


def turns(task) -> int:
    return sum(1 for e in task.events() if e["type"] == "turn")


# --- the reviewer -----------------------------------------------------------------------------

CLEAN = "# Review\n\n## Blocking\n\n## Not blocking\n\n- calc.py:3 — the docstring still says adds\n"
BLOCKING = (
    "# Review\n\n## Blocking\n\n- [ ] test_calc.py:20 — asserts True, proves nothing\n\n## Not blocking\n"
)


class FakeReviewer(FakeHarness):
    """Writes what it is told to, one text per turn, where the review container would."""

    name = "opencode"

    def __init__(self, task, out, texts):
        super().__init__(task)
        self.out, self.texts = out, list(texts)

    def turn(self, prompt, session="", title="", on_step=None):
        self.prompts.append(prompt)
        if self.texts:
            (self.out / "review.md").write_text(self.texts.pop(0))
        return Turn("rev_1", True, 0.05, 100, "done")


def reviewed(task, tmp_path, texts, results=(), mode="loop", max_reviews=2):
    """A task at verify with the plan accepted, a gate that passes, and a reviewer that answers
    with the texts, one per round. Returns the supervisor, the notes and the reviewer."""
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    (task.meta / gate.ACCEPTED_PLAN).write_text("+++\n+++\n")
    out = tmp_path / "review-out"
    out.mkdir()
    reviewer = FakeReviewer(task, out, texts)
    sup, notes = make(task, FakeHarness(task), results=results or [gate_result(True)] * 3)
    sup.reviewer, sup.review_mode, sup.max_reviews = reviewer, mode, max_reviews
    lifecycle = []
    sup.ports.review_up = lambda: lifecycle.append("up") or out
    sup.ports.review_down = lambda: lifecycle.append("down")
    sup.lifecycle = lifecycle
    return sup, notes, reviewer


def test_blocking_notes_go_back_to_the_writer_and_a_clean_review_lets_the_work_through(task, tmp_path):
    """Gate green, then the reviewer; a blocking note is a turn for the writer with the review to
    read, then the gate again, then the reviewer again; no blocking notes, and the work is yours."""
    sup, notes, reviewer = reviewed(task, tmp_path, [BLOCKING, CLEAN])
    sup.step()  # implement
    sup.step()  # verify: passes, so the reviewer is next
    assert task.read_state().state is State.REVIEW
    sup.step()  # the review: one blocking note
    st = task.read_state()
    assert st.state is State.IMPLEMENT and st.reviews == 1
    assert (task.meta / "handoff" / "review-1.md").read_text() == BLOCKING
    assert sup.lifecycle == ["up", "down"], "the review container lives for the turn"
    assert "git diff" in reviewer.prompts[0] and "/task/review/review.md" in reviewer.prompts[0]
    sup.step()  # the writer, told to fix the notes
    assert sup.harness.prompts[-1] == supervisor.REVIEW_FIX_PROMPT
    sup.step()  # verify
    sup.step()  # the review: clean
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and st.reviews == 2
    assert notes[-1].startswith("work ready for your review") and "review 2: no blocking notes" in notes[-1]
    assert [e["data"] for e in task.events() if e["type"] == "review"] == [
        {"round": 1, "blocking": 1, "not_blocking": 0, "problem": ""},
        {"round": 2, "blocking": 0, "not_blocking": 1, "problem": ""},
    ]
    assert st.sessions.get("reviewer") == "rev_1"


def test_blocking_notes_written_as_the_prompt_asks_go_back_to_the_writer(task, tmp_path):
    """One line a note, "path:line — …", no list mark: what a reviewer on deepseek-flash wrote in
    the behavioural run of 2026-09-25. Read as no notes, the work went to you with them open."""
    plain = "## Blocking\n\ntest_calc.py:10 — test_subtract asserts assertTrue(True)\n\n## Not blocking\n"
    sup, notes, _ = reviewed(task, tmp_path, [plain])
    for _ in range(3):  # implement, verify, review
        sup.step()
    st = task.read_state()
    assert st.state is State.IMPLEMENT, f"back to the writer, not to you: {notes}"
    assert [e["data"]["blocking"] for e in task.events() if e["type"] == "review"] == [1]


def test_the_last_round_sends_the_work_to_you_with_its_notes_open(task, tmp_path):
    sup, notes, _ = reviewed(task, tmp_path, [BLOCKING, BLOCKING], max_reviews=2)
    for _ in range(3):  # implement, verify, review 1
        sup.step()
    for _ in range(3):  # implement with the notes, verify, review 2
        sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and st.reviews == 2
    assert "review 2: 1 blocking note" in notes[-1]
    assert not sup.step(), "no third round: the rest is yours"


def test_supervised_mode_reviews_once_and_leaves_every_note_to_you(task, tmp_path):
    sup, notes, reviewer = reviewed(task, tmp_path, [BLOCKING], mode="supervised")
    for _ in range(3):
        sup.step()
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and st.reviews == 1
    assert "review 1: 1 blocking note" in notes[-1] and len(reviewer.prompts) == 1


def test_a_review_that_is_not_one_is_sent_back_once_then_left_to_you(task, tmp_path):
    """Like a plan draft that is not a plan: one turn to fix it, and if that fails too the work
    goes to you with the text as it is, said to be unreadable."""
    sup, notes, reviewer = reviewed(task, tmp_path, ["Looks good to me.", "Still just prose."])
    for _ in range(3):
        sup.step()
    assert len(reviewer.prompts) == 2 and "## Blocking" in reviewer.prompts[1], "told what is missing"
    st = task.read_state()
    assert st.state is State.CHECKPOINT_FINAL and st.reviews == 1
    assert "review 1 unreadable" in notes[-1]
    assert (task.meta / "handoff" / "review-1.md").read_text() == "Still just prose.\n", "kept for you"
    assert [e["data"]["problem"] for e in task.events() if e["type"] == "review"] == [
        "the section ## Blocking is missing"
    ]


def test_each_round_is_a_fresh_conversation_in_a_fresh_container(task, tmp_path):
    """The container is made anew for every round, so the last round's session is gone; the
    reviewer starts a conversation each time and gets its brief each time."""
    sup, _, reviewer = reviewed(task, tmp_path, [BLOCKING, CLEAN])
    for _ in range(6):
        sup.step()
    assert all(p.startswith(brief.role_text("reviewer")) for p in reviewer.prompts)


def test_without_a_reviewer_a_green_gate_goes_to_you_as_before(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    (task.meta / gate.ACCEPTED_PLAN).write_text("+++\n+++\n")
    sup, notes = make(task, FakeHarness(task), results=[gate_result(True)])
    sup.step()
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_FINAL and task.read_state().reviews == 0


def test_your_reply_at_the_end_gives_the_reviewer_its_rounds_back(task, tmp_path):
    task.set_reviews(2)
    task.reset_iterations()
    assert task.read_state().reviews == 0


def test_the_agents_question_stands_when_the_verification_fails_again_after_g(task):
    """You pressed g on a question about the environment, and the build failed for the same
    reason: the question is still unanswered, so it comes back to you, with no turn of the
    agent (a feedback turn would have it work around what it asked about) and no attempt spent."""
    from vivibox import actions, supervisor

    harness = FakeHarness(task)
    blocked(task, harness)
    asked = "The gate installs with `npm ci`, but this repository has no lockfile. Which way?\n"
    (task.meta / "handoff" / "question.md").write_text(asked)
    before = task.read_state().iteration
    actions.verify_again(task)
    sup, notes = make(task, harness, results=[gate_result(False)])
    sup.step()  # verify: fails again
    st = task.read_state()
    assert st.state is State.CHECKPOINT_BLOCKED and st.iteration == before
    assert supervisor.question(task) == asked.strip(), "back where the view and the next turn read it"
    assert harness.prompts == [], "no turn of the agent"
    assert "question stands" in notes[-1] and "npm ci" in notes[-1]
    assert (task.meta / "handoff" / "verify-feedback.md").exists(), "why it failed is there for l"
    actions.verify_again(task)
    sup, notes = make(task, harness, results=[gate_result(True)])
    sup.step()  # verify: passes once you fixed it
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert supervisor.question(task) is None, "answered by the build running"
