"""The keys that show the work: o opens the review copy in your editor, f the diff, l the
timeline and the logs. Mixed into the app in tui.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator

from . import actions, display, ide, logs, repo
from .app_support import in_terminal
from .config import ConfigError
from .panel import git_diff, git_env, pager_command
from .task import Task

# The colours of the diff, vivibox's own: no escape sequence in the pager is anyone else's.
BOLD, GREEN, RED, CYAN, RESET = "\x1b[1m", "\x1b[32m", "\x1b[31m", "\x1b[36m", "\x1b[0m"


def shown_diff(found: Iterable[str]) -> Iterator[str]:
    """git's diff, without its colours, as you read it: every line escaped as a risky file's is,
    so what the agent wrote can neither hide a line (SGR 8, \\r) nor be a link (OSC 8) whatever
    your pager passes on; then coloured as git does, a file's head bold, its lines by their mark."""
    head = False
    for line in found:
        head = line.startswith("diff ") or (head and not line.startswith("@"))
        text = display.content(line).removesuffix("\n")
        colour = BOLD if head else CYAN if text[:1] == "@" else {"+": GREEN, "-": RED}.get(text[:1], "")
        yield f"{colour}{text}{RESET}\n" if colour else f"{text}\n"


def diff_pager(task: Task, project) -> list[str]:
    """f: the work as a diff, from the review ref in your repository, written escaped to the
    task's metadata and opened in your pager there; git's own pager would show it as it is."""
    path = task.meta / "log" / "work.diff"
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [*git_diff(task, project), "--no-color"]
    with (
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=repo.git_environment()
        ) as p,
        open(path, "w") as out,
    ):
        out.writelines(shown_diff(raw.decode("utf-8", "surrogateescape") for raw in p.stdout))
        said = display.said(p.stderr.read().decode("utf-8", "replace"))
    if p.returncode != 0:
        raise repo.RepoError(f"git diff: {said}")
    return pager_command(path, colours=True)  # escaped, its colours vivibox's own


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
            self.notify(f"Opening {path}", markup=False)
        except Exception as e:
            self.fail(e)

    def diff_command(self) -> list[str]:
        task, _ = self.selected()
        return git_diff(task, actions.load(task.id)[1])

    def action_show_diff(self) -> None:
        """The work as a diff, in your pager, before you accept it."""
        try:
            task, _ = self.selected()
            command = diff_pager(task, actions.load(task.id)[1])
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            in_terminal(command, env=git_env())

    def action_show_log(self) -> None:
        """The timeline, a verification log, or the supervisor's, in your pager: one entry opens
        at once, more are picked from."""
        pick = self.selected()
        try:  # a copy for a pager other than less refused (its folder not yours alone): said
            if pick is None:  # a finished task: what its archive kept
                found, start = logs.archived_entries(actions.archive_path(self.selected_id() or "")), 0
            else:
                found, start = logs.entries(pick[0], pick[1], self.agent_running(pick[0].id))
        except OSError as e:
            self.fail(e)
            return
        if not found:
            self.notify("Nothing kept: this task finished before vivibox kept logs.", timeout=6)
            return
        if len(found) == 1:
            self.read_log(found[0].command)
            return
        self.push_screen(logs.ChooseLog(found, start), lambda command: command and self.read_log(command))

    def read_log(self, command: list[str]) -> None:
        with self.suspend():
            in_terminal(command)
