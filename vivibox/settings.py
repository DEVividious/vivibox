"""The settings screen (k) and a project's own screen (e on its row).

What a person changes often is a row here: "name · value", Enter opens the row's own picker or
field, and the file gets that one key written with its comments kept (configfile). What changes
rarely stays in the file, one row away. The machine's settings (where the tasks live, the address
pool) are shown, not edited: they move data.
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Label, OptionList, TextArea
from textual.widgets.option_list import Option

from . import actions, configfile, ide, look, opencode, ui
from . import init as project_init
from .config import (
    DEFAULT_NTFY_SERVER,
    ENV_NAME,
    JAVA,
    NTFY_LEVELS,
    NTFY_TOPIC,
    ORCHESTRATION_MODES,
    RESERVED_ENV,
    ROUNDS_HELP,
    ConfigError,
    config_dir,
    load_config,
    load_project,
)
from .dialogs import NOTHING_TO_PREPARE, PREPARE_HINT, PREPARE_QUESTION, ChooseEditor, ChooseModel
from .panel import edit_in_editor
from .providers_ui import ManageProviders, provider_rows
from .verify_ui import AskVerify
from .widgets import ContextHelp, Dialog, EdgeTextArea

# A row: what it is called, what it is now, and the key Enter acts on; None for a heading or a
# value that is only shown.
Row = tuple[str, str, str | None]


class Ask(Dialog):
    """One value on one line: Enter takes it, Escape leaves it as it is."""

    hint_keys = (("enter", "save"), look.ESC_CANCELS)

    def __init__(self, prompt: str, value: str, hint: str = ""):
        super().__init__()
        self.prompt, self.value, self.hint = prompt, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt, classes="wrap")
            yield Input(self.value, id="value", compact=True)
            if self.hint:
                yield Label(self.hint, classes="files wrap")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def typed(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def key_escape(self) -> None:
        self.dismiss(None)


class AskLines(Dialog):
    """A list, one entry per line: ctrl+s takes it, Escape leaves it as it is."""

    hint_keys = (("ctrl+s", "save"), look.ESC_CANCELS)

    def __init__(self, prompt: str, lines: list[str], hint: str = ""):
        super().__init__()
        self.prompt, self.lines, self.hint = prompt, lines, hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt, classes="wrap")
            yield EdgeTextArea("\n".join(self.lines), id="lines")
            if self.hint:
                yield Label(self.hint, classes="files wrap")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def key_ctrl_s(self) -> None:
        self.dismiss([line.strip() for line in self.query_one(TextArea).text.splitlines() if line.strip()])

    def key_escape(self) -> None:
        self.dismiss(None)


class Rows(Dialog):
    """Rows under headings; Enter opens the highlighted row's own picker, Escape closes. The list
    is drawn again after every change, so a row always says what the file says."""

    TITLE_TEXT = ""
    hint_keys = (("enter", "change"), look.ESC_CLOSES)
    # Rows that open a screen of their own rather than change a value: marked with ›.
    NAVIGATE = {"providers", "file"}
    # The widest the name column gets.
    NAME_WIDTH = 26

    def rows(self) -> list[Row]:
        raise NotImplementedError

    def open(self, key: str) -> None:
        raise NotImplementedError

    def about(self, key: str) -> str | look.Explained:
        """What the row does, in words, for the help under the list; "" for nothing to say; an
        Explained where it has facts to show."""
        return ""

    def compose(self) -> ComposeResult:
        self.frame_title = self.TITLE_TEXT
        with Vertical(classes="dialog"):
            yield OptionList(id="rows")
            # Read while choosing: a notification with the same words is gone before it is read.
            yield ContextHelp(id="about")

    # The help's lines of text by the terminal's height: all of a flow's from TALL lines up, three
    # (a flow in its short form) from SHORT, two below.
    TALL, SHORT = 40, 30
    # Around the list: the dialog's frame and padding, the help's blank row and rule, and the
    # row of keys with the blank row above it.
    AROUND = 2 + 2 + 2 + 2

    def on_resize(self) -> None:
        """The dialog's rectangle from the terminal alone: the help a fixed number of lines, the
        list what the screen leaves, scrolling inside it. Moving between rows changes what the
        help says, never the dialog's size."""
        height = self.size.height
        help_lines = 8 if height >= self.TALL else 3 if height >= self.SHORT else 2
        self.query_one(ContextHelp).reserve(help_lines, full=help_lines == 8)
        room = int(self.size.height * 0.9) - self.AROUND - help_lines
        self.query_one(OptionList).styles.max_height = max(4, room)

    @on(OptionList.OptionHighlighted)
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.explain(event.option_index)

    def explain(self, index: int | None) -> None:
        key = self.keys[index] if index is not None and index < len(self.keys) else None
        said = self.about(key) if key else ""
        self.query_one(ContextHelp).explain(said)

    def on_mount(self) -> None:
        self.fill()
        self.query_one(OptionList).focus()

    def fill(self) -> None:
        options = self.query_one(OptionList)
        was = options.highlighted
        options.clear_options()
        self.keys: list[str | None] = []
        rows = self.rows()
        width = min(
            max((len(label) for label, value, key in rows if value or key), default=0), self.NAME_WIDTH
        )
        # What a value has of the dialog's width once the frame, the indent, the name and the mark
        # have theirs: cut there, it never wraps under the names.
        room = max(12, min(90, self.app.size.width) - 2 - 4 - 1 - 2 - width - 2 - 3)
        for i, (label, value, key) in enumerate(rows):
            self.keys.append(key)
            if not value and key is None:
                # A section: its name as every heading is drawn, a blank line above all but the first.
                gap = "\n" if i else ""
                options.add_option(
                    Option(f"{gap}[b {look.SECONDARY}]{escape(label.upper())}[/]", disabled=True)
                )
            elif key is None:
                # Shown, not changed here: secondary, never the metadata gray, and nothing to open.
                options.add_option(
                    Option(f"  {look.secondary(label.ljust(width))}  {look.secondary(value)}", disabled=True)
                )
            else:
                mark = f"  {look.muted('›')}" if key in self.NAVIGATE else ""
                shown = escape(ui.shorten(value.strip(), room)) if value.strip() else ""
                # The name a step quieter than its value: the value is what you read.
                options.add_option(Option(f"  {look.secondary(label.ljust(width))}  {shown}{mark}"))
        first = next((i for i, key in enumerate(self.keys) if key), 0)
        options.highlighted = was if was is not None and was < len(self.keys) and self.keys[was] else first
        # The same row after a change says what it does now (the next orchestration mode).
        self.explain(options.highlighted)

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        if key := self.keys[event.option_index]:
            self.open(key)

    def key_escape(self) -> None:
        self.dismiss(None)

    def say(self, text: str) -> None:
        self.app.notify(text, timeout=6)

    def edit_file(self, path: Path) -> None:
        """The file itself, in your editor, for what has no row."""
        with self.app.suspend():
            edit_in_editor(path)


