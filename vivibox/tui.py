"""Interactive view: 'vivibox' with no arguments. Your tasks, live, and your decisions one key away.

Every action calls the same functions as the command line (actions.py); this module only shows state
and asks. Slow steps (starting a pod, accepting work) run in threads so the view stays responsive.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    TextArea,
)

from . import actions, context, gate, ide, supervisor, ui
from .config import ConfigError, load_config
from .plan import PlanError, parse_plan
from .plan import body as plan_body
from .states import State
from .task import Task, TaskState, list_tasks

REFRESH_SECONDS = 2.0
SPIN_SECONDS = 0.1
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PROTECTED_BRANCHES = ("main", "master")


def criteria(task: Task) -> str:
    try:
        if (task.meta / gate.ACCEPTED_PLAN).exists():
            total = len(parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria)
            return f"{total - len(gate.missing_criteria(task))}/{total}"
        return f"0/{len(parse_plan(task.plan_path.read_text()).criteria)}"
    except (OSError, PlanError, gate.GateError):
        return "-"


def checklist(task: Task) -> list[str]:
    """Every criterion of the accepted plan, and which of them the agent reports as met."""
    try:
        wanted = [c.text for c in parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria]
        missing = set(gate.missing_criteria(task))
    except (OSError, PlanError, gate.GateError):
        return []
    return [f"- {'☐' if text in missing else '☑'} {text}" for text in wanted]


@dataclass
class PodView:
    """What the pod is doing right now: asked once, used for both the panel and the keys."""

    address: str = ""
    listening: list = field(default_factory=list)
    demo: bool = False
    log: str = ""

    @property
    def reachable(self) -> list:
        """The ports something outside the pod can actually open."""
        return [p for p in self.listening if p.reachable]

    @property
    def state(self) -> str:
        """What became of the demo, named once so the row and the panel cannot drift apart.
        `local` is its own state and not `starting`: nothing is coming, the app bound the wrong
        interface, and waiting for it is waiting forever."""
        if not self.demo:
            return "stopped" if self.log else ""
        if self.reachable:
            return "live"
        return "local" if self.listening else "starting"

    def lines(self) -> list[str]:
        """What the pod is doing, in the states that can be told apart from outside it."""
        if not self.address:
            return []
        where = [f"[{self.address}:{p.port}](http://{self.address}:{p.port})" for p in self.reachable]
        shown = ", ".join(where)
        closed = ", ".join(f"`{p.port}`" for p in self.listening if not p.reachable)
        state = {
            "live": f"running · open at {shown}",
            "local": f"running, but {closed} is bound to localhost and nothing outside can reach it",
            "starting": "running, nothing listening yet",
        }.get(self.state, "not running")
        out = [f"Pod `{self.address}` · demo {state}"]
        if not self.demo and self.log:
            # It ran and is gone. What it said last is the only thing that explains why.
            out += ["", "It stopped. Its last output:", "", f"```\n{self.log}\n```"]
        return out


def pod_views(task_ids: list[str]) -> dict[str, PodView]:
    """What every task's pod is doing. One question for all the addresses, then one per pod that is
    up; asking each pod separately for each thing is what would make this too slow to do often."""
    pods = {task_id: actions.Pod(task_id, Path("."), "") for task_id in task_ids}
    found = actions.pod_module.addresses([pod.sidecar for pod in pods.values()])
    views = {}
    for task_id, pod in pods.items():
        if not (address := found.get(pod.sidecar, "")):
            views[task_id] = PodView()
            continue
        probe = pod.probe()
        views[task_id] = PodView(address, probe.listening, probe.demo, probe.log)
    return views


def pod_view(task_id: str) -> PodView:
    return pod_views([task_id])[task_id]


def last_gate(task: Task) -> str:
    """How the last gate run went, so a checklist that has not moved still shows whether work has."""
    for event in reversed(task.events()):
        if event["type"] == "gate":
            outcome = "passed" if event["data"].get("passed") else "failed"
            return f"Gate {outcome} at {event['ts'][11:19]}."
    return "The gate has not run yet."


def projects() -> list[str]:
    """The projects you can work in; one whose repository is gone is not offered."""
    broken = actions.broken_projects()
    return [p.stem for p in actions.project_files() if p.stem not in broken]


def read(path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def finished_detail(entry: dict) -> str:
    """A task you accepted: what it left in your repository."""
    if entry["conflicts"]:
        where = f"with conflicts in {', '.join(entry['conflicts'])}, also on `{entry['branch']}`"
    elif entry["branch"]:
        where = f"on branch `{entry['branch']}`"
    else:
        where = "in your checkout"
    # Tasks accepted before criteria were kept have none; say so rather than show an empty list.
    met = entry.get("criteria")
    delivered = (
        ["**It was accepted as meeting:**", "", *(f"- ☑ {text}" for text in met), ""]
        if met
        else ["*Its criteria were not recorded; it finished before vivibox kept them.*", ""]
    )
    return "\n".join(
        [
            f"### {entry['id']} · done",
            "",
            f"*{entry['project']} · {ui.ago(entry['finished'])} · ${entry['cost']:.2f}*",
            "",
            entry["title"],
            "",
            f"Its work is {where}, from commit `{entry['commit']}`.",
            "",
            *delivered,
            "Press `x` to forget it, `h` to hide finished tasks.",
        ]
    )


def detail(
    task: Task, st: TaskState, max_iterations: int, running: bool = True, pod: PodView | None = None
) -> str:
    """What you need to decide on this task, as markdown."""
    head = [
        f"### {st.id} · {ui.activity(st, max_iterations)}",
        "",
        f"*criteria {criteria(task)} · updated {ui.ago(st.updated)} · ${ui.cost(task):.2f}*",
        "",
    ]
    if shown := (pod if pod is not None else pod_view(st.id)).lines():
        head += [*shown, ""]
    handoff = task.meta / "handoff"
    if not running and st.state in (State.PLAN, State.IMPLEMENT, State.VERIFY):
        head += [
            "**The agent is not working on this task.** Press `s`; it goes on from where it was.",
            "",
        ]
    if st.state in (State.PLAN, State.CHECKPOINT_PLAN):
        body = [plan_body(read(task.plan_path))]
    elif st.state is State.CHECKPOINT_FINAL:
        try:
            _, project = actions.load(st.id)
            copy = actions.repo.review_worktree_path(project.repo, task.root)
            stat = actions.changed_files(task, project)
        except Exception as e:  # shown, not fatal: the view must keep working
            copy, stat = "?", f"({e})"
        body = [
            "**Ready for your review.** Press `o` to open it in your IDE, where the agent's work shows",
            "as uncommitted changes; `a` accepts it into your checkout, `r` asks for changes.",
            "",
            f"Review copy: `{copy}`",
            "",
            f"```\n{stat.rstrip() or 'no changes fetched yet'}\n```",
        ]
    elif st.state is State.APPROVAL_RISKY:
        try:
            diffs = actions.risky_diffs(task, actions.load(st.id)[1])
        except Exception as e:
            diffs = [str(e)]
        body = [
            "**Risky files changed.** They run code on your machine when your IDE imports the project.",
            "`p` approves them as shown, `r` sends the agent back with your comment.",
            "",
            *(f"```diff\n{d.rstrip()}\n```" for d in diffs),
        ]
    elif st.state is State.CHECKPOINT_BLOCKED:
        question = read(handoff / supervisor.QUESTION)
        body = (
            ["**The agent asks:**", "", question, "", "Answer with `r`."]
            if question
            else [
                "**Verification keeps failing.**",
                "",
                read(handoff / "verify-feedback.md"),
                "",
                "Help with `r`, or look at the agent with `w`.",
            ]  # fmt: skip
        )
    elif items := checklist(task):
        # What the task is still short of. The agent ticks these itself and the gate only checks
        # that none is left open, so a tick is what the agent claims, not something vivibox saw.
        body = [
            "**Acceptance criteria**, as the agent reports them:",
            "",
            *items,
            "",
            f"{last_gate(task)} Look at the agent with `w`.",
        ]
    else:
        events = task.events()[-8:]
        body = ["**Recent events**", ""] + [
            f"- `{e['ts'][11:19]}` {e['type']} "
            + " ".join(
                f"{k}={v}" for k, v in e["data"].items() if k in ("current", "reason", "passed", "cost")
            )
            for e in events
        ]
    return "\n".join(head + body)


# --- dialogs ------------------------------------------------------------------------------------


class Dialog(ModalScreen):
    """Arrow keys move between fields and buttons wherever the focused field does not use them
    itself: left and right move the cursor in a text field, up and down open a list."""

    BINDINGS = [
        Binding("up", "app.focus_previous", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("right", "app.focus_next", show=False),
    ]


class Confirm(Dialog):
    def __init__(self, question: str, yes: str = "Yes"):
        super().__init__()
        self.question, self.yes = question, yes

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="error", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class Reply(Dialog):
    def __init__(self, task_id: str, prompt: str = ""):
        super().__init__()
        self.task_id = task_id
        self.prompt = prompt or f"Your comment for the agent on {task_id}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.prompt} (ctrl+s sends):")
            yield EdgeTextArea(id="comment")
            with Horizontal(classes="buttons"):
                yield Button("Send", variant="primary", id="send")
                yield Button("Cancel", id="cancel")

    def key_ctrl_s(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def key_escape(self) -> None:
        self.dismiss("")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(TextArea).text if event.button.id == "send" else "")


MENTION_AT_CURSOR = re.compile(r"(?:^|\s)@(\S*)$")


def leave_at_edge(area: TextArea, event: events.Key) -> bool:
    """Up on the first line or down on the last moves on to the previous or next field."""
    row, _ = area.cursor_location
    if (event.key == "up" and row == 0) or (event.key == "down" and row == area.document.line_count - 1):
        event.stop()
        event.prevent_default()
        (area.screen.focus_previous if event.key == "up" else area.screen.focus_next)()
        return True
    return False


class EdgeTextArea(TextArea):
    async def _on_key(self, event: events.Key) -> None:
        if not leave_at_edge(self, event):
            await super()._on_key(event)


class DescriptionArea(TextArea):
    """A text area that suggests paths after '@', like Claude Code: arrows pick, Tab or Enter take one,
    Escape closes the list."""

    def __init__(self, suggestions: OptionList, cwd: Path, **kwargs):
        super().__init__(**kwargs)
        self.suggestions, self.cwd = suggestions, cwd

    def mention(self) -> str | None:
        row, col = self.cursor_location
        m = MENTION_AT_CURSOR.search(self.document.get_line(row)[:col])
        return m.group(1) if m else None

    def suggest(self) -> None:
        partial = self.mention()
        found = context.complete(partial, self.cwd) if partial is not None else []
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


NEW_PROJECT = "+ set up a project…"


class PathSuggester(Suggester):
    """Completes a folder as you type it, like a shell; the right arrow takes the suggestion."""

    def __init__(self):
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        found = context.complete(value, Path.cwd()) if value else []
        return found[0] if found else None


class NewProject(Dialog):
    """A repository vivibox does not know yet, or a folder where one should start. How to build and
    test it is detected, or left to the first plan you accept; it is not asked here."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Folder of the project. This is where you started vivibox; type another")
            yield Label("path to use a different one, existing or new.")
            yield Input(str(Path.cwd()), suggester=PathSuggester(), id="path")
            yield Label("Name")
            yield Input(id="name")
            yield Label("", id="notes")
            with Horizontal(classes="buttons"):
                yield Button("Set up", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.look(Path.cwd())
        self.query_one("#path", Input).focus()

    @on(Input.Changed, "#path")
    def typed(self, event: Input.Changed) -> None:
        self.look(Path(event.value.strip() or ".").expanduser())

    def look(self, where: Path) -> None:
        """Following the folder you point at: its name, and what setting it up would mean."""
        found = actions.propose_project(where)
        root = actions.git_root(where)
        self.taken = actions.project_at(root) if root else ""
        name = self.query_one("#name", Input)
        name.value = self.taken or found.name
        name.disabled = bool(self.taken)
        if self.taken:
            notes = [f"already a project: {self.taken}; Set up opens a task for it instead"]
        elif root:
            notes = [f"repository {root}", *(found.verify or ["no build found"]), *found.notes]
        else:
            notes = [f"a new repository starts in {where}", "the first plan you accept sets how to test it"]
        self.query_one("#notes", Label).update(" · ".join(notes))
        self.query_one("#create", Button).label = "Open a task" if self.taken else "Set up"

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "create":
            self.dismiss({})
        elif self.taken:
            self.dismiss({"use": self.taken})
        else:
            where = Path(self.query_one("#path", Input).value.strip()).expanduser()
            name = self.query_one("#name", Input).value.strip()
            self.dismiss({"path": str(where), "name": name, "verify": actions.propose_project(where).verify})

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})


