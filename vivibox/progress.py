"""The writer's live todo list, with exact acceptance items mirrored to its checklist."""

import json

from . import gate
from .plan import CHECKBOX, PlanError, checkboxes, parse_plan
from .states import State
from .task import Task

FILE = "writer-progress.json"
STATUSES = {"pending", "in_progress", "completed", "cancelled"}


def record(task: Task, event: dict) -> None:
    """Only completed todowrite calls in this writer's implementing session are reports."""
    if event.get("type") != "tool_use":
        return
    part = event.get("part") or {}
    state = part.get("state") or {}
    if part.get("tool") != "todowrite" or state.get("status") != "completed":
        return
    st = task.read_state()
    session = event.get("sessionID")
    if st.state is not State.IMPLEMENT or st.paused or not session or session != st.sessions.get("writer"):
        return
    todos = (state.get("input") or {}).get("todos")
    if not isinstance(todos, list) or any(
        not isinstance(t, dict) or not isinstance(t.get("content"), str) or t.get("status") not in STATUSES
        for t in todos
    ):
        return
    todos = [{"content": t["content"].strip(), "status": t["status"]} for t in todos]
    path = task.meta / FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"session": session, "todos": todos}) + "\n")
    temporary.replace(path)
    mirror(task, todos)


def mirror(task: Task, todos: list[dict]) -> None:
    """The gate still reads criteria.md. No fuzzy matching and no changes to criterion text."""
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    try:
        required = {c.text for c in parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria}
        original = path.read_text()
    except (OSError, PlanError):
        return
    states = {t["content"]: t["status"] == "completed" for t in todos if t["content"] in required}
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if CHECKBOX.match(line)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        item = checkboxes("".join(lines[start:end]))[0]
        if item.text in states and item.done != states[item.text]:
            mark = CHECKBOX.match(lines[start]).start(1)
            lines[start] = (
                lines[start][:mark] + ("x" if states[item.text] else " ") + lines[start][mark + 1 :]
            )
    changed = "".join(lines)
    # A tool editing criteria.md concurrently wins. The next todo report can reconcile it.
    if changed != original and path.read_text() == original:
        path.write_text(changed)


def read(task: Task) -> list[dict]:
    try:
        value = json.loads((task.meta / FILE).read_text())
        if value["session"] == task.read_state().sessions.get("writer"):
            return value["todos"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return []
