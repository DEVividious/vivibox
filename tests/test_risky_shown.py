"""An approval of risky files is of exactly what you were shown. A view of them has an identity
(risky.identity: the approval its diffs are made against and the files as its scan found them);
the approval carries it and takes the files only while the clone and the last approval still give
that identity. Anything else by then is refused whole, with nothing approved: you look again.

Here: the identity itself, the refusal for each way the state can move between the look and the
approval, the approval that passes, the race inside it, and the view (p) and the command line
(vivibox risky, then the command it ends with) each from end to end."""

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import make_repo
from test_gate import PLAN
from test_tui import run as run_view

from vivibox import actions, clone_read, risky
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
    assert before and after and before != after


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
    with pytest.raises(risky.NotAsShown) as refused:
        actions.approve_risky(task, type("P", (), {"risky_extra": []}), shown)
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


def test_a_drawing_that_replaces_what_you_read_is_not_what_p_approves(env):
    """The panel is drawn again for its own reasons (the pod answered, the list changed) after
    the file was edited: it shows the file as it is, and p is still an approval of what was drawn
    first, so it refuses once. A redraw you did not ask for never moves what you approve."""
    t, record = at_approval(env)
    before = record.read_bytes()

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("d")
        await pilot.pause()
        (t.repo / "pom.xml").write_text("<project><evil/></project>\n")
        app.show_detail()  # as a refresh that found something changed draws it
        await pilot.pause()
        assert "evil" in app.shown, "the panel never lies about what is there"
        await pilot.press("p")
        await pilot.press("enter")
        await pilot.pause()
        assert record.read_bytes() == before and t.read_state().state is State.APPROVAL_RISKY
        assert "changed since they were shown" in said_in(app)[-1]

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