class ChooseEditor(ModalScreen[str]):
    """What to open review copies with, asked once: arrows pick, Enter takes, Escape leaves it."""

    def __init__(self, found: list[ide.Editor]):
        super().__init__()
        self.found = found

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Open the review copy with:")
            options = OptionList(*[e.label for e in self.found], id="editors")
            yield options
            yield Label("Your choice is kept in config.toml; change it there, or per project.")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.found[event.option_index].command)

    def key_escape(self) -> None:
        self.dismiss("")


class NewTask(Dialog):
    def __init__(self, preselect: str = ""):
        super().__init__()
        self.preselect = preselect

    def compose(self) -> ComposeResult:
        names = projects()
        chosen = self.preselect if self.preselect in names else names[0]
        with Vertical(classes="dialog"):
            if len(names) == 1:
                yield Label(f"New task in {chosen}")
            else:
                yield Label("Project")
                yield Select([(n, n) for n in names], value=chosen, allow_blank=False, id="project")
            yield Select(
                [("Feature: new behaviour", "feature"), ("Bug: something works wrong", "bug"),
                 ("Other: refactoring, tests, upkeep", "other")],
                value="feature", allow_blank=False, id="kind",
            )  # fmt: skip
            yield Label("What should the agent do? The first line is the task's title; below it, as much as")
            yield Label("you like: the ticket, context, constraints. @~/path/file.md hands the agent a copy")
            yield Label("of a file or folder. ctrl+s creates the task.")
            suggestions = OptionList(id="suggestions")
            suggestions.display = False
            yield DescriptionArea(suggestions, Path.cwd(), id="goal", classes="description")
            yield suggestions
            yield Checkbox("Accept the agent's plan without stopping (--auto)", id="auto")
            yield Checkbox("Only create it, to write the plan myself (--draft)", id="draft")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Set up another project…", id="project-setup")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "create":
            self.dismiss({"project": NEW_PROJECT} if event.button.id == "project-setup" else {})
            return
        self.dismiss(
            {
                "project": self.query_one("#project", Select).value if len(projects()) > 1 else projects()[0],
                "goal": self.query_one("#goal", TextArea).text.strip(),
                "kind": self.query_one("#kind", Select).value,
                "auto": self.query_one("#auto", Checkbox).value,
                "draft": self.query_one("#draft", Checkbox).value,
            }
        )

    def on_mount(self) -> None:
        self.for_project(self.query_one("#project", Select).value if len(projects()) > 1 else projects()[0])
        self.query_one("#goal", TextArea).focus()

    @on(Select.Changed, "#project")
    def switched(self, event: Select.Changed) -> None:
        self.for_project(str(event.value))

    def for_project(self, name: str) -> None:
        """An empty project has nothing that could work wrong, so what kind of task this is is not asked."""
        self.query_one("#kind", Select).display = not actions.empty_project(name)

    def key_ctrl_s(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})


