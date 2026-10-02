"""Tasks written under another layout of a task's files and state (task.TASK_SCHEMA_VERSION).
One from an earlier vivibox is shown and can be deleted, and nothing else is done with it; one from
a newer vivibox is only shown. vivibox migrates neither, and deletes neither by itself. Looking at
either writes nothing in it."""

import json
import subprocess
import sys

import pytest
from test_tui import implementing, rows, run

from vivibox import actions, box, demo, panel, tui, ui, version, window
from vivibox import task as task_module
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.pod import Pod
from vivibox.task import TaskLayoutError, find_task

EARLIER = ("no version", "an older version")
NEWER = ("a newer version", "a version that is not a number")
EARLIER_SAYS = "created by an earlier vivibox"
NEWER_SAYS = "created by a newer vivibox"


def snapshot(task) -> dict:
    """Every file and folder under the task's .task, with each file's bytes and modification time."""
    return {
        str(p.relative_to(task.meta)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
        for p in sorted(task.meta.rglob("*"))
    }


def written_under(task, how: str, monkeypatch) -> None:
    """The task as the layout named writes it. An earlier vivibox (v0.1.x) wrote no version at all,
    nor who created the task; an older version is simulated by raising this vivibox's."""
    path = task.meta / "state.json"
    data = json.loads(path.read_text())
    if how == "no version":
        del data["task_schema_version"], data["created_by_version"]
    elif how == "an older version":
        monkeypatch.setattr(task_module, "TASK_SCHEMA_VERSION", task_module.TASK_SCHEMA_VERSION + 1)
    elif how == "a newer version":
        data["task_schema_version"] = task_module.TASK_SCHEMA_VERSION + 1
    else:
        data["task_schema_version"] = str(task_module.TASK_SCHEMA_VERSION)
    path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture(params=EARLIER + NEWER)
def other(request, env, monkeypatch):
    """A task at work, under each layout this vivibox does not run, and what it is: earlier or newer."""
    task = implementing("Add health endpoint")
    written_under(task, request.param, monkeypatch)
    return task, "earlier" if request.param in EARLIER else "newer"


@pytest.fixture(params=EARLIER)
def earlier(request, env, monkeypatch):
    task = implementing("Add health endpoint")
    written_under(task, request.param, monkeypatch)
    return task


@pytest.fixture
def old_supervisor():
    """A process the way supervisor_running knows one: 'supervise' and the task's id on its command
    line, in a session of its own, as start_supervisor runs it."""
    started = []

    def start(task):
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "supervise", task.id],
            start_new_session=True,
        )
        (task.meta / actions.SUPERVISOR_PID).write_text(str(p.pid))
        started.append(p)
        return p

    yield start
    for p in started:
        p.kill()
        p.wait()


def test_a_new_task_records_the_layout_and_the_vivibox_that_created_it(env, monkeypatch):
    monkeypatch.setattr(box, "start_box", lambda task_id: None)  # no pod for the box
    task = implementing()
    data = json.loads((task.meta / "state.json").read_text())
    assert data["task_schema_version"] == task_module.TASK_SCHEMA_VERSION
    assert data["created_by_version"] == version.current()
    assert task.read_state().task_schema_version == task_module.TASK_SCHEMA_VERSION
    opened = actions.open_box("demo")
    assert opened.read_state().task_schema_version == task_module.TASK_SCHEMA_VERSION


def test_a_task_of_this_layout_is_opened_for_anything(env):
    task = implementing()
    assert find_task(load_config().tasks_dir, task.id).id == task.id
    assert actions.load(task.id)[0].id == task.id
    keys = panel.keys_for(task, task.read_state(), True, False, False)
    assert keys["stop_task"] and keys["show_log"] and keys["remove"]


