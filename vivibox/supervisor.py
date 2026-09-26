"""Supervisor: drives one task through its states, one agent turn at a time, and stops at checkpoints.

It runs as a background process of its own, logging to .task/log/supervisor.log. Decisions at
checkpoints are yours (vivibox accept / reply); between them the supervisor sends the agent its next
prompt, runs the gate and records every step in the task's event log.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, gate, manual, proposal, reviewing, ui
from .config import Project
from .harness import Harness, HarnessError, Turn
from .plan import Plan, PlanError, parse_plan, without_notes
from .risky import Change
from .states import State, waits_for_user
from .task import Task, TaskState

NEXT_PROMPT = "next-prompt.md"
QUESTION = "question.md"

# Every prompt that starts a turn ends by naming what ends it: the two endings the brief
# (templates/instructions.md) allows, and nothing else. docs/prompt-guidelines.md says why.
PLAN_PROMPT = """Read the goal in /task/plan.md and explore the repository. Write the plan to
/task/handoff/plan-draft.md, a copy of /task/plan.md filled in:
- keep the header between the +++ lines, except: set summary to one line of at most 100
  characters naming what the task does;
- under "## Acceptance criteria", replace the line "Replace with an observable outcome you can
  check" with concrete "- [ ]" items, each checkable by reading or running code; keep the
  first item;
- fill in the other sections as their <!-- notes --> say. The writer may not remember this
  conversation: the plan says everything.
Do not change code. End the turn when the draft is written, or when a question is in
/task/handoff/question.md."""

PLAN_REPAIR_PROMPT = """The plan draft in /task/handoff/plan-draft.md is not ready: {problem}. Fix
that in the draft. End the turn when it is fixed."""

IMPLEMENT_PROMPT = """The plan in /task/plan.md is accepted. Carry it out: write the code and the
tests, commit, and tick each item in /task/handoff/criteria.md the moment you have verified it.
End the turn when every item is ticked and committed, or when a question is in
/task/handoff/question.md."""

FEEDBACK_PROMPT = """Verification failed. Read /task/handoff/verify-feedback.md: it names what
failed and quotes the lines that say why (the whole output is in /task/handoff/verify.log). Fix
what it names and commit. End the turn when that is done, or, if the cause is outside the code,
when you have written it to /task/handoff/question.md."""

REVIEW_PROMPT = """The verification passed: the build and the tests are green, do not run them.
Read /task/plan.md, /task/handoff/criteria.md, /task/handoff/red.md and /task/handoff/comments.md,
then the work itself: `git diff {base}..HEAD` in the repository you are in. Write
/task/review/review.md with two sections. Under "## Blocking": what keeps the work from being
what the plan says, or from proving it: a test that cannot fail, a criterion ticked but not met,
behaviour the plan rules out. Under "## Not blocking": the rest. Each note is one line,
"path:line — what is wrong and what would make it right"; a section may be empty. Do not report
what the verification already checks: commits, ticks, switched-off tests, red evidence named.
End the turn when the review is written."""

REVIEW_REPAIR_PROMPT = """The review in /task/review/review.md is not one the orchestrator can
read: {problem}. Rewrite it with the two sections, "## Blocking" and "## Not blocking", and a
place (path:line) on every note. End the turn when it is rewritten."""

REVIEW_FIX_PROMPT = """The reviewer read your work. Read the newest /task/handoff/review-N.md (N is
the round): fix every note under Blocking, commit, and keep the ticks in /task/handoff/criteria.md
true. Where you disagree, say why in one paragraph in /task/handoff/review-N-reply.md instead.
End the turn when that is done and committed, or when a question is in
/task/handoff/question.md."""

COMMENT_PROMPT = """The user replied. Read the newest entry in /task/handoff/comments.md and do
what it asks. End the turn when that is done and committed, or when you have written a new
question to /task/handoff/question.md."""

PLAN_COMMENT_PROMPT = """The user commented on your plan. Read the newest entry in
/task/handoff/comments.md and update /task/handoff/plan-draft.md. End the turn when the draft is
updated."""

RESUME_PREFIX = """You were interrupted. Check `git status` and the files in /task/handoff/ for what
is already done, then go on with this:

