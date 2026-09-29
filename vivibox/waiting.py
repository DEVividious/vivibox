"""vivibox wait: until a task needs you or stops moving, for an agent's CLI that drives vivibox and
cannot watch the view. It reads each task's state every few seconds, so it depends on no signal
from the supervisor: one that has died sends none, and is itself a reason to stop waiting."""

from __future__ import annotations

import time
from collections.abc import Callable

from .states import State, waits_for_user
from .task import Task, TaskState

INTERVAL = 2.0
# As timeout(1) exits when its time is up, so a script tells it from a task that ended the wait.
TIMED_OUT = 124
# The shape of wait --json; raised when a field changes or goes, not when one is added.
JSON_VERSION = 1


def reason(st: TaskState, running: bool) -> str:
    """Why this task ends a wait, or "" while it is moving on its own."""
    if st.state is State.DONE:
        return "done"
    if st.problem:
        return "problem"
    if waits_for_user(st.state):
        return "waiting"
    if st.paused:
        return "stopped"
    if not running:
        return "not running"
    if st.awaiting_review and st.state is State.REVIEW:
        return "review"  # for the agent's CLI, which reviews the round (ADR-0035)
    return ""


def said(task: Task) -> str:
    """What vivibox said when the task last changed state: the checkpoint's own words, such as a
    test removed, which the state alone does not carry."""
    last = ""
    for event in task.events():
        if event["type"] == "state":
            last = event["data"].get("reason", "")
    return last


def wait(
    tasks: list[Task],
    running: Callable[[Task], bool],
    timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[Task, TaskState, str]]:
    """The tasks that end the wait, with their state and reason, as soon as any does; [] when the
    timeout passed first. A task already there ends it at once."""
    deadline = None if timeout is None else clock() + timeout
    while True:
        found = []
        for task in tasks:
            st = task.read_state()
            if why := reason(st, running(task)):
                found.append((task, st, why))
        if found:
            return found
        if deadline is not None and clock() >= deadline:
            return []
        sleep(INTERVAL if deadline is None else min(INTERVAL, deadline - clock()))
