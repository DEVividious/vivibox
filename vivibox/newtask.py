"""The n dialog: a new task, with its project, its goal, how the roles share it and what
each runs on.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, TextArea

from . import actions, context, orchestration, panel, repo
from .browse import ANY, Browse, shown_path
from .config import (
    ORCHESTRATION_LEGEND,
    ORCHESTRATION_MODES,
    ROUNDS_HELP,
    ConfigError,
    load_config,
    load_project,
)
from .dialogs import MENTION_AT_CURSOR, NEW_PROJECT
from .widgets import Dialog, Fields, leave_at_edge


class DescriptionArea(TextArea):
    """A text area that suggests paths after '@', like Claude Code: a folder's entries, or any path in
    the project that contains what you typed; arrows pick, Tab or Enter take one, Escape closes the
    list."""

    def __init__(self, suggestions: OptionList, cwd: Path, **kwargs):
        super().__init__(**kwargs)
        self.suggestions, self.cwd = suggestions, cwd
        self._repo: Path | None = None
        self.paths: list[str] = []

    @property
    def repo(self) -> Path | None:
        """The selected project's repository: its files are suggested too, and they are the ones
        the agent has in its clone. Its tree is listed once here, not on every keystroke."""
        return self._repo

    @repo.setter
    def repo(self, path: Path | None) -> None:
        self._repo = path
        self.paths = context.project_paths(path) if path else []

    def mention(self) -> str | None:
        row, col = self.cursor_location
        m = MENTION_AT_CURSOR.search(self.document.get_line(row)[:col])
        return m.group(1) if m else None

    def suggest(self) -> None:
        partial = self.mention()
        found = (
            context.complete(partial, self.cwd, repo=self.repo, paths=self.paths)
            if partial is not None
            else []
        )
        self.suggestions.set_options(found)
        self.suggestions.display = bool(found)
        if found:
            self.suggestions.highlighted = 0

    def take(self, choice: str) -> None:
        partial = self.mention() or ""
        row, col = self.cursor_location
        self.replace(choice, (row, col - len(partial)), (row, col))
        if not choice.endswith("/"):
            self.insert(" ")
        self.suggest()  # a directory opens its contents

    async def _on_key(self, event: events.Key) -> None:
        if not self.suggestions.display and leave_at_edge(self, event):
            return
        if self.suggestions.display and event.key in ("up", "down", "tab", "enter", "escape"):
            event.stop()
            event.prevent_default()
            options = self.suggestions
            if event.key == "escape":
                options.display = False
            elif event.key == "up":
                options.action_cursor_up()
            elif event.key == "down":
                options.action_cursor_down()
            elif options.highlighted is not None:
                self.take(str(options.get_option_at_index(options.highlighted).prompt))
            return
        await super()._on_key(event)

    @on(TextArea.Changed)
    def changed(self) -> None:
        self.suggest()


def mode_line(name: str) -> str:
    """The line under the list: the mode's flow in symbols (the rest would wrap in the dialog)."""
    return ORCHESTRATION_MODES[name].flow


def mode_hint(name: str) -> str:
    """What the form says of the mode picked on hover: when it fits, which models, at what price;
    and the legend."""
    mode = ORCHESTRATION_MODES[name]
    return f"{mode.when} {mode.models} {mode.tradeoff}\n{ORCHESTRATION_LEGEND}"