"""

# Before the first implementing turn of a project that is prepared (Project.prepare): what ran, so
# the writer builds on it instead of building everything again, and where to look when it failed.
PREPARED_PREFIX = """Before this turn the orchestrator ran {commands} once in the repository, so what it
built and installed is there; its output is in /task/handoff/prepare.log. Build only the modules you
change, and all of them in one command (Maven: `-pl core,app`): a module built on its own takes its
neighbours as they were installed, before your change.

"""

# Before the first implementing turn of a task whose project has no verification command: the
# writer, who runs the build anyway, says what builds and tests it; the gate verifies the task with
# that, and you decide on its own whether the project keeps it.
PROPOSE_PREFIX = """No command verifies this project yet. When the work is done, write the one command
that builds the whole project and runs all its tests, as its pipeline would, on one line in
/task/handoff/verify-proposal.md: the orchestrator verifies your work with it, and the user
decides whether the project keeps it. A command that picks some tests (`-Dtest=`, `--tests`,
`-k`, a test file) is refused, and you propose again.

"""


def resume_prompt(state: State) -> str:
    """After a stop or a crash: the state's own prompt again, behind a word about the interruption.
    A session that survived would go on from a bare "continue"; one that was lost would not know
    what the state asks for."""
    return RESUME_PREFIX + {State.PLAN: PLAN_PROMPT, State.IMPLEMENT: IMPLEMENT_PROMPT}[state]


@dataclass(frozen=True)
class Action:
    """A button in a desktop notification and the command it runs."""

    name: str
    label: str
    command: list[str]
    # For programs that keep running, like an IDE: started and left alone.
    detach: bool = False


def notify(task_id: str, message: str, desktop: bool = True, actions: Sequence[Action] = ()) -> None:
    """Always in the supervisor window; on the desktop too unless notifications.desktop is false."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)
    if not desktop or not shutil.which("notify-send"):
        return
    command = ["notify-send", "--app-name=vivibox", f"vivibox {task_id}", message]
    if not actions:
        subprocess.run(command, check=False)
        return
    # With buttons notify-send waits for a click, so it runs beside the supervisor, not in its way.
    buttons = [f"--action={a.name}={a.label}" for a in actions]
    threading.Thread(target=_await_click, args=(task_id, command + buttons, actions), daemon=True).start()


