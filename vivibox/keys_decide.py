"""The keys that are your decisions on a task, but for a (in tui.py, which opens what it accepts):
the work brought into your checkout and its commit, r replies, g verifies again, p approves the
risky files. Mixed into the app in tui.py.
"""

from __future__ import annotations

from textual import work

from . import actions, blocks, display, risky, ui
from .dialogs import CommitWork, Reply, ReplyWithCriteria
from .states import State
from .task import Task
from .widgets import Confirm


class DecideKeys:
    @work(thread=True)
    def finish(self, task_id: str) -> None:
        self.call_from_thread(self.busy_with, task_id, "accepting…")
        try:
            task, project = actions.load(task_id)
            done = actions.finish(task, project, on_ready=lambda d: self.call_from_thread(self.finished, d))
            if left := done.left:  # the rules stayed, the helper's words: text, never markup
                self.call_from_thread(
                    self.notify, f"{task_id} is done, but {left}", severity="warning", markup=False
                )
        except Exception as e:
            self.call_from_thread(self.fail, e)
        finally:
            self.call_from_thread(self.busy_with, task_id, "")

    def finished(self, done: actions.Finished) -> None:
        self.reload()
        if done.conflicts:
            files = ", ".join(map(display.name, done.conflicts[:5]))  # the agent's names: text, never markup
            text = f"{done.task_id}: conflicts in {files}; resolve them in your IDE. Also on {done.branch}."
            self.notify(text, severity="warning", timeout=15, markup=False)
            return

        def commit(answer: dict) -> None:
            if not answer:
                self.notify("Left uncommitted; commit it in your IDE when you are ready.")
            else:
                try:
                    made = actions.commit_work(
                        done.source, answer["message"], answer["branch"], answer["create"]
                    )
                    on = f" on {answer['branch']}" if answer["branch"] else ""
                    self.notify(f"Committed{on}: {display.name(made)}", markup=False)
                except Exception as e:
                    self.fail(e)

        self.push_screen(CommitWork(done), commit)

    def action_reply(self) -> None:
        task, st = self.selected()

        def send(comment: str, criteria: list[str] = ()) -> None:
            if not comment.strip() and not criteria:
                return
            try:
                actions.reply(task, comment, criteria)
            except Exception as e:
                self.fail(e)
            else:
                added = f" with {len(criteria)} new criteria" if criteria else ""
                self.go_on(task, f"Sent{added}")
            self.reload()

        if st.state in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED):
            self.push_screen(ReplyWithCriteria(task.id), lambda a: a and send(a["comment"], a["criteria"]))
        else:
            self.push_screen(Reply(task.id), send)

    def go_on(self, task: Task, done: str = "") -> None:
        """After a decision of yours the task goes on. With nobody working on it (after a reboot,
        or once you stopped it) that takes a start, and saying "moves on" without one was a lie."""
        st = task.read_state()
        starts = actions.needs_start(task)
        if done and starts:
            self.notify(f"{done}; starting {task.id}, nobody was working on it.", markup=False)
        elif done:
            self.notify(
                f"{done}; {task.id} is {ui.WORKING.get(st.state, 'waiting for you')} now.", markup=False
            )
        if starts:
            self.start(task.id, resume=True)

    def action_verify_again(self) -> None:
        task, _ = self.selected()
        try:
            actions.verify_again(task)
        except Exception as e:
            self.fail(e)
        else:
            self.go_on(task, f"Verifying {task.id} again")
        self.reload()

    def action_approve_risky(self) -> None:
        """p approves the risky files that are on the screen: the view the panel is drawn from
        is the one whose identity goes with the approval (risky_bound, set and cleared with the
        drawing), taken here, before the question. With no such drawing on the screen nothing is
        asked and nothing approved. Files that are no longer that view's are refused whole, and
        the panel is drawn again, from the files as they are."""
        task, st = self.selected()
        bound = self.risky_bound
        if bound is None or bound[:2] != (task.id, st.updated) or self.panel.has_class("hidden"):
            said = f"The risky files of {task.id} are not shown: open the details (d), read them, then p."
            self.notify(said, severity="error", timeout=10, markup=False)
            return
        shown = bound[2].identity

        def approve(yes: bool) -> None:
            if not yes:
                return
            try:
                count, review = actions.approve_risky(task, actions.load(task.id)[1], shown)
                self.notify(
                    f"Approved {count} change(s)." + (f" Ready for review in {review}" if review else ""),
                    markup=False,
                )
            except risky.NotAsShown as e:
                again = "the details show them as they are now: read them again, then p"
                self.fail(risky.NotAsShown(f"{e}; {again}"))
            except Exception as e:
                self.fail(e)
            else:
                self.go_on(task)
            # Whatever the answer was, what is drawn next is drawn from the files as they are.
            self.risky_bound = None
            self.show_detail()
            self.reload()

        # Said in the one question: a block too large to fold shows the start of its long lines.
        unfolded = sum(block.unfolded for block in self.text.query(blocks.Fence))
        about = blocks.NOT_FOLDED.format(unfolded, "what is shown", task.id) if unfolded else ""
        asked = f"Approve the risky files of {task.id} as shown?"
        self.push_screen(Confirm(asked, "Approve", about=about), approve)
