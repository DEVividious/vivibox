import json

from conftest import make_repo

from vivibox import actions, gate, panel, repo, review, ui
from vivibox.config import load_project
from vivibox.states import State


def git(source, *args):
    return repo.git(*args, cwd=source).stdout.strip()


def test_task_can_start_from_another_branch_without_touching_checkout(tmp_path):
    source = make_repo(tmp_path / "source")
    base = git(source, "rev-parse", "HEAD")
    git(source, "branch", "release/one")
    (source / "README.md").write_text("new main\n")
    git(source, "commit", "-am", "Update main")
    (source / "README.md").write_text("my unfinished work\n")
    meta = tmp_path / ".task"
    meta.mkdir()
    clone = tmp_path / "clone"
    actual = repo.prepare(source, clone, "demo-1", meta, base_ref="refs/heads/release/one")
    assert actual == base, "the chosen branch, not the checkout's HEAD"
    assert git(source, "branch", "--show-current") == "main"
    assert (source / "README.md").read_text() == "my unfinished work\n"


def test_branch_list_puts_current_first_and_includes_remote_branches(tmp_path):
    source = make_repo(tmp_path / "source")
    git(source, "branch", "aaa")
    git(source, "update-ref", "refs/remotes/origin/release/one", "HEAD")
    git(source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release/one")
    choices = repo.branches(source)
    assert choices and choices[0] == ("Current (main)", "HEAD")
    assert ("origin/release/one", "refs/remotes/origin/release/one") in choices
    assert not any(label == "origin/HEAD" for label, _ in choices)


def test_task_can_start_from_a_commit_only_referenced_by_a_remote_branch(tmp_path):
    source = make_repo(tmp_path / "source")
    git(source, "switch", "-c", "remote-work")
    (source / "remote.txt").write_text("remote change\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Add remote change")
    wanted = git(source, "rev-parse", "HEAD")
    git(source, "update-ref", "refs/remotes/origin/topic", wanted)
    git(source, "switch", "main")
    git(source, "branch", "-D", "remote-work")
    meta = tmp_path / ".task"
    meta.mkdir()
    clone = tmp_path / "clone"
    assert repo.prepare(source, clone, "demo-1", meta, "refs/remotes/origin/topic") == wanted
    assert (clone / "remote.txt").read_text() == "remote change\n"
    assert not (source / "remote.txt").exists()


def ready_task():
    task = actions.create("demo", "A very long task description " * 6)
    task.plan_path.write_text("+++\n+++\n## Acceptance criteria\n\n- [ ] it works\n")
    task.transition(State.CHECKPOINT_PLAN)
    gate.accept_plan(task)
    (task.repo / "feature.txt").write_text("x\n")
    git(task.repo, "add", ".")
    git(task.repo, "-c", "user.name=A", "-c", "user.email=a@b", "commit", "-m", "Handle empty input")
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    return task


def test_single_commit_proposal_describes_change_without_acceptance_checklist(env):
    task = ready_task()
    st = task.read_state()
    message = review.suggested_message(task.repo, st.base_commit, "HEAD", st.goal, ["it works"])
    assert message == "Handle empty input", "a goal too long for a subject: the commit's own, no checklist"


def test_proposal_exists_before_accepting_and_is_shown_in_review(env):
    task = ready_task()
    review.prepare_review(task, load_project("demo"))
    proposal = task.meta / "commit-message.json"
    assert proposal.exists(), "prepare the suggestion before the person accepts"
    assert json.loads(proposal.read_text())["message"] == "Handle empty input"
    shown = panel.detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    assert "Proposed commit" in shown and "Handle empty input" in shown


def test_proposal_refreshes_after_a_writer_fix_and_is_reused_on_accept(env, monkeypatch):
    task = ready_task()
    project = load_project("demo")
    review.prepare_review(task, project)
    (task.repo / "feature.txt").write_text("fixed\n")
    git(task.repo, "commit", "-am", "Keep the input order")
    review.prepare_review(task, project)
    message = review.proposed_message(task)["message"]
    assert message == "Keep the input order\n\n- Handle empty input"

    def unexpected(*args, **kwargs):
        raise AssertionError("accept must reuse the prepared message for these commits")

    monkeypatch.setattr(review, "suggested_message", unexpected)
    assert review.finish(task, project).message == message


def test_commit_dialog_opens_while_pod_cleanup_is_still_running(env, monkeypatch):
    import asyncio
    import threading

    from vivibox.dialogs import CommitWork
    from vivibox.tui import Vivibox

    task = ready_task()
    review.prepare_review(task, load_project("demo"))
    cleanup_started, release = threading.Event(), threading.Event()
    remove = actions.remove

    def slow_remove(*args, **kwargs):
        cleanup_started.set()
        assert release.wait(10), "the commit dialog should appear before cleanup finishes"
        return remove(*args, **kwargs)

    monkeypatch.setattr(actions, "remove", slow_remove)

    async def scenario():
        app = Vivibox()
        async with app.run_test() as pilot:
            app.finish(task.id)
            try:
                for _ in range(40):
                    await pilot.pause(0.05)
                    if cleanup_started.is_set() and isinstance(app.screen, CommitWork):
                        break
                assert isinstance(app.screen, CommitWork)
                assert cleanup_started.is_set() and not release.is_set()
                assert (load_project("demo").repo / "feature.txt").exists()
            finally:
                release.set()
                await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_accept_offers_commit_before_slow_pod_cleanup(env, monkeypatch):
    task = ready_task()
    project = load_project("demo")
    review.prepare_review(task, project)
    offered = []

    def remove(*args, **kwargs):
        assert offered == ["Handle empty input"], "the dialog must not wait for Docker cleanup"
        assert (project.repo / "feature.txt").exists(), "offer only after applying the work"

    monkeypatch.setattr(actions, "remove", remove)
    review.finish(task, project, on_ready=lambda done: offered.append(done.message))


def test_stopping_a_blocked_task_is_visible_and_keeps_the_reason(env):
    task = ready_task()
    st = task.read_state()
    st.state = State.CHECKPOINT_BLOCKED
    task._write_state(st)
    task.event("gate", environment="Cannot connect to Docker")
    task.set_paused(True)
    seen = ui.view(task, task.read_state(), False, 3)
    assert (seen.status, seen.group) == ("stopped", "Stopped")
    assert seen.commands[0] == f"vivibox start {task.id}"
    shown = panel.detail(task, task.read_state(), 3, running=False, pod=panel.PodView())
    assert "Cannot connect to Docker" in shown
    assert "`s` start" in shown