# Every command that runs, moves or opens a task needs its layout; status, timeline and wait
# only look, and delete is the one way out.
NEEDS_LAYOUT = [
    ["start", "demo-1"],
    ["resume", "demo-1"],
    ["stop", "demo-1"],
    ["stop", "demo-1", "--force"],
    ["accept", "demo-1"],
    ["reply", "demo-1", "go on"],
    ["reply", "demo-1", "--criterion", "it works"],
    ["verify", "demo-1"],
    ["verify-again", "demo-1"],
    ["demo", "demo-1"],
    ["demo", "demo-1", "--stop"],
    ["attach", "demo-1"],
    ["follow-verification", "demo-1", "verify-1.log"],
    ["risky", "demo-1"],
    ["approve-risky", "demo-1"],
    ["plan", "prompt", "demo-1"],
    ["plan", "import", "demo-1", "answer.md"],
    ["pod", "up", "demo-1"],
    ["pod", "down", "demo-1"],
    ["pod", "rm", "demo-1"],
    ["pod", "shell", "demo-1"],
    ["review", "demo-1"],
    ["review", "demo-1", "--prompt"],
    ["review", "demo-1", "--import", "answer.md"],
    ["supervise", "demo-1"],
]


@pytest.mark.parametrize("argv", NEEDS_LAYOUT, ids=" ".join)
def test_every_command_that_needs_the_layout_refuses_and_writes_nothing(other, argv, capsys, monkeypatch):
    task, which = other
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"ran {a}"))
    before = snapshot(task)
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert (EARLIER_SAYS if which == "earlier" else NEWER_SAYS) in err
    assert ("vivibox delete demo-1" in err) == (which == "earlier"), "delete is offered where it works"
    assert snapshot(task) == before, "a refusal records nothing on the task"
    assert actions.started == [], "nothing started"


@pytest.mark.real_start
@pytest.mark.parametrize(
    "call",
    [
        lambda: actions.start("demo-1"),
        lambda: actions.start("demo-1", resume=True),
        lambda: actions.task_pod("demo-1"),
        lambda: box.start_box("demo-1"),
        lambda: box.box_shell_command("demo-1"),
        lambda: demo.demo("demo-1"),
        lambda: demo.use_instruction("demo-1", "npm start"),
        lambda: demo.demo_stop("demo-1"),
        lambda: window.attach_command("demo-1"),
        lambda: find_task(load_config().tasks_dir, "demo-1"),
    ],
    ids=["start", "resume", "pod", "box", "box shell", "demo", "demo instruction", "demo stop", "attach",
         "find"],
)  # fmt: skip
def test_what_the_view_and_the_supervisor_call_by_id_refuses_too(other, call):
    task, which = other
    before = snapshot(task)
    with pytest.raises(TaskLayoutError, match=EARLIER_SAYS if which == "earlier" else NEWER_SAYS):
        call()
    assert snapshot(task) == before


def test_the_task_itself_refuses_to_be_written(other):
    """Behind every operation: a state this vivibox did not write is never written over, so one
    that slipped past the check cannot move the task, nor drop what a newer vivibox keeps."""
    task, _ = other
    before = snapshot(task)
    with pytest.raises(TaskLayoutError):
        task.set_paused(True)
    with pytest.raises(TaskLayoutError):
        task.transition(task_module.State.VERIFY)
    assert snapshot(task) == before


def test_the_list_and_the_status_show_it_and_write_nothing(other, capsys):
    task, which = other
    before = snapshot(task)
    assert main(["status"]) == 0
    listed = capsys.readouterr().out
    assert "demo-1" in listed and "Add health endpoint" in listed
    label = "from an earlier vivibox" if which == "earlier" else "from a newer vivibox"
    assert label in listed
    assert main(["status", "demo-1"]) == 0
    shown = capsys.readouterr().out
    assert label in shown and (EARLIER_SAYS if which == "earlier" else NEWER_SAYS) in shown
    assert ("vivibox delete demo-1" in shown) == (which == "earlier")
    assert "vivibox start" not in shown and "vivibox attach" not in shown, "no next step it refuses"
    assert main(["timeline", "demo-1"]) == 0 and "demo-1" in capsys.readouterr().out
    assert main(["wait", "demo-1", "--timeout", "0", "--json"]) == 0
    waited = json.loads(capsys.readouterr().out)["tasks"][0]
    assert waited["reason"] == "problem" and waited["status"] == label
    assert snapshot(task) == before, "looking at it writes nothing"


