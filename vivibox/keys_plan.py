"""The keys about a plan: e edits it or brings it in from your chat, c and C copy the prompt for
that chat. Mixed into the app in tui.py.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess

from . import actions, manual
from .panel import edit_in_editor, planned_by_you
from .plan import PlanError, parse_plan
from .states import State
from .task import Task
from .widgets import Confirm


class PlanKeys:
    def planned_by_you(self, task: Task) -> bool:
        return planned_by_you(task)

    def action_edit_plan(self) -> None:
        task, st = self.selected()
        manual_plan = st.state is State.CHECKPOINT_PLAN and self.planned_by_you(task)
        # With a manual planner you edit your chat's answer, which is then brought in again: the
        # plan and the answer cannot drift apart, and the chat's next answer does not undo yours.
        path = actions.answer_path(task) if manual_plan else task.plan_path
        with self.suspend():
            edit_in_editor(path)
        if manual_plan and path.exists() and path.read_text().strip():
            self.bring_in_plan(task)
        self.reload()

    def bring_in_plan(self, task: Task) -> None:
        try:
            actions.import_plan(task)
        except PlanError as e:
            self.to_clipboard(manual.repair_prompt(str(e)))
            self.notify(
                f"That is not a plan yet: {e}. A message asking your chat to fix it is in your clipboard.",
                severity="warning",
                timeout=15,
            )
            return
        except Exception as e:
            self.fail(e)
            return
        count = len(parse_plan(task.plan_path.read_text()).criteria)
        self.reload()
        self.push_screen(
            Confirm(f"Plan brought in, {count} criteria. Accept it and start implementing?", "Accept"),
            lambda yes: yes and self.action_accept(),
        )

    def to_clipboard(self, text: str) -> str:
        """Through the desktop's own tool where there is one; the terminal's clipboard escape
        (OSC 52) is the fallback, and not every terminal honours it."""
        for command in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(command[0]):
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        command, input=text, text=True, check=True, timeout=5,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )  # fmt: skip
                    return command[0]
        self.copy_to_clipboard(text)
        return "the terminal"

    def action_copy_prompt(self, cli: bool = False) -> None:
        task, _ = self.selected()
        try:
            where = self.to_clipboard(actions.plan_prompt(task, cli=cli))
        except Exception as e:
            self.fail(e)
            return
        file = task.meta / (manual.PROMPT_CLI if cli else manual.PROMPT)
        self.notify(f"Copied the prompt ({where}); it is also in {file}.", timeout=8)

    def action_copy_prompt_cli(self) -> None:
        self.action_copy_prompt(cli=True)
