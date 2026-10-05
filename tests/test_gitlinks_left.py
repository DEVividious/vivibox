"""A folder the work lists in the index as a submodule that is none: the repository that was in
it is gone (its `.git` deleted, half of taking it out), nothing in `.gitmodules` names it, and
git commits the pointer and none of the folder's files, without a word. The verification says
so, as it says of any file that is not committed. What is ordinary is left alone: a submodule
of the task's base, checked out or not, moved or not, and one the work adds with its entry in
`.gitmodules`."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import IDENTITY, LOCAL_SUBMODULES, global_git_config, make_repo
from test_blocked_start import Watched
from test_gate import PLAN, FakePod
from test_submodule_commits import project  # noqa: F401 (the fixture)

from vivibox import feedback, gate, protection
from vivibox import repo as r
from vivibox.risky import Approvals
from vivibox.task import create_task

SAID = (
    "{}: none of this folder's files, because the index lists the folder as a submodule and it holds no "
    "repository. Take it out of the index (`git rm -f --cached` with its path), add its files as "
    "ordinary files and commit"
)


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def a_task(tmp_path: Path, source: Path):
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(r.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    return t


@pytest.fixture
def task(tmp_path):
    return a_task(tmp_path, make_repo(tmp_path / "source"))


def half_repaired(clone: Path, name: str = "web", committed: bool = True) -> None:
    """A repository of its own that was tracked, then its .git deleted and nothing else done."""
    make_repo(clone / name)
    git(clone, "-c", "advice.addEmbeddedRepo=false", "add", "--", name)
    if committed:
        git(clone, "commit", "-q", "-m", "Add web")
    shutil.rmtree(clone / name / ".git")


@pytest.mark.parametrize("committed", [True, False])
def test_a_folder_listed_as_a_submodule_with_no_repository_in_it_is_not_committed(task, committed):
    half_repaired(task.repo, committed=committed)
    r.check_protection(task.repo, task.meta)  # the clone is one git runs in
    seen = git(task.repo, "status", "--porcelain")
    assert seen == ("" if committed else "A  web"), "git itself has nothing to say of the folder's files"
    assert gate.uncommitted(task.repo) == [*(["web"] if seen else []), SAID.format("web")]


def test_the_verification_is_red_and_the_writer_is_told_how_to_commit_the_files(task):
    half_repaired(task.repo)
    pod = FakePod()
    result = gate.run_gate(task, pod, ["make test"], [])
    assert not result.passed and pod.commands == [], "the build is not run on commits without the files"
    assert result.build_skipped == "there are uncommitted files, and only commits are verified"
    assert gate.next_state(result, 0, 3).value == "implement", "the writer's to fix, for a round"
    assert (
        f"- Not committed (verification uses your commits only): {SAID.format('web')}\n"
        in feedback.feedback(result)
    )
    assert gate.why_red(result).endswith("1 uncommitted file")


def test_doing_what_it_says_commits_the_files_and_the_verification_runs(task):
    half_repaired(task.repo)
    git(task.repo, "rm", "-q", "-f", "--cached", "web")
    git(task.repo, "add", "web")
    git(task.repo, "commit", "-q", "-m", "Add web as ordinary files")
    assert gate.uncommitted(task.repo) == []
    assert git(task.repo, "ls-files", "-s", "web").startswith("100644 ")


def test_a_name_the_agent_chose_is_text_and_in_no_command(task, tmp_path):
    name = "$(touch {}) x; y".format(tmp_path / "PWNED")
    half_repaired(task.repo, name="a\x1b[2Jb")
    said = gate.uncommitted(task.repo)
    assert said == [SAID.format("a‹U+001B›[2Jb")]
    shutil.rmtree(task.repo / "a\x1b[2Jb")
    git(task.repo, "add", "-A")
    git(task.repo, "commit", "-q", "-m", "Drop it")
    half_repaired(task.repo, name=name)
    (said,) = gate.uncommitted(task.repo)
    assert said.startswith(f"{name}: none of this folder's files") and said.count("PWNED") == 1
    assert "`git rm -f --cached` with its path" in said and not (tmp_path / "PWNED").exists()


# --- what is ordinary ---------------------------------------------------------------------------------


@pytest.fixture
def with_a_submodule(tmp_path, monkeypatch):
    """A task of a project that has a submodule, lib, checked out in the clone."""
    global_git_config(monkeypatch, tmp_path / "home", IDENTITY + LOCAL_SUBMODULES)
    lib = make_repo(tmp_path / "lib")
    source = make_repo(tmp_path / "source")
    git(source, "submodule", "add", "-q", str(lib), "lib")
    git(source, "commit", "-q", "-m", "Add lib")
    return a_task(tmp_path, source)


def not_checked_out(task) -> None:
    """lib as a clone has a submodule it did not check out: its folder there and empty, and the
    clone recorded so (the writer cannot bring a checked-out one to this: its settings are
    read-only in the pod, and its .git gone is a change the check refuses)."""
    git(task.repo, "submodule", "deinit", "-q", "-f", "lib")
    assert (task.repo / "lib").is_dir() and not (task.repo / "lib" / ".git").exists()
    r.record_protection(task.repo, task.meta)
    r.check_protection(task.repo, task.meta)


def test_a_submodule_of_the_base_is_left_alone_checked_out_or_not(with_a_submodule):
    task = with_a_submodule
    assert gate.uncommitted(task.repo) == [], "checked out: a repository is behind it"
    not_checked_out(task)
    assert gate.uncommitted(task.repo) == [], "not checked out: the base's own, and named in .gitmodules"
    git(task.repo, "rm", "-q", "--cached", ".gitmodules")
    git(task.repo, "commit", "-q", "-m", "Drop .gitmodules")
    assert gate.uncommitted(task.repo) == [".gitmodules"], "the base listed it: its own, entry or none"


def test_a_gitlink_of_the_base_that_the_work_moves_is_left_alone(with_a_submodule):
    task = with_a_submodule
    lib = task.repo / "lib"
    (lib / "more.txt").write_text("x\n")
    git(lib, "add", "more.txt")
    git(lib, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "More")
    git(task.repo, "add", "lib")
    git(task.repo, "commit", "-q", "-m", "Move lib on")
    assert gate.uncommitted(task.repo) == []
    not_checked_out(task)
    assert gate.uncommitted(task.repo) == [], "moved, and not checked out: still the base's"


def test_a_base_gitlink_with_no_entry_in_gitmodules_is_the_bases_own_business(tmp_path):
    """A project whose base has such a pointer already: not the work's doing, and not refused."""
    task = a_task(tmp_path, make_repo(tmp_path / "source"))
    commit = git(task.repo, "rev-parse", "HEAD")
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendored")
    git(task.repo, "commit", "-q", "-m", "A pointer with nothing behind it")
    (task.repo / "vendored").mkdir(exist_ok=True)
    assert gate.uncommitted(task.repo) == [SAID.format("vendored")], "added by the work: refused"
    task.set_base_commit(git(task.repo, "rev-parse", "HEAD"))  # the same commit as the task's base
    assert gate.uncommitted(task.repo) == [], "the base's: left alone"


