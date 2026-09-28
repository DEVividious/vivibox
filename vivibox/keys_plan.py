"""The keys about a plan: e edits it or brings it in from your chat, c and C copy the prompt for
that chat. Mixed into the app in tui.py.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess

from . import actions, gate, manual, proposal
from .panel import edit_in_editor, planned_by_you
from .plan import PlanError, parse_plan
from .states import State
from .task import Task
from .verify_ui import AskVerify
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

    def action_edit_command(self) -> None:
        self.review_command(self.selected()[0])

    def review_command(self, task: Task) -> None:
        """The command the writer proposed, in the field e shows: Enter keeps it for the project
        and the verification runs with it; typed over, yours is kept instead; empty, nothing is
        decided. The box that leaves it to the writer is not offered: the writer just had its say."""
        project = actions.load(task.id)[1]
        command = proposal.proposed(task)
        if not command:
            heading = (
                f"No command came from the writer of {task.id}. Type the one that builds {project.name}"
                " and runs its tests; Enter keeps it for the project and verifies the task with it."
            )
        elif (selection := gate.narrowed_proposal(command)) and gate.DEBUG_OUTPUT.fullmatch(selection):
            heading = (
                f"The writer of {task.id} proposes this command, with debug output on ({selection}):"
                " every verification log would be megabytes of it. Take it out; Enter keeps it for the"
                " project."
            )
        elif selection:
            heading = (
                f"The writer of {task.id} proposes this command, narrowed to {selection}: verified by its"
                " own tests alone it would pass whatever it broke elsewhere. Change it to the whole build;"
                " Enter keeps it for the project."
            )
        else:
            heading = (
                f"The writer of {task.id} proposes how {project.name} is verified, the command it ran."
                " Enter keeps it for the project and verifies the task with it; Escape decides nothing."
            )

        def chosen(choice: dict) -> None:
            if not choice or not choice.get("verify"):
                if choice is not None and choice != {}:
                    self.notify("Nothing typed: type the command, or r to ask the writer for one.")
                return
            try:
                actions.accept_command(task, project, choice["verify"][0])
            except Exception as e:
                self.fail(e)
                return
            self.notify(f"{project.name} is verified with `{choice['verify'][0]}` from now on")
            self.go_on(task, "Command kept")
            self.reload()

        self.push_screen(AskVerify(project.name, [command], heading=heading, writer_box=False), chosen)
