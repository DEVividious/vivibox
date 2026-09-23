"""Interactive view: 'vivibox' with no arguments. Your tasks, live, and your decisions one key away.

Every action calls the same functions as the command line (actions.py); this module only shows state
and asks. Slow steps (starting a pod, accepting work) run in threads so the view stays responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Markdown,
    Select,
    Static,
)

from . import actions, code, ide, keys, manual, providers, ui
from . import init as project_init
from .config import ConfigError, config_dir, load_config, load_project
from .dialogs import (  # noqa: F401
    HELP,
    MENTION_AT_CURSOR,
    NEW_PROJECT,
    NO_PROJECTS,
    NOTHING_TO_SEND,
    SERENA_MODE_SAID,
    SKIPPED,
    STATUS,
    AddProvider,
    Browse,
    ChooseEditor,
    ChooseImport,
    ChooseModel,
    ChooseRole,
    ChooseSession,
    ChooseVerify,
    CommitWork,
    Confirm,
    DeleteTask,
    DescriptionArea,
    Dialog,
    EdgeTextArea,
    Fields,
    Help,
    ImportSource,
    ManageItems,
    ManageProviders,
    NameFolder,
    NewProject,
    NewTask,
    PathTree,
    Reply,
    ReplyWithCriteria,
    browse_start,
    find_providers,
    folder_verdict,
    import_label,
    is_repository,
    judge,
    leave_at_edge,
    provider_rows,
    row_label,
    shown_path,
    shows,
)
from .panel import (  # noqa: F401
    CODE_CHANGED,
    CODE_CHECK_SECONDS,
    DECISION_KEYS,
    OLDER_SUPERVISOR,
    PROJECT_ROW,
    PROTECTED_BRANCHES,
    REFRESH_SECONDS,
    SPIN_SECONDS,
    SPINNER,
    TASK_ACTIONS,
    WAITING_ONLY,
    PodView,
    after_window,
    build_said,
    checklist,
    criteria,
    deleted_detail,
    detail,
    edit_in_editor,
    finished_detail,
    gate_failed,
    git_diff,
    keys_for,
    last_gate,
    load_collapsed,
    log_command,
    newest_log,
    next_steps,
    pager_command,
    planned_by_you,
    plans_verify,
    pod_view,
    pod_views,
    project_detail,
    project_repos,
    projects,
    read,
    removed_tests,
    save_collapsed,
    ticked_at,
    verification_running,
    view_state_path,
    watchable,
)
from .plan import PlanError, parse_plan
from .states import State
from .task import Task, TaskState, list_tasks


class LiveFooter(Footer):
    """Textual's Footer stops redrawing while the terminal has no focus (bindings_changed in
    widgets/_footer.py returns early). The keys are what tells you a task now needs you, so they
    must appear while you are in another window, not once you click back into the terminal."""

    def bindings_changed(self, screen) -> None:
        self._bindings_ready = True
        if self.is_attached and screen is self.screen:
            self.call_after_refresh(self.recompose)


class LeavingExecutor(ThreadPoolExecutor):
    """Where Textual runs the thread workers (a start, a stop, the app being run): the loop's
    default executor, but one the view can close without waiting for. asyncio waits for the
    default executor at the end, so a pod start that hung on Docker held the window until Ctrl-C,
    which showed a traceback. Nothing is lost by leaving: the pod and the supervisor are
    processes of their own, and a docker command finishes on its own."""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix="vivibox-step")
        self.at_work: set[Future] = set()

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = super().submit(fn, *args, **kwargs)
        self.at_work.add(future)
        future.add_done_callback(self.at_work.discard)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        super().shutdown(wait=False, cancel_futures=cancel_futures)

    def unfinished(self) -> int:
        return sum(1 for future in self.at_work if not future.done())


class Vivibox(App):
    TITLE = "vivibox"
    # Textual's own palette (themes, screenshots) took a tenth of a narrow footer.
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    DataTable { height: 1fr; }
    .catalog { height: 8; }
    .tree { height: 16; }
    .found { height: auto; max-height: 8; }
    .group { padding: 1 0 0 0; text-style: bold; }
    #empty { height: 1fr; padding: 2 4; color: $text-muted; }
    #detail { height: 60%; border-top: solid $primary; padding: 0 1; }
    #detail.hidden { display: none; }
    .dialog { width: 90; height: auto; max-height: 90%; border: thick $primary; background: $surface;
              padding: 1 2; }
    .dialog.help { width: 76; }
    .dialog TextArea { height: 8; }
    .dialog TextArea.description { height: 10; }
    .dialog TextArea.criteria { height: 6; }
    #suggestions { max-height: 8; border: none; background: $boost; }
    #editors { max-height: 12; margin: 1 0; }
    .buttons { height: auto; margin-top: 1; }
    .fields { height: auto; }
    .wrap { width: 100%; }
    /* Text beside a button takes what the button leaves. */
    .role > .wrap { width: 1fr; }
    .role > Button { margin-left: 2; }
    .buttons Button { margin-right: 2; }
    .files { color: $text-muted; margin: 1 0; }
    .role { height: auto; }
    .role > Label { padding: 1 0; }
    .role > .role-name { width: 10; }
    .role > Select { width: 1fr; }
    /* A form: a column of labels, one field per row, only the description and the buttons boxed. */
    .dialog.form { max-width: 100%; }
    .form .section { height: auto; margin-top: 1; }
    .form #task { margin-top: 0; }
    .form .row { height: auto; }
    /* Lists of a group a row apart, unless the terminal is short (fit() decides). */
    .form .row.gap { margin-top: 1; }
    .form.tight .row.gap { margin-top: 0; }
    .form .key { width: 10; color: $text-muted; }
    .form .hint { width: 1fr; color: $text-muted; }
    .form .row > Select, .form .row > TextArea { width: 1fr; }
    .form .row > Button { margin: 0 2 0 0; min-width: 9; }
    .form .buttons > .keys { width: auto; padding: 1 0; }
    .form Select > SelectCurrent { background: $boost; }
    /* Focus is one signal: the focused control's text as the cursor block, as on a button. */
    .form Select:focus > SelectCurrent > Static#label {
        color: $block-cursor-foreground; background: $block-cursor-background; text-style: bold;
    }
    Help, Confirm, DeleteTask, Reply, ReplyWithCriteria, NewTask, NewProject, CommitWork, ChooseEditor,
    AddProvider, ChooseImport, ImportSource, ManageProviders, ManageItems, Browse, NameFolder {
        align: center middle;
    }
    """
    # In the footer's order: your decisions first, then the selected row's actions, then what
    # works anywhere. Keys that matter less often are under ? and off the footer, which is short.
    BINDINGS = [
        Binding("a", "accept", "Accept"),
        Binding("r", "reply", "Reply"),
        Binding("p", "approve_risky", "Approve risky"),
        Binding("g", "verify_again", "Verify again"),
        Binding("d", "details", "Details"),
        Binding("e", "edit_plan", "Edit plan"),
        Binding("c", "copy_prompt", "Prompt"),
        Binding("C", "copy_prompt_cli", "CLI prompt"),
        Binding("f", "show_diff", "Diff"),
        Binding("o", "open_ide", "IDE"),
        Binding("v", "demo", "Run app"),
        Binding("v", "demo_stop", "Stop app"),
        Binding("w", "watch", "Watch"),
        Binding("w", "enter_box", "Enter"),
        Binding("b", "new_box", "Box"),
        Binding("l", "show_log", "Log"),
        # One key, two meanings: the footer shows the one that applies to the selected task. At a
        # checkpoint the agent is not working, so stopping is only taking the pod down.
        Binding("s", "start_task", "Start"),
        Binding("s", "stop_task", "Stop"),
        Binding("s", "stop_pod", "Stop pod"),
        Binding("m", "models", "Model", show=False),
        Binding("x", "remove", "Delete"),
        Binding("e", "edit_project", "Edit project"),
        Binding("o", "open_repo", "IDE"),
        Binding("x", "forget_project", "Forget"),
        Binding("n", "new", "New"),
        Binding("i", "new_project", "New project", show=False),
        Binding("h", "toggle_done", "Show/hide done", show=False),
        Binding("k", "providers", "Providers & MCP", show=False),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.executor = LeavingExecutor()
        self.shown = ""
        # What the table was last drawn from; a refresh that matches it does no work.
        self.drawn: tuple = ()
        self.pairs: list[tuple[Task, TaskState]] = []
        self.done: list[dict] = []
        self.show_done = True
        self.has_done = False
        self.collapsed = load_collapsed()
        # Every project by name, with why its tasks could not start; refreshed with the tasks.
        self.problems: dict[str, str] = {}
        # Tasks whose demo is being started or worked out, and what it is doing: shown as working.
        self.starting: dict[str, str] = {}
        self.frame = 0
        self.table: DataTable = None  # type: ignore[assignment]  # set when the view mounts
        self.columns: tuple[str, ...] = ()  # the ones the terminal's width has room for
        self.running: set[str] = set()
        self.views: dict[str, ui.TaskView] = {}
        # The vivibox this view runs, against what is on disk: they part at git pull.
        self.code_started = code.signature()
        self.code_changed = False
        self.code_checked = 0.0
        # The models of the providers you have keys for: asked once, in the background, and kept
        # for a day, so a dialog never waits on a container.
        self.available: dict[str, list[str]] | None = None
        # The providers opencode knows, for adding one; read with the models, None until then.
        self.catalog: list[tuple[str, str]] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
        yield Static("", id="empty")  # in the table's place while there is nothing to list
        with VerticalScroll(id="detail", classes="hidden"):
            yield Markdown("", id="detail-text")
        yield LiveFooter(compact=True)

    def on_mount(self) -> None:
        asyncio.get_running_loop().set_default_executor(self.executor)
        # Kept by hand: a dialog on top changes what a query would find, and the timers keep running.
        self.pods: dict[str, PodView] = {}  # what each task's pod is doing, refreshed off the loop
        self.waiting = self.working = 0
        self.waiting_ids: set[str] | None = None  # None until the first refresh: nothing is new then
        self.table = self.query_one(DataTable)
        self.panel = self.query_one("#detail")
        self.text = self.query_one("#detail-text", Markdown)
        self.set_columns()
        self.reload()
        self.set_interval(SPIN_SECONDS, self.spin)
        self.set_interval(REFRESH_SECONDS, self.reload)
        self.call_after_refresh(self.check_projects)
        self.call_after_refresh(self.offer_restart)
        self.call_after_refresh(self.hint_opencode)
        self.load_models()

    def action_providers(self) -> None:
        self.push_screen(ManageProviders())

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

    def offer_restart(self) -> None:
        """After a reboot the tasks that were at work have no supervisor. Asked once, on start,
        instead of a row-by-row s; a task you stopped yourself stays stopped."""
        idle = [st.id for task, st in self.pairs if self.views[st.id].status == "not running"]
        if not idle:
            return
        one = len(idle) == 1
        were, them = ("This task was", "it") if one else ("These tasks were", "them")
        question = f"{were} running before: {', '.join(idle)}.\n\nStart {them} again?"

        def answered(yes: bool) -> None:
            for task_id in idle if yes else ():
                self.start(task_id, resume=True)

        self.push_screen(Confirm(question, "Start"), answered)

    def check_code(self) -> None:
        """Whether vivibox on disk is still the one running. Said once, and kept in the title."""
        if self.code_changed or time.monotonic() - self.code_checked < CODE_CHECK_SECONDS:
            return
        self.code_checked = time.monotonic()
        with contextlib.suppress(AttributeError):  # a test may have replaced the cached function
            code.signature.cache_clear()
        if code.signature() != self.code_started:
            self.code_changed = True
            self.notify(CODE_CHANGED.capitalize() + ".", severity="warning", timeout=15)
            self.drawn = ()  # the title and the panels' older-supervisor notes changed

    def check_projects(self) -> None:
        """A project whose repository is gone is offered for removal. With none left, the view says
        how to add one rather than open a dialog you did not ask for."""
        broken = actions.broken_projects()
        if not broken:
            return
        listed = "\n".join(f"{name}: {why}" for name, why in broken.items())
        question = f"These projects cannot be worked in:\n\n{listed}\n\nForget them?"

        def answered(yes: bool) -> None:
            for name in broken if yes else ():
                try:
                    actions.forget_project(name)
                except ConfigError as e:
                    self.fail(e)
            self.reload()

        self.push_screen(Confirm(question, "Forget", destructive=True), answered)

    # --- data ---

    def snapshot(self) -> tuple:
        """What the view shows, cheaply. Rebuilding the table and the panel costs Textual several
        hundred retained objects, and a refresh that finds nothing changed used to pay it anyway:
        a session left open overnight reached 5 GB and a quarter of a core with the tasks idle."""
        tasks = tuple(
            (
                st.id,
                str(st.state),
                st.iteration,
                st.paused,
                st.problem,
                st.updated,
                st.id in self.running,
                # The agent ticks criteria while it works, and st.updated only moves between
                # states, so without this the column freezes for the whole of a long turn --
                # exactly when watching it fill in is the only sign of progress. A stat, not a
                # parse: the redraw does the reading.
                ticked_at(task),
            )
            for task, st in self.pairs
        )
        pods = tuple(sorted((k, v.state, len(v.reachable)) for k, v in self.pods.items()))
        # With no project the panel says how to add one, and n is hidden until there is one.
        return (
            tasks,
            pods,
            self.show_done,
            self.selected_id(),
            len(self.done),
            tuple(sorted(self.problems.items())),
            tuple(sorted(self.collapsed)),
            self.has_done,
            tuple(sorted(self.starting.items())),
        )

    def reload(self) -> None:
        """Re-reads every task; the only place that does, so key checks stay cheap."""
        self.check_code()
        selected = self.selected_id()
        pairs = [(t, t.read_state()) for t in list_tasks(self.config.tasks_dir)]
        self.running = {st.id for task, st in pairs if actions.supervisor_running(task)}
        # Worked out once per refresh; the list, the panel and the keys all read it from here.
        self.views = {
            st.id: ui.view(task, st, st.id in self.running, self.config.max_iterations) for task, st in pairs
        }
        self.pairs = pairs = sorted(pairs, key=lambda p: self.views[p[1].id].rank)
        live = {st.id for _, st in pairs}
        self.done = [e for e in actions.history() if e["id"] not in live] if self.show_done else []
        self.problems = {name: actions.project_problem(name) for name in projects()}
        # A stat, not a read: whether h has any finished task to show.
        kept = actions.history_path()
        self.has_done = kept.is_file() and kept.stat().st_size > 0
        now = self.snapshot()
        if now == self.drawn:
            self.look_at_pods([st.id for _, st in pairs])
            return
        self.drawn = now
        self.fill_table(pairs, selected)

    # The columns a terminal has room for, narrowest first: task, status and goal always.
    COLUMNS = ("TASK", "STATUS", "DEMO", "CRITERIA", "COST PLAN + IMPL", "CREATED", "UPDATED", "GOAL")
    NARROW = ("TASK", "STATUS", "GOAL")
    MEDIUM = ("TASK", "STATUS", "CRITERIA", "UPDATED", "GOAL")

    def columns_for(self, width: int) -> tuple[str, ...]:
        return self.NARROW if width < 100 else self.MEDIUM if width < 130 else self.COLUMNS

    def set_columns(self) -> None:
        wanted = self.columns_for(self.size.width)
        if wanted == self.columns:
            return
        self.columns = wanted
        table = self.table
        table.clear(columns=True)
        keys = table.add_columns(*wanted)
        by_name = dict(zip(wanted, keys, strict=True))
        self.status_column, self.demo_column = by_name["STATUS"], by_name.get("DEMO")
        self.drawn = ()

    def on_resize(self) -> None:
        if self.table is not None:  # a resize before the view is built has nothing to lay out
            self.set_columns()
            self.reload()

    def project_order(self, pairs: list) -> list[str]:
        """Projects with a task waiting for you first, then by name. A project a task belongs to
        is listed even when its file is gone, so the task is not orphaned off the screen."""
        ranks: dict[str, int] = {}
        for _, st in pairs:
            ranks[st.project] = min(ranks.get(st.project, ui.FINISHED), self.views[st.id].rank)
        names = set(self.problems) | set(ranks) | {e.get("project", "") for e in self.done}
        return sorted(names, key=lambda n: (ranks.get(n, ui.FINISHED), n))

    def project_summary(self, name: str, tasks: list, done: int) -> str:
        """The project row's status: what keeps its tasks from starting, else what they are doing,
        so a collapsed project still says what waits for you."""
        waiting = sum(self.views[st.id].group == "Waiting for you" for _, st in tasks)
        working = sum(self.busy(st) for _, st in tasks)
        parts = [f"[yellow]{waiting} waiting for you[/]"] * bool(waiting)
        parts += [f"[cyan]{working} working[/]"] * bool(working)
        parts += [f"[grey50]{len(tasks) - waiting - working} stopped[/]"] * bool(
            len(tasks) - waiting - working
        )
        parts += [f"[green]{done} done[/]"] * bool(done)
        return " · ".join(parts) or "[grey50]no tasks · n creates one[/]"

    def fill_table(self, pairs: list, selected: str | None) -> None:
        table = self.table
        table.clear()
        planned: list[tuple[dict[str, str], str]] = []
        for name in self.project_order(pairs):
            own = [(t, st) for t, st in pairs if st.project == name]
            done = [e for e in self.done if e.get("project", "") == name]
            folded = name in self.collapsed
            problem, summary = self.problems.get(name, ""), self.project_summary(name, own, len(done))
            # The problem is the status; otherwise the tasks say what they do, and only a folded
            # project needs its row to say what waits, in a few characters.
            waiting = sum(self.views[st.id].group == "Waiting for you" for _, st in own)
            status = (
                f"[red]{escape(problem)}[/]" if problem
                else f"[yellow]{waiting} waiting for you[/]" if folded and waiting
                else ""
            )  # fmt: skip
            planned.append((
                {"TASK": f"[b]{'▸' if folded else '▾'} {escape(name)}[/]", "STATUS": status, "GOAL": summary},
                PROJECT_ROW + name,
            ))  # fmt: skip
            if folded:
                continue
            for task, st in own:
                spent = ui.cost(task)
                planned.append((
                    {"TASK": f"  {st.id}", "STATUS": self.status(st), "DEMO": self.demo_cell(st.id),
                     "CRITERIA": criteria(task), "COST PLAN + IMPL": str(spent) if spent else "-",
                     "CREATED": ui.ago(st.created), "UPDATED": ui.ago(st.updated), "GOAL": st.goal},
                    st.id,
                ))  # fmt: skip
            for entry in done:
                planned.append((
                    {"TASK": f"  {entry['id']}",
                     "STATUS": "[grey50]  deleted[/]" if entry.get("deleted") else "[green]  done[/]",
                     "DEMO": "-", "CRITERIA": "-", "COST PLAN + IMPL": ui.finished_cost(entry),
                     "CREATED": ui.ago(entry["created"]) if entry.get("created") else "-",
                     "UPDATED": ui.ago(entry["finished"]), "GOAL": entry["title"]},
                    entry["id"],
                ))  # fmt: skip
        # The goal gets what the other columns leave: a goal that runs off the screen is a goal
        # nobody reads. Its width comes from the widest thing each other column shows.
        taken = 0
        for name in self.columns:
            if name != "GOAL":
                widest = max(
                    (Text.from_markup(cells.get(name, "")).cell_len for cells, _ in planned), default=0
                )
                taken += max(widest, len(name)) + 2
        goal_width = max(self.size.width - taken - 3, 16)
        ids = [key for _, key in planned]
        for cells, key in planned:
            cells = {**cells, "GOAL": ui.shorten(cells.get("GOAL", ""), goal_width)}
            table.add_row(*(cells.get(name, "") for name in self.columns), key=key)
        empty = self.query_one("#empty", Static)
        table.display, empty.display = bool(ids), not ids
        if not ids:
            # Nothing left to show details of: the view is back to how it starts.
            self.panel.add_class("hidden")
        empty.update("" if ids else NO_PROJECTS)
        if selected in ids:
            table.move_cursor(row=ids.index(selected))
        elif ids:
            # The first task waiting for you; a project row is a heading, not what you came for.
            first = next((i for i, key in enumerate(ids) if not key.startswith(PROJECT_ROW)), 0)
            table.move_cursor(row=first)
        waiting_now = {st.id for _, st in pairs if self.views[st.id].group == "Waiting for you"}
        # A task that starts to wait for you rings the bell, once: the sign you can hear from
        # another window when the desktop's notifications are off.
        if self.waiting_ids is not None and waiting_now - self.waiting_ids:
            self.bell()
        self.waiting_ids = waiting_now
        self.waiting = len(waiting_now)
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
        if self.code_changed:
            parts.append(CODE_CHANGED)
        self.sub_title = " · ".join(parts)
        # The window's title, for the taskbar: how many wait for you.
        self.title = f"vivibox ({self.waiting})" if self.waiting else "vivibox"

    @property
    def pod(self) -> PodView:
        """The selected task's pod, for the panel and for which keys the footer offers."""
        return self.pods.get(self.selected_id() or "", PodView())

    def busy(self, st: TaskState) -> bool:
        """The agent or the gate is at work and nothing is needed from you, or the demo is starting."""
        return (self.seen(st).group == "Working" and not st.box) or st.id in self.starting

    def seen(self, st: TaskState) -> ui.TaskView:
        """The task as the last refresh saw it; worked out now for one that refresh has not met."""
        found = self.views.get(st.id)
        if found is None:
            task = next(t for t, s in self.pairs if s.id == st.id)
            found = ui.view(task, st, self.agent_running(st.id), self.config.max_iterations)
        return found

    def status(self, st: TaskState) -> str:
        seen = self.seen(st)
        color = {"yellow": "yellow", "cyan": "cyan", "dim": "grey50", "green": "green"}[ui.COLORS[seen.group]]
        text = seen.status
        if doing := self.starting.get(st.id):
            color, text = "cyan", doing
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

    def selected_project(self) -> str:
        """The project row under the cursor, or the project of the task under it."""
        key = self.selected_id() or ""
        if key.startswith(PROJECT_ROW):
            return key.removeprefix(PROJECT_ROW)
        if pick := self.selected():
            return pick[1].project
        if entry := self.finished_entry(key):
            return entry["project"]
        return ""

    def on_project_row(self) -> bool:
        return (self.selected_id() or "").startswith(PROJECT_ROW)

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
        elif self.on_project_row():
            name = self.selected_project()
            own = sum(st.project == name for _, st in self.pairs)
            text = project_detail(name, own, self.problems.get(name, ""))
        else:
            text = NO_PROJECTS
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
        if self.on_project_row():
            self.collapsed ^= {self.selected_project()}
            save_collapsed(self.collapsed)
            self.reload()
        else:
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
            if self.demo_column is None:
                break
            with contextlib.suppress(Exception):  # the row may have gone while docker was thinking
                self.table.update_cell(task_id, self.demo_column, self.demo_cell(task_id))
        self.show_detail()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Only the keys that do something for the selected task show in the footer."""
        if action == "new":
            return bool(projects())  # a task needs a project to be in
        if action in ("edit_project", "open_repo", "forget_project", "new_box"):
            if not self.on_project_row():
                return False
            if action == "new_box":
                return not self.problems.get(self.selected_project())
            # A project with tasks is not forgotten from under them: delete those first.
            return action != "forget_project" or not any(
                st.project == self.selected_project() for _, st in self.pairs
            )
        if action == "details":  # nothing to show details of; an open panel can still be closed
            return bool(projects()) or (self.is_mounted and not self.panel.has_class("hidden"))
        if action == "toggle_done":
            return self.has_done
        if action not in TASK_ACTIONS:  # quit, and moving focus in dialogs
            return True
        pick = self.selected()
        if not pick:
            # A finished task is history: you can only look at it or forget it.
            return action == "remove" and self.finished_entry(self.selected_id()) is not None
        task, st = pick
        return keys_for(task, st, self.agent_running(st.id), st.id in self.starting, self.pod.demo)[action]

    def fail(self, error: Exception) -> None:
        self.notify(str(error.args[0] if error.args else error), severity="error", timeout=10)

    # --- actions ---

    def action_accept(self) -> None:
        task, st = self.selected()
        if st.box and st.state is State.IMPLEMENT:
            self.close_box(task.id)
            return
        if st.state is State.CHECKPOINT_PLAN:
            project = actions.load(task.id)[1]

            def accept(yes: bool = True) -> None:
                if not yes:
                    return
                try:
                    actions.accept_plan(task, project)
                except Exception as e:
                    self.fail(e)
                else:
                    self.go_on(task, "Plan accepted")
                self.reload()

            # What the first plan settles for the project, said once, where you decide.
            if settles := actions.verify_from_plan(task, project):
                asked = f"The plan sets how {project.name} is verified from now on: {settles}."
                self.push_screen(Confirm(f"{asked}\n\nAccept the plan?", "Accept"), accept)
            else:
                accept()
        else:
            self.push_screen(
                Confirm(f"Accept the work of {task.id} into your checkout and remove the task?", "Accept"),
                lambda yes: yes and self.finish(task.id),
            )

    @work(thread=True)
    def close_box(self, task_id: str) -> None:
        self.call_from_thread(self.busy_with, task_id, "closing the box…")
        try:
            task, project = actions.load(task_id)
            where = actions.close_box(task, project)
        except Exception as e:
            self.call_from_thread(self.fail, e)
        else:
            said = f"{task_id} closed; " + (
                f"its work is ready for your review in {where}" if where else "it changed risky files"
            )
            self.call_from_thread(self.notify, said, timeout=8)
        self.call_from_thread(self.busy_with, task_id, "")

    def action_enter_box(self) -> None:
        task, _ = self.selected()
        try:
            command = actions.box_shell_command(task.id)
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)
        self.reload()

    def action_new_box(self) -> None:
        name = self.selected_project()
        self.notify(f"Opening a box in {name}…")
        self.open_box(name)

    @work(thread=True)
    def open_box(self, name: str) -> None:
        try:
            task = actions.open_box(name)
        except Exception as e:
            self.call_from_thread(self.fail, e)
        else:
            self.call_from_thread(self.notify, f"{task.id} is open; w enters it, a brings its work back.")
            self.call_from_thread(self.select, task.id)
        self.call_from_thread(self.reload)

    def select(self, task_id: str) -> None:
        """Puts the cursor on a row the next refresh will list: what you just made is what you
        look at next."""
        self.reload()
        ids = [str(key.value) for key in self.table.rows]
        if task_id in ids:
            self.table.move_cursor(row=ids.index(task_id))

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
        task, st = self.selected()

        def send(comment: str, criteria: list[str] = ()) -> None:
            if not comment.strip() and not criteria:
                return
            try:
                actions.reply(task, comment, criteria)
            except Exception as e:
                self.fail(e)
            else:
                added = f" with {len(criteria)} new criteria" if criteria else ""
                self.go_on(task, f"Sent{added}")
            self.reload()

        if st.state in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED):
            self.push_screen(ReplyWithCriteria(task.id), lambda a: a and send(a["comment"], a["criteria"]))
        else:
            self.push_screen(Reply(task.id), send)

    def go_on(self, task: Task, done: str = "") -> None:
        """After a decision of yours the task goes on. With nobody working on it (after a reboot,
        or once you stopped it) that takes a start, and saying "moves on" without one was a lie."""
        st = task.read_state()
        starts = actions.needs_start(task)
        if done and starts:
            self.notify(f"{done}; starting {task.id}, nobody was working on it.")
        elif done:
            self.notify(f"{done}; {task.id} is {ui.WORKING.get(st.state, 'waiting for you')} now.")
        if starts:
            self.start(task.id, resume=True)

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

    def action_verify_again(self) -> None:
        task, _ = self.selected()
        try:
            actions.verify_again(task)
        except Exception as e:
            self.fail(e)
        else:
            self.go_on(task, f"Verifying {task.id} again")
        self.reload()

    def diff_command(self) -> list[str]:
        task, _ = self.selected()
        return git_diff(task, actions.load(task.id)[1])

    def action_show_diff(self) -> None:
        """The work as a diff, in git's own pager, before you accept it."""
        try:
            command = self.diff_command()
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)

    def action_show_log(self) -> None:
        """The newest verification log, or the supervisor's, in your pager."""
        task, st = self.selected()
        if command := log_command(task, st, self.agent_running(task.id)):
            with self.suspend():
                subprocess.run(command)

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
            else:
                self.go_on(task)
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

    def busy_with(self, task_id: str, doing: str) -> None:
        """What a slow step (the demo, starting or stopping the task) is doing, in the task's
        status, with the spinner a working agent has; "" when it is done, one way or the other."""
        if doing:
            self.starting[task_id] = doing
        else:
            self.starting.pop(task_id, None)
        self.reload()

    @work(thread=True)
    def run_demo(self, task_id: str, ask: bool = False, use: str = "", reply: str = "") -> None:
        doing = "working out how to run it" if ask or reply else "starting the demo"
        self.call_from_thread(self.busy_with, task_id, doing)
        try:
            self.demo_outcome(task_id, ask, use, reply)
        finally:
            self.call_from_thread(self.busy_with, task_id, "")

    def demo_outcome(self, task_id: str, ask: bool, use: str, reply: str) -> None:
        try:
            if use:
                result = actions.use_instruction(task_id, use)
            else:
                result = actions.demo(task_id, ask=ask or bool(reply), reply=reply)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        if result.stopped:
            what = "; ".join(result.stopped)
            self.call_from_thread(
                self.notify, f"Stopped what the agent left running: {what}", severity="warning", timeout=8
            )
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

    def say(self, message: str) -> None:
        self.call_from_thread(self.notify, message, severity="error", timeout=8)

    def answer_demo(self, task_id: str, question: str) -> None:
        """The agent asked something only you can decide. Answering carries the same conversation on,
        and none of it can move the task between states."""
        self.push_screen(
            Reply(task_id, f"Working out how to run it, the agent asks:\n\n{question}\n\nYour answer"),
            lambda text: self.run_demo(task_id, reply=text) if text else None,
        )

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
                ChooseModel(role, offered, configured, self.current_choice(task, role, config)),
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

    def action_watch(self) -> None:
        """The agent, or the verification as it runs. A task with two conversations, the planner's
        done and the writer's under way, asks which; the verification's log is the writer's turn."""
        task, st = self.selected()
        sessions = [] if st.box or st.state is State.VERIFY else actions.watchable_sessions(task, st)
        if len(sessions) < 2:
            self.watch(task.id)
            return
        at_work = "planner" if st.state is State.PLAN else "writer"
        rows = [
            (role, "at work now" if role == at_work else "finished; its conversation") for role, _ in sessions
        ]
        self.push_screen(ChooseSession(rows), lambda role: role and self.watch(task.id, role))

    def watch(self, task_id: str, role: str = "") -> None:
        try:
            command = actions.attach_command(task_id, role)
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)
            after_window(session_gone=not actions.tmux_has(actions.tmux_session(task_id)))
        self.reload()

    def action_start_task(self) -> None:
        task, _ = self.selected()
        self.start(task.id, resume=any(e["type"] == "started" for e in task.events()))

    def action_stop_task(self) -> None:
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Stop {task.id}? Its work is kept; start it again with s.", "Stop", destructive=True),
            lambda yes: yes and self.stop(task.id),
        )

    def action_stop_pod(self) -> None:
        """At a checkpoint nothing runs but the pod; your decision starts it again by itself."""
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Take the pod of {task.id} down? Your next decision starts it again.", "Stop pod"),
            lambda yes: yes and self.stop(task.id),
        )

    def action_help(self) -> None:
        self.push_screen(Help())

    @work(thread=True)
    def start(self, task_id: str, resume: bool = False) -> None:
        step = lambda doing: self.call_from_thread(self.busy_with, task_id, doing)  # noqa: E731
        step("starting…")
        try:
            model = actions.start(task_id, resume=resume, on_step=step)
            self.call_from_thread(self.notify, f"{task_id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.busy_with, task_id, "")

    @work(thread=True)
    def stop(self, task_id: str) -> None:
        # Taking the pod down takes a while; without this the row looked as if nothing happened.
        self.call_from_thread(self.busy_with, task_id, "stopping…")
        try:
            actions.stop(actions.load(task_id)[0])
            self.call_from_thread(self.notify, f"{task_id} stopped.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.busy_with, task_id, "")

    def project_file(self) -> Path:
        return config_dir() / "projects" / f"{self.selected_project()}.toml"

    def action_edit_project(self) -> None:
        """How the project is verified, picked from what its build files name; the file itself
        for the rest, and when it cannot be read at all."""
        name = self.selected_project()
        try:
            project = load_project(name)
        except ConfigError:
            self.edit_project_file()
            return

        def chosen(choice: dict) -> None:
            if not choice:
                return
            if choice.get("edit"):
                self.edit_project_file()
                return
            actions.save_verify(project, choice["verify"], choice["no_build"])
            how = actions.NO_BUILD if choice["no_build"] else ", ".join(f"`{c}`" for c in choice["verify"])
            self.notify(f"{name} is verified from now on: {how}", timeout=6)
            self.drawn = ()
            self.reload()

        self.push_screen(
            ChooseVerify(name, project.verify, project.no_build, project_init.candidates(project.repo)),
            chosen,
        )

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

    def action_new(self, preselect: str = "") -> None:
        if not projects():
            self.notify("A task needs a project first; press i to add one.")
            return
        if actions.needs_provider(load_config()):
            self.notify("A task needs a provider first; press k to add one.")
            return
        preselect = preselect or self.selected_project()

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

        self.push_screen(NewTask(preselect, self.available), create)

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
                    Path(form["path"]), form["name"], form["verify"], create=True, no_build=form["no_build"]
                )
            except Exception as e:
                self.fail(e)
                return
            self.notify(f"Set up {form['name']} in {target}")
            self.action_new(form["name"])

        self.push_screen(NewProject(), done)

    @work(thread=True)
    def create(self, form: dict) -> None:
        try:
            task = actions.create(
                form["project"], form["goal"], auto=form["auto"], kind=form["kind"], roles=form.get("roles")
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

    def action_remove(self) -> None:
        if entry := self.finished_entry(self.selected_id()):
            kind = "deleted" if entry.get("deleted") else "done"
            kept = actions.archive_path(entry["id"])
            dialog = DeleteTask(
                f"Delete {entry['id']} from the history?",
                f"{entry['title']} ({kind}, {ui.finished_cost(entry)})",
                "its line in this list"
                + (", and its archive (the plan, the events)." if kept.exists() else "."),
                "everything else: "
                + ("nothing of it was left anyway." if kind == "deleted" else "its work in your repository."),
            )

            def forget(yes: bool) -> None:
                if yes:
                    actions.forget(entry["id"])
                    self.reload()

            self.push_screen(dialog, forget)
            return
        task, st = self.selected()
        running = self.agent_running(task.id)
        met = criteria(task)
        about_task = [self.seen(st).status]
        about_task += [f"criteria {met}"] if met != "-" else []
        about_task.append(str(ui.cost(task)))
        dialog = DeleteTask(
            f"Delete {task.id}?",
            f"{st.goal} ({', '.join(about_task)})",
            "its clone with every commit the agent made, its plan, its pod and anything running in it, "
            "and its review copy. It is not accepted: none of its work reaches your repository.",
            "a line in the history, with what it was for, what it cost and how far it got. "
            "Your repository is untouched.",
            warning="The agent is working on it now; it is stopped first." if running else "",
        )
        self.push_screen(dialog, lambda yes: self.remove_task(task.id) if yes else None)

    @work(thread=True)
    def remove_task(self, task_id: str) -> None:
        try:
            task, project = actions.load(task_id)
            actions.remove(task, project)
            self.call_from_thread(self.notify, f"Deleted {task_id}.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)


def run() -> int:
    app = Vivibox()
    app.run()
    if left := app.executor.unfinished():
        # asyncio would wait for these at the end, and so would the interpreter's exit; the pod and
        # the supervisor are processes of their own, so the tasks go on without this window.
        step = "step" if left == 1 else "steps"
        print(f"{left} {step} still finishing in the background, a pod starting or stopping; the tasks go on")
        sys.stdout.flush()
        os._exit(0)
    return 0