def test_a_submodule_the_work_adds_with_its_entry_in_gitmodules_is_left_alone(task):
    """Added as a pointer and an entry, not checked out in the clone: a submodule, properly."""
    commit = git(task.repo, "rev-parse", "HEAD")
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    (task.repo / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = vendor/lib\n\turl = https://example.org/lib.git\n'
    )
    git(task.repo, "add", ".gitmodules")
    git(task.repo, "commit", "-q", "-m", "Add lib as a submodule")
    (task.repo / "vendor" / "lib").mkdir(parents=True, exist_ok=True)
    (task.repo / "vendor" / "lib" / "left.txt").write_text("x\n")
    r.check_protection(task.repo, task.meta)
    assert gate.uncommitted(task.repo) == []


def test_the_same_pointer_without_the_entry_is_what_is_refused(task):
    commit = git(task.repo, "rev-parse", "HEAD")
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    git(task.repo, "commit", "-q", "-m", "A pointer")
    (task.repo / "vendor" / "lib").mkdir(parents=True, exist_ok=True)
    assert gate.uncommitted(task.repo) == [SAID.format("vendor/lib")]


@pytest.mark.parametrize("included", ["a file outside the clone", "a named pipe"])
def test_the_works_gitmodules_is_read_without_what_it_includes(task, tmp_path, monkeypatch, included):
    """The commit's .gitmodules is the writer's, and may name another file to include: any file
    of this machine. Read with its includes, a file elsewhere that gives the folder as a
    submodule's path would have the folder left alone, and a named pipe would have git wait on
    it. It is read as data, without them: the folder is said, and the pipe is never opened."""
    monkeypatch.setattr(r, "CLONE_TIMEOUT", 5)  # were the pipe opened: a failure, not a minute's wait
    elsewhere = tmp_path / "elsewhere.gitconfig"
    if included == "a named pipe":
        os.mkfifo(elsewhere)
    else:
        elsewhere.write_text('[submodule "web"]\n\tpath = web\n')
    (task.repo / ".gitmodules").write_text(f"[include]\n\tpath = {elsewhere}\n")
    git(task.repo, "add", ".gitmodules")
    half_repaired(task.repo)
    r.check_protection(task.repo, task.meta)
    assert gate.uncommitted(task.repo) == [SAID.format("web")]


