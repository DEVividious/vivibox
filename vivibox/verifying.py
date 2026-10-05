"""The verify state of the supervisor: the gate's run and where its result sends the task, the
whole build before the work comes to you, and the check that the commits are the verified ones.
Mixed into supervisor.Supervisor, which it reaches for the rest of the task."""

from __future__ import annotations

from . import feedback, gate, ui
from . import supervisor as _sup
from .prompts import FEEDBACK_PROMPT
from .risky import stop_said
from .states import State
from .task import TaskState


class Verifying:
    def _verify(self, st: TaskState) -> None:
        self.ports.limits_baseline()
        result = self.ports.run_gate(self.task)
        if self._limits_hit():  # the limit's, not the code's: no round, verified again at the start
            return
        target = gate.next_state(result, st.rounds, self.max_rounds)
        if result.passed:
            self.task.set_verified_commit(result.commit)
        if target in (State.IMPLEMENT, State.CHECKPOINT_BLOCKED):
            feedback.write_feedback(self.task, result)
        if (could_not := self._could_not_run(result))[0]:
            # No turn of the agent's: it cannot fix this, and a feedback turn would have it try.
            # Blocked whatever the target was: a feedback file vivibox did not write is found late,
            # after the gate's event, so the state's event carries why for the view.
            self._go(State.CHECKPOINT_BLOCKED, "verification could not run", environment=could_not[0])
            # The risky check out of budget has its own way out: g would scan the same files again.
            again = (
                ""
                if result.risky_stopped
                else f"; fix it, then press g (vivibox verify-again {self.task.id})"
            )
            self.ports.notify(self.task.id, f"verification could not run: {could_not[1]}{again}")
        elif not result.passed and (kept := _sup.held_question(self.task)):
            # You pressed g on the agent's question and the build failed again: the question is
            # still unanswered, so it comes back to you; a feedback turn would have the agent work
            # around what it asked about. No attempt is spent.
            kept.rename(kept.with_name(_sup.QUESTION))
            asked = ((_sup.question(self.task) or "").splitlines() or ["(its question.md is empty)"])[0][:150]
            self._go(State.CHECKPOINT_BLOCKED, "verification still failing; the agent's question stands")
            self.ports.notify(
                self.task.id, f"verification still failing; the agent's question stands: {asked}"
            )
        elif target is State.IMPLEMENT:
            _sup.set_next_prompt(self.task, FEEDBACK_PROMPT)
            why = gate.why_red(result)
            self._go(target, f"verification failed: {why}", why=why)
        elif target is State.CHECKPOINT_BLOCKED:
            self._go(target, f"verification still failing: {gate.why_red(result)}")
            self.ports.notify(
                self.task.id,
                f"verification still failing after {ui.count(st.rounds, 'fix turn')}",
            )
        elif self.mode.supervisor or (self.mode.separate_reviewer and self.reviewer is not None):
            self.last_gate = result
            self._go(State.REVIEW, "verification passed")
        elif not self._whole_build_holds(st):
            return
        elif target is State.APPROVAL_RISKY:
            self._go(target, "verification passed", then=str(State.CHECKPOINT_FINAL))
            self.ports.notify(
                self.task.id, f"done pending your approval of risky files: 'vivibox risky {self.task.id}'"
            )
        else:
            self._go(target, "verification passed")
            self.ports.notify(self.task.id, self._review_message(result), kind="review")

    def _could_not_run(self, result: gate.GateResult) -> tuple[str, str]:
        """Why a verification could not run, whole for the view and briefly for a notification; ""
        twice when it ran. The risky check's stop is said from its own record, whole, never found
        in what a build printed; another problem besides is pointed to, not left out."""
        stop = stop_said(result.risky_stopped, self.task.id)
        whole = "; ".join(filter(None, [stop, result.environment]))
        if not stop:
            return whole, self._briefly(result.environment)
        more = f"; and more, see vivibox status {self.task.id}" if result.environment else ""
        return whole, stop + more

    def _briefly(self, environment: str) -> str:
        """For a notification: the task's own files by their place in it, so the sentence fits;
        cut short, never in a word, it says where the rest is: vivibox status gives it whole."""
        said = environment.replace(f"{self.task.meta}/", "")  # the clone's are said to be its: kept whole
        rest = f" (the rest: vivibox status {self.task.id})"
        cut = said[:150].rsplit(" ", 1)[0] + "…"
        # A line of the build cut inside its frame (display.quoted): closed before vivibox's words.
        return said if len(said) <= 150 else cut + "»" * (cut.count("«") > cut.count("»")) + rest

    def _whole_build_holds(self, st: TaskState) -> bool:
        """The whole build, when the work is about to come to you and it was verified by its modules
        only; red, it goes back to the writer like any failed verification. True to go on."""
        self.ports.limits_baseline()
        result = self.ports.run_whole_gate(self.task)
        if result is not None and self._limits_hit():
            return False
        if result is None or result.passed:
            return True
        feedback.write_feedback(self.task, result)
        target = gate.next_state(result, st.rounds, self.max_rounds)
        why = gate.why_red(result)
        if (could_not := self._could_not_run(result))[0]:
            self._go(State.CHECKPOINT_BLOCKED, "the whole build could not run", environment=could_not[0])
            self.ports.notify(self.task.id, f"the whole build could not run: {could_not[1]}")
        elif target is State.IMPLEMENT:
            _sup.set_next_prompt(self.task, FEEDBACK_PROMPT)
            self._go(target, f"the whole build failed: {why}", why=why)
        else:
            self._go(target, f"the whole build still failing: {why}")
            self.ports.notify(
                self.task.id, f"the whole build still failing after {ui.count(st.rounds, 'fix turn')}"
            )
        return False

    def _head_moved(self, st: TaskState) -> bool:
        """The commits are not the ones the verification ran on (you talked to the agent under w,
        an agent committed after its turn, a stop fell between states): back to the verification,
        for no round of the writer's; what is reviewed and what comes to you is what was verified."""
        head = self.ports.head()
        if not head or not st.verified_commit or head == st.verified_commit:
            return False
        reason = f"commits changed since the verification ({st.verified_commit[:7]} → {head[:7]})"
        self.task.event("unverified_head", verified=st.verified_commit, head=head)
        self._abandon(reason)  # to the verification; to you, the second time in a row
        return True
