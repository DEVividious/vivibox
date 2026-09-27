import asyncio
import threading

import pytest
from test_task_improvements import git, ready_task
from test_writer_progress import event, writer_task
from textual.widgets import Button, Input
from ux import screen_text

from vivibox import actions, gate, newtask, panel, progress, repo, review, ui
from vivibox.branches import BranchPicker
from vivibox.config import load_project
from vivibox.states import State
from vivibox.tui import Vivibox


@pytest.mark.parametrize("delivery", ["branch", "checkout"])
def test_accept_refuses_a_base_outside_checkout_without_applying_anything(env, delivery):
    project = load_project("demo")
    git(project.repo, "switch", "-c", "release/one")
    (project.repo / "release-only.txt").write_text("release\n")
    git(project.repo, "add", ".")
    git(project.repo, "commit", "-m", "Add release configuration")
    base = git(project.repo, "rev-parse", "HEAD")
    git(project.repo, "switch", "main")
    task = actions.create("demo", "Add feature", base_ref="release/one")
    (task.repo / "feature.txt").write_text("feature\n")
    git(task.repo, "add", ".")
    git(task.repo, "commit", "-m", "Add feature")
    st = task.read_state()
    st.state = State.CHECKPOINT_FINAL
    task._write_state(st)
    (project.repo / "README.md").write_text("my ongoing work\n")
    try:
        review.finish(task, project)
    except gate.GateError as error:
        assert f"vivibox accept {task.id} --branch" in str(error)
    else:
        pytest.fail("accept must refuse a base outside the current checkout's history")
    assert git(project.repo, "diff", "--cached", "--name-only") == ""
    assert not (project.repo / "release-only.txt").exists()
    assert not (project.repo / "feature.txt").exists()
    assert (project.repo / "README.md").read_text() == "my ongoing work\n"
    assert task.root.exists() and task.read_state().base_commit == base
    if delivery == "branch":
        done = review.finish(task, project, branch_only=True)
        assert git(project.repo, "show", f"{done.branch}:feature.txt") == "feature"
        assert git(project.repo, "branch", "--show-current") == "main"
    else:
        git(project.repo, "switch", "release/one")
        review.finish(task, project)
        assert git(project.repo, "diff", "--cached", "--name-only") == "feature.txt"
        assert (project.repo / "README.md").read_text() == "my ongoing work\n"


@pytest.mark.parametrize(
    "state, decision",
    [
        (State.CHECKPOINT_PLAN, "accept"),
        (State.CHECKPOINT_FINAL, "accept"),
        (State.APPROVAL_RISKY, "risky"),
    ],
)
def test_stopping_pod_keeps_review_decisions_visible(env, state, decision):
    task = ready_task()
    st = task.read_state()
    st.state = state
    task._write_state(st)
    before = ui.view(task, st, True, 3)
    task.set_paused(True)
    st = task.read_state()
    after = ui.view(task, st, False, 3)
    assert after.status == before.status
    assert after.group == ui.group(st) == "Waiting for you"
    assert any(decision in command for command in after.commands)
    shown = panel.next_steps(task, st, after, False, panel.PodView())
    assert ("`p`" if state is State.APPROVAL_RISKY else "`a`") in shown


def test_stopped_blocked_task_keeps_reply_and_retry_visible(env):
    task = ready_task()
    st = task.read_state()
    st.state = State.CHECKPOINT_BLOCKED
    task._write_state(st)
    task.set_paused(True)
    st = task.read_state()
    seen = ui.view(task, st, False, 3)
    assert seen.status == "stopped" and seen.group == "Stopped"
    assert any("reply" in command for command in seen.commands)
    assert any("verify" in command for command in seen.commands)
    shown = panel.next_steps(task, st, seen, False, panel.PodView())
    assert all(key in shown for key in ("`s`", "`r`", "`g`"))


