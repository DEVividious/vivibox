"""Supervisor: drives one task through its states, one agent turn at a time, and stops at checkpoints.

It runs as a background process of its own, logging to .task/log/supervisor.log. Decisions at
checkpoints are yours (vivibox accept / reply); between them the supervisor sends the agent its next
prompt, runs the gate and records every step in the task's event log.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import brief, gate, manual
from .opencode import HarnessError, Turn
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
  characters naming what the task does, and, if verify is empty, set it to the command that
  builds and tests this project once the plan is done, a new product's too, e.g.
  verify = ["npm test"]; verify = false only when there will never be a build;
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

COMMENT_PROMPT = """The user replied. Read the newest entry in /task/handoff/comments.md and do
what it asks. End the turn when that is done and committed, or when you have written a new
question to /task/handoff/question.md."""

PLAN_COMMENT_PROMPT = """The user commented on your plan. Read the newest entry in
/task/handoff/comments.md and update /task/handoff/plan-draft.md. End the turn when the draft is
updated."""

RESUME_PREFIX = """You were interrupted. Check `git status` and the files in /task/handoff/ for what
is already done, then go on with this:

"""


def resume_prompt(state: State) -> str:
    """After a stop or a crash: the state's own prompt again, behind a word about the interruption.
    A session that survived would go on from a bare "continue"; one that was lost would not know
    what the state asks for."""
    return RESUME_PREFIX + {State.PLAN: PLAN_PROMPT, State.IMPLEMENT: IMPLEMENT_PROMPT}[state]


class Harness(Protocol):
    # The tool's name, for the event log; conversations are kept one per role, not per harness.
    name: str
    # False when a turn's reported cost is a list price rather than money spent, as it is on a
    # subscription. A total that added the two would be neither.
    metered: bool

    def turn(self, prompt: str, session: str = "", title: str = "", on_step=None) -> Turn: ...


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


def accept_plan(
    task: Task, reason: str, project_verify=(), save_verify=None, project_no_build: bool = False
) -> None:
    """Freezes the plan and its criteria, and sends the agent on to implementation."""
    plan = gate.accept_plan(task, project_verify, project_no_build)
    if plan.verify and not (project_verify or project_no_build) and save_verify:
        save_verify(plan.verify, False)  # the new project now knows how it is verified
    # verify = false in a plan holds for this task only: a new product's first plan finds an empty
    # repository, and the build the writer makes is for the next plan to name.
    # A question asked while planning is answered by the plan you accepted; left where it is, the
    # first turn of implementation would end on it as a new question.
    put_question_away(task)
    set_next_prompt(task, IMPLEMENT_PROMPT)
    task.reset_iterations()
    task.transition(State.IMPLEMENT, reason=reason)


def question(task: Task) -> str | None:
    path = task.meta / "handoff" / QUESTION
    text = path.read_text().strip() if path.exists() else ""
    return text or None


def put_question_away(task: Task) -> None:
    """A question that is answered or overtaken: kept for the record, out of the supervisor's way."""
    path = task.meta / "handoff" / QUESTION
    if path.exists():
        path.rename(path.with_name(f"question-answered-{time.strftime('%Y%m%d-%H%M%S')}.md"))


