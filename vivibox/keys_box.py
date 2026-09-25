"""The keys of a box: w enters it, b opens one in a project, a closes it for review. Mixed into
the app in tui.py.
"""

from __future__ import annotations

import subprocess

from textual import work

from . import actions


class BoxKeys:
    @work(thread=True)
    def close_box(self, task_id: str) -> None:
        self.call_from_thread(self.busy_with, task_id, "closing the box…")
        try:
            task, project = actions.load(task_id)
            where = actions.close_box(task, project)
        except Exception as e:
            self.call_from_thread(self.fail, e)
        else:
            said = f"{task_id} closed; " + (
                f"its work is ready for your review in {where}" if where else "it changed risky files"
            )
            self.call_from_thread(self.notify, said, timeout=8)
        self.call_from_thread(self.busy_with, task_id, "")

    def action_enter_box(self) -> None:
        task, _ = self.selected()
        if not actions.box_pod_running(task.id):
            self.reopen_box(task.id)
            return
        self.enter_box(task.id)

    def enter_box(self, task_id: str) -> None:
        try:
            command = actions.box_shell_command(task_id)
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)
        self.reload()

    @work(thread=True)
    def reopen_box(self, task_id: str) -> None:
        """A box whose pod Docker took down (a restart of Docker or of the machine): up again
        first, off the loop and with the row saying so, then the shell."""
        self.call_from_thread(self.busy_with, task_id, "starting the pod…")
        try:
            actions.start_box(task_id)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            self.call_from_thread(self.busy_with, task_id, "")
            return
        self.call_from_thread(self.busy_with, task_id, "")
        self.call_from_thread(self.enter_box, task_id)

    def action_new_box(self) -> None:
        name = self.selected_project()
        self.notify(f"Opening a box in {name}…")
        self.open_box(name)

    @work(thread=True)
    def open_box(self, name: str) -> None:
        try:
            task = actions.open_box(name)
        except Exception as e:
            self.call_from_thread(self.fail, e)
        else:
            self.call_from_thread(self.notify, f"{task.id} is open; w enters it, a brings its work back.")
            self.call_from_thread(self.select, task.id)
        self.call_from_thread(self.reload)
