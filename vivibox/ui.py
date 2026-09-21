"""Terminal output for people: colours when writing to a terminal, plain text otherwise."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from .states import State
from .task import Task, TaskState

CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def use_color(stream=None) -> bool:
    """https://no-color.org; also off when piped, so grep and scripts see plain text."""
    stream = stream or sys.stdout
    return "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb" and stream.isatty()


class Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return f"\033[{';'.join(CODES[s] for s in styles)}m{text}\033[0m"


def width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def shorten(text: str, limit: int) -> str:
    """Cuts to fit at a space, because a word cut in half reads like something went wrong. Falls back
    to cutting mid-word when the last space is so far back that whole words would be lost."""
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 0)]
    word, _, _ = cut.rpartition(" ")
    return (word if len(word) >= limit // 2 else cut).rstrip() + "…"


def ago(ts: str, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now(UTC)) - datetime.fromisoformat(ts)).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


@dataclass(frozen=True)
class Spend:
    """What a task cost, kept apart by whether it was money. A subscription turn reports a list
    price, so adding the two would give a number that is neither a bill nor a usage figure."""

    metered: float = 0.0
    listed: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.metered or self.listed)

    def __str__(self) -> str:
        if not self.listed:
            return f"${self.metered:.2f}"
        return f"${self.metered:.2f} + ${self.listed:.2f}*" if self.metered else f"${self.listed:.2f}*"


def cost(task: Task) -> Spend:
    metered = listed = 0.0
    for event in task.events():
        if event["type"] != "turn":
            continue
        spent = event["data"].get("cost") or 0
        # Turns recorded before roles were all metered, and said nothing either way.
        if event["data"].get("metered", True):
            metered += spent
        else:
            listed += spent
    return Spend(round(metered, 6), round(listed, 6))


# What the task needs, in words, and the commands for your next step.
WAITING = {
    State.CHECKPOINT_PLAN: ("review the plan", ["vivibox accept {id}", 'vivibox reply {id} "…"']),
    State.CHECKPOINT_FINAL: ("review the work", ["vivibox accept {id}", 'vivibox reply {id} "…"']),
    State.CHECKPOINT_BLOCKED: ("needs your help", ["vivibox status {id}", 'vivibox reply {id} "…"']),
    State.APPROVAL_RISKY: ("approve risky files", ["vivibox risky {id}"]),
}
WORKING = {State.PLAN: "planning", State.IMPLEMENT: "implementing", State.VERIFY: "verifying"}


def group(st: TaskState) -> str:
    if st.state is State.DONE:
        return "Done"
    if st.state in WAITING:
        return "Waiting for you"
    return "Stopped" if st.paused else "Working"


# A manual planner's checkpoint before you brought a plan: nothing to review yet.
AWAITING_PLAN = ("plan it yourself", ["vivibox plan prompt {id}", "vivibox plan import {id}"])


def activity(st: TaskState, max_iterations: int) -> str:
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        return AWAITING_PLAN[0]
    if st.state in WAITING:
        return WAITING[st.state][0]
    if st.state is State.DONE:
        return "done"
    text = WORKING[st.state]
    if st.state is not State.PLAN and st.iteration > 1:
        text += f" (attempt {st.iteration}/{max_iterations})"
    return text


def next_commands(st: TaskState) -> list[str]:
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        return [c.format(id=st.id) for c in AWAITING_PLAN[1]]
    if st.state in WAITING:
        return [c.format(id=st.id) for c in WAITING[st.state][1]]
    if st.paused:
        return [f"vivibox resume {st.id}"]
    if st.state is State.DONE:
        return [f"vivibox rm {st.id}"]
    return [f"vivibox attach {st.id}"]


COLORS = {"Waiting for you": "yellow", "Working": "cyan", "Stopped": "dim", "Done": "green"}
ORDER = list(COLORS)


def task_list(tasks: list[Task], criteria, max_iterations: int, style: Style, now=None) -> str:
    """One row per task, like kubectl get: the tasks waiting for you first, the goal fills the rest."""
    states = [(task, task.read_state()) for task in tasks]
    states.sort(key=lambda ts: ORDER.index(group(ts[1])))
    header = ("TASK", "STATUS", "CRITERIA", "COST", "CREATED", "UPDATED", "GOAL")
    rows = []
    for task, st in states:
        spent = cost(task)
        rows.append(
            (
                st.id,
                activity(st, max_iterations) if group(st) != "Stopped" else "stopped",
                criteria(task),
                str(spent) if spent else "-",
                ago(st.created, now),
                ago(st.updated, now),
                st.goal,
                COLORS[group(st)],
            )
        )
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(6)]
    # Piped output keeps the whole goal, for grep.
    goal_width = max(width() - sum(widths) - 3 * 6, 20) if style.color else 10_000
    lines = ["   ".join([*(h.ljust(w) for h, w in zip(header[:6], widths, strict=True)), header[6]])]
    for *cells, goal, color in rows:
        padded = [c.ljust(w) for c, w in zip(cells, widths, strict=True)]
        padded[1] = style(padded[1], color)
        lines.append("   ".join([*padded, shorten(goal, goal_width)]))
    return "\n".join(lines) + "\n"


def task_detail(task: Task, criteria, max_iterations: int, events: int, style: Style) -> str:
    st = task.read_state()
    name = group(st)
    meta = [f"{criteria(task)} criteria", ago(st.updated)]
    if spent := cost(task):
        meta.append(str(spent))
    lines = [
        f"{style(st.id, 'bold')}  {style(activity(st, max_iterations), COLORS[name])}"
        f"  {style(' · '.join(meta), 'dim')}",
        "",
        st.goal,
        "",
        f"{style('Plan', 'bold')}  {task.plan_path}",
        f"{style('Next', 'bold')}  " + "   ".join(next_commands(st)),
        "",
        style("Recent events", "bold"),
    ]
    cols = width()
    for e in task.events()[-events:]:
        when = datetime.fromisoformat(e["ts"]).astimezone().strftime("%H:%M:%S")
        data = "  ".join(f"{k}={v}" for k, v in e["data"].items() if v not in ("", None))
        kind = style(e["type"].ljust(8), "red" if e["type"] == "error" else "cyan")
        lines.append(f"  {style(when, 'dim')}  {kind}  {shorten(data, cols - 22)}")
    return "\n".join(lines) + "\n"
