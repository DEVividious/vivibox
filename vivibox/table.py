"""The task list: which columns fit the terminal, the project rows and their summaries, the
task rows, the spinner and the header's counts. Mixed into the app in tui.py.
"""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from textual.widgets import Static

from . import actions, ui
from .dialogs import NO_PROJECTS
from .panel import CODE_CHANGED, PROJECT_ROW, SPINNER, criteria
from .task import TaskState


class TaskTable:
    # The columns a terminal has room for, narrowest first: task, status and goal always.
    COLUMNS = ("TASK", "STATUS", "DEMO", "CRITERIA", "PLAN", "IMPL", "REVIEW", "CREATED", "UPDATED", "GOAL")

    NARROW = ("TASK", "STATUS", "GOAL")

    MEDIUM = ("TASK", "STATUS", "CRITERIA", "UPDATED", "GOAL")

    def columns_for(self, width: int) -> tuple[str, ...]:
        if width < 100:
            return self.NARROW
        if width < 130:
            return self.MEDIUM
        # The reviewer's figure only where there is a reviewer: a list without one looks as it did.
        return (
            self.COLUMNS
            if "reviewer" in self.config.roles
            else tuple(c for c in self.COLUMNS if c != "REVIEW")
        )

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
        self.plan_column, self.impl_column = by_name.get("PLAN"), by_name.get("IMPL")
        self.review_column, self.updated_column = by_name.get("REVIEW"), by_name.get("UPDATED")
        self.drawn = ()

    def on_resize(self) -> None:
        if self.table is not None:  # a resize before the view is built has nothing to lay out
            self.set_columns()
            self.reload()

    def take_history(self, live: set[str], known: list[str]) -> None:
        """The finished tasks the list shows, and how many of each kind are out of sight."""
        # A forgotten project's history goes with it: with no project, the view is as it starts.
        kept = [e for e in actions.history() if e["id"] not in live and e.get("project") in known]
        shown = lambda e: self.show_deleted if e.get("deleted") else self.show_done  # noqa: E731
        self.done = sorted((e for e in kept if shown(e)), key=lambda e: ui.task_number(e["id"]), reverse=True)
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
                plan, impl, review = ui.cost_cells(ui.cost(task))
                # During a turn, when the agent last finished a step: the sign it is at work.
                live = task.live_turn()
                planned.append((
                    {"TASK": f"  {st.id}", "STATUS": self.status(st), "DEMO": self.demo_cell(st.id),
                     "CRITERIA": criteria(task), "PLAN": plan, "IMPL": impl, "REVIEW": review,
                     "CREATED": ui.ago(st.created), "UPDATED": ui.ago(live["at"] if live else st.updated),
                     "GOAL": st.goal},
                    st.id,
                ))  # fmt: skip
            for entry in done:
                plan, impl, review = ui.cost_cells(ui.finished_spend(entry))
                planned.append((
                    {"TASK": f"  {entry['id']}",
                     "STATUS": "[grey50]  deleted[/]" if entry.get("deleted") else "[green]  done[/]",
                     "DEMO": "-", "CRITERIA": "-", "PLAN": plan, "IMPL": impl, "REVIEW": review,
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
        # What h and H keep out of sight, so a list that looks short is not a surprise.
        parts += [f"{count} {kind} hidden" for kind, count in sorted(self.hidden.items())]
        if self.code_changed:
            parts.append(CODE_CHANGED)
        self.sub_title = " · ".join(parts)
        # The window's title, for the taskbar: how many wait for you.
        self.title = f"vivibox ({self.waiting})" if self.waiting else "vivibox"

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
