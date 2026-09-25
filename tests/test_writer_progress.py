import io
import json
from types import SimpleNamespace

from vivibox import actions, gate, opencode, panel, progress
from vivibox.states import State


def writer_task():
    task = actions.create("demo", "Handle empty input")
    task.plan_path.write_text(
        "+++\n+++\n## Acceptance criteria\n\n- [ ] empty input returns zero\n- [ ] errors are tested\n"
    )
    task.transition(State.CHECKPOINT_PLAN)
    gate.accept_plan(task)
    task.transition(State.IMPLEMENT)
    task.set_session("writer", "ses_writer")
    return task


def event(todos, session="ses_writer", status="completed"):
    return {
        "type": "tool_use",
        "sessionID": session,
        "part": {"tool": "todowrite", "state": {"status": status, "input": {"todos": todos}}},
    }


def stream(task, events):
    class Process:
        stdout = io.StringIO("\n".join(json.dumps(e) for e in events))
        returncode = 0

        def wait(self):
            # This assertion runs before the stream returns: progress must be live.
            assert panel.criteria(task) == "1/2"

    pod = SimpleNamespace(stream=lambda *args: Process())
    opencode.OpenCode(pod, task=task)._stream(("command",), lambda *args: None)


def test_writer_todos_update_exact_criteria_during_the_stream(env):
    task = writer_task()
    stream(
        task,
        [
            event(
                [
                    {"content": "empty input returns zero", "status": "completed"},
                    {"content": "Look at the input parser", "status": "in_progress"},
                ]
            )
        ],
    )
    assert gate.missing_criteria(task) == ["errors are tested"]
    shown = panel.detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    assert "Look at the input parser" in shown


def test_other_sessions_and_reworded_todos_cannot_complete_criteria(env):
    task = writer_task()
    stream(
        task,
        [
            event([{"content": "empty input returns zero", "status": "completed"}]),
            event([{"content": "errors are tested", "status": "completed"}], session="ses_planner"),
            event([{"content": "errors are tested", "status": "completed"}], status="error"),
            event([{"content": "test errors", "status": "completed"}]),
        ],
    )
    assert gate.missing_criteria(task) == ["errors are tested"]


def test_reopening_a_todo_refreshes_progress_without_losing_writer_notes(env):
    task = writer_task()
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    path.write_text(path.read_text() + "\n## Evidence\n\nA note from the writer.\n")
    todo = {"content": "empty input returns zero", "status": "completed"}
    before = panel.ticked_at(task)
    progress.record(task, event([todo]))
    assert panel.ticked_at(task) != before
    assert panel.criteria(task) == "1/2"
    progress.record(task, event([{**todo, "status": "in_progress"}]))
    assert panel.criteria(task) == "0/2"
    assert path.read_text().endswith("## Evidence\n\nA note from the writer.\n")
    task.set_session("writer", "ses_new")
    assert progress.read(task) == [], "a replaced writer session starts without stale steps"
