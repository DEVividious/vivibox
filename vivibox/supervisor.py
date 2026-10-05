"""Supervisor: drives one task through its states, one agent turn at a time, and stops at checkpoints.

It runs as a background process of its own, logging to .task/log/supervisor.log. Decisions at
checkpoints are yours (vivibox accept / reply); between them the supervisor sends the agent its next
prompt, runs the gate and records every step in the task's event log.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import brief, display, gate, handoff, limits, orchestration, proposal, transcript, ui
from . import init as project_init
from .config import DEFAULT_VERIFY_TIMEOUT, Project, by_modules
from .harness import Harness, HarnessError, Turn
from .plan import PlanError, parse_plan
from .planning import Planning
from .prompts import (
    IMPLEMENT_PROMPT,
    PREPARED_PREFIX,
    PROPOSE_FOUND,
    PROPOSE_PREFIX,
    SCOPED_PREFIX,
    SELF_REVIEW_PROMPT,
)
from .review_round import ReviewRound
from .risky import Change
from .states import State, waits_for_user
from .task import CURRENT, Task, TaskState, layout
from .verifying import Verifying

NEXT_PROMPT = "next-prompt.md"
QUESTION = "question.md"
# The words are vivibox's when it did not read the file: never shown as the agent's question.
QUESTION_NOT_READ = "vivibox did not read the agent's question"


@dataclass(frozen=True)
class Action:
    """A button in a desktop notification and the command it runs."""

    name: str
    label: str
    command: list[str]
    # For programs that keep running, like an IDE: started and left alone.
    detach: bool = False


def notify(task_id: str, message: str, desktop: bool = True, actions: Sequence[Action] = ()) -> None:
    """Always in the supervisor window; on the desktop too unless notifications.desktop is false.
    What the agent wrote in it reaches neither as anything but text: no control character to the
    terminal, no markup to the desktop's notifications, which read <b> and &amp; in the body."""
    message = display.terminal(message)
    # A next line indented under the time: never a [hh:mm:ss] line the message draws of its own.
    print(f"[{time.strftime('%H:%M:%S')}] {message}".replace("\n", "\n    "), flush=True)
    if not desktop or not shutil.which("notify-send"):
        return
    command = ["notify-send", "--app-name=vivibox", f"vivibox {task_id}", html.escape(message, quote=False)]
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


def _notes(n: int) -> str:
    return ui.count(n, "blocking note")


# Characters of question.md that are read: a question is a few lines, and this is asked often.
QUESTION_LIMIT = 1 << 16


def read_question(task: Task) -> tuple[str, str]:
    """The agent's question and "", or "" and why vivibox did not read question.md: a link or a
    special file in its place, which stops the task for you as a question does. "" twice for
    none: no file, or nothing to see in what is read of it. The one answer, for the supervisor,
    the list, the status and the panel; bytes that are no text are shown as their codes."""
    path = task.meta / "handoff" / QUESTION
    try:
        text, more = handoff.read_start(path, QUESTION_LIMIT) if path.exists() else ("", False)
    except handoff.HandoffError as e:
        return "", e.why
    cut = f"\n\n(question.md goes on: vivibox reads its first {QUESTION_LIMIT} characters)"
    return (text.strip() + cut * more, "") if display.seen(text) else ("", "")