# The roles in the order they work, wherever they are listed.
ROLE_ORDER = {"planner": 0, "writer": 1, "reviewer": 2}
# What a row of k does, in the words of someone who has not read the docs.
ROLE_ABOUT = {
    "planner": "The planner reads the task and the repository and writes the plan you accept: "
    "the approach and the acceptance criteria. A strong model pays off here; a task can pick "
    "another one in n.",
    "writer": "The writer changes the code, commits, and fixes what the verification or the "
    "review sent back. It takes the most turns, so a cheaper model fits.",
    "reviewer": "The reviewer reads the work once the verification passed and writes notes; "
    "blocking ones send it back to the writer. Best on another model family than the writer's.",
}
SETTINGS_ABOUT = {
    "providers": "Where the models come from: the providers you have keys for, and the MCP servers "
    "the agents may use. A task cannot start without a provider for its models.",
    "add-reviewer": "No reviewer model of its own: the writer's model reviews. Enter picks one, best "
    "from another model family than the writer's.",
    "editor": "What o opens the review copy with, where the agent's work shows as uncommitted "
    "changes, like your own.",
    "notifications": "A desktop notification when a task needs you. Enter turns it on or off.",
    "ntfy": "Messages on your phone through ntfy: its app subscribes to this topic, a name nobody "
    "guesses. Empty: off.",
    "ntfy_server": "The ntfy server the messages go through: ntfy.sh, unless you run your own.",
    "max_rounds": "One round is one fix turn of the writer: after a failed verification, or after a "
    "review with blocking notes. The first implementation is not a round. When a task has used its "
    "rounds it stops for you, and your reply gives it as many again. A change applies from a "
    "task's next start.",
    "verify_timeout": "How long one verification command may run before it is stopped. Past it "
    "the task waits for you, and no round is spent.",
    "whole_build": "For a project verified by module ({modules} in its verification): the "
    "whole project is built once more before the work comes to you; red, it goes back to the "
    "writer. Off: only the modules the plan names, and the pipeline builds the rest after a push. "
    "A change applies from a task's next verification.",
    "cost_warning": "When a task has cost this many dollars, you are told once and the task goes "
    "on. None: you are never told. A change applies from a task's next turn.",
    "cost_limit": "When a task has cost this many dollars, it stops before its next turn and waits "
    "for you. None: no limit. A change applies from a task's next turn.",
    "file": "config.toml in your editor, for what has no row here.",
}
# The same for a project's rows (e on its row).
PROJECT_ABOUT = {
    "prepare": "What a new task's clone runs once while the plan is made, usually a build "
    "without tests, so the writer starts on a built project. The writer's first turn waits for it.",
    "verify": "The command that proves the work: the gate runs it on a fresh clone of the "
    "commits after every turn of the writer. Empty: the next task's writer proposes one. With "
    "{modules} in it (mvn -B -pl {modules} -am verify), a task's plan names the modules it changes "
    "and the gate builds those; work outside them, the same command without them. A change "
    "applies from the next verification, in every task.",
    "demo": "How v runs the project in its pod so you can look at it.",
    "java": "The JDK this project builds with, when not the image's Java 21, e.g. 17 for an older "
    "Gradle. A change applies from a task's next start.",
    "pass_env": "Variables the build needs from your shell, such as a package registry token: "
    "the agent and the gate get their values from the shell vivibox was started in.",
    "editor": "What o opens this project's review copies with, when not config.toml's.",
    "file": "The project file in your editor, for services on your host the agent may reach "
    "(host_services), extra risky files (risky_extra) and toolchains (tools).",
}


