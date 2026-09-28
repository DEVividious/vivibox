"""The n dialog: a new task, with its project, its goal, how the roles share it and what
each runs on.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Button, Input, Label, OptionList, Select, TextArea

from . import actions, context, look, panel, repo
from .browse import ANY, Browse, shown_path
from .config import (
    ORCHESTRATION_MODES,
    ConfigError,
    load_config,
    load_project,
)
from .dialogs import MENTION_AT_CURSOR, NEW_PROJECT
from .settings import ROLE_ABOUT as ROLE_HELP
from .widgets import ContextHelp, Dialog, Fields, follow_highlight, leave_at_edge


class Suggestions(OptionList):
    """The paths offered after '@'. The goal keeps the focus while you type, so the list takes none:
    a click picks a path and you go on typing."""

    can_focus = False


class DescriptionArea(TextArea):
    """A text area that suggests paths after '@', like Claude Code: a folder's entries, or any path in
    the project that contains what you typed; arrows pick, Enter takes one as it is, a folder too,
    Tab opens a folder, Escape closes the list."""

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

    def take(self, choice: str, open_folder: bool = False) -> None:
        partial = self.mention() or ""
        row, col = self.cursor_location
        self.replace(choice, (row, col - len(partial)), (row, col))
        if open_folder and choice.endswith("/"):
            self.suggest()  # its contents, to go on down
            return
        self.insert(" ")
        self.suggestions.display = False

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
                choice = str(options.get_option_at_index(options.highlighted).prompt)
                self.take(choice, open_folder=event.key == "tab")
            return
        await super()._on_key(event)

    @on(TextArea.Changed)
    def changed(self) -> None:
        self.suggest()


def mode_option(name: str) -> Content:
    """A mode in the Flow list: its name, and beside it, muted, what tells it from the others."""
    mode = ORCHESTRATION_MODES[name]
    return Content.assemble(mode.label, "  ", (mode.subtitle, look.MUTED))


def branch_label(name: str) -> Content:
    """The branch, whole, and a muted › that says Enter opens a list, as a settings row that
    opens a screen of its own says it."""
    return Content.assemble(name, "  ", ("›", look.MUTED))


def model_name(choice) -> str:
    """A role's model as the help names it: the whole model, or who plans."""
    harness, model = choice
    if harness == "manual":
        return "you, in your own chat"
    return model or "no model yet"


