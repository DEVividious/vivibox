"""The keys that start and stop a task: s, and S for a stop by force. Mixed into the app in
tui.py.
"""

from __future__ import annotations

from textual import work

from . import actions
from .widgets import Confirm


class RunKeys:
    def action_start_task(self) -> None:
        task, _ = self.selected()
        self.start(task.id, resume=any(e["type"] == "started" for e in task.events()))

    def action_stop_task(self) -> None:
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Stop {task.id}? Its work is kept; start it again with s.", "Stop", destructive=True),
            lambda yes: yes and self.stop(task.id),
        )

    def action_stop_pod(self) -> None:
        """At a checkpoint nothing runs but the pod; your decision starts it again by itself."""
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Take the pod of {task.id} down? Your next decision starts it again.", "Stop pod"),
            lambda yes: yes and self.stop(task.id),
        )

    def action_force_stop(self) -> None:
        """For a stop that hangs (a container that ignores it, a supervisor stuck in docker) or a
        start that never ends: kill both, at once. Asked once, since S is one Shift away from s."""
        task, _ = self.selected()
        self.push_screen(
            Confirm(
                f"Stop {task.id} by force? The turn under way is lost; its work on disk is kept.",
                "Stop by force",
                destructive=True,
            ),
            lambda yes: yes and self.stop(task.id, force=True),
        )

    @work(thread=True)
    def start(self, task_id: str, resume: bool = False) -> None:
        step = lambda doing: self.call_from_thread(self.busy_with, task_id, doing)  # noqa: E731
        step("starting…")
        try:
            model = actions.start(task_id, resume=resume, on_step=step)
            self.call_from_thread(self.notify, f"{task_id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.busy_with, task_id, "")

    @work(thread=True)
    def stop(self, task_id: str, force: bool = False) -> None:
        # Taking the pod down takes a while; without this the row looked as if nothing happened.
        # A forced stop runs beside a stop that hangs; that one is left to finish on its own.
        self.call_from_thread(self.busy_with, task_id, "stopping…")
        try:
            actions.stop(actions.load(task_id)[0], force=force)
            self.call_from_thread(self.notify, f"{task_id} stopped{' by force' if force else ''}.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.busy_with, task_id, "")
