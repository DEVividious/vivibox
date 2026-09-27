"""The task list: which columns fit the terminal, the project rows and their summaries, the
task rows, the spinner and the header's counts. Mixed into the app in tui.py.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

from rich.markup import escape
from rich.text import Text
from textual.content import Content
from textual.events import Resize
from textual.widgets import Static

from . import actions, look, orchestration, ui
from .dialogs import NO_PROJECTS
from .panel import CODE_CHANGED, PROJECT_ROW, SPINNER, criteria
from .task import TaskState


def finished_on(stamp: str):
    """The local day a history entry's timestamp falls on."""
    return datetime.fromisoformat(stamp).astimezone().date()


def short_ago(ts: str) -> str:
    """How long ago, in a few characters: "now", "12m", "3h", "2d"; the list has many of them."""
    since = ui.ago(ts)
    if since == "just now":
        return "now"
    number, unit, _ = since.split(" ")
    return f"{number}{unit[0]}"


# Columns whose figures line up on the right, so they can be compared down the list.
RIGHT = {"CRITERIA", "COST", "PLAN", "IMPL", "REVIEW", "CREATED", "UPDATED"}
# What an empty cell shows: a faint dot, not a dash in the foreground on every row.
NONE = look.faint("·")
# A project with no task yet: said as an empty state, with the key that fills it.
EMPTY_PROJECT = f"[i {look.MUTED}]no tasks yet[/]  [{look.ACCENT}]n[/] [{look.MUTED}]new task[/]"