@pytest.mark.parametrize("status", ["pending", "in_progress", "cancelled"])
def test_stale_todo_preserves_the_writers_own_checkmark(env, status):
    task = writer_task()
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    path.write_text(path.read_text().replace("[ ] empty", "[x] empty"))
    original = path.read_text()
    task.transition(State.VERIFY)
    task.transition(State.IMPLEMENT)
    progress.record(task, event([{"content": "empty input returns zero", "status": status}]))
    assert path.read_text() == original
    assert gate.missing_criteria(task) == ["errors are tested"]


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_todo_mirroring_does_not_follow_a_checklist_link(env, tmp_path, link):
    task = writer_task()
    outside = tmp_path / "host-notes.md"
    original = "- [ ] empty input returns zero\n"
    outside.write_text(original)
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    path.unlink()
    path.symlink_to(outside) if link == "symlink" else path.hardlink_to(outside)
    progress.record(task, event([{"content": "empty input returns zero", "status": "completed"}]))
    assert outside.read_text() == original, "a pod-controlled path must not modify another host file"


def test_branch_search_preserves_loading_and_error_while_typing(env, monkeypatch):
    release = threading.Event()

    def failed(source):
        release.wait(10)
        raise repo.RepoError("repository unavailable")

    monkeypatch.setattr(repo, "branches", failed)

    async def scenario():
        app = Vivibox()
        async with app.run_test() as pilot:
            app.push_screen(BranchPicker(load_project("demo").repo))
            try:
                await pilot.pause()
                app.screen.query_one(Input).value = "topic"
                await pilot.pause()
                assert "Loading branches" in screen_text(app)
                assert "No matching branches" not in screen_text(app)
            finally:
                release.set()
                await app.workers.wait_for_complete()
            await pilot.pause()
            assert "repository unavailable" in screen_text(app)
            app.screen.query_one(Input).value = "other"
            await pilot.pause()
            assert "repository unavailable" in screen_text(app)
            assert "Escape returns" in screen_text(app)

    asyncio.run(scenario())


def test_branch_field_names_current_checkout_before_opening_picker(env):
    async def scenario():
        app = Vivibox()
        async with app.run_test() as pilot:
            app.push_screen(newtask.NewTask("demo"))
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert "Current (main)" in str(app.screen.query_one("#base-ref", Button).label)

    asyncio.run(scenario())


def test_proposed_subject_describes_implementation_after_test_first_commit(env):
    source = load_project("demo").repo
    base = git(source, "rev-parse", "HEAD")
    git(source, "commit", "--allow-empty", "-m", "Add failing test for empty input")
    git(source, "commit", "--allow-empty", "-m", "Handle empty input")
    message = review.suggested_message(source, base, "HEAD", "Fix input")
    assert message == "Fix input\n\n- Add failing test for empty input\n- Handle empty input", (
        "the task as the subject, the agent's commits as the body, in order"
    )


def test_a_commit_about_the_tasks_files_is_left_out_of_the_proposed_body(env):
    """On a work laptop the commit a proposed listed "- Record test red evidence": a subject about
    red.md, which never reaches the repository's history. Older tasks may still have such
    commits; the gate refuses new ones."""
    source = load_project("demo").repo
    base = git(source, "rev-parse", "HEAD")
    for subject in ("Add subtract with a test", "Record test red evidence", "Tick criteria"):
        git(source, "commit", "--allow-empty", "-m", subject)
    assert review.suggested_message(source, base, "HEAD", "Add subtraction") == (
        "Add subtraction\n\n- Add subtract with a test"
    )


def test_latest_subject_is_kept_when_it_repeats_an_earlier_commit(env):
    source = load_project("demo").repo
    base = git(source, "rev-parse", "HEAD")
    for subject in ("Handle empty input", "Cover an edge case", "Handle empty input"):
        git(source, "commit", "--allow-empty", "-m", subject)
    assert review.suggested_message(source, base, "HEAD", "Fix input") == (
        "Fix input\n\n- Handle empty input\n- Cover an edge case"
    ), "a subject repeated is listed once"
