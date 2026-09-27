"""Creating tasks: check the project before the form and again before creating its clone."""

from __future__ import annotations

from textual import work
from textual.widgets import Select

from . import actions, repo
from .config import ConfigError, load_config, load_project
from .dialogs import NEW_PROJECT
from .newtask import NewTask
from .panel import projects


class NewKeys:
    def git_identity_ready(self, project: str) -> bool:
        try:
            repo.identity(load_project(project).repo)
        except (repo.RepoError, ConfigError) as error:
            self.fail(error)
            return False
        return True

    def action_new(self, preselect: str = "") -> None:
        if not projects():
            self.notify("A task needs a project first; press i to add one.")
            return
        if actions.needs_provider(load_config()):
            self.notify("A task needs a provider first; press k to add one.")
            return
        preselect = preselect or self.selected_project() or projects()[0]
        if not self.git_identity_ready(preselect):
            return

        def create(form: dict) -> None:
            if form.get("project") == NEW_PROJECT:
                self.new_project()
                return
            if not form:
                return
            if not form["goal"] or form["project"] is Select.NULL:
                self.notify("A task needs a project and a description.", severity="error")
                return
            if not self.git_identity_ready(form["project"]):
                return
            self.notify(f"Creating a task in {form['project']}…")
            self.create(form)

        self.push_screen(NewTask(preselect, self.available), create)

    @work(thread=True)
    def create(self, form: dict) -> None:
        try:
            options = ("roles", "orchestration", "max_rounds", "no_build", "base_ref")
            task = actions.create(
                form["project"],
                form["goal"],
                auto=form["auto"],
                kind=form["kind"],
                **{name: form[name] for name in options if name in form},
            )
            self.call_from_thread(self.reload)
            for note in actions.context_notes(task):
                self.call_from_thread(self.notify, f"{task.id}: {note}.", severity="warning", timeout=12)
            if form["draft"]:
                self.call_from_thread(
                    self.notify, f"Created {task.id}; edit its plan with e, start it with s."
                )
                return
            step = lambda doing: self.call_from_thread(self.busy_with, task.id, doing)  # noqa: E731
            step("starting…")
            try:
                model = actions.start(task.id, on_step=step)
            finally:
                step("")
            self.call_from_thread(self.notify, f"{task.id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)
