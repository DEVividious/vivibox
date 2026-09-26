"""The keys on a project row: e edits it, o opens it, x forgets it, i sets up another. Mixed into
the app in tui.py.
"""

from __future__ import annotations

from pathlib import Path

from . import actions
from .browse import shown_path
from .config import ConfigError, config_dir, load_project
from .dialogs import DeleteTask, NewProject
from .panel import edit_in_editor, projects
from .settings import ProjectSettings


class ProjectKeys:
    def project_file(self) -> Path:
        return config_dir() / "projects" / f"{self.selected_project()}.toml"

    def action_edit_project(self) -> None:
        """The project's screen: how it is verified and run, its JDK, pass_env, its editor; the
        file itself for the rest, and when it cannot be read at all."""
        name = self.selected_project()
        try:
            load_project(name)
        except ConfigError:
            self.edit_project_file()
            return
        self.push_screen(ProjectSettings(name))

    def edit_project_file(self) -> None:
        with self.suspend():
            edit_in_editor(self.project_file())
        self.drawn = ()  # verify, pass_env, the repository: any of it may have changed
        self.reload()

    def action_open_repo(self) -> None:
        try:
            project = load_project(self.selected_project())
            actions.open_in_ide(self.config, project.repo, project)
            self.notify(f"Opening {shown_path(project.repo)}")
        except Exception as e:
            self.fail(e)

    def action_forget_project(self) -> None:
        name = self.selected_project()
        dialog = DeleteTask(
            f"Forget the project {name}?",
            self.problems.get(name, "") or "vivibox will no longer offer it for tasks.",
            "its project file, with its verify commands and settings.",
            "the repository, exactly as it is.",
        )

        def forget(yes: bool) -> None:
            if yes:
                try:
                    actions.forget_project(name)
                except ConfigError as e:
                    self.fail(e)
                self.reload()

        self.push_screen(dialog, forget)

    def action_new_project(self) -> None:
        self.new_project()

    def new_project(self) -> None:
        """Sets up a project, starting a repository when the folder has none, then asks for its first task."""

        def done(form: dict) -> None:
            if not form:
                if not projects():
                    self.notify("vivibox needs a project to work on; press i to set one up.", timeout=10)
                return
            if used := form.get("use"):
                self.action_new(used)  # the folder is a project already: straight to its next task
                return
            try:
                target = actions.setup_project(
                    Path(form["path"]), form["name"], form["verify"], create=True,
                    no_build=form["no_build"], prepare=form["prepare"],
                )  # fmt: skip
            except Exception as e:
                self.fail(e)
                return
            self.notify(f"Set up {form['name']} in {target}")
            self.action_new(form["name"])

        self.push_screen(NewProject(), done)
