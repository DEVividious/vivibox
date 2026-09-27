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

from . import brief, feedback, gate, manual, orchestration, proposal, reviewing, ui
from .config import DEFAULT_VERIFY_TIMEOUT, Project
from .harness import Harness, HarnessError, Turn
from .plan import Plan, PlanError, parse_plan, without_notes
from .prompts import (
    FEEDBACK_PROMPT,
    IMPLEMENT_PROMPT,
    PLAN_PROMPT,
    PLAN_REPAIR_PROMPT,
    PREPARED_PREFIX,
    PROPOSE_PREFIX,
    REVIEW_AGAIN_PREFIX,
    REVIEW_FIX_PROMPT,
    REVIEW_PROMPT,
    REVIEW_REPAIR_PROMPT,
    SELF_REVIEW_PROMPT,
)
from .risky import Change
from .states import State, waits_for_user
from .task import Task, TaskState

NEXT_PROMPT = "next-prompt.md"
QUESTION = "question.md"


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
    task.reset_rounds()
    task.transition(State.IMPLEMENT, reason=reason)


def accept_command(task: Task, command: str, reason: str) -> None:
    """The command is the project's now (the caller kept it); the verification runs with it."""
    task.transition(State.VERIFY, reason=reason, command=command)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _notes(n: int) -> str:
    return f"{n} blocking note{'s' if n != 1 else ''}"


def question(task: Task) -> str | None:
    path = task.meta / "handoff" / QUESTION
    text = path.read_text().strip() if path.exists() else ""
    return text or None


def put_question_away(task: Task) -> Path | None:
    """A question that is answered or overtaken: kept for the record, out of the supervisor's way.
    Returns where it went, for a verification that may find it unanswered after all."""
    path = task.meta / "handoff" / QUESTION
    if not path.exists():
        return None
    kept = path.with_name(f"question-answered-{time.strftime('%Y%m%d-%H%M%S')}.md")
    path.rename(kept)
    return kept


def held_question(task: Task) -> Path | None:
    """The question you answered with g, as this verification recorded it when it started: back
    to the agent's place if the build fails again, because nothing answered it yet."""
    entered = next((e for e in reversed(task.events()) if e["type"] == "state"), None)
    if not entered or entered["data"].get("current") != str(State.VERIFY):
        return None
    name = entered["data"].get("question")
    kept = task.meta / "handoff" / name if name else None
    return kept if kept and kept.exists() else None


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
    # Keeps the command the writer proposed for the project, when --auto takes it without you.
    keep_command: Callable[[str], None] = lambda command: None
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
    # The commit the task's clone stands on, for the check that what is reviewed and what comes to
    # you is what the verification ran on; "" when nobody can say (a test), and nothing is checked.
    head: Callable[[], str] = lambda: ""