# What each field of the form is for, in the help under the fields while it has focus. The flow
# and the models say more, from what the form holds (NewTask.help_for).
FIELD_HELP = {
    "project": look.Explained(
        "Project",
        "The repository the task works on. Its last entry sets up another project.",
    ),
    "kind": look.Explained(
        "Kind",
        "What sort of change it is: the planner's brief, and the commit's branch prefix (feature/, bugfix/).",
    ),
    "base-ref": look.Explained(
        "Branch",
        "The commit the task starts from; Enter opens the list of branches. Another branch changes "
        "only the task's base, never your checkout.",
    ),
    "no-build": look.Explained(
        "Build",
        "Whether the verification builds and tests the work. Nothing to build: the answer is a text "
        "(research, a ticket's analysis), and only the criteria and the commits are checked.",
    ),
    "goal": look.Explained(
        "Goal",
        "What the agent should do: a line, or a whole ticket with its context and constraints. "
        "Type @ for a file: one of the project is the one in the agent's clone; any other is "
        "copied into the task, read-only. Files that look like credentials are refused.",
    ),
    "attach": look.Explained(
        "Files",
        "Attach… picks a file or a folder and adds it to the goal as @path. A file of the project "
        "is the one in the agent's clone; any other is copied into the task, read-only.",
    ),
    "plan": look.Explained(
        "Plan review",
        "Stop: you accept or change the plan before anything is written. --auto: the writer starts "
        "on the planner's plan. --draft: the task waits for a plan you write yourself.",
    ),
}
# What one fix round is, by flow, for the help of Fix rounds.
ROUND_IS = {
    "single_agent": "after a failed verification",
    "planner_executor": "after a failed verification",
    "planner_maker_checker": "after a failed verification or the reviewer's blocking notes",
    "supervisor_worker": "after a failed verification or the supervisor's blocking notes",
}


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

    frame_title = "New task"
    hint_keys = (("ctrl+s", "create"), look.ESC_CANCELS)

    def compose(self) -> ComposeResult:
        names = panel.projects()
        chosen = self.preselect if self.preselect in names else names[0]
        with Vertical(classes="dialog form"):
            with Fields(classes="fields"):
                with Vertical(id="task", classes="section"):
                    yield Label("TASK", classes="title")
                    # A list even with one project in it: the dialog looks the same however many
                    # you have. What is not on it is set up from it, like a provider from a model list.
                    with Horizontal(classes="row"):
                        yield Label("Project", classes="key")
                        yield Select(
                            [*((n, n) for n in names), (NEW_PROJECT, NEW_PROJECT)],
                            value=chosen, allow_blank=False, compact=True, id="project",
                        )  # fmt: skip
                    with Horizontal(classes="row", id="kind-row"):
                        yield Label("Kind", classes="key")
                        yield Select(
                            [("Feature: new behaviour", "feature"), ("Bug: something works wrong", "bug"),
                             ("Other: refactoring, tests, upkeep", "other")],
                            value="feature", allow_blank=False, compact=True, id="kind",
                        )  # fmt: skip
                    with Horizontal(classes="row"):
                        yield Label("Branch", classes="key")
                        yield Button(branch_label("Current"), compact=True, id="base-ref", classes="field")
                    # What the verification does with the work: build and test it, or nothing to
                    # build (research, a ticket's analysis). A list, so both answers are named.
                    with Horizontal(classes="row", id="build-row"):
                        yield Label("Build", classes="key")
                        yield Select(
                            [("Build and test the work", False),
                             ("Nothing to build or test: research, a ticket analysis", True)],
                            value=False, allow_blank=False, compact=True, id="no-build",
                        )  # fmt: skip
                    suggestions = Suggestions(id="suggestions")
                    suggestions.display = False
                    with Horizontal(classes="row text-row", id="task-row"):
                        yield Label("Goal", classes="key")
                        yield DescriptionArea(
                            suggestions, Path.cwd(), id="goal", classes="description",
                            placeholder="What should the agent do? A line, or a whole ticket with its"
                            " context and constraints.",
                        )  # fmt: skip
                    yield suggestions
                    # Attach fills the description, so it stands under it, not among the lists.
                    with Horizontal(classes="row"):
                        yield Label("Files", classes="key")
                        yield Button("Attach…", compact=True, id="attach", classes="inline")
                        yield Label("or add @path in Goal", classes="hint")
                with Vertical(id="planning", classes="section"):
                    yield Label("WORKFLOW", classes="title")
                    # In the order the task goes: whether you review the plan, then how the
                    # agents share the work, which models they run on, and how many fix rounds
                    # they get before the work comes to you.
                    with Horizontal(classes="row"):
                        yield Label("Plan review", classes="key")
                        yield Select(
                            [("Stop for my review of the plan", "review"),
                             ("Accept the agent's plan without stopping (--auto)", "auto"),
                             ("Only create the task, to write the plan myself (--draft)", "draft")],
                            value="review", allow_blank=False, compact=True, id="plan",
                        )  # fmt: skip
                    # Each option says in a few words what tells it from the others; the help
                    # under the fields says the rest while Flow has focus.
                    config = load_config()
                    with Horizontal(classes="row"):
                        yield Label("Flow", classes="key")
                        yield Select(
                            [(mode_option(name), name) for name in ORCHESTRATION_MODES],
                            value=config.orchestration, allow_blank=False, compact=True, id="orchestration",
                        )  # fmt: skip
                    # A row per agent the flow has, named as the flow names it (Agent, Executor,
                    # Supervisor, Worker), on config.toml's model unless you pick another; the
                    # roles one agent plays share its model and its session.
                    order = {"planner": 0, "writer": 1, "reviewer": 2}
                    for name in sorted(config.roles, key=lambda n: (order.get(n, 9), n)):
                        offered = actions.choices(name, config, self.available)
                        configured = actions.configured_choice(config, name)
                        with Horizontal(classes="row agent"):
                            yield Label(name.capitalize(), classes="key", id=f"agent-{name}")
                            options = [(actions.choice_label(c, configured), c) for c in offered]
                            yield Select(
                                options, value=configured, allow_blank=False, compact=True,
                                id=f"role-{name}", classes="model",
                            )  # fmt: skip
                    # The number alone; what one round is, is the help's to say.
                    with Horizontal(classes="row"):
                        yield Label("Fix rounds", classes="key")
                        yield Input(str(config.max_rounds), id="max-rounds", compact=True, type="integer")
            # Under the fields, in one place whatever has focus: the flow highlighted, with the
            # models picked, or what the focused field is for.
            yield ContextHelp(id="about")
            with Horizontal(classes="buttons"):
                yield Button("Create task", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

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
                "no_build": bool(self.query_one("#no-build", Select).value),
                "base_ref": self.base_ref,
            }
        )

    # The dialog's frame, padding and buttons: what the fields leave room for.
    CHROME = 7
    # Lines of terminal under which the dialog takes all but a line of the screen.
    SHORT = 30
    # The help under the fields, its rule included: two lines of text, or all of a flow's eight.
    HELP_SHORT, HELP_FULL = 3, 9
    fitted = False
    # The description's lines at least (three of text in a frame), and at most.
    GOAL_MIN, GOAL_MAX = 5, 14

    def on_mount(self) -> None:
        self.for_project(str(self.query_one("#project", Select).value))
        self.follow_mode()
        follow_highlight(self, self.query_one("#orchestration", Select), self.flow_highlighted)
        self.call_after_refresh(self.fit)

    def mode(self) -> str:
        return str(self.query_one("#orchestration", Select).value)

    @on(Select.Changed, "#orchestration")
    def mode_changed(self, event: Select.Changed) -> None:
        self.follow_mode()
        self.call_after_refresh(self.fit)

    @on(Select.Changed, ".model")
    def model_changed(self) -> None:
        self.refresh_help()

    def flow_highlighted(self, index: int | None) -> None:
        """While the Flow list is open, the help says what the highlighted flow is, to compare;
        closed, it is the focused field's again."""
        names = list(ORCHESTRATION_MODES)
        if index is not None and index < len(names):
            self.query_one(ContextHelp).explain(self.flow_help(names[index]))
        else:
            self.refresh_help()

    def follow_mode(self) -> None:
        """A row per agent of the flow, named as the flow names it: roles one agent plays have
        one model, so one row; a flow without a reviewer of its own has no reviewer's row."""
        mode = ORCHESTRATION_MODES[self.mode()]
        agents = {role: name for role, name, _ in mode.agents}
        for row in self.query(".agent"):
            role = row.query_one(Select).id.removeprefix("role-")
            row.display = role in agents
            if role in agents:
                row.query_one(".key", Label).update(agents[role])
        self.refresh_help()

    def flow_help(self, name: str) -> look.Explained:
        """A flow in the help, on the models picked in the form."""
        selects = self.query(".model").results(Select)
        models = {s.id.removeprefix("role-"): model_name(s.value) for s in selects}
        rounds = self.query_one("#max-rounds", Input).value or "0"
        return look.flow(name, models, int(rounds))

    def help_for(self, widget) -> str | look.Explained | None:
        """What the focused field is for. The flow's details only while Flow has focus: elsewhere
        its row says it in a line."""
        if widget.id == "orchestration":
            return self.flow_help(self.mode())
        if widget.id in FIELD_HELP:
            return FIELD_HELP[widget.id]
        mode = ORCHESTRATION_MODES[self.mode()]
        if widget.id == "max-rounds":
            rounds = self.query_one("#max-rounds", Input).value or "0"
            return look.Explained(
                "Fix rounds",
                f"Up to {rounds} fix turns of the writer {ROUND_IS[self.mode()]}, before the work "
                "comes to you with what sent it back; your reply gives it as many again. The first "
                "implementation is not a round.",
            )
        if widget.id and widget.id.startswith("role-"):
            role = widget.id.removeprefix("role-")
            agent, plays = next(((a, p) for r, a, p in mode.agents if r == role), (role.capitalize(), ""))
            shared = f" It plays the {plays}, in one session, on this one model." if plays else ""
            return look.Explained(agent, ROLE_HELP.get(role, "") + shared)
        return None

    def refresh_help(self) -> None:
        """The help again for what has focus, after what it depends on changed."""
        if self.focused is not None and (said := self.help_for(self.focused)) is not None:
            self.query_one(ContextHelp).explain(said)

    @on(Input.Changed, "#max-rounds")
    def rounds_changed(self) -> None:
        self.refresh_help()

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        """The form's rectangle from the terminal alone, so that moving between fields never
        resizes it: every field on the screen, the help under the fields a fixed number of lines
        whatever it says, and the description what is left. Beyond the rows and a line of the
        description, what the terminal has goes in this order to: the help's two lines, the
        description's three, the blank row between the sections, the sections' headings, all of
        the help, three more lines of the description, and the description again."""
        fields = self.query_one(Fields)
        goal = self.query_one("#goal", TextArea)
        dialog = self.query_one(".dialog")
        about = self.query_one(ContextHelp)
        height = self.size.height
        room = (height - 2 if height < self.SHORT else int(height * 0.9)) - self.CHROME
        # Counted, not measured: a measure is the last layout's, whatever class the dialog has
        # been given since. A row is a line; the description is one line of text in a frame at
        # least.
        rows = [row for row in self.query(".row") if row.id != "task-row" and row.display]
        spare = room - len(rows) - 3
        gap, titles = len(self.query(".section")) - 1, len(self.query(".title"))
        wanted = (
            ("help", self.HELP_SHORT + 1),  # its rule, two lines, and the blank row above it
            ("goal", 2),
            ("gap", gap),
            ("titles", titles),
            ("full", self.HELP_FULL - self.HELP_SHORT),
            ("goal", 3),
        )
        given: list[str] = []
        for part, lines in wanted:
            if part == "full" and "help" not in given:
                continue
            if 0 < lines <= spare:
                given.append(part)
                spare -= lines
        dialog.set_class("titles" not in given, "plain")
        dialog.set_class("gap" not in given, "tight")
        about.display = "help" in given
        help_lines = 0
        if "help" in given:
            help_lines = self.HELP_FULL if "full" in given else self.HELP_SHORT
            about.reserve(help_lines - 1, full="full" in given)
        fields.styles.max_height = max(5, room - help_lines - (1 if help_lines else 0))
        extra = 2 * given.count("goal") + (1 if given.count("goal") == 2 else 0)
        goal.styles.height = min(self.GOAL_MAX, 3 + extra + max(0, spare))
        if not self.fitted:  # the description has the keys when the form opens, never again
            self.fitted = True
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
        self.query_one("#base-ref", Button).label = branch_label("Current")
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
            self.query_one("#base-ref", Button).label = branch_label(f"Current ({label})")

    def branch_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice:
            label, self.base_ref = choice
            self.query_one("#base-ref", Button).label = branch_label(label)

    def attach(self, path: Path | None) -> None:
        """The picked file or folder as an @mention, where the cursor is in the description."""
        goal = self.query_one("#goal", DescriptionArea)
        if path is not None:
            goal.insert(f"@{shown_path(path)} ")
        goal.focus()

    def key_ctrl_s(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        suggestions = self.query_one("#suggestions", Suggestions)
        if suggestions.display:  # only the list: the goal you were typing stays
            suggestions.display = False
            return
        self.dismiss({})

    @on(OptionList.OptionSelected, "#suggestions")
    def suggestion_clicked(self, event: OptionList.OptionSelected) -> None:
        goal = self.query_one("#goal", DescriptionArea)
        goal.take(str(event.option.prompt))
        goal.focus()
