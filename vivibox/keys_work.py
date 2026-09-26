"""The keys that show the work: o opens the review copy in your editor, f the diff, l the
timeline and the logs. Mixed into the app in tui.py.
"""

from __future__ import annotations

from . import actions, ide, logs
from .app_support import in_terminal
from .config import ConfigError
from .panel import git_diff, git_env


class WorkKeys:
    def action_open_ide(self) -> None:
        """With the editor the project or config.toml names, else the one the repository's own
        folders point at among those found here; k, or the project's row, changes it."""
        task_id = self.selected()[1].id
        if not actions.editor_command(self.config, actions.load(task_id)[1]):
            self.fail(ConfigError("no editor found; pick one under k, or set [review] ide in config.toml"))
            return
        self.open_ide(task_id)

    def open_ide(self, task_id: str) -> None:
        task, project = actions.load(task_id)
        command = actions.editor_command(self.config, project)
        try:
            path = actions.review_copy(task, project)
        except Exception as e:
            self.fail(e)
            return
        if ide.is_terminal(command):
            with self.suspend():  # it takes over the terminal, like the plan editor does
                in_terminal(ide.command_for(path, command))
            self.reload()
            return
        try:
            actions.open_in_ide(self.config, path, project)
            self.notify(f"Opening {path}")
        except Exception as e:
            self.fail(e)

    def diff_command(self) -> list[str]:
        task, _ = self.selected()
        return git_diff(task, actions.load(task.id)[1])

    def action_show_diff(self) -> None:
        """The work as a diff, in git's own pager, before you accept it."""
        try:
            command = self.diff_command()
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            in_terminal(command, env=git_env())

    def action_show_log(self) -> None:
        """The timeline, a verification log, or the supervisor's, in your pager: one entry opens
        at once, more are picked from."""
        task, st = self.selected()
        found, start = logs.entries(task, st, self.agent_running(task.id))
        if len(found) == 1:
            self.read_log(found[0].command)
            return
        self.push_screen(logs.ChooseLog(found, start), lambda command: command and self.read_log(command))

    def read_log(self, command: list[str]) -> None:
        with self.suspend():
            in_terminal(command)
