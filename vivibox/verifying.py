"""The verify state of the supervisor: the gate's run and where its result sends the task, the
whole build before the work comes to you, and the check that the commits are the verified ones.
Mixed into supervisor.Supervisor, which it reaches for the rest of the task."""

from __future__ import annotations

from . import feedback, gate, repo, ui
from . import supervisor as _sup
from .prompts import FEEDBACK_PROMPT
from .risky import stop_said
from .states import State
from .task import TaskState

# What --auto did with the command the writer proposed, on the task's record.
KEPT, NOT_KEPT = "command_kept", "command_not_kept"
# Why a command was not kept that the clone's own refusal does not say: after these, what you
# do is set the command yourself.
NOT_RUN, NO_HEAD = "the verification did not run it", "vivibox could not tell which commit the clone is on"
MOVED = "the commits changed since the verification passed"
SET_IT = (NOT_RUN, NO_HEAD, MOVED)  # and risky files changed (supervisor.RISKY_CHANGED)


def command_to_keep(events: list[dict]) -> str:
    """The command --auto took from the writer for the verification under way, which the project
    keeps once the gate has passed with it; "" when there is none. It is the one the task went
    to its verification with (supervisor._command_checkpoint), as the task's event log has it,
    which the host alone writes and the pod mounts read-only: never the handoff file again, which
    the writer's process can change after it was read. It stands through a verification that
    could not run and your "verify again", and a review given up for the verification; it ends
    when the project keeps it, and when the writer has a turn, after which it proposes again."""
    through = {str(State.VERIFY), str(State.CHECKPOINT_BLOCKED), str(State.REVIEW)}
    for event in reversed(events):
        data = event["data"]
        if event["type"] == KEPT:
            return ""
        if event["type"] != "state":
            continue
        if data.get("to_keep"):
            return str(data.get("command") or "")
        if not {data.get("previous"), data.get("current")} <= through:
            return ""  # a turn of the writer's since, or a decision of yours on the command
    return ""


class Verifying:
    def _verify(self, st: TaskState) -> None:
        self.ports.limits_baseline()
        taken = command_to_keep(self.task.events()) if st.auto_plan else ""
        result = self.ports.run_gate(self.task, taken) if taken else self.ports.run_gate(self.task)
        if self._limits_hit():  # the limit's, not the code's: no round, verified again at the start
            return
        target = gate.next_state(result, st.rounds, self.max_rounds)
        if result.passed:
            self.task.set_verified_commit(result.commit)
            if taken:
                self._keep(st, taken, result)
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

    def _keep(self, st: TaskState, command: str, result: gate.GateResult) -> None:
        """--auto: the project keeps the command the writer proposed, now that the gate has passed
        the work with it, and only while what the gate verified is still what is there
        (_not_kept). Kept or not, it is on the task's record, and said unless the project has
        your own command by now; not kept, the task goes on, verified, and the project's file
        is as it was."""
        if why := self._not_kept(st, command, result):
            self.task.event(NOT_KEPT, command=command, why=why)
            if why != self._own(st):
                # One step: vivibox's own reasons end in it; the clone's words carry theirs.
                own = why in SET_IT or why == _sup.RISKY_CHANGED
                step = "; set it under e on the project" if own else ""
                if why.startswith(repo.BLOCKED_BY):  # a fact with no step of its own: the task goes on to you
                    step = "; reply, and the writer takes it out"
                said = f"the verification passed with `{command}`, which {st.project} does not keep"
                self.ports.notify(self.task.id, f"{said}: {why}{step}")
            return
        if self.ports.keep_command(command) is False:  # yours, set since the look above: as it is
            self.task.event(NOT_KEPT, command=command, why=self._own(st))
            return
        self.task.event(KEPT, command=command, commit=result.commit)
        self.ports.notify(
            self.task.id, f"the verification passed with `{command}`: kept for every task of {st.project}"
        )

    def _own(self, st: TaskState) -> str:
        return f"{st.project} has a command of its own now"

    def _not_kept(self, st: TaskState, command: str, result: gate.GateResult) -> str:
        """Why the project does not keep the command after a verification that passed, "" when
        it does: the project has a command by now, which is yours; the gate did not run it; the
        gate found risky changes, or the clone is no longer one --auto decides on
        (_not_by_itself); the clone is not on the commit the gate verified. Asked in this order,
        the commit last: nothing is read of the clone between that answer and the write."""
        if self.current_project:  # the project's file as it is now, not as the step began
            project = self.current_project()
            self.project_verify, self.project_no_build = project.verify, project.no_build
        if self.project_verify or self.project_no_build:
            return self._own(st)
        if not any(c.ok and c.command == command for c in result.commands):
            return NOT_RUN
        if result.risky:
            return _sup.RISKY_CHANGED
        if why := self._not_by_itself():
            return why
        if not (head := self.ports.head()):
            return NO_HEAD
        return "" if head == result.commit else MOVED

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
