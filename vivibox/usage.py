"""How long a task took, from its events: each role's turns (turn_started to turn), the
verifications, and the whole of it from its creation to done, or to now while it lives. What u
shows and `vivibox usage` prints, for a note to paste the numbers into.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from . import actions, stats, ui
from .config import load_config
from .task import list_tasks

COLUMNS = ("TASK", "PLAN", "WRITE", "REVIEW", "GATE", "TOTAL")
# A role's turns come from the states it works in, for events written before turns named their role.
ROLE_OF_STATE = {**dict.fromkeys(ui.PLANNING_STATES, "planner"), "review": "reviewer"}
COLUMN_OF_ROLE = {"planner": "plan", "writer": "write", "reviewer": "review"}


@dataclass
class Usage:
    task: str
    project: str
    live: bool
    # Seconds.
    plan: float = 0.0
    write: float = 0.0
    review: float = 0.0
    gate: float = 0.0
    total: float = 0.0


def seconds_between(start: str, end: str | datetime) -> float:
    end = end if isinstance(end, datetime) else datetime.fromisoformat(end)
    return max((end - datetime.fromisoformat(start)).total_seconds(), 0.0)


def of_events(
    task_id: str, project: str, events: list[dict], live: bool, now: datetime | None = None
) -> Usage:
    """One task's times. A turn under way counts until now on a live task; a verification from
    before gates said how long they took counts from entering verify to its result."""
    now = now or datetime.now(UTC)
    used = Usage(task_id, project, live)
    started: tuple[str, str] | None = None  # (column, ts) of the turn under way
    verifying = ""
    for event in events:
        kind, data, ts = event["type"], event.get("data", {}), event["ts"]
        if kind == "turn_started":
            role = data.get("role") or ROLE_OF_STATE.get(data.get("state"), "writer")
            started = (COLUMN_OF_ROLE.get(role, "write"), ts)
        elif kind == "turn" and started:
            column, since = started
            setattr(used, column, getattr(used, column) + seconds_between(since, ts))
            started = None
        elif kind == "state" and data.get("current") == "verify":
            verifying = ts
        elif kind == "gate":
            if isinstance(data.get("seconds"), (int, float)):
                used.gate += data["seconds"]
            elif verifying:
                used.gate += seconds_between(verifying, ts)
            verifying = ""
    if started and live:
        column, since = started
        setattr(used, column, getattr(used, column) + seconds_between(since, now))
    if events:
        done = next(
            (e["ts"] for e in events if e["type"] == "state" and e["data"].get("current") == "done"), ""
        )
        end = done or (now if live else events[-1]["ts"])
        used.total = seconds_between(events[0]["ts"], end)
    return used


def gather(finished: bool = True, project: str = "") -> list[Usage]:
    """Every live task, newest first, then the finished ones from their archive when asked for."""
    rows = []
    live = list_tasks(load_config().tasks_dir)
    for task in reversed(live):
        st = task.read_state()
        if not project or st.project == project:
            rows.append(of_events(task.id, st.project, task.events(), live=True))
    if finished:
        ids = {task.id for task in live}
        for entry in actions.history(limit=None):
            if entry["id"] in ids or (project and entry.get("project") != project):
                continue
            events = stats.read_events(actions.archive_path(entry["id"]) / "events.jsonl")
            if events:
                rows.append(of_events(entry["id"], entry.get("project", ""), events, live=False))
    return rows


def duration(seconds: float) -> str:
    """ "45 s", "12 min", "1 h 02"; "-" for nothing."""
    seconds = int(seconds)
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02}"


def cells(used: Usage) -> list[str]:
    return [used.task, *(duration(getattr(used, c.lower())) for c in COLUMNS[1:])]


def report(rows: list[Usage]) -> str:
    """The rows as a table for a terminal: one task a row, the columns u shows."""
    if not rows:
        return "No tasks yet.\n"
    table = [list(COLUMNS), *(cells(u) for u in rows)]
    widths = [max(len(row[i]) for row in table) for i in range(len(COLUMNS))]
    lines = []
    for row in table:
        padded = [
            cell.ljust(w) if i == 0 else cell.rjust(w)
            for i, (cell, w) in enumerate(zip(row, widths, strict=True))
        ]
        lines.append("  ".join(padded).rstrip())
    return "\n".join(lines) + "\n"


def as_dicts(rows: list[Usage]) -> list[dict]:
    return [{k: round(v, 1) if isinstance(v, float) else v for k, v in asdict(u).items()} for u in rows]