def test_delete_removes_everything_of_a_task_from_an_earlier_vivibox(
    earlier, old_supervisor, monkeypatch, capsys
):
    from vivibox import secrets

    supervisor = old_supervisor(earlier)
    pods, keys = [], []
    monkeypatch.setattr(Pod, "remove", lambda self: pods.append(self.task_id))
    monkeypatch.setattr(secrets, "remove", lambda task_id: keys.append(task_id))
    assert main(["delete", "demo-1", "--yes"]) == 0
    assert "Deleted demo-1" in capsys.readouterr().out
    assert not earlier.root.exists(), "its folder"
    assert pods == ["demo-1"], "its containers, volumes and network"
    assert keys == ["demo-1", "demo-1-review"], "its keys, the reviewer's too"
    assert supervisor.wait(timeout=10) is not None, "the earlier vivibox's supervisor is stopped"
    entry = actions.history()[0]
    assert (entry["id"], entry["deleted"]) == ("demo-1", "implement")
    assert actions.archive_path("demo-1").is_dir()


@pytest.mark.parametrize("how", NEWER)
def test_delete_refuses_a_task_from_a_newer_vivibox(env, how, monkeypatch, capsys):
    """An older vivibox cannot know what a newer layout made for the task: half a deletion is
    worse than a clear refusal."""
    task = implementing("Add health endpoint")
    written_under(task, how, monkeypatch)
    monkeypatch.setattr(Pod, "remove", lambda self: pytest.fail("removed its pod"))
    before = snapshot(task)
    assert main(["delete", "demo-1", "--yes"]) == 1
    assert NEWER_SAYS in capsys.readouterr().err
    with pytest.raises(TaskLayoutError):
        actions.remove(task, actions.load(task.id, "show")[1])
    assert snapshot(task) == before and actions.history() == []


def test_a_running_supervisor_of_an_earlier_vivibox_is_said_and_left_alone(earlier, old_supervisor, capsys):
    supervisor = old_supervisor(earlier)
    before = snapshot(earlier)
    assert main(["status"]) == 0
    assert "from an earlier vivibox, still running" in capsys.readouterr().out
    assert main(["status", "demo-1"]) == 0
    shown = capsys.readouterr().out
    assert "still running" in shown and "vivibox delete demo-1" in shown
    assert main(["start", "demo-1"]) == 1 and main(["stop", "demo-1"]) == 1
    assert supervisor.poll() is None, "nothing here stops it but a delete"
    assert snapshot(earlier) == before


def test_the_view_shows_it_offers_only_delete_and_writes_nothing(earlier, monkeypatch):
    from ux import screen_text

    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: panel.PodView() for i in ids})
    before = snapshot(earlier)

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("demo-1"))
        await pilot.pause()
        assert "from an earlier vivibox" in screen_text(app)
        offered = {a for a in panel.TASK_ACTIONS if app.check_action(a, ())}
        assert offered == {"remove", "show_log"}
        await pilot.press("d")
        await pilot.pause()
        assert "### demo-1 · from an earlier vivibox" in app.shown
        assert "`x` delete it" in app.shown and EARLIER_SAYS in app.shown
        for key in ("s", "a", "r", "g", "w", "v", "m", "e", "S"):  # keys it has off do nothing
            await pilot.press(key)
            await pilot.pause()
        assert snapshot(earlier) == before, "the view and its keys wrote nothing"
        assert actions.started == []

    run(scenario)


def test_the_view_deletes_it(earlier, monkeypatch):
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: panel.PodView() for i in ids})
    monkeypatch.setattr(Pod, "remove", lambda self: None)

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("demo-1"))
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("left", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not earlier.root.exists()

    run(scenario)


def test_the_view_offers_nothing_for_a_task_from_a_newer_vivibox(env, monkeypatch):
    task = implementing("Add health endpoint")
    written_under(task, "a newer version", monkeypatch)
    keys = panel.keys_for(task, task.read_state(), True, False, False)
    assert not any(keys.values())
    st = task.read_state()
    seen = ui.view(task, st, False, 3)
    assert seen.status == "from a newer vivibox" and NEWER_SAYS in seen.problem and not seen.commands
    assert "`x`" not in panel.detail(task, st, 3, False, panel.PodView())


def test_a_pod_of_an_earlier_vivibox_still_up_is_said_too(earlier):
    st = earlier.read_state()
    up = panel.PodView(address="10.0.0.2")
    assert "still running" in panel.detail(earlier, st, 3, False, up)