class NewTask(Dialog):
    """A form: labels on the left, one field per row, the description and the closing buttons
    the only boxes. Every field is on the screen at once; a short terminal shrinks the
    description before anything scrolls."""

    def __init__(self, preselect: str = "", available: dict[str, list[str]] | None = None):
        super().__init__()
        self.preselect = preselect
        # The models of the providers you have keys for; None while the view is still asking.
        self.available = available
        self.base_ref = "HEAD"

    def compose(self) -> ComposeResult:
        names = panel.projects()
        chosen = self.preselect if self.preselect in names else names[0]
        with Vertical(classes="dialog form"):
            with Fields(classes="fields"):
                with Vertical(id="task", classes="section"):
                    yield Label("Task", classes="title")
                    # A list even with one project in it: the dialog looks the same however many
                    # you have. What is not on it is set up from it, like a provider from a model list.
                    with Horizontal(classes="row"):
                        yield Label("Project", classes="key")
                        yield Select(
                            [*((n, n) for n in names), (NEW_PROJECT, NEW_PROJECT)],
                            value=chosen, allow_blank=False, compact=True, id="project",
                        )  # fmt: skip
                    with Horizontal(classes="row gap", id="kind-row"):
                        yield Label("Kind", classes="key")
                        yield Select(
                            [("Feature: new behaviour", "feature"), ("Bug: something works wrong", "bug"),
                             ("Other: refactoring, tests, upkeep", "other")],
                            value="feature", allow_blank=False, compact=True, id="kind",
                        )  # fmt: skip
                    with Horizontal(classes="row"):
                        yield Label("Branch", classes="key")
                        branch = Button("Current…", compact=True, id="base-ref")
                        # Its text flush with the lists' above it; Textual's CSS takes no 0 here.
                        branch.styles.line_pad = 0
                        yield branch
                    with Horizontal(classes="row", id="build-row"):
                        yield Label("Build", classes="key")
                        yield Checkbox(
                            "Nothing to build or test: research, a ticket analysis",
                            compact=True, id="no-build",
                        )  # fmt: skip
                    suggestions = OptionList(id="suggestions")
                    suggestions.display = False
                    with Horizontal(classes="row", id="task-row"):
                        yield Label("Goal", classes="key")
                        yield DescriptionArea(
                            suggestions, Path.cwd(), id="goal", classes="description",
                            placeholder="What should the agent do? A line, or a whole ticket with its"
                            " context and constraints.",
                        )  # fmt: skip
                    yield suggestions
                    # Attach fills the description, so it stands under it, not among the lists.
                    with Horizontal(classes="row"):
                        yield Label("", classes="key")
                        yield Button("Attach…", compact=True, id="attach")
                        yield Label("or @path, for a copy of a file or folder.", classes="hint")
                with Vertical(id="planning", classes="section"):
                    yield Label("Agents", classes="title")
                    # One question, not two boxes that could both be ticked.
                    with Horizontal(classes="row"):
                        yield Label("Plan", classes="key")
                        yield Select(
                            [("Stop for my review of the plan", "review"),
                             ("Accept the agent's plan without stopping (--auto)", "auto"),
                             ("Only create the task, to write the plan myself (--draft)", "draft")],
                            value="review", allow_blank=False, compact=True, id="plan",
                        )  # fmt: skip
                    # Each role on config.toml's choice unless you pick another; m changes it later.
                    # In the order they work: the planner, the writer, then the reviewer.
                    config = load_config()
                    order = {"planner": 0, "writer": 1, "reviewer": 2}
                    for name in sorted(config.roles, key=lambda n: (order.get(n, 9), n)):
                        offered = actions.choices(name, config, self.available)
                        configured = actions.configured_choice(config, name)
                        with Horizontal(classes="row gap"):
                            yield Label(name.capitalize(), classes="key")
                            options = [(actions.choice_label(c, configured), c) for c in offered]
                            yield Select(
                                options, value=configured, allow_blank=False, compact=True,
                                id=f"role-{name}", classes="model",
                            )  # fmt: skip
                    # How the roles share the work: the mode's name on the list, its flow in symbols
                    # under it (together they would wrap at 80 columns), and when it fits, the models,
                    # the trade-off and the legend on hover.
                    with Horizontal(classes="row gap"):
                        yield Label("Orchestration", classes="key")
                        yield Select(
                            [(m.label, name) for name, m in ORCHESTRATION_MODES.items()],
                            value=config.orchestration, allow_blank=False, compact=True, id="orchestration",
                            tooltip=mode_hint(config.orchestration),
                        )  # fmt: skip
                        yield Label("Rounds", classes="key rounds")
                        yield Input(
                            str(config.max_rounds), id="max-rounds", compact=True, type="integer",
                            tooltip=ROUNDS_HELP,
                        )  # fmt: skip
                    # One line on when the mode fits; it goes first when the terminal is short.
                    with Horizontal(classes="row hint-row"):
                        yield Label("", classes="key")
                        yield Label(mode_line(config.orchestration), classes="hint", id="orchestration-hint")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")
                yield Label("ctrl+s creates the task", classes="hint keys")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "base-ref":
            from .branches import BranchPicker

            source = load_project(str(self.query_one("#project", Select).value)).repo
            self.app.push_screen(BranchPicker(source, self.base_ref), self.branch_chosen)
            return
        if event.button.id == "attach":
            self.app.push_screen(Browse(ANY, "Attach a file or a folder for the agent"), self.attach)
            return
        if event.button.id != "create":
            self.dismiss({})
            return
        self.dismiss(
            {
                "project": self.query_one("#project", Select).value,
                "goal": self.query_one("#goal", TextArea).text.strip(),
                "kind": self.query_one("#kind", Select).value,
                "auto": self.query_one("#plan", Select).value == "auto",
                "draft": self.query_one("#plan", Select).value == "draft",
                "roles": {
                    s.id.removeprefix("role-"): s.value
                    for s in self.query(".model").results(Select)
                    if s.parent.display  # a mode without a reviewer of its own names none
                },
                "orchestration": self.query_one("#orchestration", Select).value,
                "max_rounds": int(self.query_one("#max-rounds", Input).value or 0),
                "no_build": self.query_one("#no-build", Checkbox).value,
                "base_ref": self.base_ref,
            }
        )

    # The dialog's frame, padding and buttons: what the fields leave room for.
    CHROME = 9
    # Lines of terminal under which the dialog takes all but a line of the screen.
    SHORT = 30

    def on_mount(self) -> None:
        self.for_project(str(self.query_one("#project", Select).value))
        self.reviewer_follows_mode()
        self.call_after_refresh(self.fit)

    @on(Select.Changed, "#orchestration")
    def mode_changed(self, event: Select.Changed) -> None:
        self.reviewer_follows_mode()
        self.query_one("#orchestration-hint", Label).update(mode_line(str(event.value)))
        event.select.tooltip = mode_hint(str(event.value))
        self.call_after_refresh(self.fit)

    def reviewer_follows_mode(self) -> None:
        """The reviewer's model row only where the mode has a reviewer of its own; the other modes
        have the planner or the writer review."""
        if self.query("#role-reviewer"):
            separate = orchestration.MODES[
                str(self.query_one("#orchestration", Select).value)
            ].separate_reviewer
            self.query_one("#role-reviewer", Select).parent.display = separate

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        """The description as tall as the screen leaves after the other rows, a line at least, so
        the whole form stays in view and the description scrolls inside itself. Before the
        description would shrink below three lines, the groups' headings go, then the blank rows
        between the lists of a group and the one between the groups."""
        fields = self.query_one(Fields)
        goal = self.query_one("#goal", TextArea)
        dialog = self.query_one(".dialog")
        # Nine tenths of the screen, or all but a line of it on a short one (under 30 lines).
        height = self.size.height
        room = (height - 2 if height < self.SHORT else int(height * 0.9)) - self.CHROME
        fields.styles.max_height = max(5, room)
        # Counted, not measured: a measure is the last layout's, whatever class the dialog has
        # been given since. A row is a line. The blank rows (between the lists, between the two
        # groups) and a hint row go together when the terminal is short.
        rows = [row for row in self.query(".row") if row.id != "task-row" and not row.has_class("hint-row")]
        rows = [row for row in rows if row.display]
        hints = len(self.query(".hint-row"))
        # The blank rows between the lists, and the one the second group stands below the first.
        gaps = len([row for row in rows if row.has_class("gap")]) + 1
        titles = 2 * len(self.query(".title"))  # a heading and the blank row under it
        spare = room - len(rows) - 3
        plain, tight = spare < gaps + titles + hints, spare < gaps + hints
        dialog.set_class(plain, "plain")
        dialog.set_class(tight, "tight")
        taken = len(rows) + (0 if tight else gaps + hints) + (0 if plain else titles)
        goal.styles.height = max(3, min(12, room - taken))
        goal.focus()

    @on(Select.Changed, "#project")
    def switched(self, event: Select.Changed) -> None:
        if event.value == NEW_PROJECT:
            self.dismiss({"project": NEW_PROJECT})
            return
        self.for_project(str(event.value))

    def for_project(self, name: str) -> None:
        """An empty project has nothing that could work wrong, so what kind of task this is is not asked."""
        self.query_one("#kind-row").display = not actions.empty_project(name)
        self.base_ref = "HEAD"
        self.query_one("#base-ref", Button).label = "Current…"
        with contextlib.suppress(ConfigError):
            self.query_one("#goal", DescriptionArea).repo = load_project(name).repo
            self.load_current_branch(name, load_project(name).repo)

    @work(thread=True, exclusive=True, group="current-branch")
    def load_current_branch(self, project: str, source: Path) -> None:
        try:
            label = actions.current_branch(source)
        except repo.RepoError:
            return  # opening the picker shows the repository error and how to return
        self.app.call_from_thread(self.current_branch_loaded, project, label)

    def current_branch_loaded(self, project: str, label: str) -> None:
        if (
            self.is_mounted
            and self.base_ref == "HEAD"
            and self.query_one("#project", Select).value == project
        ):
            self.query_one("#base-ref", Button).label = f"Current ({label})…"

    def branch_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice:
            label, self.base_ref = choice
            self.query_one("#base-ref", Button).label = label + "…"

    def attach(self, path: Path | None) -> None:
        """The picked file or folder as an @mention, where the cursor is in the description."""
        goal = self.query_one("#goal", DescriptionArea)
        if path is not None:
            goal.insert(f"@{shown_path(path)} ")
        goal.focus()

    def key_ctrl_s(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})
