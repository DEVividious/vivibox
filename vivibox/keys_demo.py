"""The keys that run the project (v) and answer the agent working out how: mixed into the app
in tui.py.
"""

from __future__ import annotations

from textual import work

from . import actions
from .dialogs import Reply
from .widgets import Confirm


class DemoKeys:
    def action_demo_stop(self) -> None:
        task, _ = self.selected()
        self.stop_demo(task.id)

    @work(thread=True)
    def stop_demo(self, task_id: str) -> None:
        try:
            actions.demo_stop(task_id)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.notify, "Stopped.", timeout=3)
        self.call_from_thread(self.reload)

    def action_demo(self) -> None:
        """Runs the project in its pod so you can open it. When nothing says how, the agent works it
        out without asking first: you pressed the key that means run it, and there is no second
        answer you could give. An instruction an earlier task left behind is a real choice, though,
        because it may be stale, so that one is still yours to confirm."""
        task, project = actions.load(self.selected()[1].id)
        if actions.demo_commands(project, task)[0]:
            self.run_demo(task.id)
            return
        if earlier := actions.demo_from_history(project.name):
            asked = f"The last task you accepted was run like this:\n\n{earlier}\n\nStill right?"
            self.push_screen(
                Confirm(asked, "Use it"),
                # Saying no means work it out again, not do nothing: you asked for it to run.
                lambda yes: self.run_demo(task.id, use=earlier) if yes else self.run_demo(task.id, ask=True),
            )
            return
        self.run_demo(task.id, ask=True)

    @work(thread=True)
    def run_demo(self, task_id: str, ask: bool = False, use: str = "", reply: str = "") -> None:
        doing = "working out how to run it" if ask or reply else "starting the demo"
        self.call_from_thread(self.busy_with, task_id, doing)
        try:
            self.demo_outcome(task_id, ask, use, reply)
        finally:
            self.call_from_thread(self.busy_with, task_id, "")

    def demo_outcome(self, task_id: str, ask: bool, use: str, reply: str) -> None:
        try:
            if use:
                result = actions.use_instruction(task_id, use)
            else:
                result = actions.demo(task_id, ask=ask or bool(reply), reply=reply)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        if result.stopped:
            what = "; ".join(result.stopped)
            self.call_from_thread(
                self.notify, f"Stopped what the agent left running: {what}", severity="warning", timeout=8
            )
        if result.question:
            self.call_from_thread(self.answer_demo, task_id, result.question)
        elif urls := result.urls:
            self.call_from_thread(self.open_url, urls[0])
        elif blocked := result.unreachable:
            self.call_from_thread(
                self.notify, f"Port {blocked[0].port} is {blocked[0].why_not}", severity="warning", timeout=10
            )
        elif not result.commands:
            self.say("Still nothing says how to run it")
        elif result.starting:
            # It is alive and installing or compiling. The DEMO column is watching and will say when.
            self.call_from_thread(
                self.notify, "Still starting; the DEMO column says when it listens", timeout=8
            )
        else:
            self.say("It stopped without listening; press d for what it said")

    def say(self, message: str) -> None:
        self.call_from_thread(self.notify, message, severity="error", timeout=8)

    def answer_demo(self, task_id: str, question: str) -> None:
        """The agent asked something only you can decide. Answering carries the same conversation on,
        and none of it can move the task between states."""
        self.push_screen(
            Reply(task_id, f"Working out how to run it, the agent asks:\n\n{question}\n\nYour answer"),
            lambda text: self.run_demo(task_id, reply=text) if text else None,
        )