def question(task: Task) -> str | None:
    """The agent's question; for a question.md vivibox did not read, why."""
    text, why = read_question(task)
    return text or (f"{QUESTION_NOT_READ}: {why}" if why else None)


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
# What --auto is told of the clone by a caller that wired no check of it (Ports.clone_refused).
NO_CLONE_CHECK = "nothing here checks the task's clone"
# Why --auto decided nothing, when the clone is as recorded: yours to approve first.
RISKY_CHANGED = "risky files changed"
# Why a task went to its verification with a command --auto took from the writer, which the
# project keeps only once that verification has passed (the state's event: command, to_keep).
TO_KEEP_REASON = "the writer's command is verified before the project keeps it"
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

    # run_gate(task), or run_gate(task, command) for a verification with the command --auto took
    # from the writer, before the project keeps it: that command, whatever the handoff says by then.
    run_gate: Callable[..., gate.GateResult]
    risky_changes: Callable[[], list[Change]]
    # Why vivibox does not take the task's clone as it is now, "" when it is as recorded: the check
    # the gate starts with (repo.check_protection), which reads files and runs no git. Asked
    # before --auto decides anything. A caller that wires none gets a refusal, never a pass, and
    # what it raises is no answer: the decision is not taken.
    clone_refused: Callable[[], str] = lambda: NO_CLONE_CHECK
    # notify(task_id, message, kind): kind "plan" or "review" lets the caller add notification buttons.
    notify: Callable[..., None] = lambda task_id, message, kind="": notify(task_id, message)
    # Fetches the work into your repository and updates the review copy; returns the copy's path.
    prepare_review: Callable[[], Path | None] = lambda: None
    # Keeps the command the writer proposed for the project, when --auto takes it without you:
    # called once the gate has passed the work with it (verifying.Verifying._keep), never before.
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
    # The whole build, once, before the work comes to you (limits.whole_build_before_review), for a
    # task verified by its modules; None when there is nothing to run.
    run_whole_gate: Callable[[Task], gate.GateResult | None] = lambda task: None
    # What the pod hit of its limits (limits.py): the counts read as a turn or a verification
    # begins, and what passed them when it ends; nothing, for a caller without a pod.
    limits_baseline: Callable[[], None] = lambda: None
    limits_exceeded: Callable[[], list[limits.Exceeded]] = lambda: []


