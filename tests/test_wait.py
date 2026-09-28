import json

import pytest

from vivibox import actions, waiting
from vivibox.states import State
from vivibox.task import create_task

PLAN = '+++\nmode = "code-only"\nverify = []\n+++\n\n# Goal\n\nHealth.\n'


class Clock:
    """The time wait sees, moved on by its own sleeps, so a test waits for nothing."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make(env, title="Add health endpoint", state=State.IMPLEMENT):
    task = create_task(env / "tasks", "demo", title, PLAN)
    set_state(task, state)
    return task


def set_state(task, state, **fields):
    st = task.read_state()
    st.state = state
    for name, value in fields.items():
        setattr(st, name, value)
    task._write_state(st)


def wait(tasks, running=lambda task: True, timeout=None, clock=None, on_sleep=None):
    clock = clock or Clock()

    def sleep(seconds):
        clock.sleep(seconds)
        if on_sleep:
            on_sleep()

    return waiting.wait(tasks, running, timeout=timeout, clock=clock, sleep=sleep)


@pytest.mark.parametrize(
    "state,fields,running,reason",
    [
        (State.CHECKPOINT_PLAN, {}, True, "waiting"),
        (State.APPROVAL_RISKY, {}, True, "waiting"),
        (State.CHECKPOINT_FINAL, {}, False, "waiting"),
        (State.DONE, {}, False, "done"),
        (State.IMPLEMENT, {"paused": True, "problem": "could not start: no image"}, False, "problem"),
        (State.IMPLEMENT, {"paused": True}, False, "stopped"),
        (State.IMPLEMENT, {}, False, "not running"),
    ],
)
def test_a_task_already_there_returns_at_once(env, state, fields, running, reason):
    task = make(env)
    set_state(task, state, **fields)
    clock = Clock()
    found = wait([task], running=lambda t: running, clock=clock)
    assert [(t.id, why) for t, _, why in found] == [(task.id, reason)]
    assert clock.slept == []


def test_a_working_task_is_read_again_until_it_needs_you(env):
    task = make(env)
    turns = iter([None, lambda: set_state(task, State.CHECKPOINT_FINAL)])
    clock = Clock()
    found = wait([task], clock=clock, on_sleep=lambda: (step := next(turns)) and step())
    assert [why for _, _, why in found] == ["waiting"]
    assert clock.slept == [waiting.INTERVAL, waiting.INTERVAL]


def test_of_several_tasks_the_first_that_needs_you_ends_the_wait(env):
    busy, soon = make(env, "Busy"), make(env, "Soon")
    found = wait([busy, soon], on_sleep=lambda: set_state(soon, State.CHECKPOINT_PLAN))
    assert [t.id for t, _, _ in found] == [soon.id]


def test_a_timeout_ends_the_wait_with_nothing(env):
    task = make(env)
    clock = Clock()
    assert wait([task], timeout=5, clock=clock) == []
    assert sum(clock.slept) == 5 and max(clock.slept) <= waiting.INTERVAL


def test_the_command_line_says_what_ended_the_wait(env, capsys, monkeypatch):
    from vivibox.cli import main

    task = make(env)
    set_state(task, State.CHECKPOINT_FINAL)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    assert main(["wait", task.id]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith(task.id) and "review the work" in line and f"vivibox accept {task.id}" in line
    assert main(["wait", task.id, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["version"] == waiting.JSON_VERSION
    assert shown["tasks"] == [
        {
            "id": task.id,
            "state": "checkpoint:final",
            "reason": "waiting",
            "status": shown["tasks"][0]["status"],
            "problem": "",
            "next": shown["tasks"][0]["next"],
        }
    ]
    assert f"vivibox accept {task.id}" in shown["tasks"][0]["next"]


def test_a_timeout_exits_as_timeout_does_and_an_unknown_task_is_an_error(env, capsys, monkeypatch):
    from vivibox.cli import main

    task = make(env)
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    monkeypatch.setattr(waiting.time, "sleep", lambda s: None)
    assert main(["wait", task.id, "--timeout", "0"]) == waiting.TIMED_OUT == 124
    assert main(["wait", task.id, "--timeout", "0", "--json"]) == 124
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "version": waiting.JSON_VERSION,
        "tasks": [],
    }
    assert main(["wait", "nope-1"]) == 1
    assert "nope-1" in capsys.readouterr().err
