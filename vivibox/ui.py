"""Terminal output for people: colours when writing to a terminal, plain text otherwise."""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


def clock(ts: str) -> str:
    """An event's time of day on your clock. Events are kept in UTC, and a time cut out of one is
    hours away from the clock on your screen."""
    return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")


def ago(ts: str, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now(UTC)) - datetime.fromisoformat(ts)).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


# The states whose turns are planning; every other turn is implementing, fixing, or a demo.
PLANNING_STATES = {str(State.PLAN), str(State.CHECKPOINT_PLAN)}


@dataclass(frozen=True)
class Spend:
    """What a task cost, planning and implementation apart: the stronger model plans and the
    cheaper one writes, and the split shows whether that is where the money goes."""

    planning: float = 0.0
    implementation: float = 0.0

    @property
    def total(self) -> float:
        return round(self.planning + self.implementation, 6)

    def __bool__(self) -> bool:
        return bool(self.planning or self.implementation)

    def __str__(self) -> str:
        return f"${self.planning:.2f} + ${self.implementation:.2f}"


def finished_cost(entry: dict) -> str:
    """A finished task's cost as the list shows a live one; tasks finished before the split was
    kept show their total."""
    total = entry.get("cost", 0)
    if "planning" not in entry:
        return f"${total:.2f}"
    return str(Spend(entry["planning"], round(total - entry["planning"], 6)))


def cost(task: Task) -> Spend:
    planning = implementation = 0.0
    for event in task.events():
        if event["type"] != "turn":
            continue
        spent = event["data"].get("cost") or 0
        if event["data"].get("state") in PLANNING_STATES:
            planning += spent
        else:
            implementation += spent
    return Spend(round(planning, 6), round(implementation, 6))


# What the task needs, in words, and the commands for your next step.
WAITING = {
    State.CHECKPOINT_PLAN: ("review the plan", ["vivibox accept {id}", 'vivibox reply {id} "…"']),
    State.CHECKPOINT_FINAL: ("review the work", ["vivibox accept {id}", 'vivibox reply {id} "…"']),
    State.CHECKPOINT_BLOCKED: ("needs your help", ['vivibox reply {id} "…"', "vivibox attach {id}"]),
    State.APPROVAL_RISKY: ("approve risky files", ["vivibox risky {id}"]),
}
WORKING = {State.PLAN: "planning", State.IMPLEMENT: "implementing", State.VERIFY: "verifying"}


def when_deleted(state: str) -> str:
    """What a deleted task was doing, in the list's own words; "" for a state no longer known."""
    try:
        was = State(state)
    except ValueError:
        return ""
    if was in WORKING:
        return f"while {WORKING[was]}"
    if was is State.CHECKPOINT_BLOCKED:
        return "while it needed your help"
    return f"while it waited for you to {WAITING[was][0]}" if was in WAITING else ""


def why_blocked(task: Task) -> Path | None:
    """The file that says why a blocked task needs you: the agent's question, or what the last
    verification found."""
    handoff = task.meta / "handoff"
    return next((p for p in (handoff / "question.md", handoff / "verify-feedback.md") if p.exists()), None)


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


@dataclass(frozen=True)
class TaskView:
    """What a task is doing and what it needs, worked out once: the list, the details panel and
    the command line all show this, so they cannot call one task three different things."""

    status: str
    group: str
    # Where it sorts: your decisions first, then what failed, then what is not running, then the rest.
    rank: int
    # The reason behind a status that says something went wrong, in the failing tool's words.
    problem: str = ""
    commands: tuple[str, ...] = ()


WAITS, WORKS, STOPPED, DONE = ORDER
# Within "Waiting for you": a decision of yours, something that failed, something nobody is running.
DECISION, FAILED, IDLE, AT_WORK, PARKED, FINISHED = range(6)