@dataclass
class Supervisor(Planning, Verifying, ReviewRound):
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
        if layout(st) != CURRENT:  # no longer one this vivibox runs: no turn, and nothing written
            return False
        if st.state is State.CHECKPOINT_PLAN and st.awaiting_plan and not st.paused:
            self._watch_answer()
            return False
        if st.state is State.REVIEW and st.awaiting_review and not st.paused:
            return self._watch_cli_review(st)
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
        asked, began = prompt, time.monotonic()
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
        self.ports.limits_baseline()  # the kernel's counts start again with a container
        turn = self._attempts(harness, prompt, was, title, st, role)
        transcript.write(self.task, role, agent, str(st.state), kind, asked, turn, time.monotonic() - began)
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
        if self._limits_hit():  # before a failure is read as the agent's: it was the limit
            return None
        if not turn.ok:
            self.task.set_paused(True, problem=f"agent turn failed: {turn.error or turn.text}")
            self.ports.notify(self.task.id, f"agent turn failed, task paused: {turn.error[:200]}")
            return None
        clear_next_prompt(self.task)
        return turn

    def _limits_hit(self) -> bool:
        """What the pod hit of its limits, on the record as the plan names it; the task then
        stops for you where it is, with what happened, the kernel's words and the command that
        raises the limit. What cannot be read where it should be raises, to the catch-all."""
        found = self.ports.limits_exceeded()
        for hit in found:
            self.task.event("resource.limit.exceeded", **limits.event_data(hit))
        if not found:
            return False
        said = limits.problem(self.task.id, found)
        self.task.set_paused(True, problem=said)
        self.ports.notify(self.task.id, said)
        return True

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

    def _not_by_itself(self) -> str:
        """Why --auto decides nothing on the clone as it is now, "" when it may: the clone is not
        as recorded (the check the gate starts with, which runs no git), or risky files changed
        (RISKY_CHANGED), which are yours to approve first. The clone is checked before its risky
        files are read, so that they are read in a clone vivibox takes, and again after, so that
        the last look before a decision is at the clone as it is then."""
        if why := self.ports.clone_refused():
            return why
        if self.ports.risky_changes():
            return RISKY_CHANGED
        return self.ports.clone_refused()

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

    def _asks(self) -> str:
        """Why the task stops for the agent's question, "" when it asked none: the question, or
        vivibox's own reason for a question.md it did not read, with the key that moves it away."""
        text, why = read_question(self.task)
        if why:
            return f"{QUESTION_NOT_READ}: {why}; press r to answer (vivibox reply {self.task.id})"
        return f"question from the agent: {text[:200]}" if text else ""

    @property
    def by_module(self) -> bool:
        """The project's command has {modules}: a draft names its modules, and the writer's first
        turn is told what verifies it."""
        return any(by_modules(command) for command in self.project_verify)

    def _planned_modules(self) -> list[str]:
        if not self.by_module:
            return []
        try:
            return parse_plan((self.task.meta / gate.ACCEPTED_PLAN).read_text()).modules
        except (OSError, PlanError):
            return []

    def _implement(self, st: TaskState) -> None:
        if self.ports.preparing():
            self.ports.sleep(PREPARE_POLL)
            return
        prompt = next_prompt(self.task, IMPLEMENT_PROMPT)
        if self.prepared and prompt.endswith(IMPLEMENT_PROMPT):
            prompt = PREPARED_PREFIX.format(commands=", ".join(f"`{c}`" for c in self.prepared)) + prompt
        if prompt.endswith(IMPLEMENT_PROMPT) and (modules := self._planned_modules()):
            command = "` && `".join(proposal.for_modules(self.project_verify, modules))
            prompt = SCOPED_PREFIX.format(modules=", ".join(modules), command=command) + prompt
        if prompt.endswith(IMPLEMENT_PROMPT) and self._asks_for_command():
            # What the build files and the pipeline name, as init shows it: the writer, who never
            # saw init's notes, proposed a plainer command than the project's own.
            named = project_init.candidates(self.task.repo) if self.task.repo.is_dir() else []
            if named:
                listed = "; ".join(f"`{command}` ({source})" for command, source in named)
                prompt = PROPOSE_FOUND.format(commands=listed) + prompt
            prompt = PROPOSE_PREFIX.format(minutes=max(1, round(self.verify_timeout / 60))) + prompt
        # A self-review resumed after a stop is one on the record too.
        resumed = prompt == self._self_review_prompt(st)
        if self._turn(st, prompt, kind="self-review" if resumed else "") is None:
            return
        if q := self._asks():
            self._checkpoint(State.CHECKPOINT_BLOCKED, q)
            return
        if self.mode.self_review and not resumed:
            # The writer reads its own work before the gate does, in the same conversation. The
            # prompt is kept first, so a stop in between resumes here, not with the work over.
            set_next_prompt(self.task, self._self_review_prompt(st))
            if self._turn(st, self._self_review_prompt(st), kind="self-review") is None:
                return
            if q := self._asks():
                self._checkpoint(State.CHECKPOINT_BLOCKED, q)
                return
        if self._asks_for_command():
            self._command_checkpoint(st)
            return
        self._go(State.VERIFY, "the writer's turn is over")

    def _self_review_prompt(self, st: TaskState) -> str:
        return SELF_REVIEW_PROMPT.format(base=st.base_commit)

    def _command_checkpoint(self, st: TaskState) -> None:
        """The command the writer proposed is what the task is about to be verified with: yours to
        keep for the project, change or send back, before the gate runs. --auto takes a whole one
        to the verification itself, as it read it here, and writes nothing for the project yet:
        the project keeps it once the gate has passed the work with it (verifying.Verifying._keep).
        A missing or narrowed one, and any while risky files wait for you, still waits for you."""
        command = proposal.proposed(self.task)
        selection = gate.narrowed_proposal(command)
        if st.auto_plan and command and not selection and not self.ports.risky_changes():
            self._go(State.VERIFY, TO_KEEP_REASON, command=command, to_keep=True)
            return
        if not command:
            none = f"it wrote none to /task/handoff/{proposal.PROPOSAL}"
            why = handoff.refused(self.task.meta / "handoff", proposal.PROPOSAL) or none
            reason = f"no command came from the writer: {why}; type one, or send it back"
        elif selection:
            reason = (
                f"command proposed: `{command}`, {gate.proposal_fault(selection)}; change it, or send it back"
            )
        else:
            reason = (
                f"command proposed: `{command}`; keep it for every task of {st.project}, change it,"
                " or send it back"
            )
        self._checkpoint(State.CHECKPOINT_COMMAND, reason, kind="command")

    def _asks_for_command(self) -> bool:
        """The project has no command, and neither it nor the task says there is nothing to build."""
        nothing = self.project_no_build or proposal.nothing_to_build(self.task)
        return not self.project_verify and not nothing