def config_path() -> Path:
    return config_dir() / "config.toml"


# The row for what o opens, in the settings and on a project: the tool, not the key alone.
EDITOR_LABEL = "IDE / text editor (o)"


def duration(seconds: int) -> str:
    """A time limit as a person reads it: minutes from a minute up, else seconds."""
    return f"{seconds / 60:g} min" if seconds >= 60 else f"{seconds} s"


def parse_duration(text: str) -> int:
    """Minutes as "30m", seconds as "1800" or "45s"; 0 for anything else."""
    text = text.strip().lower()
    try:
        if text.endswith("m"):
            return round(float(text[:-1]) * 60)
        return int(text.removesuffix("s"))
    except ValueError:
        return 0


class Settings(Rows):
    """Providers & MCP first, since a task cannot start without a model; then the roles' defaults,
    the review, the limits, and the machine's own settings, shown only."""

    TITLE_TEXT = "Settings"

    def rows(self) -> list[Row]:
        config = self.app.config
        on = [name for _, name, _, enabled in provider_rows() if enabled]
        editor = config.ide or self.found_editor()
        return [
            ("Providers & MCP", "", None),
            ("providers", f"{len(on)} on: {', '.join(on)}" if on else "none yet", "providers"),
            # The flow first, then the roles in the order they work, as under n.
            ("Default workflow", "", None),
            ("flow", ORCHESTRATION_MODES[config.orchestration].label, "orchestration"),
            *(
                (
                    name,
                    actions.choice_label(
                        actions.configured_choice(config, name), available=self.app.available
                    ),
                    f"role:{name}",
                )
                for name in sorted(config.roles, key=lambda n: (ROLE_ORDER.get(n, 9), n))
            ),
            *(
                []
                if "reviewer" in config.roles
                else [
                    (
                        "reviewer",
                        "the writer's model: Enter picks one, on another family than the writer",
                        "add-reviewer",
                    )
                ]
            ),
            ("Review copy", "", None),
            (
                EDITOR_LABEL,
                editor if config.ide else f"{editor} (found here)" if editor else "none found",
                "editor",
            ),
            ("Notifications", "", None),
            ("desktop notifications", "on" if config.desktop_notifications else "off", "notifications"),
            ("ntfy topic", config.ntfy or "off", "ntfy"),
            ("ntfy server", config.ntfy_server, "ntfy_server"),
            ("ntfy events", config.ntfy_events, "ntfy_events"),
            ("Limits", "", None),
            ("rounds", str(config.max_rounds), "max_rounds"),
            ("verification timeout", duration(config.verify_timeout), "verify_timeout"),
            ("whole build before review", "on" if config.whole_build_before_review else "off", "whole_build"),
            (
                "cost warning",
                f"${config.cost_warning:.2f}" if config.cost_warning else "none",
                "cost_warning",
            ),
            ("cost limit", f"${config.cost_limit:.2f}" if config.cost_limit else "none", "cost_limit"),
            ("Machine, in config.toml", "", None),
            ("tasks folder", str(config.tasks_dir), None),
            ("network pool", config.network_pool, None),
            ("config.toml", "open in your editor", "file"),
        ]

    def about(self, key: str) -> str | look.Explained:
        config = self.app.config
        if key == "orchestration":
            # The same as under n's Flow, on config.toml's models; the help draws it in two lines
            # on a short terminal.
            models = {
                name: actions.choice_label(actions.configured_choice(config, name)) for name in config.roles
            }
            return look.flow(config.orchestration, models, config.max_rounds)
        if key == "ntfy_events":
            return (
                "What your phone is told: decisions, when a task needs you (a plan, the work, a "
                "question, a failure); all, every stage too. Enter switches between them."
            )
        if key.startswith("role:"):
            return ROLE_ABOUT.get(key.removeprefix("role:"), "")
        return SETTINGS_ABOUT.get(key, "")

    @staticmethod
    def found_editor() -> str:
        found = ide.candidates()
        return found[0].command if found else ""

    def reread(self) -> None:
        try:
            self.app.config = load_config()
        except ConfigError as e:
            self.app.fail(e)
        self.fill()

    def write(self, key: str, value: object, table: str) -> None:
        """The row shows the new value, and the line under the list when it applies: no
        notification says it again."""
        configfile.set_value(config_path(), key, value, table)
        self.reread()

    def open(self, key: str) -> None:
        config = self.app.config
        if key == "providers":
            self.app.push_screen(ManageProviders(), lambda _: self.fill())
        elif key.startswith("role:"):
            role = key.removeprefix("role:")
            configured = actions.configured_choice(config, role)
            offered = actions.choices(role, config, self.app.available)

            def picked(choice) -> None:
                if choice is None or (not choice[1] and choice[0] != "manual"):
                    return
                configfile.set_value(config_path(), "harness", choice[0], f"roles.{role}")
                self.write(
                    "model",
                    choice[1],
                    f"roles.{role}",
                )

            self.app.push_screen(
                ChooseModel(role, offered, configured, configured, self.app.available), picked
            )
        elif key == "editor":
            found = ide.candidates()
            if not found:
                self.say('No editor found here; set [review] ide in config.toml, e.g. "code {path}".')
                return
            self.app.push_screen(
                ChooseEditor(found),
                lambda command: command and self.write("ide", command, "review"),
            )
        elif key == "notifications":
            on = not config.desktop_notifications
            self.write("desktop", on, "notifications")
        elif key == "whole_build":
            self.write("whole_build_before_review", not config.whole_build_before_review, "limits")
        elif key == "ntfy":

            def typed(value: str | None) -> None:
                if value is None:
                    return
                if value.startswith(("https://", "http://")):  # the address, pasted: both at once
                    server, _, value = value.rstrip("/").rpartition("/")
                    configfile.set_value(config_path(), "ntfy_server", server, "notifications")
                if value and not NTFY_TOPIC.match(value):
                    self.say("A topic is a name: letters, digits, - and _.")
                    return
                self.write("ntfy", value, "notifications")

            self.app.push_screen(
                Ask(
                    "Name of the ntfy topic the supervisor's messages go to (empty turns it off):",
                    config.ntfy,
                    "One nobody guesses; the app on your phone subscribes to it. A token, if the topic "
                    "needs one: vivibox auth set ntfy",
                ),
                typed,
            )
        elif key == "ntfy_server":

            def server_typed(value: str | None) -> None:
                if value is None:
                    return
                if not value.startswith(("https://", "http://")):
                    self.say(f"The server is an address, e.g. {DEFAULT_NTFY_SERVER}.")
                    return
                self.write("ntfy_server", value.rstrip("/"), "notifications")

            self.app.push_screen(
                Ask("Address of the ntfy server:", config.ntfy_server, "ntfy.sh, or a server of your own."),
                server_typed,
            )
        elif key == "ntfy_events":
            level = NTFY_LEVELS[(NTFY_LEVELS.index(config.ntfy_events) + 1) % len(NTFY_LEVELS)]
            self.write("ntfy_events", level, "notifications")
        elif key == "add-reviewer":
            offered = [(opencode.NAME, m) for models in (self.app.available or {}).values() for m in models]
            if not offered:
                self.say(
                    "No models to pick from yet: add a provider first, or put [roles.reviewer] in "
                    "config.toml."
                )
                return

            def picked(choice) -> None:
                if choice is None or not choice[1]:
                    return
                configfile.set_value(config_path(), "harness", choice[0], "roles.reviewer")
                self.write("model", choice[1], "roles.reviewer")

            self.app.push_screen(
                ChooseModel("reviewer", offered, None, offered[0], self.app.available), picked
            )
        elif key == "orchestration":
            names = list(ORCHESTRATION_MODES)
            name = names[(names.index(config.orchestration) + 1) % len(names)]
            # What the mode means is under the list, on the row it stays on: nothing to notify.
            configfile.set_value(config_path(), "agent_orchestration_mode", name, "")
            self.reread()
        elif key == "max_rounds":

            def rounds_typed(value: str | None) -> None:
                if value is None:
                    return
                if not value.isdigit() or int(value) < 1:
                    self.say("Rounds is a whole number of at least 1.")
                    return
                self.write(key, int(value), "limits")

            self.app.push_screen(Ask(ROUNDS_HELP, str(config.max_rounds)), rounds_typed)
        elif key == "verify_timeout":

            def timeout_typed(value: str | None) -> None:
                if value is None:
                    return
                seconds = parse_duration(value)
                if seconds < 1:
                    self.say("The timeout is minutes (30m) or seconds (1800), at least 1 second.")
                    return
                self.write(key, seconds, "limits")

            self.app.push_screen(
                Ask(
                    "How long one verification command may run before it is stopped:",
                    duration(config.verify_timeout),
                    "Minutes as 30m, or seconds as 1800. Past it the task waits for you, as on any"
                    " failure outside the code.",
                ),
                timeout_typed,
            )
        elif key in ("cost_warning", "cost_limit"):
            prompts = {
                "cost_warning": "Dollars a task may cost before you are told (0 for none):",
                "cost_limit": "Dollars a task may cost before it stops for you (0 for none):",
            }
            now = config.cost_warning if key == "cost_warning" else config.cost_limit

            def dollars(value: str | None) -> None:
                if value is None:
                    return
                try:
                    amount = float(value)
                except ValueError:
                    amount = -1.0
                if amount < 0:
                    self.say(f"{key} is dollars, e.g. 2.5; 0 for none.")
                    return
                amount = int(amount) if amount == int(amount) else amount
                self.write(key, amount, "limits")

            self.app.push_screen(Ask(prompts[key], f"{now:g}"), dollars)
        elif key == "file":
            self.edit_file(config_path())
            self.reread()


