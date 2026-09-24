"""The keys and background work about models and providers: m, the models list read once a
day, bringing an opencode configuration over. Mixed into the app in tui.py.
"""

from __future__ import annotations

from pathlib import Path

from textual import work

from . import actions, keys, manual, providers
from .browse import shown_path
from .config import ConfigError, load_config
from .dialogs import ChooseModel, ChooseRole
from .panel import project_repos
from .providers_ui import ChooseImport, ImportSource
from .task import Task


class ModelKeys:
    def import_opencode(self, done) -> None:
        """Which opencode configuration, then which of its providers; done gets the names brought over."""

        def picked(path: Path | None) -> None:
            if path is None:
                done([])
                return
            try:
                reading = providers.read_opencode(path)
            except ConfigError as e:
                self.notify(e.args[0], severity="error", timeout=10)
                done([])
                return
            self.push_screen(ChooseImport(path, reading), chosen)

        def chosen(found: list[providers.Found]) -> None:
            if found:
                providers.bring_over(found)
                self.notify(f"Imported {', '.join(f.name for f in found)}.", timeout=8)
            done([f.name for f in found])

        self.push_screen(ImportSource(providers.discover(project_repos())), picked)

    @work(thread=True)
    def refresh_models(self, added: list[str] = ()) -> None:
        """The models again, after providers changed. One just added that lists none is most
        likely a name opencode does not know; a task's list would only show that it has nothing."""
        self.available = actions.available_models(refresh=True)
        if missing := [name for name in added if not self.available.get(name)]:
            self.call_from_thread(
                self.notify,
                f"opencode lists no models for {', '.join(missing)}; check the name.",
                severity="warning",
            )

    def hint_opencode(self) -> None:
        """Someone who uses opencode has providers set up already; say they can be brought over."""
        if keys.list_keys() or providers.load():
            return
        if found := providers.discover(project_repos()):
            where = shown_path(found[0][0])
            self.notify(
                f"Found your opencode configuration, {where}. Press k to bring its providers over.",
                timeout=15,
            )

    @work(thread=True)
    def load_models(self) -> None:
        """A container per provider the first time in a day; a file read after that."""
        try:
            self.available = actions.available_models()
        except Exception:  # the dialogs fall back to the models config.toml names
            self.available = None
        try:
            self.catalog = actions.provider_catalog()
        except Exception:  # the dialog reads it itself, or you type the name
            self.catalog = None

    def action_models(self) -> None:
        """Which model each role runs on, for this task only. The machine's config.toml is the
        default and stays untouched; a task that needs more, or less, says so here."""
        pick = self.selected()
        if not pick:
            return
        task = pick[0]
        try:
            config = load_config()
            st = task.read_state()
            rows = [
                (name, actions.choice_label(self.current_choice(task, name, config)),
                 name in st.models or name in st.harnesses)
                for name in sorted(config.roles)
            ]  # fmt: skip
        except (ConfigError, OSError) as e:
            self.fail(e)
            return

        def role_picked(role: str) -> None:
            if not role:
                return
            offered = actions.choices(role, config, self.available)
            configured = actions.configured_choice(config, role)
            self.push_screen(
                ChooseModel(
                    role, offered, configured, self.current_choice(task, role, config), self.available
                ),
                lambda choice: self.set_choice(task, role, choice, config),
            )

        self.push_screen(ChooseRole(rows), role_picked)

    @staticmethod
    def current_choice(task: Task, role: str, config) -> actions.Choice:
        r = actions.role_of(task, role, config)
        return r.harness, r.model if r.harness != manual.NAME else ""

    def set_choice(self, task: Task, role: str, choice: actions.Choice | None, config) -> None:
        if choice is None:
            return
        harness, model = choice
        if not model and harness != manual.NAME:
            return  # "no model yet" is where the role is, not a model to put it on
        if choice == actions.configured_choice(config, role):
            task.set_role(role)  # back to config.toml, and following it when it changes
        else:
            task.set_role(role, harness if harness != config.roles[role].harness else "", model)
        self.notify(f"{role} runs on {actions.choice_label(choice)} from the next start.", timeout=6)
        self.reload()