class CommitWork(Dialog):
    """After accepting: the work is staged in your checkout; commit it now, or leave it for your IDE."""

    def __init__(self, done: actions.Finished):
        super().__init__()
        self.done = done
        self.branch = actions.current_branch(done.source)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.done.task_id} is done. Its work is uncommitted in {self.done.source}:")
            yield Label(self.done.status.rstrip() or "(no changes)", classes="files")
            warn = " (your main branch)" if self.branch in PROTECTED_BRANCHES else ""
            yield Label(f"Commit to {self.branch}{warn} with this message?")
            yield Input(self.done.message, id="message")
            with Horizontal(classes="buttons"):
                # On a main branch the safe choice comes first.
                commit = Button("Commit", variant="primary", id="commit")
                later = Button("Leave uncommitted", id="later")
                yield from ((later, commit) if warn else (commit, later))

    def on_mount(self) -> None:
        self.query_one("#later" if self.branch in PROTECTED_BRANCHES else "#message").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if event.button.id == "commit" else "")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.dismiss(self.query_one(Input).value)

    def key_escape(self) -> None:
        self.dismiss("")


# --- the view -----------------------------------------------------------------------------------

WAITING_ONLY = {State.CHECKPOINT_PLAN, State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED, State.APPROVAL_RISKY}


