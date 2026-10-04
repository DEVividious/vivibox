"""A repository of its own that the task tracks, or a worktree it added: git on the host goes into
neither, and no approval lets it. The task is blocked until what was added is gone, never
rejected for good: it waits for you with what is true, your reply sends the writer back, and the
writer reads how to take it out, in words that carry no command line made of a name it chose."""

import subprocess
from pathlib import Path

import pytest
from conftest import make_repo
from test_gate import PLAN, FakePod
from test_supervisor import FakeHarness

from vivibox import actions, feedback, gate, panel, supervisor, ui  # noqa: F401 (actions first)
from vivibox import repo as r
from vivibox.risky import Approvals
from vivibox.states import State
from vivibox.task import create_task

# What you do, right after what happened: a notification holds the first 150 characters.
REPLY = "is blocked until it is out: reply, and the writer removes it, or takes it out of what git tracks."
WHY = (
    "git on your machine would run what its settings say, so until then vivibox runs no git in the "
    "clone, and the work is neither verified nor brought back"
)


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def task(tmp_path):
    source = make_repo(tmp_path / "source")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(r.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    return t


def nested(clone: Path, name: str = "web") -> None:
    """As a scaffolding tool and `git add` leave it: a folder with a .git of its own, which the
    index lists as a submodule."""
    make_repo(clone / name)
    git(clone, "-c", "advice.addEmbeddedRepo=false", "add", "--", name)


def worktree(clone: Path, where: Path) -> None:
    git(clone, "worktree", "add", "-q", str(where))


def blocked(t) -> str:
    with pytest.raises(r.Blocked) as e:
        r.check_protection(t.repo, t.meta)
    return str(e.value)


def test_a_repository_the_task_tracks_blocks_it_until_it_is_out(task):
    nested(task.repo)
    assert blocked(task) == (
        f"the task added a repository of its own to its clone and {REPLY} Found: web/.git was added, a "
        f"repository git would go into. {WHY}"
    )
    git(task.repo, "rm", "-q", "-f", "--cached", "web")  # out of what git tracks: the folder stays
    assert (task.repo / "web" / ".git").is_dir()
    r.check_protection(task.repo, task.meta)


def test_the_repository_removed_unblocks_it_too(task):
    import shutil

    nested(task.repo)
    blocked(task)
    shutil.rmtree(task.repo / "web" / ".git")
    r.check_protection(task.repo, task.meta)


def test_a_worktree_the_task_added_blocks_it_until_it_is_removed(task, tmp_path):
    worktree(task.repo, tmp_path / "elsewhere")
    said = blocked(task)
    assert said.startswith(
        f"the task added a worktree to its clone and {REPLY} Found: .git/worktrees was added; "
        ".git/worktrees/elsewhere was added; "
    )
    assert said.endswith(f" and 1 more. {WHY}")
    git(task.repo, "worktree", "remove", str(tmp_path / "elsewhere"))
    r.check_protection(task.repo, task.meta)


@pytest.mark.parametrize("added", ["a repository", "a worktree"])
def test_nothing_says_the_work_is_lost(task, tmp_path, added):
    nested(task.repo) if added == "a repository" else worktree(task.repo, tmp_path / "elsewhere")
    said = blocked(task)
    assert "delete" not in said and "can be neither" not in said


def gitfile_elsewhere(clone: Path) -> None:
    """A folder the index lists whose .git is a file that names a git dir: not what git init leaves."""
    (clone / "web").mkdir()
    (clone / "web" / ".git").write_text("gitdir: /elsewhere/.git\n")
    git(clone, "update-index", "--add", "--cacheinfo", f"160000,{git(clone, 'rev-parse', 'HEAD')},web")


def test_in_a_box_nobody_is_said_to_be_replied_to(task):
    """A box has no writer: whoever works in it takes out what they added."""
    task.set_box()
    nested(task.repo)
    said = blocked(task)
    assert "reply" not in said and "the writer" not in said
    assert said.startswith("this box's clone has a repository of its own in it and the box cannot close")
    assert "takes it out of what git tracks" not in said, "which does not last in a box"


CHANGES = {
    "a .git that names another git dir": gitfile_elsewhere,
    "a hook's folder moved": lambda clone: (clone / ".git" / "commondir").write_text("../elsewhere\n"),
    "a worktree that is a link": lambda clone: (clone / ".git" / "worktrees").symlink_to("/elsewhere"),
    "a pipe in a worktree": lambda clone: (
        (clone / ".git" / "worktrees" / "x").mkdir(parents=True),
        __import__("os").mkfifo(clone / ".git" / "worktrees" / "x" / "gitdir"),
    ),
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_any_other_change_is_refused_in_the_words_it_was(task, change):
    """Nothing here is told apart that sends git elsewhere: only what ordinary work adds."""
    if change != "a .git that names another git dir":
        nested(task.repo)
    CHANGES[change](task.repo)
    with pytest.raises(r.RepoError) as e:
        r.check_protection(task.repo, task.meta)
    assert not isinstance(e.value, r.Blocked)
    assert "git files in the task's clone changed" in str(e.value) and "delete the task" in str(e.value)


def test_a_hook_named_worktrees_is_no_worktree(task):
    hook = task.repo / ".git" / "hooks" / "worktrees"
    hook.write_text("#!/bin/sh\n")
    with pytest.raises(r.RepoError) as e:
        r.check_protection(task.repo, task.meta)
    assert not isinstance(e.value, r.Blocked)


def at_the_gate(t, monkeypatch=None):
    """The supervisor after the writer's turn and the verification of t."""
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        t.transition(s)
    notes, pod = [], FakePod()
    ports = supervisor.Ports(
        run_gate=lambda x: gate.run_gate(x, pod, ["make test"], []),
        risky_changes=lambda: [],
        notify=lambda _id, msg, kind="": notes.append(msg),
        sleep=lambda seconds: None,
    )
    sup = supervisor.Supervisor(t, FakeHarness(t), ports, max_rounds=3, project_verify=["make test"])
    sup.step()  # implement
    sup.step()  # verify
    return sup, notes, pod


def test_the_task_waits_for_you_and_your_reply_sends_the_writer_back(task):
    nested(task.repo)
    sup, notes, pod = at_the_gate(task)
    st = task.read_state()
    assert (st.state, st.rounds, st.paused, st.problem) == (State.CHECKPOINT_BLOCKED, 0, False, "")
    assert pod.commands == [], "nothing was built, and no git ran in the clone"
    assert ui.view(task, st, True, 3).status == "verification could not run"
    whole = ui.environment_problem(task)
    assert whole.startswith("the task added a repository of its own to its clone") and whole.endswith(WHY)
    assert notes[-1].startswith(
        "verification could not run: the task added a repository of its own to its clone and is blocked "
        "until it is out: reply, and the writer removes it, or takes it out of"
    ), "cut short, the notification still says what to do"
    offered = ui.view(task, st, True, 3).commands
    assert f'vivibox reply {task.id} "…"' in offered, "r sends the writer back"
    assert f"vivibox verify-again {task.id}" in offered, "g, once it is out by hand"
    actions.reply(task, "take the nested repository out")
    assert task.read_state().state is State.IMPLEMENT
    git(task.repo, "rm", "-q", "-f", "--cached", "web")  # as the writer is told to
    __import__("shutil").rmtree(task.repo / "web" / ".git")
    git(task.repo, "add", "web")
    git(task.repo, "commit", "-q", "-m", "Add web as ordinary files")
    sup.step()  # the writer's turn
    sup.step()  # verified again
    assert pod.commands == ["make test"], "the work is verified once the repository is out"


def test_g_verifies_again_and_blocks_again_while_it_is_there(task):
    nested(task.repo)
    sup, notes, pod = at_the_gate(task)
    actions.verify_again(task)
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED and pod.commands == []


def test_the_writer_reads_how_to_take_a_repository_out(task):
    nested(task.repo)
    at_the_gate(task)
    said = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert said == (
        "# Verification failed\n\n"
        "- Verification could not run: web/.git was added, a repository git would go into. A folder "
        "named there has a `.git` of its own and the index lists it as a submodule, which the "
        "orchestrator's git does not go into. Take the folder out of the index first (`git rm -f "
        "--cached` with its path, which leaves its files), then delete the `.git` inside it, add its "
        "files as ordinary files and commit; or delete the folder, when the task does not need it. "
        "With the `.git` deleted and the folder still in the index, none of its files is in your "
        "commits. Nothing is verified until then.\n"
    )


def test_the_writer_is_told_what_half_of_it_leaves(task):
    """The middle step alone (the `.git` deleted, the index left listing a submodule) passes the
    protection check, and to git nothing is left to commit, while none of the folder's files is
    committed: the feedback says so beforehand, and the verification afterwards
    (test_gitlinks_left)."""
    nested(task.repo)
    git(task.repo, "commit", "-q", "-m", "Add web")
    __import__("shutil").rmtree(task.repo / "web" / ".git")
    r.check_protection(task.repo, task.meta)  # not checked out: git does not go into it
    assert git(task.repo, "status", "--porcelain") == "", "and git sees nothing to commit"
    assert git(task.repo, "ls-files", "-s", "web").startswith("160000 "), "a submodule, and no file of it"
    said = feedback.TAKE_OUT["a repository of its own"]
    assert "out of the index first" in said
    assert said.endswith("still in the index, none of its files is in your commits.")
    assert gate.uncommitted(task.repo) == [r.NO_SUBMODULE.format("web")]


def test_the_writer_reads_how_to_remove_a_worktree(task, tmp_path):
    worktree(task.repo, tmp_path / "elsewhere")
    at_the_gate(task)
    said = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert said.startswith(
        "# Verification failed\n\n- Verification could not run: .git/worktrees was added; "
        ".git/worktrees/elsewhere was added; "
    )
    assert said.endswith(
        " and 1 more. That is a worktree of this repository, which the orchestrator's git does not go "
        "into. Remove it (`git worktree remove` with its path, which `git worktree list` names), "
        "then run `git worktree prune`, so that `.git/worktrees` is gone, and work in the "
        "repository's own folder. Where the index lists the worktree's folder, take it out of the "
        "index as well (`git rm -f --cached` with its path) and commit. Nothing is verified until "
        "then.\n"
    )


@pytest.mark.parametrize("added", ["a repository", "a worktree"])
def test_the_writer_is_not_told_what_you_are_told_to_do(task, tmp_path, added):
    nested(task.repo) if added == "a repository" else worktree(task.repo, tmp_path / "elsewhere")
    at_the_gate(task)
    said = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "reply" not in said and "the writer" not in said
    assert "outside the code" not in said and "Do not change" not in said, "it is the writer's to undo"


def test_the_instruction_is_one_the_writer_can_follow(task):
    """The commands the feedback names, run as it says, unblock the clone and keep the files."""
    nested(task.repo)
    blocked(task)
    git(task.repo, "rm", "-f", "--cached", "web")
    __import__("shutil").rmtree(task.repo / "web" / ".git")
    git(task.repo, "add", "web")
    git(task.repo, "commit", "-q", "-m", "Add web as ordinary files")
    r.check_protection(task.repo, task.meta)
    assert git(task.repo, "ls-files", "-s", "web").startswith("100644 "), "its files, no submodule"


# A name the agent chose, which a shell pasting it would run: no text makes a command of it.
PWNED = "$(touch {marker}/PWNED) x; y"


def test_a_name_the_agent_chose_is_in_no_command_the_writer_or_you_would_run(task, tmp_path):
    import re

    name = PWNED.format(marker=tmp_path)
    nested(task.repo, name)
    _, notes, _ = at_the_gate(task)
    fed = (task.meta / "handoff" / "verify-feedback.md").read_text()
    log = (task.meta / "handoff" / "verify.log").read_text()
    for said in (fed, log, ui.environment_problem(task), notes[-1]):
        commands = re.findall(r"`([^`]*)`", said) + re.findall(r"\(vivibox [^)]*\)", said)
        assert not [c for c in commands if "PWNED" in c or "$(" in c], said
        assert_named_only_as_found(said, name)
    assert f"{name}/.git was added" in fed, "the name as text, whole"
    assert not (tmp_path / "PWNED").exists()


def assert_named_only_as_found(said: str, name: str) -> None:
    """The name stands in said only where vivibox says what it found (the finding's own
    sentence, or its start where a notification was cut), never anywhere else: a command line
    made of it needs no backticks to be one ("Run: git rm -f --cached <name>")."""
    if "PWNED" not in said and "$(" not in said:
        return  # cut before what was found: the name is not said at all
    finding = f"{name}/.git was added, a repository git would go into"
    cut = next((finding[:n] for n in range(len(finding), 0, -1) if finding[:n] + "…" in said), "")
    rest = said.replace(finding, "").replace(cut + "…" if cut else finding, "")
    assert name not in rest and "PWNED" not in rest and "$(" not in rest, rest
    before = said.split(name)[0].rsplit(".", 1)[-1]
    assert before.strip().endswith(("Found:", "could not run:", "added to it:")), before


def test_the_rule_sees_a_command_line_that_has_no_backticks():
    name = "$(touch /tmp/PWNED) x; y"
    finding = f"{name}/.git was added, a repository git would go into"
    assert_named_only_as_found(f"Verification could not run: {finding}. Take it out.", name)
    for made_of_the_name in (
        f"Verification could not run: {finding}. Run: git rm -f --cached {name}",
        f"Run git rm -f --cached {name}/.git was added, a repository git would go into",
    ):
        with pytest.raises(AssertionError):
            assert_named_only_as_found(made_of_the_name, name)


def test_control_characters_in_the_name_reach_neither_of_you(task):
    nested(task.repo, "a\x1b[2Jb")
    at_the_gate(task)
    for said in ((task.meta / "handoff" / "verify-feedback.md").read_text(), ui.environment_problem(task)):
        assert "a‹U+001B›[2Jb/.git" in said and "\x1b" not in said


def test_an_ordinary_failure_outside_the_code_reads_as_before(tmp_path):
    result = gate.GateResult(tmp_path / "verify-1.log", environment="Cannot connect to the Docker daemon")
    assert feedback.feedback(result) == (
        "# Verification failed\n\n- Verification could not run: Cannot connect to the Docker daemon. "
        "That is outside the code; the user has been told and will run the verification again. Do "
        "not change code for it.\n"
    )


# --- found after the verification passed: before or during a review ---------------------------------


def in_review(task, tmp_path, blocked_at: str):
    """A supervisor whose verification passed and whose clone is then blocked by what the task
    added, found where the review first reads the clone (its HEAD) or where its view is made."""
    from test_supervisor import CLEAN, gate_result, reviewed

    why = r.Blocked("the task added a worktree to its clone, …", [])
    blocked_result = gate.GateResult(Path("/dev/null"), environment=str(why))
    sup, notes, _ = reviewed(task, tmp_path, [CLEAN], results=[gate_result(True), blocked_result])

    def refuse():
        raise why

    if blocked_at == "its head":
        sup.ports.head = refuse
    else:
        sup.ports.review_up = refuse
    sup.step()  # implement
    sup.step()  # verify: green
    assert task.read_state().state is State.REVIEW
    return sup, notes


@pytest.mark.parametrize("blocked_at", ["its head", "its view"])
def test_a_clone_blocked_at_the_review_goes_back_to_the_verification_not_to_an_error(
    task, tmp_path, blocked_at
):
    """Stopped on an error in the review, the task had no checkpoint a reply is taken at, and
    its message told you to reply. The verification says what blocks the clone and waits for
    you; no round of the writer's is spent."""
    sup, notes = in_review(task, tmp_path, blocked_at)
    sup.step()  # the review meets the blocked clone
    st = task.read_state()
    assert (st.state, st.paused, st.problem, st.reviews) == (State.VERIFY, False, "", 0)
    sup.step()  # the verification refuses it, for you
    st = task.read_state()
    assert (st.state, st.rounds) == (State.CHECKPOINT_BLOCKED, 0)
    assert notes[-1].startswith("verification could not run: the task added a worktree to its clone")
    actions.reply(task, "remove the worktree")
    assert task.read_state().state is State.IMPLEMENT


def test_the_reviews_view_keeps_what_blocks_the_clone_apart_from_other_refusals(task, tmp_path):
    """reviewing.view words a refusal as the review's ("the review could not start"); what the
    task added passes as it is, for the supervisor to send to the verification."""
    from types import SimpleNamespace

    from vivibox import reviewing

    nested(task.repo)
    task.set_verified_commit(git(task.repo, "rev-parse", "HEAD"))
    for folder in ("work", "src"):
        (tmp_path / folder).mkdir()
    pod = SimpleNamespace(review_work=tmp_path / "work", review_src=str(tmp_path / "src"))
    with pytest.raises(r.Blocked, match="the task added a repository of its own to its clone"):
        reviewing.view(task, pod)


# --- after your reply: the writer is sent to what says how --------------------------------------------


def test_after_the_reply_the_writers_turn_names_the_file_that_says_how(task):
    """Your reply may say no more than "go on": the turn it starts sends the writer to the
    verification's feedback, in the words a failed verification's turn has, and then to your
    reply; and the file it names holds how the repository is taken out."""
    from vivibox import prompts

    nested(task.repo)
    sup, _, _ = at_the_gate(task)
    actions.reply(task, "please go on")
    sup.step()  # the writer's turn
    asked = sup.harness.prompts[-1]
    assert "/task/handoff/verify-feedback.md" in asked and "/task/handoff/comments.md" in asked
    assert asked == prompts.FEEDBACK_PROMPT + "\n\n" + prompts.COMMENT_PROMPT, "no word of either changed"
    assert asked == prompts.BLOCKED_REPLY_PROMPT
    fed = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "web/.git was added" in fed and "`git rm -f --cached`" in fed


def test_a_reply_to_work_blocked_since_it_was_verified_writes_how_to_take_it_out(task, tmp_path):
    """Blocked after the verification passed (the work waits for you, and accepting refuses): the
    feedback there is an earlier round's, or none. The reply writes what the turn's prompt names."""
    from vivibox import prompts

    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(s)
    stale = task.meta / "handoff" / "verify-feedback.md"
    stale.write_text("# Verification failed\n\n- Criterion not ticked: an earlier round's\n")
    worktree(task.repo, tmp_path / "elsewhere")
    assert actions.reply(task, "take it out") is State.IMPLEMENT
    fed = stale.read_text()
    assert "an earlier round's" not in fed
    assert ".git/worktrees was added" in fed and "`git worktree remove`" in fed
    assert supervisor.next_prompt(task, "") == prompts.BLOCKED_REPLY_PROMPT
    log = (task.meta / "handoff" / "verify.log").read_text()
    assert ".git/worktrees was added" in log and "reply" not in log


def test_a_reply_to_a_task_whose_clone_is_as_recorded_is_the_reply_alone(task):
    from vivibox import prompts

    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_BLOCKED):
        task.transition(s)
    fed = task.meta / "handoff" / "verify-feedback.md"
    fed.write_text("# Verification failed\n\n- Verification could not run: Docker is down.\n")
    actions.reply(task, "it is up again")
    assert supervisor.next_prompt(task, "") == prompts.COMMENT_PROMPT
    assert "Docker is down" in fed.read_text(), "the verification's own feedback is left as it is"


def test_a_reply_that_cannot_write_how_is_refused_whole(task):
    """A link where the feedback goes: vivibox does not write through it, and a turn sent to a
    file that does not say how would be paid for nothing. Nothing of the reply is kept."""
    nested(task.repo)
    at_the_gate(task)
    fed = task.meta / "handoff" / "verify-feedback.md"
    outside = task.root / "outside.txt"
    outside.write_text("the host's\n")
    fed.unlink()
    fed.symlink_to(outside)
    before = (task.meta / "comments.md").read_text()
    with pytest.raises(gate.GateError, match="verify-feedback.md"):
        actions.reply(task, "please go on")
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    assert (task.meta / "comments.md").read_text() == before and outside.read_text() == "the host's\n"


def test_neither_file_the_writer_reads_tells_it_what_you_are_told(task):
    """handoff/verify.log is the verification's log, copied for the writer: what blocks the
    clone is in it as a fact, and the step that is yours is not."""
    nested(task.repo)
    at_the_gate(task)
    for name in ("verify-feedback.md", "verify.log"):
        said = (task.meta / "handoff" / name).read_text()
        assert "web/.git was added" in said, name
        assert "reply" not in said and "the writer" not in said, name


# --- in a box ------------------------------------------------------------------------------------------

BOX_REPOSITORY = (
    "this box's clone has a repository of its own in it and the box cannot close until it is out: "
    "whoever works in the box removes its folder, or has git ignore it (the folder in .gitignore, and "
    "`git rm -f --cached` with its path), or keeps its files as ordinary ones (`git rm -f --cached` "
    "with its path, and the `.git` inside it deleted). Out of the index alone it comes back: closing a "
    "box adds every file git does not ignore. Found: tool/.git was added, a repository git would go "
    "into. git on your machine would run what its settings say, so until then vivibox runs no git in "
    "the clone, and the work is not brought back"
)


def a_box_with_a_tool(task, monkeypatch):
    """A box where you cloned a tool to use it: a repository of its own, untracked, which the
    check lets be. Closing the box runs git add -A in its pod, which lists it in the index."""
    from test_git_timeout import a_box

    box_ = a_box(task, monkeypatch)
    make_repo(box_.repo / "tool")
    (box_.repo / "work.txt").write_text("by hand\n")
    r.check_protection(box_.repo, box_.meta)
    monkeypatch.setattr(actions, "prepare_review", lambda task, project: Path("review"))
    return box_


def close(box_):
    from types import SimpleNamespace

    from vivibox import box

    return box.close_box(box_, SimpleNamespace(risky_extra=[]))


def test_a_box_that_cannot_close_for_a_repository_in_it_is_told_what_works_there(task, monkeypatch):
    box_ = a_box_with_a_tool(task, monkeypatch)
    with pytest.raises(r.Blocked) as refused:
        close(box_)
    assert str(refused.value) == BOX_REPOSITORY
    assert box_.read_state().state is State.IMPLEMENT, "still open"


def remove_the_folder(clone: Path) -> None:
    __import__("shutil").rmtree(clone / "tool")


def have_git_ignore_it(clone: Path) -> None:
    (clone / ".gitignore").write_text("tool/\n")
    git(clone, "rm", "-q", "-f", "--cached", "tool")


def keep_its_files(clone: Path) -> None:
    git(clone, "rm", "-q", "-f", "--cached", "tool")
    __import__("shutil").rmtree(clone / "tool" / ".git")


WAYS_OUT = {
    "its folder removed": remove_the_folder,
    "ignored": have_git_ignore_it,
    "its files kept": keep_its_files,
}


@pytest.mark.parametrize("way", WAYS_OUT)
def test_each_way_the_box_is_told_closes_it(task, monkeypatch, way):
    box_ = a_box_with_a_tool(task, monkeypatch)
    with pytest.raises(r.Blocked):
        close(box_)
    WAYS_OUT[way](box_.repo)
    assert close(box_) == Path("review")
    tracked = git(box_.repo, "ls-files", "-s")
    assert "160000" not in tracked, "no submodule is left in what the box commits"
    assert ("tool/README.md" in tracked) == (way == "its files kept")
    assert (box_.repo / "tool" / ".git").exists() == (way == "ignored"), "ignored, the tool stays to be used"


def test_out_of_the_index_alone_it_comes_back_as_the_box_is_told(task, monkeypatch):
    box_ = a_box_with_a_tool(task, monkeypatch)
    with pytest.raises(r.Blocked):
        close(box_)
    git(box_.repo, "rm", "-q", "-f", "--cached", "tool")
    git(box_.repo, "-c", "user.name=You", "-c", "user.email=you@example.com", "commit", "-q", "-m", "Out")
    r.check_protection(box_.repo, box_.meta)
    with pytest.raises(r.Blocked):
        close(box_)


def test_a_box_with_a_worktree_is_told_how_it_is_removed(task, monkeypatch, tmp_path):
    from test_git_timeout import a_box

    box_ = a_box(task, monkeypatch)
    worktree(box_.repo, tmp_path / "elsewhere")
    said = blocked(box_)
    assert said.startswith(
        "this box's clone has a worktree in it and the box cannot close until it is out: whoever works "
        "in the box removes it (`git worktree remove` with its path, then `git worktree prune`). Found: "
    )
    assert "reply" not in said and "the writer" not in said and "the task added" not in said


# --- review round 1: what is said first when more than one thing is wrong ---------------------------


def test_a_worktree_and_a_repository_are_named_together(task, tmp_path):
    """The index is read too when the git dirs differ by an added worktree alone: one message
    for both, and the writer is told how each goes."""
    nested(task.repo)
    worktree(task.repo, tmp_path / "elsewhere")
    said = blocked(task)
    assert said.startswith("the task added a repository of its own and a worktree to its clone and is ")
    assert "web/.git was added, a repository git would go into" in said
    at_the_gate(task)
    fed = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "`git rm -f --cached`" in fed and "`git worktree remove`" in fed


def test_a_worktree_beside_what_no_ordinary_work_adds_is_refused_for_good_at_once(task, tmp_path):
    """ "Blocked until it is out" first, and "delete the task" once the worktree was out, was a
    promise made before the index was read: the stronger refusal is said first."""
    worktree(task.repo, tmp_path / "elsewhere")
    gitfile_elsewhere(task.repo)
    with pytest.raises(r.RepoError) as e:
        r.check_protection(task.repo, task.meta)
    assert not isinstance(e.value, r.Blocked) and "delete the task" in str(e.value)
    assert "read another repository: web/.git was added, a repository git would go into; " in str(e.value)


# --- review round 2: a worktree made inside the clone, and listed by the index ----------------------

LISTED = [
    (path, state)
    for path in (".worktrees/feature", "wt")
    for state in ("not added", "added", "added and committed")
]


def worktree_inside(clone: Path, path: str, state: str) -> None:
    """As a writer makes one where it can write, and as git add -A then lists it: the worktree's
    folder a gitlink in the index, its .git a file that names the clone's own worktrees/<name>."""
    git(clone, "worktree", "add", "-q", path)
    (clone / path / "new.txt").write_text("work in the worktree\n")
    if state != "not added":
        git(clone, "-c", "advice.addEmbeddedRepo=false", "add", "-A")
    if state == "added and committed":
        git(clone, "commit", "-q", "-m", "Add everything")


@pytest.mark.parametrize(("path", "state"), LISTED)
def test_a_worktree_inside_the_clone_blocks_until_it_is_removed_listed_or_not(task, path, state):
    """The ordinary way to one: the clone is the only place the writer writes. git lists the
    worktree's folder as a submodule once it is added, and that is still the worktree: the two
    commands the writer is told take it out, and the check passes."""
    worktree_inside(task.repo, path, state)
    said = blocked(task)
    assert said.startswith("the task added a worktree to its clone and is blocked until it is out: reply, ")
    assert "delete" not in said
    listed = f"{path}/.git was added, the folder of a worktree, which the index lists" in said
    assert listed == (state != "not added")
    git(task.repo, "worktree", "remove", "--force", path)
    git(task.repo, "worktree", "prune")
    r.check_protection(task.repo, task.meta)


@pytest.mark.parametrize("state", ["added", "added and committed"])
def test_the_writer_reads_the_commands_for_a_worktree_the_index_lists(task, state):
    worktree_inside(task.repo, ".worktrees/feature", state)
    sup, notes, pod = at_the_gate(task)
    st = task.read_state()
    assert (st.state, st.paused, st.rounds) == (State.CHECKPOINT_BLOCKED, False, 0), "where a reply is taken"
    fed = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert fed.startswith(
        "# Verification failed\n\n- Verification could not run: .worktrees/feature/.git was added, the "
        "folder of a worktree, which the index lists; .git/worktrees was added; "
    )
    assert fed.endswith(
        " That is a worktree of this repository, which the orchestrator's git does not go into. Remove it "
        "(`git worktree remove` with its path, which `git worktree list` names), then run `git worktree "
        "prune`, so that `.git/worktrees` is gone, and work in the repository's own folder. Where the "
        "index lists the worktree's folder, take it out of the index as well (`git rm -f --cached` with "
        "its path) and commit. Nothing is verified until then.\n"
    )
    assert "`git rm -f --cached`" in fed and "a `.git` of its own" not in fed, "no repository's instruction"


def test_the_commands_the_writer_is_told_leave_a_clone_the_verification_runs_on(task):
    worktree_inside(task.repo, ".worktrees/feature", "added and committed")
    blocked(task)
    git(task.repo, "worktree", "remove", "--force", ".worktrees/feature")
    git(task.repo, "worktree", "prune")
    git(task.repo, "rm", "-q", "-f", "--cached", ".worktrees/feature")
    git(task.repo, "commit", "-q", "-m", "Take the worktree's folder out")
    r.check_protection(task.repo, task.meta)
    assert gate.uncommitted(task.repo) == []


def admin(clone: Path, name: str = "feature") -> Path:
    return clone / ".git" / "worktrees" / name


def rewrite(path: Path, text: str) -> None:
    path.chmod(0o644)
    path.write_text(text)


# What is not the worktree git made, though a worktree was added beside it: each stays refused
# for good. clone: the task's; all of them start from a worktree inside it that the index lists.
HOSTILE = {
    "its .git names a git dir elsewhere": lambda clone: rewrite(
        clone / "wt" / ".git", "gitdir: /elsewhere/.git\n"
    ),
    "its .git names the clone's own git dir": lambda clone: rewrite(
        clone / "wt" / ".git", f"gitdir: {clone}/.git\n"
    ),
    "its .git names a worktree that is not there": lambda clone: rewrite(
        clone / "wt" / ".git", f"gitdir: {clone}/.git/worktrees/other\n"
    ),
    "its .git names something inside the worktree's entry": lambda clone: rewrite(
        clone / "wt" / ".git", f"gitdir: {clone}/.git/worktrees/wt/logs\n"
    ),
    "its .git leaves through ..": lambda clone: rewrite(
        clone / "wt" / ".git", f"gitdir: {clone}/.git/worktrees/wt/../../../elsewhere\n"
    ),
    "its .git has a second line": lambda clone: rewrite(
        clone / "wt" / ".git", f"gitdir: {clone}/.git/worktrees/wt\ngitdir: /elsewhere\n"
    ),
    "its .git is a link": lambda clone: (
        (clone / "wt" / ".git").rename(clone / "kept"),
        (clone / "wt" / ".git").symlink_to("../kept"),
    ),
    "the entry's commondir is not git's own": lambda clone: rewrite(
        admin(clone, "wt") / "commondir", "/elsewhere/.git\n"
    ),
    "the entry's gitdir does not lead back": lambda clone: rewrite(
        admin(clone, "wt") / "gitdir", "/elsewhere/wt/.git\n"
    ),
    "the entry has settings of its own": lambda clone: (admin(clone, "wt") / "config.worktree").write_text(
        "[core]\n\tfsmonitor = /elsewhere/run\n"
    ),
    "another gitlink beside it names a git dir elsewhere": gitfile_elsewhere,
}


@pytest.mark.parametrize("what", list(HOSTILE))
def test_a_worktree_beside_what_git_did_not_write_for_it_is_refused_for_good(task, what):
    worktree_inside(task.repo, "wt", "added")
    blocked(task)  # as git left it: repairable
    HOSTILE[what](task.repo)
    with pytest.raises(r.RepoError) as refused:
        r.check_protection(task.repo, task.meta)
    assert not isinstance(refused.value, r.Blocked), what
    assert "delete the task" in str(refused.value)


def test_what_a_file_said_when_the_clone_was_looked_at_decides_not_what_it_says_later(task, monkeypatch):
    """The entry's files are hashed when the clone's git dirs are looked at, and read again to
    tell a worktree's own folder: a file the writer swaps between the two is not what was
    hashed, and the stricter class wins, whichever way it was swapped."""
    from vivibox import protection

    worktree_inside(task.repo, "wt", "added")
    commondir = admin(task.repo, "wt") / "commondir"
    real = protection.fingerprint

    def swap_after(to: str):
        def looked(root, files):
            found = real(root, files)
            commondir.write_text(to)  # after the look that hashed it
            return found

        return looked

    commondir.write_text("/elsewhere/.git\n")  # hostile when hashed, git's own when read again
    monkeypatch.setattr(protection, "fingerprint", swap_after("../..\n"))
    with pytest.raises(r.RepoError) as refused:
        r.check_protection(task.repo, task.meta)
    assert not isinstance(refused.value, r.Blocked), "the hash is of what was hostile"
    commondir.write_text("../..\n")  # git's own when hashed, hostile when read again
    monkeypatch.setattr(protection, "fingerprint", swap_after("/elsewhere/.git\n"))
    with pytest.raises(r.RepoError) as refused:
        r.check_protection(task.repo, task.meta)
    assert not isinstance(refused.value, r.Blocked)


def test_a_git_file_swapped_while_it_is_classified_is_not_taken_for_a_worktrees(task, monkeypatch):
    """Read once to decide and once more to hold the decision: not the same twice, not repairable."""
    from vivibox import protection

    worktree_inside(task.repo, "wt", "added")
    real, reads = protection._read, []

    def swapping(root, rel, limit):
        data = real(root, rel, limit)
        if rel == "wt/.git":
            reads.append(rel)
            rewrite(task.repo / "wt" / ".git", "gitdir: /elsewhere/.git\n")  # after it was read as git's own
        return data

    monkeypatch.setattr(protection, "_read", swapping)
    with pytest.raises(r.RepoError) as refused:
        r.check_protection(task.repo, task.meta)
    assert reads and not isinstance(refused.value, r.Blocked)


def test_a_reply_in_a_box_sends_nobody_to_a_feedback_file(task):
    """A box has no writer and no verification's feedback (the review's R07): a reply there, its
    clone blocked or not, is the reply alone, and writes neither file."""
    from vivibox import prompts

    task.set_box()
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)
    task.transition(State.APPROVAL_RISKY, reason="box closed", then=str(State.CHECKPOINT_FINAL))
    nested(task.repo)
    assert r.blocked(task.repo) is not None
    assert actions.reply(task, "undo the build file") is State.IMPLEMENT
    assert supervisor.next_prompt(task, "") == prompts.COMMENT_PROMPT
    assert not (task.meta / "handoff" / "verify-feedback.md").exists()
    assert not (task.meta / "handoff" / "verify.log").exists()