class ProjectSettings(Rows):
    """What belongs to the project: how it is verified and run, its JDK, what its build needs from
    your shell, and the editor for its review copies. The rest is in its file, the last row."""

    def __init__(self, name: str):
        super().__init__()
        self.project_name = name
        self.TITLE_TEXT = f"Project · {name}"

    def path(self) -> Path:
        return config_dir() / "projects" / f"{self.project_name}.toml"

    def about(self, key: str) -> str:
        return PROJECT_ABOUT.get(key, "")

    def rows(self) -> list[Row]:
        project = load_project(self.project_name)
        config = self.app.config
        if project.no_build:
            verify = actions.NO_BUILD
        elif project.verify:
            verify = ", ".join(project.verify)
        else:
            verify = actions.WRITER_PROPOSES
        machine = config.ide or Settings.found_editor()
        return [
            ("preparation", " && ".join(project.prepare) or NOTHING_TO_PREPARE, "prepare"),
            ("verification", verify, "verify"),
            (
                "run app (v)",
                ", ".join(project.demo) or "worked out from the repository, or asked of the agent",
                "demo",
            ),
            ("java", project.java or "21, the image's", "java"),
            ("variables", ", ".join(project.pass_env) or "nothing from your shell", "pass_env"),
            (
                EDITOR_LABEL,
                project.ide or f"config.toml's: {machine}" if machine else "none found",
                "editor",
            ),
            ("project file", "open in your editor", "file"),
        ]

    def changed(self) -> None:
        """The list redraws: verify, pass_env, the repository, any of it may have changed."""
        self.app.drawn = ()
        self.app.reload()
        self.fill()

    def write(self, key: str, value: object) -> None:
        """As in the settings: the row says it, not a notification."""
        configfile.set_value(self.path(), key, value)
        self.changed()

    def open(self, key: str) -> None:
        project = load_project(self.project_name)
        if key == "verify":

            def chosen(choice: dict) -> None:
                if not choice:
                    return
                actions.save_verify(project, choice["verify"], choice["no_build"])
                self.changed()

            found = project_init.modules(project.repo)
            self.app.push_screen(
                AskVerify(
                    self.project_name, project.verify, project.no_build, modules=found, repo=project.repo
                ),
                chosen,
            )
        elif key == "demo":
            self.app.push_screen(
                AskLines(
                    f"How to run {self.project_name} for v, one command per line, the app itself last",
                    project.demo,
                    "Empty: vivibox works it out from the repository, or asks the agent once.",
                ),
                lambda lines: lines is not None and self.write("demo", lines),
            )
        elif key == "prepare":

            def typed(value: str | None) -> None:
                if value is None:
                    return
                self.write("prepare", [value] if value else [])

            # An empty field starts on what the build files suggest, for Enter to take.
            suggested = " && ".join(project.prepare or project_init.prepare_suggestion(project.repo))
            self.app.push_screen(Ask(PREPARE_QUESTION, suggested, PREPARE_HINT), typed)
        elif key == "java":

            def typed(value: str | None) -> None:
                if value is None:
                    return
                if not JAVA.match(value):
                    self.say('java must look like "17" or "temurin-17", or be empty for 21.')
                    return
                self.write("java", value)

            self.app.push_screen(
                Ask(
                    "A JDK other than the image's Java 21, as a mise version (17 is Corretto 17); empty: 21:",
                    project.java,
                ),
                typed,
            )
        elif key == "pass_env":

            def typed(value: str | None) -> None:
                if value is None:
                    return
                names = value.replace(",", " ").split()
                if bad := [n for n in names if not ENV_NAME.match(n)]:
                    self.say(f"not a variable name: {', '.join(bad)}")
                    return
                if reserved := sorted(set(names) & RESERVED_ENV):
                    self.say(f"vivibox sets {', '.join(reserved)} in the pod itself; leave them out.")
                    return
                self.write("pass_env", names)

            self.app.push_screen(
                Ask(
                    "Variables the build needs from your shell, separated by spaces:",
                    " ".join(project.pass_env),
                    "Their values come from the shell you start vivibox in; a task waits while one is unset.",
                ),
                typed,
            )
        elif key == "editor":
            found = ide.candidates()
            if not found:
                self.say('No editor found here; set ide in the project file, e.g. "code {path}".')
                return
            self.app.push_screen(
                ChooseEditor(found, first=("the one in config.toml", "")),
                lambda command: command is not None and self.write("ide", command),
            )
        elif key == "file":
            self.app.edit_project_file()
            self.fill()