@dataclass
class Supervisor:
    task: Task
    harness: Harness
    ports: Ports
    # Fix turns the writer gets, from the gate or from the review, before the work comes to you.
    max_rounds: int
    # How the task is shared between the roles; the agents below play what it says.
    mode: orchestration.Mode = orchestration.DEFAULT
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
    # The reviewer of its own, in the review container, where the mode has one.
    reviewer: Harness | None = None
    # The gate's result the review followed, for the message that ends the task's work.
    last_gate: gate.GateResult | None = None
    # The seconds the verification may take (limits.verify_timeout, or the project's own): what
    # the writer is told to allow its own whole build, so the two are one number.
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT

    def role_for(self, state: State) -> str:
        return {State.PLAN: "planner", State.REVIEW: "reviewer"}.get(state, "writer")

    def agent_of(self, role: str) -> str:
        """The agent that plays a role in this mode: its conversation and its model. Planning is
        where a wrong decision costs the most and the fewest tokens are spent, so it is worth a
        different model, and sometimes a different tool, from the one that types; a caller with
        one harness and no planner has the writer plan."""
        agent = self.mode.agents[role]
        return "writer" if agent == "planner" and self.planner is None else agent

    def harness_of(self, agent: str) -> Harness:
        if agent == "reviewer" and self.reviewer:
            return self.reviewer
        return self.planner if agent == "planner" and self.planner else self.harness

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

    def _turn(self, st: TaskState, prompt: str, role: str = "", kind: str = "") -> Turn | None:
        """kind: what the turn is besides the state's own work ("self-review"), for the record."""
        role = role or self.role_for(st.state)
        agent = self.agent_of(role)
        harness = self.harness_of(agent)
        # The session as it is now, not as the state given had it: a second turn of the same
        # step (the plan's repair, the review's) would not see the one the first turn made, and
        # would make another, briefed again, doing the first's work over.
        was = self.task.read_state().sessions.get(agent, "")
        if not was:
            # The first message of an agent's conversation says what it is and owns: its role, or
            # the roles it plays in one conversation.
            roles = self.mode.brief_of(agent) if agent == self.mode.agents[role] else agent
            prompt = f"{brief.role_text(roles)}\n{prompt}"
        title = f"{self.task.id}: {st.goal}"[:80]
        if not was:
            try:
                was = harness.start_session(title)
            except HarnessError as e:  # the turn makes its own, as before; only watching waits
                self.task.event("session_not_started", error=str(e)[:500])
            if was:  # a tool that keeps no session ahead of the turn gives ""
                self.task.set_session(agent, was)
                self.ports.session_started(self.task.read_state())
        turn = self._attempts(harness, prompt, was, title, st, role)
        if turn.session and turn.session != was:
            self.task.set_session(agent, turn.session)
        self.task.event(
            "turn",
            state=str(st.state),
            role=role,
            agent=agent,
            harness=harness.name,
            metered=harness.metered,
            ok=turn.ok,
            cost=turn.cost,
            tokens=turn.tokens,
            error=turn.error[:500],
            **({"kind": kind} if kind else {}),
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

    def _go(self, target: State, reason: str, **data) -> TaskState:
        """Every change of state, with why, on the record and in the supervisor's window: the log
        used to say only some, and a used-up round had to be worked out from the timeline."""
        st = self.task.transition(target, reason=reason, **data)
        print(f"[{time.strftime('%H:%M:%S')}] → {target}: {reason}", flush=True)
        return st

    def _checkpoint(self, target: State, reason: str, kind: str = "") -> None:
        # Every checkpoint first checks risky files: you may open the project in IntelliJ at a checkpoint.
        if self.ports.risky_changes():
            self._go(State.APPROVAL_RISKY, reason, then=str(target))
            self.ports.notify(
                self.task.id, f"{reason}; risky files changed, run 'vivibox risky {self.task.id}' first"
            )
        else:
            self._go(target, reason)
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
            self._go(State.CHECKPOINT_PLAN, "plan ready")
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
            prompt = PROPOSE_PREFIX.format(minutes=max(1, round(self.verify_timeout / 60))) + prompt
        # A self-review resumed after a stop is one on the record too.
        resumed = prompt == self._self_review_prompt(st)
        if self._turn(st, prompt, kind="self-review" if resumed else "") is None:
            return
        if q := question(self.task):
            self._checkpoint(State.CHECKPOINT_BLOCKED, f"question from the agent: {q[:200]}")
            return
        if self.mode.self_review and not resumed:
            # The writer reads its own work before the gate does, in the same conversation. The
            # prompt is kept first, so a stop in between resumes here, not with the work over.
            set_next_prompt(self.task, self._self_review_prompt(st))
            if self._turn(st, self._self_review_prompt(st), kind="self-review") is None:
                return
            if q := question(self.task):
                self._checkpoint(State.CHECKPOINT_BLOCKED, f"question from the agent: {q[:200]}")
                return
        if self._asks_for_command():
            self._command_checkpoint(st)
            return
        self._go(State.VERIFY, "the writer's turn is over")

    def _self_review_prompt(self, st: TaskState) -> str:
        return SELF_REVIEW_PROMPT.format(base=st.base_commit)

    def _command_checkpoint(self, st: TaskState) -> None:
        """The command the writer proposed is what the task is about to be verified with: yours to
        keep for the project, change or send back, before the gate runs. --auto keeps a whole one
        itself; a missing or narrowed one still waits for you."""
        command = proposal.proposed(self.task)
        selection = gate.narrowed_proposal(command)
        if st.auto_plan and command and not selection and not self.ports.risky_changes():
            self.ports.keep_command(command)
            accept_command(self.task, command, "command kept automatically")
            return
        if not command:
            reason = (
                f"no command came from the writer: it wrote none to /task/handoff/{proposal.PROPOSAL};"
                " type one, or send it back"
            )
        elif selection:
            reason = f"command proposed: `{command}`, narrowed to {selection}; change it, or send it back"
        else:
            reason = f"command proposed: `{command}`; keep it for the project, change it, or send it back"
        self._checkpoint(State.CHECKPOINT_COMMAND, reason, kind="command")

    def _asks_for_command(self) -> bool:
        """The project has no command, and neither it nor the task says there is nothing to build."""
        nothing = self.project_no_build or proposal.nothing_to_build(self.task)
        return not self.project_verify and not nothing

    def _verify(self, st: TaskState) -> None:
        result = self.ports.run_gate(self.task)
        target = gate.next_state(result, st.rounds, self.max_rounds)
        if result.passed:
            self.task.set_verified_commit(result.commit)
        if target in (State.IMPLEMENT, State.CHECKPOINT_BLOCKED):
            feedback.write_feedback(self.task, result)
        if result.environment:
            # No turn of the agent's: it cannot fix this, and a feedback turn would have it try.
            self._go(target, "verification could not run")
            self.ports.notify(
                self.task.id,
                f"verification could not run: {result.environment[:150]}; fix it, then press g"
                f" (vivibox verify-again {self.task.id})",
            )
        elif not result.passed and (kept := held_question(self.task)):
            # You pressed g on the agent's question and the build failed again: the question is
            # still unanswered, so it comes back to you; a feedback turn would have the agent work
            # around what it asked about. No attempt is spent.
            kept.rename(kept.with_name(QUESTION))
            asked = (question(self.task) or "").splitlines()[0][:150]
            self._go(State.CHECKPOINT_BLOCKED, "verification still failing; the agent's question stands")
            self.ports.notify(
                self.task.id, f"verification still failing; the agent's question stands: {asked}"
            )
        elif target is State.IMPLEMENT:
            set_next_prompt(self.task, FEEDBACK_PROMPT)
            why = gate.why_red(result)
            self._go(target, f"verification failed: {why}", why=why)
        elif target is State.CHECKPOINT_BLOCKED:
            self._go(target, f"verification still failing: {gate.why_red(result)}")
            self.ports.notify(
                self.task.id,
                f"verification still failing after {st.rounds} fix turn{'s' if st.rounds != 1 else ''}",
            )
        elif self.mode.supervisor or (self.mode.separate_reviewer and self.reviewer is not None):
            self.last_gate = result
            self._go(State.REVIEW, "verification passed")
        elif target is State.APPROVAL_RISKY:
            self._go(target, "verification passed", then=str(State.CHECKPOINT_FINAL))
            self.ports.notify(
                self.task.id, f"done pending your approval of risky files: 'vivibox risky {self.task.id}'"
            )
        else:
            self._go(target, "verification passed")
            self.ports.notify(self.task.id, self._review_message(result), kind="review")

    def _head_moved(self, st: TaskState) -> bool:
        """The commits are not the ones the verification ran on (you talked to the agent under w,
        an agent committed after its turn, a stop fell between states): back to the verification,
        for no round of the writer's; what is reviewed and what comes to you is what was verified."""
        head = self.ports.head()
        if not head or not st.verified_commit or head == st.verified_commit:
            return False
        reason = f"commits changed since the verification ({st.verified_commit[:7]} → {head[:7]})"
        self.task.event("unverified_head", verified=st.verified_commit, head=head)
        self._go(State.VERIFY, reason)
        return True

    def _review(self, st: TaskState) -> None:
        """A round of the reviewer's after a green gate, kept as handoff/review-N.md: in the review
        container a fresh clone and a fresh conversation, or the supervisor in the pod, on the
        worker's clone, in the conversation it planned in. Blocking notes go back to the writer
        while it has rounds; then the work comes to you, with the notes."""
        if self._head_moved(st):
            return
        n = st.reviews + 1
        if self.mode.supervisor:
            out = self.task.meta / "review"
            out.mkdir(exist_ok=True)
            (out / "review.md").unlink(missing_ok=True)  # the last round's, or the supervisor reads it
            text = self._review_turns(st, n, out)
        else:
            out = self.ports.review_up()
            try:
                self.task.set_session("reviewer", "")  # the last round's server is gone with its container
                text = self._review_turns(st, n, out)
            finally:
                self.ports.review_down()
        if text is None:
            return
        reviewing.keep(self.task, n, text)
        self.task.set_reviews(n)
        problem = reviewing.problem(text)
        review = reviewing.parse_review(text) if not problem else reviewing.Review()
        blocking, others = len(review.blocking), len(review.not_blocking)
        self.task.event("review", round=n, blocking=blocking, not_blocking=others, problem=problem)
        if problem:
            said = f"review {n} unreadable ({problem})"
        elif blocking:
            said = f"review {n}: {_notes(blocking)}, {others} not blocking"
        else:
            said = f"review {n}: no blocking notes, {others} not blocking"
        if blocking and st.rounds < self.max_rounds:
            set_next_prompt(self.task, REVIEW_FIX_PROMPT)
            self._go(State.IMPLEMENT, f"review {n}: {blocking} blocking", why=_notes(blocking))
            print(f"[{time.strftime('%H:%M:%S')}] {said}, back to the writer", flush=True)
            return
        if self._head_moved(self.task.read_state()):
            return
        self._checkpoint(
            State.CHECKPOINT_FINAL, f"{self._review_message(self.last_gate)}; {said}", kind="review"
        )

    def _review_turns(self, st: TaskState, n: int, out: Path) -> str | None:
        """The reviewer's turn, and one more when what it wrote is not a review; None when a turn
        failed and the task stopped."""
        st = self.task.read_state()
        prompt = REVIEW_PROMPT.format(base=st.base_commit)
        if n > 1 and (self.task.meta / "handoff" / f"review-{n - 1}-reply.md").exists():
            prompt = REVIEW_AGAIN_PREFIX + prompt
        if self._turn(st, prompt, role="reviewer") is None:
            return None
        text = _read(out / "review.md")
        if problem := reviewing.problem(text):
            if self._turn(st, REVIEW_REPAIR_PROMPT.format(problem=problem), role="reviewer") is None:
                return None
            text = _read(out / "review.md")
        return text

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