@dataclass
class Supervisor:
    task: Task
    harness: Harness
    run_gate: Callable[[Task], gate.GateResult]
    risky_changes: Callable[[], list[Change]]
    max_iterations: int
    # notify(task_id, message, kind): kind "plan" or "review" lets the caller add notification buttons.
    notify: Callable[..., None] = lambda task_id, message, kind="": notify(task_id, message)
    # Fetches the work into your repository and updates the review copy; returns the copy's path.
    prepare_review: Callable[[], Path | None] = lambda: None
    # The project's verify commands, and where to keep the ones a plan brings for a new project.
    project_verify: list[str] = field(default_factory=list)
    project_no_build: bool = False
    save_verify: Callable[[list[str], bool], None] = lambda commands, no_build: None
    # The role that plans. None means the writer plans too, which is what a caller with one harness
    # gets; the command line always passes both, because the config always names both.
    planner: Harness | None = None
    # Your checkout, which a manual planner's CLI prompt points at instead of the task's clone.
    source: Path | None = None
    # The answer file as last tried, so an answer that is not a plan is reported once, not every poll.
    answer_seen: float = 0.0
    # Called once a turn's session is known and recorded, before the turn runs: your view of the
    # agent opens then, not when the turn you wanted to watch is already over.
    session_started: Callable[[TaskState], None] = lambda st: None

    def role_for(self, state: State) -> str:
        """Planning is where a wrong decision costs the most and the fewest tokens are spent, so it
        is worth a different model, and sometimes a different tool, from the one that types."""
        return "planner" if state is State.PLAN and self.planner else "writer"

    def harness_of(self, role: str) -> Harness:
        return self.planner if role == "planner" and self.planner else self.harness

    def step(self) -> bool:
        """Does one unit of work. False when there is nothing to do until you act."""
        st = self.task.read_state()
        if st.state is State.CHECKPOINT_PLAN and st.awaiting_plan and not st.paused:
            self._watch_answer()
            return False
        if st.paused or st.state is State.DONE or waits_for_user(st.state):
            return False
        handlers = {State.PLAN: self._plan, State.IMPLEMENT: self._implement, State.VERIFY: self._verify}
        handlers[st.state](st)
        return True

    def run(self, poll: float = 2.0, on_step: Callable[[TaskState], None] = lambda st: None) -> None:
        while self.task.read_state().state is not State.DONE:
            try:
                progressed = self.step()
            except Exception as e:  # keep the task inspectable instead of dying silently
                self.task.set_paused(True, problem=f"stopped on an error: {e}")
                self.task.event("error", message=str(e)[:2000])
                self.notify(self.task.id, f"stopped on an error, see 'vivibox status {self.task.id}': {e}")
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
        if not was and hasattr(harness, "start_session"):
            try:
                was = harness.start_session(title)
            except HarnessError as e:  # the turn makes its own, as before; only watching waits
                self.task.event("session_not_started", error=str(e)[:500])
            else:
                self.task.set_session(role, was)
                self.session_started(self.task.read_state())
        self.task.event("turn_started", state=str(st.state), role=role)
        try:
            turn = harness.turn(prompt, session=was, title=title, on_step=self.task.set_live_turn)
        finally:
            self.task.clear_live_turn()  # the turn event is the record from here on
        if turn.session and turn.session != was:
            self.task.set_session(role, turn.session)
        self.task.event(
            "turn",
            state=str(st.state),
            role=role,
            harness=harness.name,
            metered=getattr(harness, "metered", True),
            ok=turn.ok,
            cost=turn.cost,
            tokens=turn.tokens,
            error=turn.error[:500],
        )
        if not turn.ok:
            self.task.set_paused(True, problem=f"agent turn failed: {turn.error or turn.text}")
            self.notify(self.task.id, f"agent turn failed, task paused: {turn.error[:200]}")
            return None
        clear_next_prompt(self.task)
        return turn

    def _checkpoint(self, target: State, reason: str, kind: str = "") -> None:
        # Every checkpoint first checks risky files: you may open the project in IntelliJ at a checkpoint.
        if self.risky_changes():
            self.task.transition(State.APPROVAL_RISKY, reason=reason, then=str(target))
            self.notify(
                self.task.id, f"{reason}; risky files changed, run 'vivibox risky {self.task.id}' first"
            )
        else:
            self.task.transition(target, reason=reason)
            self.notify(self.task.id, reason, kind=kind)

    def _plan(self, st: TaskState) -> None:
        if (plan := self._your_plan(st)) is not None:
            self._plan_ready(st, plan, yours=True)
            return
        if getattr(self.planner, "manual", False):
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
            gate.check_plan(plan, self.project_verify)
        except (OSError, PlanError, gate.GateError):
            return None
        return plan

    def _plan_ready(self, st: TaskState, plan: Plan, yours: bool = False) -> None:
        if plan.summary:
            self.task.set_goal(plan.summary)
        if st.auto_plan and not self.risky_changes():
            # Through the plan checkpoint, so the event log reads the same as when you accept.
            self.task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
            try:
                accept_plan(
                    self.task,
                    "plan accepted automatically",
                    self.project_verify,
                    self.save_verify,
                    self.project_no_build,
                )
                print(f"[{time.strftime('%H:%M:%S')}] plan accepted automatically", flush=True)
            except gate.GateError as e:
                self.notify(self.task.id, f"plan not accepted automatically ({e}); review it")
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
            gate.check_plan(plan, self.project_verify)
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
            self.notify(
                self.task.id, f"the plan in {answer.name} is not readable yet ({e}); press e to see it"
            )
            return
        count = len(parse_plan(self.task.plan_path.read_text()).criteria)
        self.notify(self.task.id, f"your plan is in, {count} criteria; review and accept it", kind="plan")

    def _implement(self, st: TaskState) -> None:
        if self._turn(st, next_prompt(self.task, IMPLEMENT_PROMPT)) is None:
            return
        if q := question(self.task):
            self._checkpoint(State.CHECKPOINT_BLOCKED, f"question from the agent: {q[:200]}")
            return
        self.task.transition(State.VERIFY)

    def _verify(self, st: TaskState) -> None:
        result = self.run_gate(self.task)
        target = gate.next_state(result, st.iteration, self.max_iterations)
        if target in (State.IMPLEMENT, State.CHECKPOINT_BLOCKED):
            gate.write_feedback(self.task, result)
        if result.environment:
            # No turn of the agent's: it cannot fix this, and a feedback turn would have it try.
            self.task.transition(target, reason="verification could not run")
            self.notify(
                self.task.id,
                f"verification could not run: {result.environment[:150]}; fix it, then press g"
                f" (vivibox verify-again {self.task.id})",
            )
        elif target is State.IMPLEMENT:
            set_next_prompt(self.task, FEEDBACK_PROMPT)
            self.task.transition(target, reason="verification failed")
        elif target is State.CHECKPOINT_BLOCKED:
            self.task.transition(target, reason="verification still failing")
            self.notify(self.task.id, f"verification still failing after {st.iteration} attempts")
        elif target is State.APPROVAL_RISKY:
            self.task.transition(target, reason="verification passed", then=str(State.CHECKPOINT_FINAL))
            self.notify(
                self.task.id, f"done pending your approval of risky files: 'vivibox risky {self.task.id}'"
            )
        else:
            self.task.transition(target, reason="verification passed")
            self.notify(self.task.id, self._review_message(result), kind="review")

    def _review_message(self, result: gate.GateResult | None = None) -> str:
        # Tests that went missing are said here, not to the agent, which would put them back.
        n = len(result.removed_tests) if result else 0
        gone = f"; {n} test{'s' if n != 1 else ''} removed" if n else ""
        try:
            path = self.prepare_review()
        except Exception as e:  # the work is done either way; the review copy is a convenience
            self.task.event("review_prepare_failed", error=str(e)[:500])
            copy = f"the review copy failed ({e}), try: vivibox review {self.task.id}"
            return f"ready for your review{gone}; {copy}"
        if path is None:
            return f"ready for your review{gone}: vivibox review {self.task.id}"
        return f"ready for your review in {path}{gone}"
