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

from . import gate, manual
from .opencode import HarnessError, Turn
from .plan import PlanError, parse_plan, without_notes
from .risky import Change
from .states import State, waits_for_user
from .task import Task, TaskState

NEXT_PROMPT = "next-prompt.md"
QUESTION = "question.md"

PLAN_PROMPT = """Read the goal in /task/plan.md and explore the repository. Then write a plan to
/task/handoff/plan-draft.md: keep the header between the +++ lines of /task/plan.md, set its
summary to one sentence of at most 100 characters naming what the task does, describe the approach,
and list concrete, checkable items under "## Acceptance criteria" as "- [ ]" lines. If the header's
verify list is empty, set it to the command that builds and tests this project once your plan is
carried out, for example verify = ["npm test"]; the gate runs it on a fresh clone of your commits.
Do not change code yet. End your turn when the draft is written."""

IMPLEMENT_PROMPT = """The plan in /task/plan.md is accepted. Implement it, commit your work, and tick
the items in /task/handoff/criteria.md as you verify them. End your turn when everything is done."""

FEEDBACK_PROMPT = """Verification failed. Read /task/handoff/verify-feedback.md (full output in
/task/handoff/verify.log), fix the problems, commit, and end your turn."""

COMMENT_PROMPT = """The user commented on your work. Read the newest entry in /task/handoff/comments.md,
act on it, and end your turn."""

RESUME_PROMPT = """You were interrupted and the task is resuming. Check `git status` and the files in
/task/handoff/, then continue where you left off and end your turn when done."""

PLAN_COMMENT_PROMPT = """The user commented on your plan. Read the newest entry in
/task/handoff/comments.md, update /task/handoff/plan-draft.md, and end your turn."""


class Harness(Protocol):
    # Names the conversation it owns: sessions are kept one per harness, never one per task.
    name: str
    # False when a turn's reported cost is a list price rather than money spent, as it is on a
    # subscription. A total that added the two would be neither.
    metered: bool

    def turn(self, prompt: str, session: str = "", title: str = "") -> Turn: ...


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


def accept_plan(task: Task, reason: str, project_verify=(), save_verify=None) -> None:
    """Freezes the plan and its criteria, and sends the agent on to implementation."""
    plan = gate.accept_plan(task, project_verify)
    if plan.verify and not project_verify and save_verify:
        save_verify(plan.verify)  # the new project now has a command of its own
    set_next_prompt(task, IMPLEMENT_PROMPT)
    task.reset_iterations()
    task.transition(State.IMPLEMENT, reason=reason)


def question(task: Task) -> str | None:
    path = task.meta / "handoff" / QUESTION
    text = path.read_text().strip() if path.exists() else ""
    return text or None


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
    save_verify: Callable[[list[str]], None] = lambda commands: None
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

    def harness_for(self, state: State) -> Harness:
        """Planning is where a wrong decision costs the most and the fewest tokens are spent, so it
        is worth a different model, and sometimes a different tool, from the one that types."""
        return self.planner if state is State.PLAN and self.planner else self.harness

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
                self.task.set_paused(True)
                self.task.event("error", message=str(e)[:2000])
                self.notify(self.task.id, f"stopped on an error, see 'vivibox status {self.task.id}': {e}")
                progressed = False
            on_step(self.task.read_state())
            if not progressed:
                time.sleep(poll)

    # --- states -----------------------------------------------------------------------------

    def _turn(self, st: TaskState, prompt: str, harness: Harness | None = None) -> Turn | None:
        harness = harness or self.harness_for(st.state)
        was = st.sessions.get(harness.name, "")
        title = f"{self.task.id}: {st.goal}"[:80]
        if not was and hasattr(harness, "start_session"):
            try:
                was = harness.start_session(title)
            except HarnessError as e:  # the turn makes its own, as before; only watching waits
                self.task.event("session_not_started", error=str(e)[:500])
            else:
                self.task.set_session(harness.name, was)
                self.session_started(self.task.read_state())
        turn = harness.turn(prompt, session=was, title=title)
        if turn.session and turn.session != was:
            self.task.set_session(harness.name, turn.session)
        self.task.event(
            "turn",
            state=str(st.state),
            harness=harness.name,
            metered=getattr(harness, "metered", True),
            ok=turn.ok,
            cost=turn.cost,
            tokens=turn.tokens,
            error=turn.error[:500],
        )
        if not turn.ok:
            self.task.set_paused(True)
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
        if getattr(self.planner, "manual", False):
            self._plan_manually(st)
            return
        if self._turn(st, next_prompt(self.task, PLAN_PROMPT)) is None:
            return
        if q := question(self.task):
            self._checkpoint(State.CHECKPOINT_PLAN, f"question from the agent: {q[:200]}")
            return
        draft = self.task.meta / "handoff" / "plan-draft.md"
        try:
            plan = parse_plan(draft.read_text())
        except (OSError, PlanError) as e:
            self._checkpoint(
                State.CHECKPOINT_PLAN, f"no valid plan draft ({e}); edit the plan yourself or reply"
            )
            return
        # Without the template's notes: from here the plan is yours to read, not a form to fill.
        self.task.plan_path.write_text(without_notes(draft.read_text()))
        if plan.summary:
            self.task.set_goal(plan.summary)
        if st.auto_plan and not self.risky_changes():
            # Through the plan checkpoint, so the event log reads the same as when you accept.
            self.task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
            try:
                accept_plan(self.task, "plan accepted automatically", self.project_verify, self.save_verify)
                print(f"[{time.strftime('%H:%M:%S')}] plan accepted automatically", flush=True)
            except gate.GateError as e:
                self.notify(self.task.id, f"plan not accepted automatically ({e}); review it")
            return
        self._checkpoint(State.CHECKPOINT_PLAN, "plan ready for review", kind="plan")

    def _plan_manually(self, st: TaskState) -> None:
        """You plan in your own chat. A chat in a browser cannot see the repository, so the writer
        first reports on it, for cents; a new project has nothing to report."""
        context = self.task.meta / "handoff" / manual.CONTEXT
        known = context.exists() or manual.repository_is_empty(self.task.repo)
        if not known and self._turn(st, manual.RECON_PROMPT, self.harness) is None:
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
        if target is State.IMPLEMENT:
            set_next_prompt(self.task, FEEDBACK_PROMPT)
            self.task.transition(target, reason="verification failed")
        elif target is State.CHECKPOINT_BLOCKED:
            self.task.transition(target, reason="verification still failing")
            self.notify(self.task.id, f"verification still failing after {st.iteration} iterations")
        elif target is State.APPROVAL_RISKY:
            self.task.transition(target, reason="verification passed", then=str(State.CHECKPOINT_FINAL))
            self.notify(
                self.task.id, f"done pending your approval of risky files: 'vivibox risky {self.task.id}'"
            )
        else:
            self.task.transition(target, reason="verification passed")
            self.notify(self.task.id, self._review_message(), kind="review")

    def _review_message(self) -> str:
        try:
            path = self.prepare_review()
        except Exception as e:  # the work is done either way; the review copy is a convenience
            self.task.event("review_prepare_failed", error=str(e)[:500])
            return f"ready for your review; the review copy failed ({e}), try: vivibox review {self.task.id}"
        if path is None:
            return f"ready for your review: vivibox review {self.task.id}"
        return f"ready for your review in {path}"