def _await_click(task_id: str, command: list[str], actions: Sequence[Action]) -> None:
    clicked = subprocess.run(command, capture_output=True, text=True, check=False).stdout.strip()
    for action in actions:
        if action.name == clicked:
            print(f"[{time.strftime('%H:%M:%S')}] {action.label}", flush=True)
            try:
                if action.detach:
                    subprocess.Popen(action.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)  # fmt: skip
                    continue
                p = subprocess.run(action.command, capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                notify(task_id, f"{action.label} failed: {e}")
                continue
            if p.returncode != 0:
                notify(task_id, f"{action.label} failed: {(p.stderr or p.stdout).strip()[:200]}")


def set_next_prompt(task: Task, text: str) -> None:
    (task.meta / NEXT_PROMPT).write_text(text)


def next_prompt(task: Task, default: str) -> str:
    """The message for the agent's next turn. Kept until a turn succeeds, so a stop does not lose it."""
    path = task.meta / NEXT_PROMPT
    return path.read_text() if path.exists() else default


def clear_next_prompt(task: Task) -> None:
    (task.meta / NEXT_PROMPT).unlink(missing_ok=True)


def accept_plan(task: Task, reason: str) -> None:
    """Freezes the plan and its criteria, and sends the agent on to implementation."""
    gate.accept_plan(task)
    # A question asked while planning is answered by the plan you accepted; left where it is, the
    # first turn of implementation would end on it as a new question.
    put_question_away(task)
    set_next_prompt(task, IMPLEMENT_PROMPT)
    task.reset_iterations()
    task.transition(State.IMPLEMENT, reason=reason)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def question(task: Task) -> str | None:
    path = task.meta / "handoff" / QUESTION
    text = path.read_text().strip() if path.exists() else ""
    return text or None


def put_question_away(task: Task) -> None:
    """A question that is answered or overtaken: kept for the record, out of the supervisor's way."""
    path = task.meta / "handoff" / QUESTION
    if path.exists():
        path.rename(path.with_name(f"question-answered-{time.strftime('%Y%m%d-%H%M%S')}.md"))


# Errors that pass with time: the provider busy or rate limiting, the network gone for a moment.
TRANSIENT = re.compile(
    r"\b(429|502|503|504)\b|rate.?limit|too many requests|overloaded|bad gateway|service unavailable"
    r"|gateway time.?out|timed? ?out|ECONNRESET|ECONNREFUSED|connection (reset|refused)|temporarily",
    re.IGNORECASE,
)
# How long to wait before each retry of a turn; as many retries as there are waits.
RETRY_WAITS = (30, 60, 120)
# Seconds between looks at a preparation the writer waits for.
PREPARE_POLL = 15


def transient(error: str) -> bool:
    return bool(TRANSIENT.search(error))


@dataclass
class Ports:
    """What the supervisor asks of whoever runs it: the gate and the risky files of the task's
    clone, you (a notification, the review copy, a window on the agent), where a plan's
    verification is kept, and how a wait passes. The command line wires them to the project and
    the pod in cli.make_supervisor; a test passes what it wants to see."""

    run_gate: Callable[[Task], gate.GateResult]
    risky_changes: Callable[[], list[Change]]
    # notify(task_id, message, kind): kind "plan" or "review" lets the caller add notification buttons.
    notify: Callable[..., None] = lambda task_id, message, kind="": notify(task_id, message)
    # Fetches the work into your repository and updates the review copy; returns the copy's path.
    prepare_review: Callable[[], Path | None] = lambda: None
    # Called once a turn's session is known and recorded, before the turn runs: your view of the
    # agent opens then, not when the turn you wanted to watch is already over.
    session_started: Callable[[TaskState], None] = lambda st: None
    # The reviewer's container: up, with the directory its review is written to; and down.
    review_up: Callable[[], Path] = lambda: Path()
    review_down: Callable[[], None] = lambda: None
    # True while the project's preparation runs, which the writer's turn waits for.
    preparing: Callable[[], bool] = lambda: False
    # How a retry waits; a test passes something that does not.
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Supervisor:
    task: Task
    harness: Harness
    ports: Ports
    max_iterations: int
    # The project's verify commands, and whether it has nothing to build.
    project_verify: list[str] = field(default_factory=list)
    project_no_build: bool = False
    # The project's preparation, which ran before the writer's first turn; that turn is told of it.
    prepared: list[str] = field(default_factory=list)
    # The project file as it is now, read at every step; None keeps the fields above as given.
    current_project: Callable[[], Project] | None = None
    # The role that plans. None means the writer plans too, which is what a caller with one harness
    # gets; the command line always passes both, because the config always names both.
    planner: Harness | None = None
    # Your checkout, which a manual planner's CLI prompt points at instead of the task's clone.
    source: Path | None = None
    # The answer file as last tried, so an answer that is not a plan is reported once, not every poll.
    answer_seen: float = 0.0
    # Dollars the task may cost before you are told, and before it stops for you; 0 is no limit.
    cost_warning: float = 0.0
    cost_limit: float = 0.0
    # The reviewer, when there is one: reads the work after a green gate. loop sends its blocking
    # notes back to the writer, up to max_reviews rounds; supervised reviews once, for you.
    reviewer: Harness | None = None
    review_mode: str = "loop"
    max_reviews: int = 2
    # The gate's result the review followed, for the message that ends the task's work.
    last_gate: gate.GateResult | None = None

    def role_for(self, state: State) -> str:
        """Planning is where a wrong decision costs the most and the fewest tokens are spent, so it
        is worth a different model, and sometimes a different tool, from the one that types."""
        if state is State.REVIEW:
            return "reviewer"
        return "planner" if state is State.PLAN and self.planner else "writer"

    def harness_of(self, role: str) -> Harness:
        if role == "reviewer" and self.reviewer:
            return self.reviewer
        return self.planner if role == "planner" and self.planner else self.harness

    def step(self) -> bool:
        """Does one unit of work. False when there is nothing to do until you act."""
        if self.current_project:
            p = self.current_project()
            self.project_verify, self.project_no_build, self.prepared = p.verify, p.no_build, p.prepare
        st = self.task.read_state()
        if st.state is State.CHECKPOINT_PLAN and st.awaiting_plan and not st.paused:
            self._watch_answer()
            return False
        if st.paused or st.state is State.DONE or waits_for_user(st.state):
            return False
        if st.state in (State.PLAN, State.IMPLEMENT) and self._past_the_limit():
            return False
        handlers = {
            State.PLAN: self._plan,
            State.IMPLEMENT: self._implement,
            State.VERIFY: self._verify,
            State.REVIEW: self._review,
        }
        handlers[st.state](st)
        return True

    def _past_the_limit(self) -> bool:
        """Before a turn is paid for: past the warning, said once; at the limit, the task stops
        for you with the figures on its row, and goes on once you raise the limit and start it."""
        if not (self.cost_warning or self.cost_limit):
            return False
        spent = ui.cost(self.task).total
        if self.cost_limit and spent >= self.cost_limit:
            figures = f"cost limit reached: ${spent:.2f} of ${self.cost_limit:.2f}"
            self.task.set_paused(True, problem=figures)
            self.ports.notify(self.task.id, f"{figures}; raise limits.cost_limit under k, then s")
            return True
        warned = any(e["type"] == "cost_warning" for e in self.task.events())
        if self.cost_warning and spent >= self.cost_warning and not warned:
            self.task.event("cost_warning", spent=spent, warning=self.cost_warning)
            self.ports.notify(
                self.task.id, f"cost ${spent:.2f}, past the warning of ${self.cost_warning:.2f}"
            )
        return False

    def run(self, poll: float = 2.0, on_step: Callable[[TaskState], None] = lambda st: None) -> None:
        while self.task.read_state().state is not State.DONE:
            try:
                progressed = self.step()
            except Exception as e:  # keep the task inspectable instead of dying silently
                self.task.set_paused(True, problem=f"stopped on an error: {e}")
                self.task.event("error", message=str(e)[:2000])
                self.ports.notify(
                    self.task.id, f"stopped on an error, see 'vivibox status {self.task.id}': {e}"
                )
                progressed = False
            on_step(self.task.read_state())
            if not progressed:
                time.sleep(poll)

    # --- states -----------------------------------------------------------------------------

    def _turn(self, st: TaskState, prompt: str, role: str = "") -> Turn | None:
        role = role or self.role_for(st.state)
        harness = self.harness_of(role)
        was = st.sessions.get(role, "")
        if not was:
            # The first message of a role's conversation says what the role is and owns.
            prompt = f"{brief.role_text(role)}\n{prompt}"
        title = f"{self.task.id}: {st.goal}"[:80]
        if not was:
            try:
                was = harness.start_session(title)
            except HarnessError as e:  # the turn makes its own, as before; only watching waits
                self.task.event("session_not_started", error=str(e)[:500])
            if was:  # a tool that keeps no session ahead of the turn gives ""
                self.task.set_session(role, was)
                self.ports.session_started(self.task.read_state())
        turn = self._attempts(harness, prompt, was, title, st, role)
        if turn.session and turn.session != was:
            self.task.set_session(role, turn.session)
        self.task.event(
            "turn",
            state=str(st.state),
            role=role,
            harness=harness.name,
            metered=harness.metered,
            ok=turn.ok,
            cost=turn.cost,
            tokens=turn.tokens,
            error=turn.error[:500],
        )
        if not turn.ok:
            self.task.set_paused(True, problem=f"agent turn failed: {turn.error or turn.text}")
            self.ports.notify(self.task.id, f"agent turn failed, task paused: {turn.error[:200]}")
            return None
        clear_next_prompt(self.task)
        return turn

    def _attempts(
        self, harness: Harness, prompt: str, session: str, title: str, st: TaskState, role: str
    ) -> Turn:
        """The turn, and again after an error that passes with time (the provider busy, the
        network gone for a moment): half a minute, a minute, then two minutes later. The failure
        after the last wait is a failure like any other, and the task stops for you."""
        for wait in RETRY_WAITS:
            turn = self._one_turn(harness, prompt, session, title, st, role)
            if turn.ok or not transient(turn.error):
                return turn
            self.task.event("turn_retry", wait=wait, error=turn.error[:500])
            self.ports.notify(
                self.task.id, f"agent turn failed ({turn.error[:80]}); trying again in {wait} s"
            )
            self.ports.sleep(wait)
        return self._one_turn(harness, prompt, session, title, st, role)

    def _one_turn(
        self, harness: Harness, prompt: str, session: str, title: str, st: TaskState, role: str
    ) -> Turn:
        self.task.event("turn_started", state=str(st.state), role=role)
        try:
            return harness.turn(prompt, session=session, title=title, on_step=self.task.set_live_turn)
        finally:
            self.task.clear_live_turn()  # the turn event is the record from here on

    def _checkpoint(self, target: State, reason: str, kind: str = "") -> None:
        # Every checkpoint first checks risky files: you may open the project in IntelliJ at a checkpoint.
        if self.ports.risky_changes():
            self.task.transition(State.APPROVAL_RISKY, reason=reason, then=str(target))
            self.ports.notify(
                self.task.id, f"{reason}; risky files changed, run 'vivibox risky {self.task.id}' first"
            )
        else:
            self.task.transition(target, reason=reason)
            self.ports.notify(self.task.id, reason, kind=kind)

    def _plan(self, st: TaskState) -> None:
        if (plan := self._your_plan(st)) is not None:
            self._plan_ready(st, plan, yours=True)
            return
        if self.planner is not None and self.planner.manual:
            self._plan_manually(st)
            return
        if self._turn(st, next_prompt(self.task, PLAN_PROMPT)) is None:
            return
        if q := question(self.task):
            self._checkpoint(State.CHECKPOINT_PLAN, f"question from the agent: {q[:200]}")
            return
        plan, problem = self._read_draft()
        if problem:
            # One turn to fix what acceptance would refuse anyway (the placeholder left in, no
            # criteria, no verify command): cheaper than your reply, and once, not a loop.
            if self._turn(st, PLAN_REPAIR_PROMPT.format(problem=problem)) is None:
                return
            if q := question(self.task):
                self._checkpoint(State.CHECKPOINT_PLAN, f"question from the agent: {q[:200]}")
                return
            plan, problem = self._read_draft()
        if problem:
            self._checkpoint(
                State.CHECKPOINT_PLAN, f"no valid plan draft ({problem}); edit the plan yourself or reply"
            )
            return
        draft = self.task.meta / "handoff" / "plan-draft.md"
        # Without the template's notes: from here the plan is yours to read, not a form to fill.
        self.task.plan_path.write_text(without_notes(draft.read_text()))
        self._plan_ready(st, plan)

    def _your_plan(self, st: TaskState) -> Plan | None:
        """The plan as you left it, when it is finished (--draft, then e) and this is the task's first
        planning: acceptance would take it as it is, so no planner, yours or an agent, is asked to
        write it again. A plan still carrying the template's placeholder is a starting point for the
        planner. After your reply, or a stop mid-planning, the next prompt is set, and the planner
        goes on."""
        if (self.task.meta / NEXT_PROMPT).exists() or (self.task.meta / "handoff" / "plan-draft.md").exists():
            return None
        try:
            plan = parse_plan(self.task.plan_path.read_text())
            gate.check_plan(plan)
        except (OSError, PlanError, gate.GateError):
            return None
        return plan

    def _plan_ready(self, st: TaskState, plan: Plan, yours: bool = False) -> None:
        if plan.summary:
            self.task.set_goal(plan.summary)
        if st.auto_plan and not self.ports.risky_changes():
            # Through the plan checkpoint, so the event log reads the same as when you accept.
            self.task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
            try:
                accept_plan(self.task, "plan accepted automatically")
                print(f"[{time.strftime('%H:%M:%S')}] plan accepted automatically", flush=True)
            except gate.GateError as e:
                self.ports.notify(self.task.id, f"plan not accepted automatically ({e}); review it")
            return
        self._checkpoint(
            State.CHECKPOINT_PLAN,
            "your plan is ready for review" if yours else "plan ready for review",
            kind="plan",
        )

    def _read_draft(self):
        """The draft as a plan, or what keeps it from being one: unreadable, or one acceptance
        would refuse."""
        draft = self.task.meta / "handoff" / "plan-draft.md"
        try:
            plan = parse_plan(draft.read_text())
            gate.check_plan(plan)
        except (OSError, PlanError, gate.GateError) as e:
            return None, str(e)
        return plan, ""

    def _plan_manually(self, st: TaskState) -> None:
        """You plan in your own chat. A chat in a browser cannot see the repository, so the writer
        first reports on it, for cents; a new project has nothing to report."""
        context = self.task.meta / "handoff" / manual.CONTEXT
        known = context.exists() or manual.repository_is_empty(self.task.repo)
        if not known and self._turn(st, manual.RECON_PROMPT, "writer") is None:
            return
        manual.write_prompts(self.task, self.source or self.task.repo, self.project_verify)
        self.task.set_awaiting_plan(True)
        self._checkpoint(
            State.CHECKPOINT_PLAN,
            f"plan it in your own chat: vivibox plan prompt {self.task.id}, "
            f"then vivibox plan import {self.task.id}",
        )

    def _watch_answer(self) -> None:
        """A CLI you plan with writes its answer to the file itself, and nothing else would tell
        vivibox it is there: you would press e only to find the plan waiting. Only an answer newer
        than the prompt counts, so one left from an earlier round is not taken for the new one, and
        only one that has stopped changing, so a file still being written is not read half-done."""
        answer, prompt = self.task.meta / manual.ANSWER, self.task.meta / manual.PROMPT
        try:
            written, asked = answer.stat().st_mtime, prompt.stat().st_mtime
        except OSError:
            return
        if written <= asked or written == self.answer_seen or time.time() - written < 1:
            return
        self.answer_seen = written
        try:
            manual.import_answer(self.task)
        except (OSError, PlanError) as e:
            self.task.event("plan_unreadable", error=str(e)[:500])
            self.ports.notify(
                self.task.id, f"the plan in {answer.name} is not readable yet ({e}); press e to see it"
            )
            return
        count = len(parse_plan(self.task.plan_path.read_text()).criteria)
        self.ports.notify(
            self.task.id, f"your plan is in, {count} criteria; review and accept it", kind="plan"
        )

    def _implement(self, st: TaskState) -> None:
        if self.ports.preparing():
            self.ports.sleep(PREPARE_POLL)
            return
        prompt = next_prompt(self.task, IMPLEMENT_PROMPT)
        if self.prepared and prompt.endswith(IMPLEMENT_PROMPT):
            prompt = PREPARED_PREFIX.format(commands=", ".join(f"`{c}`" for c in self.prepared)) + prompt
        if prompt.endswith(IMPLEMENT_PROMPT) and self._asks_for_command():
            prompt = PROPOSE_PREFIX + prompt
        if self._turn(st, prompt) is None:
            return
        if q := question(self.task):
            self._checkpoint(State.CHECKPOINT_BLOCKED, f"question from the agent: {q[:200]}")
            return
        self.task.transition(State.VERIFY)

    def _asks_for_command(self) -> bool:
        """The project has no command, and neither it nor the task says there is nothing to build."""
        nothing = self.project_no_build or proposal.nothing_to_build(self.task)
        return not self.project_verify and not nothing

    def _verify(self, st: TaskState) -> None:
        result = self.ports.run_gate(self.task)
        target = gate.next_state(result, st.iteration, self.max_iterations)
        if target in (State.IMPLEMENT, State.CHECKPOINT_BLOCKED):
            gate.write_feedback(self.task, result)
        if result.environment:
            # No turn of the agent's: it cannot fix this, and a feedback turn would have it try.
            self.task.transition(target, reason="verification could not run")
            self.ports.notify(
                self.task.id,
                f"verification could not run: {result.environment[:150]}; fix it, then press g"
                f" (vivibox verify-again {self.task.id})",
            )
        elif target is State.IMPLEMENT:
            set_next_prompt(self.task, FEEDBACK_PROMPT)
            self.task.transition(target, reason="verification failed")
        elif target is State.CHECKPOINT_BLOCKED:
            self.task.transition(target, reason="verification still failing")
            self.ports.notify(self.task.id, f"verification still failing after {st.iteration} attempts")
        elif self._review_due(st):
            self.last_gate = result
            self.task.transition(State.REVIEW, reason="verification passed")
        elif target is State.APPROVAL_RISKY:
            self.task.transition(target, reason="verification passed", then=str(State.CHECKPOINT_FINAL))
            self.ports.notify(
                self.task.id, f"done pending your approval of risky files: 'vivibox risky {self.task.id}'"
            )
        else:
            self.task.transition(target, reason="verification passed")
            self.ports.notify(self.task.id, self._review_message(result), kind="review")

    def _review_due(self, st: TaskState) -> bool:
        """Whether the reviewer reads the work now: once in supervised mode, and in loop mode
        after every green gate until its rounds are used up. Your reply gives it them back."""
        if self.reviewer is None or self.review_mode == "none":
            return False
        rounds = self.max_reviews if self.review_mode == "loop" else 1
        return st.reviews < rounds

    def _review(self, st: TaskState) -> None:
        """The reviewer's round: a fresh container and a fresh conversation, its review kept as
        handoff/review-N.md. Blocking notes go back to the writer while rounds are left; the last
        round, or none, and the work goes on to you with the notes."""
        n = st.reviews + 1
        out = self.ports.review_up()
        try:
            self.task.set_session("reviewer", "")  # the last round's server is gone with its container
            st = self.task.read_state()
            if self._turn(st, REVIEW_PROMPT.format(base=st.base_commit), role="reviewer") is None:
                return
            text = _read(out / "review.md")
            if problem := reviewing.problem(text):
                if self._turn(st, REVIEW_REPAIR_PROMPT.format(problem=problem), role="reviewer") is None:
                    return
                text = _read(out / "review.md")
        finally:
            self.ports.review_down()
        reviewing.keep(self.task, n, text)
        self.task.set_reviews(n)
        problem = reviewing.problem(text)
        review = reviewing.parse_review(text) if not problem else reviewing.Review()
        blocking, others = len(review.blocking), len(review.not_blocking)
        self.task.event("review", round=n, blocking=blocking, not_blocking=others, problem=problem)
        if blocking and self.review_mode == "loop" and n < self.max_reviews:
            set_next_prompt(self.task, REVIEW_FIX_PROMPT)
            self.task.transition(State.IMPLEMENT, reason=f"review {n}: {blocking} blocking")
            print(
                f"[{time.strftime('%H:%M:%S')}] review {n}: {blocking} blocking, back to the writer",
                flush=True,
            )
            return
        if problem:
            said = f"review {n} unreadable ({problem})"
        elif blocking:
            said = (
                f"review {n}: {blocking} blocking note{'s' if blocking != 1 else ''}, {others} not blocking"
            )
        else:
            said = f"review {n}: no blocking notes, {others} not blocking"
        self._checkpoint(
            State.CHECKPOINT_FINAL, f"{self._review_message(self.last_gate)}; {said}", kind="review"
        )

    def _review_message(self, result: gate.GateResult | None = None) -> str:
        # Tests that went missing are said here, not to the agent, which would put them back.
        n = len(result.removed_tests) if result else 0
        gone = f"; {n} test{'s' if n != 1 else ''} removed" if n else ""
        try:
            path = self.ports.prepare_review()
        except Exception as e:  # the work is done either way; the review copy is a convenience
            self.task.event("review_prepare_failed", error=str(e)[:500])
            copy = f"the review copy failed ({e}), try: vivibox review {self.task.id}"
            return f"work ready for your review{gone}; {copy}"
        if path is None:
            return f"work ready for your review{gone}: vivibox review {self.task.id}"
        return f"work ready for your review in {path}{gone}"