def long_pointers(monkeypatch, names: list[str]) -> list[int]:
    """The clone's index as listing names as submodules, each a folder with no repository in
    it, without making them: what the check reads of the index and of the folders says so.
    Returns the list that fills with the length of each command line git is run with."""
    made, real, bounded, handed = set(names), protection.look, r._bounded, []

    def look(root, rel, read=True, mode=False):
        if rel in made or rel.removesuffix("/.git") in made:
            return protection.FOLDER if rel in made else protection.ABSENT
        return real(root, rel, read, mode)

    def run(command, *rest):
        handed.append(sum(len(argument) + 1 for argument in command))
        return bounded(command, *rest)

    listed = protection.index_gitlinks
    monkeypatch.setattr(protection, "index_gitlinks", lambda root: [*listed(root), *names])
    monkeypatch.setattr(protection, "look", look)
    monkeypatch.setattr(r, "_bounded", run)
    return handed


def test_many_pointers_with_long_names_are_asked_of_git_a_handful_at_a_time(with_a_submodule, monkeypatch):
    """Six hundred folders listed as submodules, with names of four thousand characters: in one
    command line they are more than a command line holds, and git was not run at all
    (Argument list too long). git is asked of them a handful at a time, and each is answered
    as before: the base's own, whichever handful it is in, is left alone, and the rest are said.
    The base has two here, both asked of in the first handful of many: lib, which .gitmodules
    names too, and one that nothing names, which only git's answer for that handful leaves alone."""
    task = with_a_submodule
    commit = git(task.repo, "rev-parse", "HEAD")
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},a-pointer-of-the-base")
    git(task.repo, "commit", "-q", "-m", "A pointer nothing names")
    (task.repo / "a-pointer-of-the-base").mkdir()
    task.set_base_commit(git(task.repo, "rev-parse", "HEAD"))
    not_checked_out(task)
    names = [f"{n:03}" + "x" * 3997 for n in range(600)]
    handed = long_pointers(monkeypatch, names)
    said = r.gitlinks_left(task.repo)
    assert max(handed) < 100_000, "git was handed every pointer's path in one command line"
    assert said == [SAID.format(name) for name in names], "lib is the base's; the others are the work's"
    assert len(handed) > 2 and sum(handed) > 600 * 4000, "each was asked of git"
    assert r._handfuls(["a" * 20_000, "b" * 12_000, "c", "d" * 40_000, "e", "f"]) == [
        ["a" * 20_000, "b" * 12_000],
        ["c"],
        ["d" * 40_000],
        ["e", "f"],
    ], "a handful is counted from its own first path"


def test_a_pointer_at_a_path_longer_than_a_command_line_stops_the_verification_with_why(task, monkeypatch):
    """One path that is longer than a single argument may be: git cannot be asked of it. The
    verification stops on a reason, as it does for a clone it cannot read, not on what the
    system said of the command line."""
    long_pointers(monkeypatch, ["x" * 200_000])
    with pytest.raises(r.RepoError) as why:
        gate.uncommitted(task.repo)
    assert str(why.value) == (
        "the work lists a submodule at a path of 200000 characters, which is longer than git can be asked "
        "about, so the verification cannot check its submodule pointers: take that path out of the index"
    )


def test_a_pointer_whose_folder_is_gone_is_gits_own_to_say(task):
    """A worktree removed, its folder still in the index: git status has it (deleted), and it is
    said once, in git's words."""
    commit = git(task.repo, "rev-parse", "HEAD")
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},wt")
    assert gate.uncommitted(task.repo) == ["wt"]


def test_a_project_with_no_such_pointer_costs_no_git_beyond_the_status(task, tmp_path, monkeypatch):
    watched = Watched(tmp_path, monkeypatch)
    watched.start()
    assert gate.uncommitted(task.repo) == []
    assert [call.split("\t")[1].split()[0] for call in watched.in_clone(task.repo)] == ["status"]


def test_a_blocked_clone_is_asked_nothing(task, tmp_path, monkeypatch):
    """The check is the gate's git, behind the protection: on a clone it refuses, no git runs."""
    watched = Watched(tmp_path, monkeypatch)
    half_repaired(task.repo)
    watched.hostile(task.repo, "other")
    watched.start()
    with pytest.raises(r.Blocked):
        gate.uncommitted(task.repo)
    with pytest.raises(r.Blocked):
        r.gitlinks_left(task.repo)
    watched.nothing_ran_in(task.repo)


def test_accepting_such_work_still_refuses_in_its_own_words(project):  # noqa: F811
    """The check at accept is another one and is not changed: work that got to you with such a
    pointer (verified before this check, say) is refused there as it was."""
    from test_submodule_commits import at_final, refused

    from vivibox import actions

    task = actions.create("demo", "Add web")
    half_repaired(task.repo)
    assert gate.uncommitted(task.repo) == [SAID.format("web")], "the verification's word, a state earlier"
    at_final(task, project)
    said = refused(task, project)
    assert "web at " in said and "the work adds this submodule" in said