class LiveFooter(Footer):
    """Textual's Footer stops redrawing while the terminal has no focus (bindings_changed in
    widgets/_footer.py returns early). The keys are what tells you a task now needs you, so they
    must appear while you are in another window, not once you click back into the terminal."""

    def bindings_changed(self, screen) -> None:
        self._bindings_ready = True
        if self.is_attached and screen is self.screen:
            self.call_after_refresh(self.recompose)


class Vivibox(App):
    TITLE = "vivibox"
    CSS = """
    DataTable { height: 1fr; }
    #detail { height: 60%; border-top: solid $primary; padding: 0 1; }
    #detail.hidden { display: none; }
    .dialog { width: 90; height: auto; max-height: 90%; border: thick $primary; background: $surface;
              padding: 1 2; }
    .dialog TextArea { height: 8; }
    .dialog TextArea.description { height: 16; }
    #suggestions { max-height: 8; border: none; background: $boost; }
    #editors { max-height: 12; margin: 1 0; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    .files { color: $text-muted; margin: 1 0; }
    Confirm, Reply, NewTask, NewProject, CommitWork, ChooseEditor { align: center middle; }
    """
    BINDINGS = [
        Binding("d", "details", "Details"),
        Binding("h", "toggle_done", "Show/hide done"),
        Binding("a", "accept", "Accept"),
        Binding("r", "reply", "Reply"),
        Binding("e", "edit_plan", "Edit plan"),
        Binding("o", "open_ide", "Open in IDE"),
        Binding("p", "approve_risky", "Approve risky"),
        Binding("w", "watch", "Watch agent"),
        Binding("v", "demo", "Run it"),
        Binding("v", "demo_stop", "Stop it"),
        # One key, two meanings: the footer shows the one that applies to the selected task.
        Binding("s", "start_task", "Start"),
        Binding("s", "stop_task", "Stop"),
        Binding("n", "new", "New task"),
        Binding("i", "new_project", "New project"),
        Binding("P", "new_project", "New project", show=False),
        Binding("x", "remove", "Remove"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.shown = ""
        self.pairs: list[tuple[Task, TaskState]] = []
        self.done: list[dict] = []
        self.show_done = True
        self.frame = 0
        self.table: DataTable = None  # type: ignore[assignment]  # set when the view mounts
        self.running: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="detail", classes="hidden"):
            yield Markdown("", id="detail-text")
        yield LiveFooter()

    def on_mount(self) -> None:
        # Kept by hand: a dialog on top changes what a query would find, and the timers keep running.
        self.pods: dict[str, PodView] = {}  # what each task's pod is doing, refreshed off the loop
        self.waiting = self.working = 0
        self.table = self.query_one(DataTable)
        self.panel = self.query_one("#detail")
        self.text = self.query_one("#detail-text", Markdown)
        table = self.table
        columns = ("TASK", "STATUS", "DEMO", "CRITERIA", "COST", "CREATED", "UPDATED", "GOAL")
        keys = table.add_columns(*columns)
        self.status_column, self.demo_column = keys[1], keys[2]
        self.reload()
        self.set_interval(SPIN_SECONDS, self.spin)
        self.set_interval(REFRESH_SECONDS, self.reload)
        self.call_after_refresh(self.check_projects)

    def check_projects(self) -> None:
        """A project whose repository is gone is offered for removal; then, if none is left, set one up."""
        broken = actions.broken_projects()
        if not broken:
            if not projects():
                # Nothing to work on yet: the first thing to do is point vivibox at a repository.
                self.new_project()
            return
        listed = "\n".join(f"{name}: {why}" for name, why in broken.items())
        question = f"These projects cannot be worked in:\n\n{listed}\n\nForget them?"

        def answered(yes: bool) -> None:
            for name in broken if yes else ():
                try:
                    actions.forget_project(name)
                except ConfigError as e:
                    self.fail(e)
            if not projects():
                self.new_project()

        self.push_screen(Confirm(question, "Forget"), answered)

    # --- data ---

    def reload(self) -> None:
        """Re-reads every task; the only place that does, so key checks stay cheap."""
        table = self.table
        selected = self.selected_id()
        pairs = [(t, t.read_state()) for t in list_tasks(self.config.tasks_dir)]
        self.pairs = pairs = sorted(pairs, key=lambda p: ui.ORDER.index(ui.group(p[1])))
        self.running = {st.id for task, st in pairs if actions.supervisor_running(task)}
        table.clear()
        for task, st in pairs:
            spent = ui.cost(task)
            table.add_row(
                st.id, self.status(st), self.demo_cell(st.id), criteria(task),
                f"${spent:.2f}" if spent else "-",
                ui.ago(st.created), ui.ago(st.updated), st.goal, key=st.id,
            )  # fmt: skip
        live = {st.id for _, st in pairs}
        self.done = [e for e in actions.history() if e["id"] not in live] if self.show_done else []
        for entry in self.done:
            table.add_row(
                entry["id"], "[green]  done[/]", "-", "-", f"${entry['cost']:.2f}",
                ui.ago(entry["created"]) if entry.get("created") else "-",
                ui.ago(entry["finished"]), entry["title"], key=entry["id"],
            )  # fmt: skip
        ids = [st.id for _, st in pairs] + [e["id"] for e in self.done]
        if selected in ids:
            table.move_cursor(row=ids.index(selected))
        self.waiting = sum(st.state in WAITING_ONLY for _, st in pairs)
        self.working = sum(self.busy(st) for _, st in pairs)
        self.set_sub_title()
        self.show_detail()
        self.refresh_bindings()
        self.look_at_pods([st.id for _, st in pairs])

    def demo_cell(self, task_id: str) -> str:
        """Whether this task is serving anything, on the row itself: the list is what you look at.
        The addresses stay in the panel, where they are clickable and all of them fit; a single port
        here would have to pick one of a front end and a back end, and pick it silently."""
        view = self.pods.get(task_id)
        if view is None or not view.address or not (state := view.state):
            return "-"
        color = {"live": "green", "local": "yellow", "starting": "yellow", "stopped": "red"}[state]
        # How many came up separates "the back end died" from "everything is there".
        count = f" ×{len(view.reachable)}" if len(view.reachable) > 1 else ""
        return f"[{color}]{state}{count}[/]"

    def set_sub_title(self) -> None:
        parts = [f"{self.waiting} waiting for you" if self.waiting else "nothing waiting for you"]
        if self.working:
            parts.append(f"{self.working} working")
        self.sub_title = " · ".join(parts)

    @property
    def pod(self) -> PodView:
        """The selected task's pod, for the panel and for which keys the footer offers."""
        return self.pods.get(self.selected_id() or "", PodView())

    def busy(self, st: TaskState) -> bool:
        """The agent or the gate is at work and nothing is needed from you."""
        return ui.group(st) == "Working" and self.agent_running(st.id)

    def status(self, st: TaskState) -> str:
        group = ui.group(st)
        color = {"yellow": "yellow", "cyan": "cyan", "dim": "grey50", "green": "green"}[ui.COLORS[group]]
        text = "stopped" if group == "Stopped" else ui.activity(st, self.config.max_iterations)
        if group == "Working" and not self.agent_running(st.id):
            text = "not started" if st.state is State.PLAN else "not running"  # s starts it
        mark = SPINNER[self.frame % len(SPINNER)] if self.busy(st) else " "
        return f"[{color}]{mark} {text}[/]"

    def spin(self) -> None:
        """Turns the spinner of busy tasks between full refreshes, touching only their status cells."""
        self.frame += 1
        table = self.table
        for _, st in self.pairs:
            if self.busy(st):
                table.update_cell(st.id, self.status_column, self.status(st))

    def selected_id(self) -> str | None:
        table = self.table
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def selected(self) -> tuple[Task, TaskState] | None:
        task_id = self.selected_id()
        return next(((t, st) for t, st in self.pairs if st.id == task_id), None)

    def agent_running(self, task_id: str) -> bool:
        return task_id in self.running

    def show_detail(self) -> None:
        if self.panel.has_class("hidden"):
            return
        pick = self.selected()
        if pick:
            text = detail(*pick, self.config.max_iterations, self.agent_running(pick[1].id), self.pod)
        elif entry := self.finished_entry(self.selected_id()):
            text = finished_detail(entry)
        else:
            text = "No tasks yet. Press `n` to create one."
        if text != self.shown:  # redrawing resets the scroll position
            self.shown = text
            self.text.update(text)

    def action_toggle_done(self) -> None:
        self.show_done = not self.show_done
        self.reload()

    def finished_entry(self, task_id: str | None) -> dict | None:
        return next((e for e in self.done if e["id"] == task_id), None)

    def action_details(self) -> None:
        """The list is what you look at; the plan, the diff or the agent's question on request."""
        self.panel.set_class(not self.panel.has_class("hidden"), "hidden")
        self.show_detail()

    @on(DataTable.RowSelected)
    def selected_row(self) -> None:
        self.action_details()

    @on(DataTable.RowHighlighted)
    def highlighted(self) -> None:
        # Queued messages can still arrive after the view is gone, and both of these reach for the
        # screen. There is nothing to redraw for a view that has closed.
        if not self.screen_stack:
            return
        self.show_detail()
        self.refresh_bindings()

    @work(thread=True, exclusive=True, group="pod-view")
    def look_at_pods(self, task_ids: list[str]) -> None:
        """Asking the pods means running docker, which is far too slow for the event loop.
        Exclusive: a refresh that arrives while one is in flight replaces it."""
        found = pod_views(task_ids) if task_ids else {}
        # Docker can take longer than the app lives, and a closed view has nobody to tell.
        if self.screen_stack:
            self.call_from_thread(self.pods_answered, found)

    def pods_answered(self, found: dict) -> None:
        # Checked again here: the app can close between that check and this call.
        if found == self.pods or not self.screen_stack:
            return
        self.pods = found
        for task_id in found:
            with contextlib.suppress(Exception):  # the row may have gone while docker was thinking
                self.table.update_cell(task_id, self.demo_column, self.demo_cell(task_id))
        self.show_detail()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Only the keys that do something for the selected task show in the footer."""
        task_actions = ("accept", "reply", "edit_plan", "open_ide", "approve_risky", "watch",
                        "start_task", "stop_task", "remove", "demo", "demo_stop")  # fmt: skip
        if action not in task_actions:  # new, quit, and moving focus in dialogs
            return True
        pick = self.selected()
        if not pick:
            # A finished task is history: you can only look at it or forget it.
            return action == "remove" and self.finished_entry(self.selected_id()) is not None
        state, running = pick[1].state, self.agent_running(pick[1].id)
        allowed = {
            "accept": state in (State.CHECKPOINT_PLAN, State.CHECKPOINT_FINAL),
            "reply": state in WAITING_ONLY,
            # Not while the agent may be writing its own draft.
            "edit_plan": state is State.CHECKPOINT_PLAN or (state is State.PLAN and not running),
            "open_ide": state is State.CHECKPOINT_FINAL,
            "approve_risky": state is State.APPROVAL_RISKY,
            "watch": running and bool(pick[1].session),
            "start_task": state is not State.DONE and not running,
            "stop_task": state is not State.DONE and running,
            "remove": True,
            # Worth looking at once there is something to look at. Running it again while it
            # runs is a restart, which is what you want after the agent has changed something.
            "demo": state in (State.CHECKPOINT_FINAL, State.IMPLEMENT, State.VERIFY),
            "demo_stop": self.pod.demo,
        }
        return allowed.get(action, True)

    def fail(self, error: Exception) -> None:
        self.notify(str(error.args[0] if error.args else error), severity="error", timeout=10)

    # --- actions ---

    def action_accept(self) -> None:
        task, st = self.selected()
        if st.state is State.CHECKPOINT_PLAN:
            try:
                actions.accept_plan(task, actions.load(task.id)[1])
                self.notify(f"Plan accepted; {task.id} moves on to implementation.")
            except Exception as e:
                self.fail(e)
            self.reload()
        else:
            self.push_screen(
                Confirm(f"Accept the work of {task.id} into your checkout and remove the task?", "Accept"),
                lambda yes: yes and self.finish(task.id),
            )

    @work(thread=True)
    def finish(self, task_id: str) -> None:
        try:
            task, project = actions.load(task_id)
            done = actions.finish(task, project)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.finished, done)

    def finished(self, done: actions.Finished) -> None:
        self.reload()
        if done.conflicts:
            files = ", ".join(done.conflicts[:5])
            text = f"{done.task_id}: conflicts in {files}; resolve them in your IDE. Also on {done.branch}."
            self.notify(text, severity="warning", timeout=15)
            return

        def commit(message: str) -> None:
            if not message:
                self.notify("Left uncommitted; commit it in your IDE when you are ready.")
                return
            try:
                self.notify(f"Committed: {actions.commit_work(done.source, message)}")
            except Exception as e:
                self.fail(e)

        self.push_screen(CommitWork(done), commit)

    def action_reply(self) -> None:
        task, _ = self.selected()

        def send(comment: str) -> None:
            if not comment.strip():
                return
            try:
                target = actions.reply(task, comment)
                self.notify(f"Sent; {task.id} goes back to {target}.")
            except Exception as e:
                self.fail(e)
            self.reload()

        self.push_screen(Reply(task.id), send)

    def action_edit_plan(self) -> None:
        task, _ = self.selected()
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or "vi"
        with self.suspend():
            subprocess.run([*editor.split(), str(task.plan_path)])
        self.reload()

    def action_open_ide(self) -> None:
        task_id = self.selected()[1].id
        if actions.editor_command(self.config, actions.load(task_id)[1]):
            self.open_ide(task_id)
            return
        found = ide.candidates()
        if not found:
            self.fail(ConfigError('no editor found; set [review] ide in config.toml, e.g. "code {path}"'))
            return

        def chosen(command: str) -> None:
            if not command:
                return
            ide.remember(command)
            self.config = load_config()
            self.open_ide(task_id)

        self.push_screen(ChooseEditor(found), chosen)

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
                subprocess.run(ide.command_for(path, command))
            self.reload()
            return
        try:
            actions.open_in_ide(self.config, path, project)
            self.notify(f"Opening {path}")
        except Exception as e:
            self.fail(e)

    def action_approve_risky(self) -> None:
        task, _ = self.selected()

        def approve(yes: bool) -> None:
            if not yes:
                return
            try:
                count, review = actions.approve_risky(task, actions.load(task.id)[1])
                self.notify(
                    f"Approved {count} change(s)." + (f" Ready for review in {review}" if review else "")
                )
            except Exception as e:
                self.fail(e)
            self.reload()

        self.push_screen(Confirm(f"Approve the risky files of {task.id} as shown?", "Approve"), approve)

    @on(Markdown.LinkClicked)
    def open_link(self, event: Markdown.LinkClicked) -> None:
        """An address in the details panel: what the agent is running, opened in your browser."""
        event.prevent_default()
        self.open_url(event.href)

    def action_demo_stop(self) -> None:
        task, _ = self.selected()
        self.stop_demo(task.id)

    @work(thread=True)
    def stop_demo(self, task_id: str) -> None:
        try:
            actions.demo_stop(task_id)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.notify, "Stopped.", timeout=3)
        self.call_from_thread(self.reload)

    def action_demo(self) -> None:
        """Runs the project in its pod so you can open it. When nothing says how, the agent works it
        out without asking first: you pressed the key that means run it, and there is no second
        answer you could give. An instruction an earlier task left behind is a real choice, though,
        because it may be stale, so that one is still yours to confirm."""
        task, project = actions.load(self.selected()[1].id)
        if actions.demo_commands(project, task)[0]:
            self.run_demo(task.id)
            return
        if earlier := actions.demo_from_history(project.name):
            asked = f"The last task you accepted was run like this:\n\n{earlier}\n\nStill right?"
            self.push_screen(
                Confirm(asked, "Use it"),
                # Saying no means work it out again, not do nothing: you asked for it to run.
                lambda yes: self.run_demo(task.id, use=earlier) if yes else self.run_demo(task.id, ask=True),
            )
            return
        self.run_demo(task.id, ask=True)

    @work(thread=True)
    def run_demo(self, task_id: str, ask: bool = False, use: str = "", reply: str = "") -> None:
        self.call_from_thread(self.notify, "Working on it…" if ask else "Starting it…", timeout=3)
        try:
            if use:
                result = actions.use_instruction(task_id, use)
            else:
                result = actions.demo(task_id, ask=ask or bool(reply), reply=reply)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        if result.question:
            self.call_from_thread(self.answer_demo, task_id, result.question)
        elif urls := result.urls:
            self.call_from_thread(self.open_url, urls[0])
        elif blocked := result.unreachable:
            self.call_from_thread(
                self.notify, f"Port {blocked[0].port} is {blocked[0].why_not}", severity="warning", timeout=10
            )
        elif not result.commands:
            self.say("Still nothing says how to run it")
        elif result.starting:
            # It is alive and installing or compiling. The DEMO column is watching and will say when.
            self.call_from_thread(
                self.notify, "Still starting; the DEMO column says when it listens", timeout=8
            )
        else:
            self.say("It stopped without listening; press d for what it said")
        self.call_from_thread(self.reload)

    def say(self, message: str) -> None:
        self.call_from_thread(self.notify, message, severity="error", timeout=8)

    def answer_demo(self, task_id: str, question: str) -> None:
        """The agent asked something only you can decide. Answering carries the same conversation on,
        and none of it can move the task between states."""
        self.push_screen(
            Reply(task_id, f"Working out how to run it, the agent asks:\n\n{question}\n\nYour answer"),
            lambda text: self.run_demo(task_id, reply=text) if text else None,
        )

    def action_watch(self) -> None:
        task, _ = self.selected()
        try:
            command = actions.attach_command(task.id)
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)
        self.reload()

    def action_start_task(self) -> None:
        task, _ = self.selected()
        self.notify(f"Starting {task.id}…")
        self.start(task.id, resume=any(e["type"] == "started" for e in task.events()))

    def action_stop_task(self) -> None:
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Stop {task.id}? Its work is kept; start it again with s.", "Stop"),
            lambda yes: yes and self.stop(task.id),
        )

    @work(thread=True)
    def start(self, task_id: str, resume: bool = False) -> None:
        try:
            model = actions.start(task_id, resume=resume)
            self.call_from_thread(self.notify, f"{task_id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)

    @work(thread=True)
    def stop(self, task_id: str) -> None:
        try:
            actions.stop(actions.load(task_id)[0])
            self.call_from_thread(self.notify, f"{task_id} stopped.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)

    def action_new(self, preselect: str = "") -> None:
        if not projects():
            self.new_project()
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
            self.notify(f"Creating a task in {form['project']}…")
            self.create(form)

        self.push_screen(NewTask(preselect), create)

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
                target = actions.setup_project(Path(form["path"]), form["name"], form["verify"], create=True)
            except Exception as e:
                self.fail(e)
                return
            self.notify(f"Set up {form['name']} in {target}")
            self.action_new(form["name"])

        self.push_screen(NewProject(), done)

    @work(thread=True)
    def create(self, form: dict) -> None:
        try:
            task = actions.create(form["project"], form["goal"], auto=form["auto"], kind=form["kind"])
            self.call_from_thread(self.reload)
            if form["draft"]:
                self.call_from_thread(
                    self.notify, f"Created {task.id}; edit its plan with e, start it with s."
                )
                return
            model = actions.start(task.id)
            self.call_from_thread(self.notify, f"{task.id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)

    def action_remove(self) -> None:
        if entry := self.finished_entry(self.selected_id()):
            actions.forget(entry["id"])
            self.reload()
            return
        task, st = self.selected()
        self.push_screen(
            Confirm(f"Remove {task.id} ({st.state}) and all its work, without accepting it?", "Remove"),
            lambda yes: yes and self.remove_task(task.id),
        )

    @work(thread=True)
    def remove_task(self, task_id: str) -> None:
        try:
            task, project = actions.load(task_id)
            actions.remove(task, project)
            self.call_from_thread(self.notify, f"Removed {task_id}.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)


def run() -> int:
    Vivibox().run()
    return 0
