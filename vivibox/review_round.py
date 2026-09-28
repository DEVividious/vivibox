"""The review state of the supervisor: a round of the reviewer's after a green gate, and what its
notes decide. Mixed into supervisor.Supervisor, which it reaches for the rest of the task."""

from __future__ import annotations

import time
from pathlib import Path

from . import gate, reviewing, ui
from . import supervisor as _sup
from .prompts import REVIEW_AGAIN_PREFIX, REVIEW_FIX_PROMPT, REVIEW_PROMPT, REVIEW_REPAIR_PROMPT
from .states import State
from .task import TaskState


class ReviewRound:
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
            said = f"review {n}: {_sup._notes(blocking)}, {others} not blocking"
        else:
            said = f"review {n}: no blocking notes, {others} not blocking"
        if blocking and st.rounds < self.max_rounds:
            _sup.set_next_prompt(self.task, REVIEW_FIX_PROMPT)
            self._go(State.IMPLEMENT, f"review {n}: {blocking} blocking", why=_sup._notes(blocking))
            print(f"[{time.strftime('%H:%M:%S')}] {said}, back to the writer", flush=True)
            return
        if self._head_moved(self.task.read_state()) or not self._whole_build_holds(st):
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
        text = _sup._read(out / "review.md")
        if problem := reviewing.problem(text):
            if self._turn(st, REVIEW_REPAIR_PROMPT.format(problem=problem), role="reviewer") is None:
                return None
            text = _sup._read(out / "review.md")
        return text

    def _review_message(self, result: gate.GateResult | None = None) -> str:
        # Tests that went missing are said here, not to the agent, which would put them back.
        n = len(result.removed_tests) if result else 0
        gone = f"; {ui.count(n, 'test')} removed" if n else ""
        try:
            path = self.ports.prepare_review()
        except Exception as e:  # the work is done either way; the review copy is a convenience
            self.task.event("review_prepare_failed", error=str(e)[:500])
            copy = f"the review copy failed ({e}), try: vivibox review {self.task.id}"
            return f"work ready for your review{gone}; {copy}"
        if path is None:
            return f"work ready for your review{gone}: vivibox review {self.task.id}"
        return f"work ready for your review in {path}{gone}"