class TaskTable:
    # The columns a terminal has room for, narrowest first: task, status and goal always. What a
    # task cost is one figure where the terminal has room for one more column (COST), and each
    # role's share apart where it has room for all (PLAN, IMPL, REVIEW), not a sum to read.
    COLUMNS = ("TASK", "STATUS", "APP", "CRITERIA", "PLAN", "IMPL", "REVIEW", "CREATED", "UPDATED", "GOAL")

    NARROW = ("TASK", "STATUS", "GOAL")

    MEDIUM = ("TASK", "STATUS", "CRITERIA", "COST", "UPDATED", "GOAL")

    def columns_for(self, width: int) -> tuple[str, ...]:
        if width < 100:
            return self.NARROW
        if width < 130:
            return self.MEDIUM
        # The reviewer's figure only where someone other than the writer reviews: a list without
        # one looks as it did.
        return (
            self.COLUMNS
            if orchestration.MODES[self.config.orchestration].reviews()
            else tuple(c for c in self.COLUMNS if c != "REVIEW")
        )

    def set_columns(self, width: int | None = None) -> None:
        wanted = self.columns_for(self.size.width if width is None else width)
        if wanted == self.columns:
            return
        self.columns = wanted
        table = self.table
        table.clear(columns=True)
        keys = [
            table.add_column(Text(name, justify="right") if name in RIGHT else name, key=name)
            for name in wanted
        ]
        by_name = dict(zip(wanted, keys, strict=True))
        self.status_column, self.demo_column = by_name["STATUS"], by_name.get("APP")
        self.plan_column, self.impl_column = by_name.get("PLAN"), by_name.get("IMPL")
        self.review_column, self.updated_column = by_name.get("REVIEW"), by_name.get("UPDATED")
        self.drawn = ()

    def on_resize(self, event: Resize) -> None:
        if self.table is not None:  # a resize before the view is built has nothing to lay out
            self.set_columns(event.size.width)
            self.reload()

    def take_history(self, live: set[str], known: list[str]) -> None:
        """The finished tasks the list shows, and how many of each kind are out of sight."""
        # A forgotten project's history goes with it: with no project, the view is as it starts.
        kept = [e for e in actions.history() if e["id"] not in live and e.get("project") in known]
        shown = lambda e: self.show_deleted if e.get("deleted") else self.show_done  # noqa: E731
        self.done = sorted((e for e in kept if shown(e)), key=lambda e: ui.task_number(e["id"]), reverse=True)
        # What the tasks finished today cost; the live ones are added where the header is set.
        today = datetime.now().astimezone().date()
        self.spent_finished = sum(
            e.get("cost") or 0 for e in kept if e.get("finished") and finished_on(e["finished"]) == today
        )
        self.hidden = {}
        for entry in kept:
            if not shown(entry):
                kind = "deleted" if entry.get("deleted") else "done"
                self.hidden[kind] = self.hidden.get(kind, 0) + 1
        self.has_done = any(not e.get("deleted") for e in kept)
        self.has_deleted = any(e.get("deleted") for e in kept)

    def project_order(self, pairs: list) -> list[str]:
        """Projects by name, whatever their tasks do: a project that moved when a task of its
        started to wait moved every row under the cursor. What waits for you says so by its colour,
        the header's count and where the cursor starts. A project a task belongs to is listed even
        when its file is gone, so the task is not orphaned off the screen."""
        names = (
            set(self.problems) | {st.project for _, st in pairs} | {e.get("project", "") for e in self.done}
        )
        return sorted(names)

    def project_summary(self, name: str, tasks: list, done: int) -> str:
        """The project row's status: what keeps its tasks from starting, else what they are doing,
        so a collapsed project still says what waits for you."""
        waiting = sum(self.views[st.id].group == "Waiting for you" for _, st in tasks)
        working = sum(self.busy(st) for _, st in tasks)
        stopped = len(tasks) - waiting - working
        parts = [look.colored(f"{waiting} waiting for you", look.WAITING)] * bool(waiting)
        parts += [look.colored(f"{working} working", look.WORKING)] * bool(working)
        parts += [look.secondary(f"{stopped} stopped")] * bool(stopped)
        parts += [look.secondary(f"{done} done")] * bool(done)
        return look.muted(" · ").join(parts) or EMPTY_PROJECT

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
                f"[{look.ERROR}]✕ {escape(problem)}[/]" if problem
                else f"[{look.WAITING}]● {waiting} waiting for you[/]" if folded and waiting
                else ""
            )  # fmt: skip
            fold = look.muted("▸" if folded else "▾")
            planned.append((
                {"TASK": f"{fold} [b]{escape(name)}[/]", "STATUS": status, "GOAL": summary},
                PROJECT_ROW + name,
            ))  # fmt: skip
            if folded:
                continue
            for task, st in own:
                # During a turn, when the agent last finished a step: the sign it is at work.
                live = task.live_turn()
                ticked = criteria(task)
                spent = ui.cost(task)
                plan, impl, review = self.cost_cells(spent)
                planned.append((
                    {"TASK": self.task_name(st.id, name), "STATUS": self.status(st),
                     "APP": self.demo_cell(st.id), "CRITERIA": NONE if ticked == "-" else ticked,
                     "COST": self.total_cell(spent), "PLAN": plan, "IMPL": impl, "REVIEW": review,
                     "CREATED": look.muted(short_ago(st.created)),
                     "UPDATED": look.muted(short_ago(live["at"] if live else st.updated)),
                     "GOAL": escape(st.goal)},
                    st.id,
                ))  # fmt: skip
            for entry in done:
                # A finished task is history: muted, so the live ones stand out.
                spent = ui.finished_spend(entry)
                plan, impl, review = self.cost_cells(spent, muted=True)
                planned.append((
                    {"TASK": self.task_name(entry["id"], name, finished=True),
                     "STATUS": look.finished_badge(bool(entry.get("deleted"))),
                     "APP": NONE, "CRITERIA": NONE, "COST": self.total_cell(spent, muted=True),
                     "PLAN": plan, "IMPL": impl, "REVIEW": review,
                     "CREATED": look.muted(short_ago(entry["created"])) if entry.get("created") else NONE,
                     "UPDATED": look.muted(short_ago(entry["finished"])),
                     "GOAL": look.secondary(entry["title"])},
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
        # The goal's column reaches the right edge whatever its goals: a wide terminal is used, and
        # the cursor's row and the header span the screen.
        goal_column = table.columns.get("GOAL")
        if goal_column is not None:
            goal_column.auto_width = False
            goal_column.width = goal_width
        ids = [key for _, key in planned]
        for cells, key in planned:
            goal = Text.from_markup(cells.get("GOAL", ""))
            goal.truncate(goal_width, overflow="ellipsis")
            table.add_row(
                *(goal if name == "GOAL" else self.cell(name, cells.get(name, "")) for name in self.columns),
                key=key,
            )
        empty = self.query_one("#empty", Static)
        table.display, empty.display = bool(ids), not ids
        if ids and self.focused is None:  # hiding the list took its focus; the arrows are for it
            table.focus()
        if not ids:
            # Nothing left to show details of: the view is back to how it starts.
            self.panel.add_class("hidden")
        empty.update("" if ids else NO_PROJECTS)
        if selected in ids:
            table.move_cursor(row=ids.index(selected))
        waiting_now = {st.id for _, st in pairs if self.views[st.id].group == "Waiting for you"}
        if selected not in ids and ids:
            # The first task waiting for you, else the first task; a project row is a heading,
            # not what you came for.
            tasks = [i for i, key in enumerate(ids) if not key.startswith(PROJECT_ROW)]
            first = next((i for i in tasks if ids[i] in waiting_now), tasks[0] if tasks else 0)
            table.move_cursor(row=first)
        # A task that starts to wait for you rings the bell, once: the sign you can hear from
        # another window when the desktop's notifications are off.
        if self.waiting_ids is not None and waiting_now - self.waiting_ids:
            self.bell()
        self.waiting_ids = waiting_now
        self.waiting = len(waiting_now)
        self.working = sum(self.busy(st) for _, st in pairs)
        self.spent_today = self.spent_finished + sum(ui.cost(task).total for task, _ in pairs)
        self.set_sub_title()
        self.show_detail()
        self.refresh_bindings()
        self.look_at_pods([st.id for _, st in pairs])

    @staticmethod
    def cell(column: str, markup: str) -> Text:
        return Text.from_markup(markup, justify="right" if column in RIGHT else "left")

    @staticmethod
    def task_name(task_id: str, project: str, finished: bool = False) -> str:
        """A task under its project, indented: a live one whole in the foreground, a finished one
        quieter, the project's part of its id muted."""
        prefix = f"{project}-"
        rest = task_id.removeprefix(prefix) if task_id.startswith(prefix) else task_id
        if finished:
            return f"  {look.muted(prefix) if rest != task_id else ''}{look.secondary(rest)}"
        # A live task's name is content, whole in the foreground.
        return f"  {escape(task_id)}"

    @staticmethod
    def cost_cells(spent: ui.Spend, muted: bool = False) -> tuple[str, ...]:
        """PLAN, IMPL and REVIEW: a figure each, a faint dot for none."""
        return tuple(
            NONE if cell == "-" else (look.muted if muted else look.secondary)(cell)
            for cell in ui.cost_cells(spent)
        )

    @staticmethod
    def total_cell(spent: ui.Spend, muted: bool = False) -> str:
        if not spent:
            return NONE
        return (look.muted if muted else look.secondary)(ui.money(spent.total))

    def demo_cell(self, task_id: str) -> str:
        """Whether this task is serving anything, on the row itself: the list is what you look at.
        The addresses stay in the panel, where they are clickable and all of them fit; a single port
        here would have to pick one of a front end and a back end, and pick it silently."""
        view = self.pods.get(task_id)
        if view is None or not view.address or not (state := view.state):
            return NONE
        color = {
            "live": look.SUCCESS,
            "local": look.WAITING,
            "starting": look.WAITING,
            "stopped": look.ERROR,
        }[state]
        # How many came up separates "the back end died" from "everything is there".
        count = f" ×{len(view.reachable)}" if len(view.reachable) > 1 else ""
        return f"[{color}]{state}{count}[/]"

    def set_sub_title(self) -> None:
        # First what would keep every task from starting: Docker down, a provider without a key.
        parts = [(self.machine_note, f"bold {look.ERROR}")] if self.machine_note else []
        if self.waiting:
            parts.append((f"● {self.waiting} waiting for you", f"bold {look.WAITING}"))
        else:
            parts.append(("nothing waiting for you", look.MUTED))
        if self.working:
            parts.append((f"{self.working} working", look.WORKING))
        # What h and H keep out of sight, so a list that looks short is not a surprise.
        parts += [(f"{count} {kind} hidden", look.MUTED) for kind, count in sorted(self.hidden.items())]
        # The limits are in dollars and every row shows its own figure; the day's sum is here.
        if self.spent_today:
            parts.append((f"${self.spent_today:.2f} today", look.MUTED))
        if self.code_changed:
            parts.append((CODE_CHANGED, look.WAITING))
        self.sub_title = " · ".join(text.removeprefix("● ") for text, _ in parts)
        # The window's title, for the taskbar: how many wait for you.
        self.title = f"vivibox ({self.waiting})" if self.waiting else "vivibox"
        # The line above the list: the same words, each in its meaning's colour.
        pieces: list = [("vivibox", "bold")]
        for text, style in parts:
            pieces += [("   " if len(pieces) == 1 else "  ·  ", look.FAINT), (text, style)]
        with contextlib.suppress(Exception):  # before the view is built there is no bar to draw on
            self.query_one("#state", Static).update(Content.assemble(*pieces))

    def status(self, st: TaskState) -> str:
        seen = self.seen(st)
        spinner = SPINNER[self.frame % len(SPINNER)] if self.busy(st) else ""
        if doing := self.starting.get(st.id):
            return f"[{look.WORKING}]{spinner or ' '} {escape(doing)}[/]"
        return look.badge(seen, spinner)

    def spin(self) -> None:
        """Turns the spinner of busy tasks between full refreshes, touching only their status cells."""
        self.frame += 1
        table = self.table
        for _, st in self.pairs:
            if self.busy(st):
                table.update_cell(st.id, self.status_column, self.cell("STATUS", self.status(st)))
        with contextlib.suppress(Exception):
            self.query_one("#clock", Static).update(datetime.now().strftime("%H:%M"))
