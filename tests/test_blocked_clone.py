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

UNTIL = (
    "vivibox runs no git in the clone while it is there, so the work is neither verified nor "
    "brought back until then: reply, and the writer removes it, or takes it out of what git tracks"
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
        "the task added a repository of its own to its clone, and git on your machine would run what "
        f"its settings say: web/.git was added, a repository git would go into. {UNTIL}"
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
        "the task added a worktree to its clone, and git on your machine would run what its settings "
        "say: .git/worktrees was added; .git/worktrees/elsewhere was added; "
    )
    assert said.endswith(f" and 1 more. {UNTIL}")
    git(task.repo, "worktree", "remove", str(tmp_path / "elsewhere"))
    r.check_protection(task.repo, task.meta)


def test_with_both_each_is_said_in_its_turn(task, tmp_path):
    """What changed in the git dirs is looked at first, and the index only once they are as
    recorded: the worktree is named, and with it gone the repository."""
    nested(task.repo)
    worktree(task.repo, tmp_path / "elsewhere")
    assert blocked(task).startswith("the task added a worktree to its clone, ")
    git(task.repo, "worktree", "remove", str(tmp_path / "elsewhere"))
    assert blocked(task).startswith("the task added a repository of its own to its clone, ")


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
    assert said.endswith(
        "until then: whoever works in this box, which has no writer, removes it, or takes it out of what "
        "git tracks"
    )


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
    assert whole.startswith("the task added a repository of its own to its clone") and whole.endswith(UNTIL)
    assert notes[-1].startswith("verification could not run: the task added a repository of its own")
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
        "orchestrator's git does not go into. Take the folder out of the index (`git rm -f --cached` "
        "with its path, which leaves its files), delete the `.git` inside it, then add its files as "
        "ordinary files and commit; or delete the folder, when the task does not need it. Nothing "
        "is verified until then.\n"
    )


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
        "repository's own folder. Nothing is verified until then.\n"
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
    for said in (fed, ui.environment_problem(task), notes[-1]):
        commands = re.findall(r"`([^`]*)`", said) + re.findall(r"\(vivibox [^)]*\)", said)
        assert not [c for c in commands if "PWNED" in c or "$(" in c], said
    assert f"{name}/.git was added" in fed, "the name as text, whole"
    assert not (tmp_path / "PWNED").exists()


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