def view(task: Task, st: TaskState, running: bool, max_iterations: int) -> TaskView:
    """running: whether the task's supervisor is alive. Whatever will not move without you waits
    for you; "Stopped" is only what you stopped yourself."""
    if st.state is State.DONE:
        return TaskView("done", DONE, FINISHED, commands=(f"vivibox rm {st.id}",))
    if st.state in WAITING:
        status = activity(st, max_iterations)
        if st.state is State.CHECKPOINT_BLOCKED:
            asks = (task.meta / "handoff" / "question.md").exists()
            status = "agent asks" if asks else f"verification failed {st.iteration}×"
        return TaskView(status, WAITS, DECISION, commands=tuple(next_commands(st)))
    if st.problem:
        what, _, why = st.problem.partition(": ")
        return TaskView(what, WAITS, FAILED, why, (f"vivibox resume {st.id}",))
    if st.paused:
        return TaskView("stopped", STOPPED, PARKED, commands=(f"vivibox resume {st.id}",))
    if not running:
        if any(e["type"] == "started" for e in task.events()):
            return TaskView("not running", WAITS, IDLE, commands=(f"vivibox resume {st.id}",))
        return TaskView("not started", WAITS, IDLE, commands=(f"vivibox start {st.id}",))
    return TaskView(activity(st, max_iterations), WORKS, AT_WORK, commands=(f"vivibox attach {st.id}",))


def task_list(
    tasks: list[Task], criteria, max_iterations: int, style: Style, now=None, running=lambda task: True
) -> str:
    """One row per task, like kubectl get: the tasks waiting for you first, the goal fills the rest."""
    states = [(task, task.read_state()) for task in tasks]
    seen = [(task, st, view(task, st, running(task), max_iterations)) for task, st in states]
    seen.sort(key=lambda found: found[2].rank)
    header = ("TASK", "STATUS", "CRITERIA", "COST PLAN + IMPL", "CREATED", "UPDATED", "GOAL")
    rows = []
    for task, st, shown in seen:
        spent = cost(task)
        rows.append(
            (
                st.id,
                shown.status,
                criteria(task),
                str(spent) if spent else "-",
                ago(st.created, now),
                ago(st.updated, now),
                st.goal,
                COLORS[shown.group],
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


def task_detail(
    task: Task, criteria, max_iterations: int, events: int, style: Style, running: bool = True
) -> str:
    st = task.read_state()
    shown = view(task, st, running, max_iterations)
    meta = [f"{criteria(task)} criteria", ago(st.updated)]
    if spent := cost(task):
        meta.append(str(spent))
    lines = [
        f"{style(st.id, 'bold')}  {style(shown.status, COLORS[shown.group])}"
        f"  {style(' · '.join(meta), 'dim')}",
        "",
        st.goal,
        "",
        f"{style('Plan', 'bold')}  {task.plan_path}",
    ]
    if st.state is State.CHECKPOINT_BLOCKED and (why := why_blocked(task)):
        lines.append(f"{style('Why', 'bold')}   {why}")
    if shown.problem:
        lines.append(f"{style('Why', 'bold')}   {style(shown.problem, 'red')}")
    lines += [
        f"{style('Next', 'bold')}  " + "   ".join(shown.commands),
        "",
        style("Recent events", "bold"),
    ]
    cols = width()
    for e in task.events()[-events:]:
        when = clock(e["ts"])
        data = "  ".join(f"{k}={v}" for k, v in e["data"].items() if v not in ("", None))
        kind = style(e["type"].ljust(8), "red" if e["type"] == "error" else "cyan")
        lines.append(f"  {style(when, 'dim')}  {kind}  {shorten(data, cols - 22)}")
    return "\n".join(lines) + "\n"


# Lines of a build log that say what went wrong, in the usual tools' words.
TROUBLE = re.compile(
    r"\[ERROR\]|BUILD FAILURE|FAILED|FAILURE|Tests run:.*(Failures: [1-9]|Errors: [1-9])"
    r"|Exception\b|\berror\b|Error:|permission denied|not found|\bfail(ed|s)?\b",
    re.IGNORECASE,
)


def log_excerpt(text: str, limit: int = 30) -> str:
    """What a failed build said, short enough to read in the view: its trouble lines, or its end
    when none of them looks like trouble. The whole log is a file away."""
    lines = [line.rstrip() for line in text.splitlines()]
    trouble = list(dict.fromkeys(line for line in lines if TROUBLE.search(line)))
    if not trouble:
        return "\n".join(lines[-limit:]).strip()
    shown = trouble[:limit]
    if len(trouble) > limit:
        shown.append(f"… {len(trouble) - limit} more such lines in the full log")
    return "\n".join(shown)
