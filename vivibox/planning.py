"""The plan state of the supervisor: the planner's turn and its one repair, a plan you finished
yourself, one you write in your own chat, and what a ready plan waits for. Mixed into
supervisor.Supervisor, which it reaches for the rest of the task."""

from __future__ import annotations

import time

from . import gate, handoff, manual, proposal
from . import supervisor as _sup
from .plan import Plan, PlanError, parse_plan, without_notes
from .prompts import PLAN_PROMPT, PLAN_REPAIR_PROMPT
from .states import State
from .task import TaskState


class Planning:
    def _plan(self, st: TaskState) -> None:
        if (plan := self._your_plan(st)) is not None:
            self._plan_ready(st, plan, yours=True)
            return
        if self.planner is not None and self.planner.manual:
            self._plan_manually(st)
            return
        if self._turn(st, _sup.next_prompt(self.task, PLAN_PROMPT)) is None:
            return
        if q := self._asks():
            self._checkpoint(State.CHECKPOINT_PLAN, q)
            return
        text, plan, problem = self._read_draft()
        if problem:
            # One turn to fix what acceptance would refuse anyway (the placeholder left in, no
            # criteria, no verify command): cheaper than your reply, and once, not a loop. What
            # was wrong goes with the turn, so the timeline says why the planner took a second.
            repair = f"plan repair: {problem[:120]}"
            if self._turn(st, PLAN_REPAIR_PROMPT.format(problem=problem), kind=repair) is None:
                return
            if q := self._asks():
                self._checkpoint(State.CHECKPOINT_PLAN, q)
                return
            text, plan, problem = self._read_draft()
        if problem:
            # The planner's draft, when it is a plan at all, is what you fix under e: one line short
            # of acceptable is not a reason to start again from the template.
            try:
                parse_plan(text)
                self.task.plan_path.write_text(without_notes(text))
            except (OSError, PlanError):
                pass
            self._checkpoint(
                State.CHECKPOINT_PLAN, f"no valid plan draft ({problem}); edit the plan yourself or reply"
            )
            return
        # Without the template's notes: from here the plan is yours to read, not a form to fill.
        self.task.plan_path.write_text(without_notes(text))
        self._plan_ready(st, plan)

    def _your_plan(self, st: TaskState) -> Plan | None:
        """The plan as you left it, when it is finished (--draft, then e) and this is the task's first
        planning: acceptance would take it as it is, so no planner, yours or an agent, is asked to
        write it again. A plan still carrying the template's placeholder is a starting point for the
        planner. After your reply, or a stop mid-planning, the next prompt is set, and the planner
        goes on."""
        draft = self.task.meta / "handoff" / "plan-draft.md"
        if (self.task.meta / _sup.NEXT_PROMPT).exists() or draft.exists():
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
                _sup.accept_plan(self.task, "plan accepted automatically")
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
        """The draft's text, read once, and the draft as a plan, or what keeps it from being one:
        unreadable (a link or a special file is not read: its text is ""), or one acceptance would
        refuse."""
        text = ""
        try:
            text = handoff.read_file(self.task.meta / "handoff" / "plan-draft.md")
            plan = parse_plan(text)
            gate.check_plan(plan)
        except (OSError, PlanError, gate.GateError) as e:
            return text, None, str(e)
        if problem := proposal.modules_missing(plan, self.by_module):
            return text, None, problem
        return text, plan, ""

    def _plan_manually(self, st: TaskState) -> None:
        """You plan in your own chat. A chat in a browser cannot see the repository, so the writer
        first reports on it, for cents; a new project has nothing to report, and an agent's CLI
        reads your checkout itself."""
        context = self.task.meta / "handoff" / manual.CONTEXT
        known = st.plan_in_cli or context.exists() or manual.repository_is_empty(self.task.repo)
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
