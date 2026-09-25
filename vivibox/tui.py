"""Interactive view: 'vivibox' with no arguments. Your tasks, live, and your decisions one key away.

Every action calls the same functions as the command line (actions.py); this module only shows state
and asks. Slow steps (starting a pod, accepting work) run in threads so the view stays responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Header, Markdown, Select, Static

from . import actions, code, ide, ui
from .app_support import LeavingExecutor, LiveFooter
from .config import ConfigError, load_config
from .dialogs import (
    NEW_PROJECT,
    NO_PROJECTS,
    ChooseSession,
    CommitWork,
    DeleteTask,
    Help,
    NewTask,
    Reply,
    ReplyWithCriteria,
)
from .keys_box import BoxKeys
from .keys_demo import DemoKeys
from .keys_models import ModelKeys
from .keys_plan import PlanKeys
from .keys_project import ProjectKeys
from .keys_run import RunKeys
from .keys_work import WorkKeys
from .panel import (
    CODE_CHANGED,
    CODE_CHECK_SECONDS,
    PROJECT_ROW,
    REFRESH_SECONDS,
    SPIN_SECONDS,
    TASK_ACTIONS,
    PodView,
    after_window,
    criteria,
    detail,
    finished_detail,
    keys_for,
    live_at,
    load_view,
    pod_views,
    project_detail,
    projects,
    save_collapsed,
    save_view,
    ticked_at,
    watchable,
)
from .settings import Settings
from .states import State
from .table import TaskTable
from .task import Task, TaskState, list_tasks
from .widgets import Confirm


class Vivibox(TaskTable, BoxKeys, DemoKeys, ModelKeys, ProjectKeys, PlanKeys, RunKeys, WorkKeys, App):
    TITLE = "vivibox"
    # Textual's own palette (themes, screenshots) took a tenth of a narrow footer.
    ENABLE_COMMAND_PALETTE = False
    CSS_PATH = "vivibox.tcss"
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
        Binding("S", "force_stop", "Force stop", show=False),
        Binding("m", "models", "Model", show=False),
        Binding("x", "remove", "Delete"),
        Binding("e", "edit_project", "Edit project"),
        Binding("o", "open_repo", "IDE"),
        Binding("x", "forget_project", "Forget"),
        Binding("n", "new", "New"),
        Binding("i", "new_project", "New project", show=False),
        Binding("h", "toggle_done", "Show/hide accepted", show=False),
        Binding("H", "toggle_deleted", "Show/hide deleted", show=False),
        Binding("k", "settings", "Settings", show=False),
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
        view = load_view()
        # The ones you deleted start hidden: they are the ones you rarely look at again.
        self.show_done = view.get("show_done", True)
        self.show_deleted = view.get("show_deleted", False)
        # What the history holds of each kind, so h and H are offered only when they would show something.
        self.has_done = self.has_deleted = False
        # How many of each kind are out of sight now: the header says so.
        self.hidden: dict[str, int] = {}
        self.collapsed = set(view.get("collapsed", []))
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

    def call_from_thread(self, callback, *args, **kwargs):
        """A step that outlives the view (a docker command that took longer than the window was
        open, a start after q) has nobody to tell: its loop is closed. Textual would raise in the
        thread, or leave the callback's coroutine unawaited; here the message is dropped."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        return super().call_from_thread(callback, *args, **kwargs)

    def action_settings(self) -> None:
        self.push_screen(Settings())

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
                # The turn under way writes its running cost at every step; the same reason.
                live_at(task),
                # What the row says and whether w has something to show move without the state:
                # a session recorded after the start, a preparation that ends, a log opened.
                self.views[st.id].status,
                watchable(task, st, st.id in self.running),
            )
            for task, st in self.pairs
        )
        pods = tuple(sorted((k, v.state, len(v.reachable)) for k, v in self.pods.items()))
        # With no project the panel says how to add one, and n is hidden until there is one.
        return (
            tasks,
            pods,
            self.show_done,
            self.show_deleted,
            self.selected_id(),
            len(self.done),
            tuple(sorted(self.problems.items())),
            tuple(sorted(self.collapsed)),
            self.has_done,
            self.has_deleted,
            tuple(sorted(self.hidden.items())),
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
        # A row keeps its place whatever its task does: newest first, by number, not by state.
        self.pairs = pairs = sorted(pairs, key=lambda p: ui.task_number(p[1].id), reverse=True)
        live = {st.id for _, st in pairs}
        known = projects()
        self.take_history(live, known)
        self.problems = {name: actions.project_problem(name) for name in known}
        now = self.snapshot()
        if now == self.drawn:
            self.look_at_pods([st.id for _, st in pairs])
            return
        self.drawn = now
        self.fill_table(pairs, selected)

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
        save_view(show_done=self.show_done)
        self.reload()

    def action_toggle_deleted(self) -> None:
        self.show_deleted = not self.show_deleted
        save_view(show_deleted=self.show_deleted)
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
            # A forgotten project's row stays for its history, with no file or repository behind it.
            if not self.on_project_row() or self.selected_project() not in self.problems:
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
        if action == "toggle_deleted":
            return self.has_deleted
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

    def action_verify_again(self) -> None:
        task, _ = self.selected()
        try:
            actions.verify_again(task)
        except Exception as e:
            self.fail(e)
        else:
            self.go_on(task, f"Verifying {task.id} again")
        self.reload()

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

    def busy_with(self, task_id: str, doing: str) -> None:
        """What a slow step (the demo, starting or stopping the task) is doing, in the task's
        status, with the spinner a working agent has; "" when it is done, one way or the other."""
        if doing:
            self.starting[task_id] = doing
        else:
            self.starting.pop(task_id, None)
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

    def action_help(self) -> None:
        self.push_screen(Help(self.editor_note()))

    def editor_note(self) -> str:
        """What o opens with on this machine: chosen, or the first editor found."""
        if self.config.ide:
            return f"o opens with {self.config.ide} (config.toml; k changes it)."
        found = ide.candidates()
        if not found:
            return "o has no editor to open with: none found here; k sets one."
        return (
            f"o opens with {found[0].command}, the first editor found here, or what .idea or .vscode "
            "point at (k changes it)."
        )

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

    @work(thread=True)
    def create(self, form: dict) -> None:
        try:
            task = actions.create(
                form["project"],
                form["goal"],
                auto=form["auto"],
                kind=form["kind"],
                roles=form.get("roles"),
                review_mode=form.get("review", ""),
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
