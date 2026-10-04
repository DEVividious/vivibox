"""Accepting work that moved a submodule is refused while the commit it keeps is kept by no
repository of yours: accepting deletes the task, and the task's clone may hold the only copy.

Real git throughout, no pod: a project with submodules checked out, a task whose writer moves
one, and you, running the commands the refusal names, in your own shell.
"""

import ast
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import IDENTITY, LOCAL_SUBMODULES, global_git_config, make_repo
from test_git_environment import snapshot

from vivibox import actions, box, gate, repo, submodules
from vivibox.cli import main
from vivibox.config import load_project
from vivibox.states import State

WRITER = ("-c", "user.name=Writer", "-c", "user.email=writer@example.com")


def soon() -> float:
    """When a question asked of a repository here must be answered: time enough."""
    return time.monotonic() + 60


def git(path: Path, *args: str, input: bytes = b"") -> str:
    """Your own git, and the writer's in the task's clone: with your settings."""
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, input=input)
    assert done.returncode == 0, done.stderr
    return done.stdout.decode().strip()


@pytest.fixture
def project(env, monkeypatch) -> SimpleNamespace:
    """The project demo with two submodules, lib and tools, checked out in your repository; lib
    has a submodule of its own, deep, checked out too."""
    global_git_config(monkeypatch, env / "home", IDENTITY + LOCAL_SUBMODULES)
    source = load_project("demo").repo
    deep, lib, tools = make_repo(env / "deep"), make_repo(env / "lib"), make_repo(env / "tools")
    git(lib, "submodule", "add", "-q", str(deep), "deep")
    git(lib, "commit", "-qm", "A library of the library's")
    git(source, "submodule", "add", "-q", str(lib), "lib")
    git(source, "submodule", "add", "-q", str(tools), "tools")
    git(source, "submodule", "update", "-q", "--init", "--recursive")
    git(source, "commit", "-qm", "A library and tools")
    return SimpleNamespace(source=source, lib=lib, deep=deep, tools=tools, demo=load_project("demo"))


def written_in(folder: Path, text: str = "the writer's\n") -> str:
    """The writer's commit in a repository of the task's clone."""
    (folder / "README.md").write_text(text)
    git(folder, *WRITER, "commit", "-qam", "The writer's")
    return git(folder, "rev-parse", "HEAD")


def newer_at(remote: Path) -> str:
    """A commit the submodule's remote has, made after you last fetched."""
    (remote / "README.md").write_text("newer\n")
    git(remote, "commit", "-qam", "Newer, at the remote")
    return git(remote, "rev-parse", "HEAD")


def moved_to_remotes(task, name: str) -> str:
    """The writer moves the submodule to the newest commit of its remote."""
    git(task.repo / name, "fetch", "-q", "origin", "main")
    git(task.repo / name, "checkout", "-q", "FETCH_HEAD")
    return git(task.repo / name, "rev-parse", "HEAD")


def committed(task, said: str = "Move the library") -> str:
    git(task.repo, "add", "-A")
    git(task.repo, *WRITER, "commit", "-qm", said)
    return git(task.repo, "rev-parse", "HEAD")


def at_final(task, project, risky: bool = False) -> None:
    """The task as it waits for you with its work: its pointers committed, verified as far as
    this goes, its risky files approved by you when it changed any, the review copy made."""
    if git(task.repo, "status", "--porcelain"):
        committed(task)
    if task.read_state().box:
        return
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "done"))
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    if risky:
        actions.approve_risky(task, project.demo)
    actions.prepare_review(task, project.demo)


def everything(task, project) -> dict:
    """The task and your repository as they are: every file of the task's folder (its state, its
    events, its clone, the review copy), your refs, your checkout, your submodules' commits and
    their refs."""
    source = project.source
    return {
        "task": snapshot(task.root),
        "refs": git(source, "for-each-ref"),
        "head": git(source, "rev-parse", "HEAD"),
        "status": git(source, "status", "--porcelain"),
        "submodules": git(source, "submodule", "status", "--recursive"),
        "kept": git(source, "submodule", "foreach", "--recursive", "git for-each-ref"),
        "history": actions.history(),
    }


def refused(task, project, how=("accept",)) -> str:
    """What accepting says, whole, when it refuses: nothing is accepted, and nothing changed."""
    before = everything(task, project)
    with pytest.raises(submodules.Unkept) as said:
        actions.finish(task, project.demo, branch_only="--branch" in how)
    assert everything(task, project) == before, "a refusal changed the task or your repository"
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert str(said.value).startswith(f"{task.id} was not accepted: "), "the short form, for the view"
    return said.value.whole


FROM_TASK = "GIT_NO_LAZY_FETCH=1 git"  # the fetch from the task's clone: nothing fetched lazily there


def ref_of(task, commit: str) -> str:
    return f"refs/vivibox/{task.id}/{commit[:12]}"


def from_task(task, mine: Path, theirs: Path, commit: str) -> str:
    """The command that keeps a commit of the task's clone in your submodule, as it is printed."""
    return (
        f"{FROM_TASK} -C {mine} -c protocol.version=2 fetch --no-tags --no-recurse-submodules "
        f"{theirs} {commit}:{ref_of(task, commit)}"
    )


def commands(said: str) -> list[str]:
    return [line for line in said.splitlines() if line.startswith(("git ", FROM_TASK))]


def as_told(command: str) -> None:
    """A command of the refusal, pasted into your shell."""
    done = subprocess.run(["sh", "-c", command], env=dict(os.environ), capture_output=True, text=True)
    assert done.returncode == 0, f"{command}: {done.stderr}"


def cleaned_up(folder: Path) -> None:
    """git's clean-up as it is at its most thorough: what no ref reaches goes at once."""
    git(folder, "gc", "-q", "--prune=now")


def test_a_commit_made_inside_a_submodule_is_accepted_once_you_keep_it(project, capsys):
    """The story from end to end, on the command line: the writer commits inside the submodule
    and moves the pointer; accepting refuses, naming the submodule and the commit; the task is
    there, unchanged; the command that fetches from the task's clone is run, and keeps the
    commit under a ref; accepting again succeeds and the task goes; git's clean-up in your
    submodule, before and after, takes nothing; your git submodule update then gives you the
    writer's change."""
    source, lib = project.source, project.source / "lib"
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    before = everything(task, project)

    assert main(["accept", task.id]) == 1
    said = capsys.readouterr().err
    what, why, _ = said.partition("The task's clone may hold the only copy, and accepting deletes the task.")
    assert what.startswith(f"vivibox: {task.id} was not accepted: its work keeps a submodule commit")
    assert why and said.rstrip().endswith("Then accept the work again.")
    assert f"lib at {commit} is not in your submodule." in said, "the submodule and the whole commit id"
    assert f"({ref_of(task, commit)} keeps it there, and may be deleted once" in said
    assert commands(said) == [
        f"git -C {lib} fetch --no-recurse-submodules",
        from_task(task, lib, task.repo / "lib", commit),
    ]
    assert everything(task, project) == before and task.root.exists(), "the task, as it was"

    as_told(commands(said)[1])
    assert git(lib, "rev-parse", ref_of(task, commit)) == commit, "kept, under a ref of its own"
    cleaned_up(lib)
    assert main(["accept", task.id]) == 0
    assert not task.root.exists(), "accepted, and the task gone with its clone"
    cleaned_up(lib)
    git(source, "commit", "-qm", "The work")
    assert git(source, "status", "--porcelain") == "M lib", "the pointer moved; your submodule not yet"
    git(source, "submodule", "update")  # your own, after the accept: vivibox never runs it for you
    assert git(lib, "rev-parse", "HEAD") == commit and (lib / "README.md").read_text() == "the writer's\n"
    assert git(source, "status", "--porcelain") == ""


def test_a_commit_the_submodules_remote_has_is_accepted_once_you_fetched_it(project):
    task = actions.create("demo", "Goal")
    newer_at(project.lib)
    commit = moved_to_remotes(task, "lib")
    at_final(task, project)
    said = refused(task, project)
    assert f"lib at {commit} is not in your submodule." in said
    as_told(commands(said)[0])  # from the submodule's remote: its branch there keeps the commit
    cleaned_up(project.source / "lib")
    done = actions.finish(task, project.demo)
    assert not done.conflicts and not task.root.exists()
    git(project.source, "submodule", "update")
    assert git(project.source / "lib", "rev-parse", "HEAD") == commit


@pytest.mark.parametrize("how", [("accept",), ("accept", "--branch")], ids=" ".join)
def test_a_commit_you_keep_is_accepted_at_once(project, how):
    """As before: a task with a verification command whose work moves a submodule to a commit
    your submodule keeps, and one that touches no submodule at all, are accepted."""
    task = actions.create("demo", "Goal")
    newer_at(project.lib)
    git(project.source / "lib", "fetch", "-q")  # you have it; your work tree is where it was
    moved_to_remotes(task, "lib")
    at_final(task, project)
    actions.finish(task, project.demo, branch_only="--branch" in how)
    assert not task.root.exists()
    if "--branch" not in how:
        git(project.source, "commit", "-qm", "The work")
    plain = actions.create("demo", "No submodule in it")
    (plain.repo / "calc.py").write_text("x = 1\n")
    at_final(plain, project)
    actions.finish(plain, project.demo, branch_only="--branch" in how)
    assert not plain.root.exists()


def test_a_commit_that_is_in_your_submodule_and_kept_by_nothing_is_refused(project):
    """Fetched by its id alone, a commit is an object no ref reaches (FETCH_HEAD keeps nothing):
    git's next clean-up removes it. Having it is not keeping it: refused, with the command that
    gives it a ref; then accepted, and the clean-up leaves it."""
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    git(lib, "fetch", "-q", "--no-tags", str(task.repo / "lib"), commit)
    assert git(lib, "cat-file", "-t", commit) == "commit" and not git(
        lib, "for-each-ref", "--contains", commit
    )
    said = refused(task, project)
    assert f"lib at {commit} is in your submodule, and nothing keeps it there" in said
    assert commands(said) == [f"git -C {lib} update-ref {ref_of(task, commit)} {commit}"]
    as_told(commands(said)[0])
    actions.finish(task, project.demo)
    cleaned_up(lib)
    assert git(lib, "cat-file", "-t", commit) == "commit"


def test_a_commit_your_submodule_stands_on_is_kept(project):
    """Checked out by its id, on no branch: HEAD keeps it. Accepted."""
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    git(lib, "fetch", "-q", "--no-tags", str(task.repo / "lib"), commit)
    git(lib, "checkout", "-q", commit)
    assert not git(lib, "for-each-ref", "--contains", commit), "no ref: the detached HEAD alone"
    actions.finish(task, project.demo)
    assert not task.root.exists()


@pytest.mark.parametrize("kept", [False, True], ids=["nothing of it kept", "its repository kept"])
def test_a_submodule_you_have_not_checked_out_is_refused_until_you_do(project, kept):
    """Your checkout has no copy of the submodule (a fresh clone of your project, or one you
    deinitialised, with or without what .git/modules keeps of it): refused, with the command
    that checks it out first; the three commands, run, make the accepting pass."""
    source = project.source
    git(source, "submodule", "deinit", "-f", "lib")
    if not kept:
        subprocess.run(["rm", "-rf", str(source / ".git" / "modules" / "lib")], check=True)
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    said = refused(task, project)
    assert f"lib at {commit}: your checkout has no copy of this submodule." in said
    assert commands(said) == [
        f"git -C {source} submodule update --init -- lib",
        f"git -C {source / 'lib'} fetch --no-recurse-submodules",
        from_task(task, source / "lib", task.repo / "lib", commit),
    ]
    as_told(commands(said)[0])
    as_told(commands(said)[2])
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_what_your_repository_kept_of_a_submodule_counts_when_it_keeps_the_commit(project):
    """Deinitialised, its repository still under .git/modules, the commit under a ref in it:
    accepted."""
    source = project.source
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    git(source / "lib", "fetch", "-q", "--no-tags", str(task.repo / "lib"), f"{commit}:refs/heads/kept")
    git(source, "submodule", "deinit", "-f", "lib")
    at_final(task, project)
    actions.finish(task, project.demo)
    assert not task.root.exists()


def added(task, name: str, commit: str, url: str, more: str = "") -> None:
    """A submodule as a writer can add one in its pod: the .gitmodules entry and the pointer."""
    with (task.repo / ".gitmodules").open("a") as f:
        f.write(f'{more}[submodule "{name}"]\n\tpath = {name}\n\turl = "{url}"\n')
    (task.repo / name).mkdir()  # as git leaves the folder of a submodule that is not checked out
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},{name}")


def test_a_submodule_the_work_adds_is_refused_until_you_have_a_copy_of_it(project, env):
    """The work adds a submodule: its .gitmodules entry and its pointer, which is all a writer
    can make of one in its pod. Nothing of yours keeps the commit: refused, with the command
    that clones the submodule into your checkout; then accepted, and your git submodule update
    --init finds it there. The remote here is a folder, which vivibox puts in no command: you
    clone from the one the work names."""
    source, extra = project.source, make_repo(env / "extra")
    commit = git(extra, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    added(task, "extra", commit, str(extra))
    at_final(task, project, risky=True)  # .gitmodules changed: yours to approve first
    said = refused(task, project)
    assert f"extra at {commit}: the work adds this submodule, and your checkout has no copy of it." in said
    assert f"Clone it to {source / 'extra'} from the remote the work's .gitmodules names" in said
    assert "(an address vivibox does not put in a command)" in said
    assert commands(said) == [f"git clone -- <its remote> {source / 'extra'}"]
    as_told(commands(said)[0].replace("<its remote>", str(extra)))
    actions.finish(task, project.demo)
    assert not task.root.exists()
    git(source, "commit", "-qm", "The work")
    git(source, "submodule", "update", "--init")
    assert git(source / "extra", "rev-parse", "HEAD") == commit
    assert git(source, "status", "--porcelain") == ""


ADDRESSES = ["https://example.com/org/extra.git", "ssh://git@example.com:2222/org/extra.git",
             "git://example.com/extra.git", "git@example.com:org/extra.git"]  # fmt: skip
NO_ADDRESSES = ["../relative.git", "./here", "/a/folder/on/this/machine", "file:///a/folder", "ext::sh -c x",
                "fd::3", "-oProxyCommand=x", "--upload-pack=x", "ssh://-oProxyCommand=x/y", "-x@host:path",
                "https://example.com/a b", "https://example.com/$(touch x)", "https://example.com/a;b",
                "https://example.com/a'b", "https://example.com/a\x1b[8mb", "http://example.com/plain",
                ""]  # fmt: skip


def test_only_an_address_that_runs_nothing_here_is_put_in_a_command(project):
    """The address is the writer's. One of a protocol that only fetches (https, ssh, git) or of
    the form user@host:path, with no option, space or character a shell reads, is printed in
    the clone command; anything else (a transport that runs a command or waits on a
    descriptor, a folder of this machine, an option) is said to be there and left out."""
    assert [a for a in ADDRESSES if not submodules.ADDRESS.fullmatch(a)] == []
    assert [a for a in NO_ADDRESSES if submodules.ADDRESS.fullmatch(a)] == []
    commit = git(project.lib, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    added(task, "extra", commit, ADDRESSES[3])
    added(task, "other", commit, "ext::sh -c 'touch MARK'")
    at_final(task, project, risky=True)
    said = refused(task, project)
    assert commands(said) == [
        f"git clone -- {ADDRESSES[3]} {project.source / 'extra'}",
        f"git clone -- <its remote> {project.source / 'other'}",
    ]
    assert "ext::" not in said and "touch MARK" not in said


def test_a_submodule_of_a_submodule_is_checked_once_you_keep_the_commit_above_it(project):
    """The writer commits in lib/deep, then in lib, then moves lib. Your lib lacks its commit:
    refused for lib. Once you keep that one, its own pointer is read in your lib: refused for
    lib/deep, by its path. Once you keep both, accepted; git submodule update --recursive then
    stands on the writer's commits all the way down, after git's clean-up too."""
    source = project.source
    task = actions.create("demo", "Goal")
    below = written_in(task.repo / "lib" / "deep", "the writer's, below\n")
    git(task.repo / "lib", "add", "deep")
    above = written_in(task.repo / "lib")
    at_final(task, project)
    said = refused(task, project)
    assert f"lib at {above} is not in your submodule." in said and "lib/deep" not in said
    as_told(commands(said)[1])
    said = refused(task, project)
    assert f"lib/deep at {below} is not in your submodule." in said and f"lib at {above}" not in said
    assert commands(said)[1] == from_task(task, source / "lib" / "deep", task.repo / "lib" / "deep", below)
    as_told(commands(said)[1])
    actions.finish(task, project.demo)
    assert not task.root.exists()
    cleaned_up(source / "lib")
    cleaned_up(source / "lib" / "deep")
    git(source, "commit", "-qm", "The work")
    git(source, "submodule", "update", "--recursive")
    assert git(source / "lib", "rev-parse", "HEAD") == above
    assert git(source / "lib" / "deep", "rev-parse", "HEAD") == below
    assert (source / "lib" / "deep" / "README.md").read_text() == "the writer's, below\n"


def test_every_submodule_the_work_moved_is_checked_not_the_first_alone(project):
    """Two submodules moved: lib to a commit you keep, tools to one of the writer's. Refused,
    for tools, and lib is not named."""
    task = actions.create("demo", "Goal")
    newer_at(project.lib)
    git(project.source / "lib", "fetch", "-q")
    moved_to_remotes(task, "lib")
    commit = written_in(task.repo / "tools")
    at_final(task, project)
    said = refused(task, project)
    assert f"tools at {commit} is not in your submodule." in said and "lib at" not in said
    as_told(commands(said)[1])
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_submodules_more_than_eight_deep_are_refused_not_accepted(env, project):
    """Ten repositories, each a submodule of the one above, all moved to commits you keep: the
    check follows eight levels under your project's submodule and refuses the rest, saying why."""
    chain = [make_repo(env / f"level-{n}") for n in range(10)]
    for above, below in zip(reversed(chain[:-1]), reversed(chain[1:]), strict=True):
        git(above, "submodule", "add", "-q", str(below), "s")
        git(above, "commit", "-qm", "One below")
    source = project.source
    git(source, "submodule", "add", "-q", str(chain[0]), "top")
    git(source, "submodule", "update", "-q", "--init", "--recursive")
    git(source, "commit", "-qm", "Ten deep")
    newer_at(chain[-1])
    for above in reversed(chain[:-1]):  # each remote moves its pointer to the commit below
        git(above, "submodule", "update", "-q", "--remote", "s")
        git(above, "commit", "-qam", "Newer below")
    yours = source / "top"
    for _ in chain:  # you fetched every level, and stand where you stood
        git(yours, "fetch", "-q")
        yours = yours / "s"
    task = actions.create("demo", "Goal")
    moved_to_remotes(task, "top")
    at_final(task, project)
    said = refused(task, project)
    deepest = "top" + "/s" * submodules.DEEPEST
    assert f"{deepest} at " in said and "submodules more than 8 deep are not followed" in said
    assert said.startswith(f"{task.id} was not accepted: {NOT_KNOWN}\n")
    assert "send the work back with a reply" in said and commands(said) == []


@pytest.fixture
def a_box(monkeypatch):
    """A box with no pod: closing commits in the clone here, as its pod would."""
    monkeypatch.setattr(actions, "start_box", lambda task_id: None)
    monkeypatch.setattr(box, "start_box", lambda task_id: None)
    monkeypatch.setattr(box, "commit_in_box", lambda task: None)


@pytest.mark.parametrize("flow", ["a box", "a task with nothing to build", "as a branch"])
def test_every_way_of_accepting_work_refuses_the_same(project, a_box, flow):
    """A box's work and the work of a task with no build never pass through the verification's
    fresh clone, which would have failed on a commit only the task has; accepting as a branch
    changes nothing in your checkout. Each is refused as the plain accept is, and nothing of
    the task or of your repository changes: no branch, no commit, no line in the history."""
    if flow == "a box":
        task = actions.open_box("demo")
        commit = written_in(task.repo / "lib")
        at_final(task, project)
        assert actions.close_box(task, project.demo), "closed: its work is with you for review"
    else:
        task = actions.create("demo", "Goal", no_build=flow == "a task with nothing to build")
        commit = written_in(task.repo / "lib")
        at_final(task, project)
    said = refused(task, project, ("accept", "--branch") if flow == "as a branch" else ("accept",))
    assert f"lib at {commit} is not in your submodule." in said
    assert "vivibox/" not in git(project.source, "branch", "--list")
    as_told(commands(said)[1])
    actions.finish(task, project.demo, branch_only=flow == "as a branch")
    assert not task.root.exists()


@pytest.mark.parametrize("kept", ["squashed", "as a branch", "squashed on a branch of yours that moved"])
def test_a_commit_an_earlier_commit_of_the_work_pointed_at_is_checked_where_it_is_kept(project, kept):
    """The writer moves lib to a commit of its own and, in a later commit, back. Squashed into
    your checkout where the task started, only the last tree is kept, and it points nowhere
    new: accepted. Kept as a branch, every commit of the work stays, the first with its
    pointer: refused. And where your branch has moved since the task started, the work may
    conflict, and work that conflicts is kept as a branch too: refused the same."""
    task = actions.create("demo", "Goal")
    start = git(task.repo / "lib", "rev-parse", "HEAD")
    commit = written_in(task.repo / "lib")
    committed(task, "Move the library")
    git(task.repo / "lib", "checkout", "-q", start)
    (task.repo / "notes.txt").write_text("the rest of the work\n")
    committed(task, "And back")
    at_final(task, project)
    if kept == "squashed":
        actions.finish(task, project.demo)
        assert not task.root.exists() and (project.source / "notes.txt").exists()
        return
    if kept != "as a branch":
        (project.source / "yours.txt").write_text("since the task started\n")
        git(project.source, "add", "yours.txt")
        git(project.source, "commit", "-qm", "Yours, meanwhile")
    said = refused(task, project, ("accept", "--branch") if kept == "as a branch" else ("accept",))
    assert f"lib at {commit} is not in your submodule." in said
    as_told(commands(said)[1])
    actions.finish(task, project.demo, branch_only=kept == "as a branch")
    assert not task.root.exists()


def moved_and_back(task, top: str = "lib", name: str = "deep", to_remotes: bool = False) -> SimpleNamespace:
    """In the task's clone, the submodule `name` of `top` moved and moved back, each time with a
    commit of top: below, the commit it was moved to (the writer's own, or the newest of its
    remote); then, the commit of top that points at below; last, the one that points where it
    stood, and where top is left."""
    above = task.repo / top
    start = git(above / name, "rev-parse", "HEAD")
    below = (
        moved_to_remotes(task, f"{top}/{name}")
        if to_remotes
        else written_in(above / name, "the writer's, below\n")
    )
    git(above, "add", name)
    then = written_in(above, "pointing below\n")
    git(above / name, "checkout", "-q", start)
    git(above, "add", name)
    return SimpleNamespace(below=below, then=then, last=written_in(above, "and back\n"))


def kept_from(theirs: Path, mine: Path, commit: str) -> None:
    """You keep a commit of the task's clone in your submodule, under a branch of yours."""
    git(mine, "fetch", "-q", "--no-tags", "--no-recurse-submodules", str(theirs), f"{commit}:refs/heads/kept")


def needed_by(path: str, commit: str) -> str:
    return f" (needed by {path} at {commit}, in the history of {path} that the work keeps)"


@pytest.mark.parametrize("kept", ["squashed", "as a branch", "squashed on a branch of yours that moved"])
def test_a_commit_only_a_submodules_history_points_at_is_kept_before_the_task_goes(project, kept):
    """In lib, the writer's first commit points lib/deep at a commit the writer made there, and
    its second moves lib/deep back; the work leaves lib on the second. You keep that one: the
    first stays in your lib as its parent, and the commit it points at is in the task's clone
    alone. Refused, naming lib's history, the commit of it, lib/deep and the commit there, with
    nothing changed; the command that fetches from the task's clone is run; accepted, the task
    gone; and after git's clean-up in both, your lib checked out at its first commit gets what
    it points at from git submodule update."""
    source, lib = project.source, project.source / "lib"
    how = ("accept", "--branch") if kept == "as a branch" else ("accept",)
    task = actions.create("demo", "Goal")
    made = moved_and_back(task)
    at_final(task, project)
    if kept == "squashed on a branch of yours that moved":
        (source / "yours.txt").write_text("since the task started\n")
        git(source, "add", "yours.txt")
        git(source, "commit", "-qm", "Yours, meanwhile")
    said = refused(task, project, how)
    assert f"lib at {made.last} is not in your submodule." in said and "lib/deep" not in said
    as_told(commands(said)[1])  # as the refusal says: the last commit of lib, under a ref
    said = refused(task, project, how)
    assert said.startswith(
        f"{task.id} was not accepted: its work keeps a submodule commit that no repository of yours "
        "keeps. The task's clone may hold the only copy, and accepting deletes the task.\n"
    )
    assert f"lib/deep at {made.below}{needed_by('lib', made.then)} is not in your submodule." in said
    assert f"({ref_of(task, made.below)} keeps it there, and may be deleted once its remote has it)" in said
    assert commands(said) == [
        f"git -C {lib / 'deep'} fetch --no-recurse-submodules",
        from_task(task, lib / "deep", task.repo / "lib" / "deep", made.below),
    ]
    assert said.endswith("\nThen accept the work again.")
    as_told(commands(said)[1])
    actions.finish(task, project.demo, branch_only=kept == "as a branch")
    assert not task.root.exists(), "accepted, and the task gone with its clone"
    cleaned_up(lib)
    cleaned_up(lib / "deep")
    git(lib, "checkout", "-q", made.then)
    git(lib, "submodule", "update")
    assert git(lib / "deep", "rev-parse", "HEAD") == made.below
    assert (lib / "deep" / "README.md").read_text() == "the writer's, below\n"


def test_a_history_that_moves_no_pointer_costs_one_listing_and_no_submodule_none(project, monkeypatch):
    """Two commits of the writer's in lib, neither touching lib/deep, and you keep the last:
    accepted, for one listing of lib's new history and no look at its first commit. Work that
    touches no submodule is checked by the five gits it always was."""
    task = actions.create("demo", "Goal")
    first, last = written_in(task.repo / "lib", "one\n"), written_in(task.repo / "lib", "two\n")
    start = git(project.source / "lib", "rev-parse", "HEAD")
    at_final(task, project)
    kept_from(task.repo / "lib", project.source / "lib", last)
    plain = actions.create("demo", "No submodule in it")
    (plain.repo / "calc.py").write_text("x = 1\n")
    at_final(plain, project)
    ran, real = [], repo.run_git
    limits = []
    monkeypatch.setattr(
        repo, "run_git", lambda args, **how: ran.append(args) or limits.append(how) or real(args, **how)
    )
    work = git(task.repo, "rev-parse", "HEAD")
    submodules.check(task.id, project.source, task.repo, task.read_state().base_commit, work)
    listings = [args for args in ran if "log" in args]
    assert len(listings) == 1 and f"{start}..{last}" in listings[0], listings
    assert not [args for args in ran if first in " ".join(args)], "a commit that moves no pointer was read"
    assert not [args for args in ran if str(task.repo) in " ".join(args)], "asked of the task's clone"
    for how in limits:  # the listing as every other git of the check: nothing fetched, and a time limit
        assert how["env"] == {"GIT_NO_LAZY_FETCH": "1"} and 0 < how["timeout"] <= submodules.SECONDS
    ran.clear()
    work = git(plain.repo, "rev-parse", "HEAD")
    submodules.check(plain.id, project.source, plain.repo, plain.read_state().base_commit, work)
    assert len(ran) == 5 and not [args for args in ran if "log" in args], ran


def test_a_commit_the_history_points_at_that_you_keep_is_accepted(project):
    """lib/deep moved to the newest commit of its remote, which you have fetched, and back."""
    task = actions.create("demo", "Goal")
    newer_at(project.deep)
    git(project.source / "lib" / "deep", "fetch", "-q")
    made = moved_and_back(task, to_remotes=True)
    at_final(task, project)
    kept_from(task.repo / "lib", project.source / "lib", made.last)
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_a_submodule_the_history_points_into_that_you_have_not_checked_out_is_refused(project):
    """Your lib has no copy of lib/deep, checked out or kept: refused, with the command that
    checks it out before the one that fetches; run, they make the accepting pass."""
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    made = moved_and_back(task)
    at_final(task, project)
    kept_from(task.repo / "lib", lib, made.last)
    git(lib, "submodule", "deinit", "-f", "deep")
    subprocess.run(["rm", "-rf", git(lib, "rev-parse", "--absolute-git-dir") + "/modules/deep"], check=True)
    said = refused(task, project)
    named = f"lib/deep at {made.below}{needed_by('lib', made.then)}"
    assert f"{named}: your checkout has no copy of this submodule." in said
    assert commands(said) == [
        f"git -C {lib} submodule update --init -- deep",
        f"git -C {lib / 'deep'} fetch --no-recurse-submodules",
        from_task(task, lib / "deep", task.repo / "lib" / "deep", made.below),
    ]
    as_told(commands(said)[0])
    as_told(commands(said)[2])
    actions.finish(task, project.demo)
    assert not task.root.exists()


NOT_KNOWN = (
    "vivibox could not check that you keep every submodule commit its work keeps. The task's clone may "
    "hold the only copy of one, and accepting deletes the task."
)


@pytest.mark.parametrize("where, checked_out", [("lib", True), ("lib/deep", True), ("lib", False)])
def test_a_submodule_of_yours_without_the_commit_the_task_started_from_gets_the_fetch(
    project, where, checked_out
):
    """You pulled a commit that moves a submodule to a newer commit of its remote, and did not
    update the submodule: yours lacks that commit. The work moves the submodule back to the
    one you stand on. Which commits are new between the two cannot be listed without the
    first, so it is refused: as what vivibox could not check, with the fetch that brings the
    commit, never as work to send back. After the fetch, accepted. The same one level down,
    and for a submodule your repository only kept, which is checked out first."""
    source, lib = project.source, project.source / "lib"
    mine = source / where
    stood, newer = (
        git(mine, "rev-parse", "HEAD"),
        newer_at(project.deep if where == "lib/deep" else project.lib),
    )
    if where == "lib/deep":  # lib's remote moves lib/deep on; your lib follows it, your lib/deep does not
        git(project.lib / "deep", "pull", "-q", "origin", "main")
        git(project.lib, "commit", "-qam", "A newer library below")
        git(lib, "pull", "-q", "--no-recurse-submodules", "origin", "main")
        git(source, "add", "lib")
    else:
        git(source, "update-index", "--cacheinfo", f"160000,{newer},lib")
    git(source, "commit", "-qm", "Pulled; the submodule not updated")
    task = actions.create("demo", "Goal")
    if not checked_out:
        git(source, "submodule", "deinit", "-f", "lib")
    git(task.repo / where, "checkout", "-q", stood)
    if where == "lib/deep":
        git(task.repo / "lib", "add", "deep")
        kept_from(task.repo / "lib", lib, written_in(task.repo / "lib"))
    at_final(task, project)
    said = refused(task, project)
    owner = "lib" if where == "lib/deep" else "your project"
    assert said.startswith(f"{task.id} was not accepted: {NOT_KNOWN}\n")
    assert (
        f"{where} at {stood}: your submodule does not have {newer}, the commit {owner} had it at when the "
        "task started, so vivibox cannot tell which commits of its history are new. "
        f"{'Fetch' if checked_out else 'Check the submodule out, then fetch'} it from the submodule's remote:"
    ) in said
    init = [] if checked_out else [f"git -C {source} submodule update --init -- lib"]
    assert commands(said) == [*init, f"git -C {mine} fetch --no-recurse-submodules"]
    assert "send the work back" not in said and said.endswith("\nThen accept the work again.")
    with pytest.raises(submodules.Unkept) as short:
        actions.finish(task, project.demo)
    assert str(short.value) == (
        f"{task.id} was not accepted: vivibox could not check its work's submodules ({where}), and "
        f"accepting deletes the task. In a terminal, vivibox accept {task.id} says more."
    )
    for command in commands(said):
        as_told(command)
    actions.finish(task, project.demo)
    assert not task.root.exists()


LISTINGS = ["takes longer than the check has", "is still being read when the check's time is out", "fails"]


@pytest.mark.parametrize("listing", LISTINGS)
def test_a_history_vivibox_cannot_list_is_refused_and_said(project, monkeypatch, listing):
    """You keep every commit, the one lib's history points at too: only the listing of that
    history is in the way. One that outlasts the check's time, in git or in the reading of
    what git printed, or that git refuses, is no history without pointers: refused, saying
    which."""
    task = actions.create("demo", "Goal")
    made = moved_and_back(task)
    at_final(task, project)
    kept_from(task.repo / "lib", project.source / "lib", made.last)
    kept_from(task.repo / "lib" / "deep", project.source / "lib" / "deep", made.below)
    real, clock, later = repo.run_git, time.monotonic, [0.0]

    def run_git(args, **how):
        if "log" not in args:
            return real(args, **how)
        if listing == "fails":
            return subprocess.CompletedProcess(args, 128, b"", b"")
        if listing == LISTINGS[0]:
            raise subprocess.TimeoutExpired(args, how["timeout"])
        done = real(args, **how)
        later[0] = submodules.SECONDS  # git answered in time; what it printed is read past it
        return done

    monkeypatch.setattr(repo, "run_git", run_git)
    monkeypatch.setattr(submodules, "time", SimpleNamespace(monotonic=lambda: clock() + later[0]))
    said = refused(task, project)
    later[0] = 0.0
    monkeypatch.setattr(repo, "run_git", real)
    assert said.startswith(f"{task.id} was not accepted: {NOT_KNOWN}\n"), "what vivibox knows, and no more"
    if listing == "fails":
        lib, start = project.source / "lib", git(project.source / "lib", "rev-parse", "HEAD")
        why = "vivibox could not list the commits of its history in your repository"
        assert f"lib at {made.last}: {why}, so the work is not accepted with it. The git that" in said
        assert commands(said) == [
            f"git -C {lib} log --format=%H --raw -r -c --root --no-renames --ignore-submodules=none "
            f"--no-show-signature --no-color {start}..{made.last} --"
        ]
        as_told(commands(said)[0])  # yours to run: here it lists, since git itself was not in the way
        assert "send the work back" not in said and said.endswith("\nThen accept the work again.")
    else:
        late = f"vivibox did not finish checking the work's submodules in {submodules.SECONDS:g} s"
        assert f"{late} (reading the history of lib): there may be more." in said
        assert commands(said) == [] and "Then accept the work again" not in said
    actions.finish(task, project.demo)
    assert not task.root.exists()


def printed(monkeypatch, listed: bytes) -> None:
    """git's answer to the check from here on, whatever it is asked: a listing as given."""
    monkeypatch.setattr(submodules, "_git", lambda args, until: (0, listed))


ROW = ":160000 160000 1111111 2222222 M\tdeep\n"


def test_a_long_listing_is_read_in_one_pass_and_within_the_checks_time(project, monkeypatch):
    """Fifty thousand commits, each moving the pointer: read once through, in a time that does
    not grow with the square of their number (seconds then, minutes at ten times as many),
    and not at all once the check's time is out, which is said as git's own lateness is."""
    lib = submodules.at(project.source / "lib", soon())
    moves = [f"{n:040x}" for n in range(1, 50_001)]
    printed(monkeypatch, "".join(f"{commit}\n\n{ROW}" for commit in moves).encode())
    began = time.monotonic()
    assert lib.history("a" * 40, moves[0]) == moves[1:]
    assert time.monotonic() - began < 5, "the reading grows with the square of the commits"
    with pytest.raises(submodules.Late):
        submodules.Repository(lib.tree, lib.git_dir, time.monotonic() - 1).history("a" * 40, moves[0])


COMMIT = "c" * 40 + "\n"
NO_LISTINGS = {
    "a line that is no commit": "gpg: Signature made\n" + COMMIT + ROW,
    "a change before any commit": ROW + COMMIT,
    "a change with a field missing": COMMIT + ":160000 160000 1111111 M\tdeep\n",
    "a merge's change with one parent's fields": COMMIT + ":" + ROW,
}


@pytest.mark.parametrize("what", NO_LISTINGS)
def test_a_listing_with_a_line_the_check_does_not_know_is_no_listing(project, monkeypatch, what):
    """A line that is neither a commit's id nor a change as git log --raw prints one: the
    listing is not one vivibox can answer from, and is said so, never read around."""
    lib = submodules.at(project.source / "lib", soon())
    printed(monkeypatch, (COMMIT + ROW).encode())
    assert lib.history("a" * 40, "b" * 40) == ["c" * 40]
    printed(monkeypatch, NO_LISTINGS[what].encode())
    assert lib.history("a" * 40, "b" * 40) is None


def test_your_settings_for_what_a_log_shows_hide_no_pointer_from_the_check(project):
    """Settings of your lib that hide submodules from a diff, or add lines to a log, change
    what git log prints for you; the check's listing overrides them."""
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    made = moved_and_back(task)
    at_final(task, project)
    kept_from(task.repo / "lib", lib, made.last)
    for setting in ("diff.ignoreSubmodules=all", "log.showSignature=true", "log.showRoot=false",
                    "color.ui=always", "core.quotePath=false", "log.diffMerges=first-parent"):  # fmt: skip
        git(lib, "config", *setting.split("="))
    assert f"lib/deep at {made.below}{needed_by('lib', made.then)}" in refused(task, project)


@pytest.mark.parametrize("history", ["its remote's", "not its remote's"])
def test_the_history_of_a_submodule_the_work_adds_is_checked_where_no_remote_of_yours_has_it(
    project, env, history
):
    """The work adds the submodule extra, which you clone as the refusal says. There is no
    commit of it the task started from to count its new history from, so the commits counted
    are those no branch of a remote in your extra reaches: only those can be a writer's alone.
    Here extra's history points extra/inner at a commit you do not keep, and moves it back.
    Where that history is the remote's, the remote answers for it: accepted. Where you have it
    under a ref of your own and no remote's branch reaches it: refused, until you keep the
    commit it points at."""
    source, inner, extra = project.source, make_repo(env / "inner"), make_repo(env / "extra")
    git(extra, "submodule", "add", "-q", str(inner), "inner")
    git(extra, "commit", "-qm", "A library of its own")
    written = env / "written"  # where extra's further history was written
    git(env, "clone", "-q", "--recurse-submodules", str(extra), str(written))
    made = moved_and_back(SimpleNamespace(repo=env), top="written", name="inner")
    if history == "its remote's":
        git(extra, "pull", "-q", "--no-recurse-submodules", str(written), "HEAD")
    git(env, "clone", "-q", "--recurse-submodules", str(extra), str(source / "extra"))
    if history != "its remote's":
        git(
            source / "extra",
            "fetch",
            "-q",
            "--no-recurse-submodules",
            str(written),
            f"{made.last}:refs/yours",
        )
    task = actions.create("demo", "Goal")
    added(task, "extra", made.last, str(extra))
    at_final(task, project, risky=True)
    if history != "its remote's":
        said = refused(task, project)
        named = f"extra/inner at {made.below}{needed_by('extra', made.then)}"
        assert f"{named} is not in your submodule." in said and said.count(" is not in your ") == 1
        kept_from(written / "inner", source / "extra" / "inner", made.below)
    actions.finish(task, project.demo)
    assert not task.root.exists()
    there = subprocess.run(["git", "-C", str(source / "extra" / "inner"), "cat-file", "-e", made.below])
    assert (there.returncode == 0) == (history != "its remote's"), (
        "accepted without it only where vouched for"
    )


def test_the_history_of_a_commit_the_history_points_at_is_checked_too(project, env):
    """Three deep: top, top/s, top/s/s. top's history points top/s at a commit whose own
    history points top/s/s at a commit of the writer's; both moved back. Each is refused in
    turn as you keep the one above it, by its path and with the commit that needs it."""
    source = project.source
    chain = [make_repo(env / f"level-{n}") for n in range(3)]
    for above, below in ((chain[1], chain[2]), (chain[0], chain[1])):
        git(above, "submodule", "add", "-q", str(below), "s")
        git(above, "commit", "-qm", "One below")
    git(source, "submodule", "add", "-q", str(chain[0]), "top")
    git(source, "submodule", "update", "-q", "--init", "--recursive")
    git(source, "commit", "-qm", "Three deep")
    task = actions.create("demo", "Goal")
    top, middle = task.repo / "top", task.repo / "top" / "s"
    start = git(middle, "rev-parse", "HEAD")
    lower = moved_and_back(task, top="top/s", name="s")
    git(top, "add", "s")
    then = written_in(top, "pointing at the middle's new history\n")
    git(middle, "checkout", "-q", start)
    git(top, "add", "s")
    last = written_in(top, "and back\n")
    at_final(task, project)
    kept_from(top, source / "top", last)
    said = refused(task, project)
    assert f"top/s at {lower.last}{needed_by('top', then)} is not in your submodule." in said
    assert "top/s/s" not in said
    as_told(commands(said)[1])
    said = refused(task, project)
    assert f"top/s/s at {lower.below}{needed_by('top/s', lower.then)} is not in your submodule." in said
    assert commands(said)[1] == from_task(task, source / "top/s/s", task.repo / "top/s/s", lower.below)
    as_told(commands(said)[1])
    actions.finish(task, project.demo)
    assert not task.root.exists()
    for mine in (source / "top", source / "top/s", source / "top/s/s"):
        cleaned_up(mine)
    git(source / "top", "checkout", "-q", then)
    git(source / "top", "submodule", "update")
    git(source / "top/s", "checkout", "-q", lower.then)
    git(source / "top/s", "submodule", "update")
    assert git(source / "top/s/s", "rev-parse", "HEAD") == lower.below


def test_the_history_of_a_submodule_your_repository_only_kept_is_listed_there(project):
    """lib is not checked out: your repository kept it under .git/modules, and keeps the last
    commit of the writer's there. Its history is listed in that repository all the same, and
    the commit it points at refused; the commands check lib out, all the way down, and fetch."""
    source = project.source
    task = actions.create("demo", "Goal")
    made = moved_and_back(task)
    at_final(task, project)
    kept = git(source / "lib", "rev-parse", "--absolute-git-dir")
    git(source, "submodule", "deinit", "-f", "lib")
    fetch = ("fetch", "-q", "--no-tags", "--no-recurse-submodules", str(task.repo / "lib"))
    subprocess.run(["git", "--git-dir", kept, *fetch, f"{made.last}:refs/heads/kept"], check=True)
    said = refused(task, project)
    named = f"lib/deep at {made.below}{needed_by('lib', made.then)}"
    assert f"{named}: your checkout has no copy of this submodule." in said
    assert commands(said)[0] == f"git -C {source} submodule update --init --recursive -- lib"
    as_told(commands(said)[0])
    as_told(commands(said)[2])
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_what_a_commit_of_the_history_points_at_says_the_same_commit_all_the_way_down(project, env):
    """Three deep. top's history points top/s at a commit of the writer's, which itself
    points top/s/s at another; top moved back. The one below is needed by the same commit of
    top's history as the one above it, and is named with it."""
    source = project.source
    chain = [make_repo(env / f"level-{n}") for n in range(3)]
    for above, below in ((chain[1], chain[2]), (chain[0], chain[1])):
        git(above, "submodule", "add", "-q", str(below), "s")
        git(above, "commit", "-qm", "One below")
    git(source, "submodule", "add", "-q", str(chain[0]), "top")
    git(source, "submodule", "update", "-q", "--init", "--recursive")
    git(source, "commit", "-qm", "Three deep")
    task = actions.create("demo", "Goal")
    top, middle = task.repo / "top", task.repo / "top" / "s"
    start = git(middle, "rev-parse", "HEAD")
    lowest = written_in(middle / "s", "the writer's, lowest\n")
    git(middle, "add", "s")
    below = written_in(middle, "the writer's, pointing at the lowest\n")
    git(top, "add", "s")
    then = written_in(top, "pointing below\n")
    git(middle, "checkout", "-q", start)
    git(top, "add", "s")
    last = written_in(top, "and back\n")
    at_final(task, project)
    kept_from(top, source / "top", last)
    said = refused(task, project)
    assert f"top/s at {below}{needed_by('top', then)} is not in your submodule." in said
    as_told(commands(said)[1])
    said = refused(task, project)
    assert f"top/s/s at {lowest}{needed_by('top', then)} is not in your submodule." in said
    as_told(commands(said)[1])
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_the_listing_names_the_commits_that_move_a_pointer_and_no_other(env):
    """A history with a commit that adds a pointer, one that changes a file whose name is no
    text, a merge that carries the pointer of one side, a merge that moves it to a commit of
    its own, a commit that removes it and one after that: the first and the second merge are
    listed. With
    nothing to count from, a remote's branch that reaches a commit takes it out; a start that
    is no commit of the repository is said, not taken for a history without pointers."""
    there = make_repo(env / "there")
    start, other = git(there, "rev-parse", "HEAD"), "1" * 40
    git(there, "config", "core.quotePath", "false")

    def committed(*args: str) -> str:
        git(there, *args)
        return git(there, "rev-parse", "HEAD")

    git(there, "update-index", "--add", "--cacheinfo", f"160000,{start},sub")
    adds = committed("commit", "-qm", "A pointer")
    blob = git(there, "hash-object", "-w", "README.md")
    git(there, "update-index", "-z", "--index-info", input=f"100644 {blob}".encode() + b"\tcaf\xe9\0")
    committed("commit", "-qm", "A name that is no text")
    git(there, "checkout", "-q", "-b", "side", start)
    (there / "side.txt").write_text("side\n")
    git(there, "add", "side.txt")
    committed("commit", "-qm", "A side")
    git(there, "checkout", "-q", "main")
    carries = committed("merge", "-q", "--no-edit", "side")
    git(there, "checkout", "-q", "-b", "more", start)
    (there / "more.txt").write_text("more\n")
    git(there, "add", "more.txt")
    committed("commit", "-qm", "More")
    git(there, "checkout", "-q", "main")
    git(there, "merge", "-q", "--no-commit", "more")
    git(there, "update-index", "--cacheinfo", f"160000,{other},sub")
    moves = committed("commit", "-qm", "A merge that moves the pointer")
    git(there, "update-index", "--force-remove", "sub")
    committed("commit", "-qm", "No pointer")
    last = committed("commit", "-qm", "And on", "--allow-empty")
    yours = submodules.at(there, soon())
    assert yours.history(start, last) == [moves, adds] == yours.history("", last)
    assert yours.history(carries, last) == [moves] and yours.history(last, last) == []
    git(there, "update-ref", "refs/remotes/origin/main", carries)
    assert yours.history("", last) == [moves]
    git(there, "update-ref", "refs/remotes/origin/main", last)
    assert yours.history("", last) == []
    assert yours.history("0" * 40, last) is None and yours.history(start, "0" * 40) is None
    # A first commit that has a pointer adds it, whatever log.showRoot says of first commits.
    git(there, "config", "log.showRoot", "false")
    tree = git(there, "mktree", input=f"160000 commit {start}\tsub\n".encode())
    first = git(there, "commit-tree", tree, "-m", "A first commit with a pointer")
    tree = git(there, "mktree", input=f"160000 commit {start}\tsub\n100644 blob {blob}\tfile\n".encode())
    assert yours.history("", git(there, "commit-tree", tree, "-p", first, "-m", "And a file")) == [first]


def test_a_refusal_says_ten_submodules_in_full_and_counts_the_rest(project):
    commit = git(project.lib, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    for n in range(13):
        added(task, f"extra-{n:02}", commit, f"https://example.com/extra-{n}.git")
    at_final(task, project, risky=True)
    with pytest.raises(submodules.Unkept) as refusal:
        actions.finish(task, project.demo)
    said = refusal.value.whole
    assert (
        len(commands(said)) == submodules.LISTED == 10 and "extra-09 at" in said and "extra-10 at" not in said
    )
    assert "And 3 more submodule commits, said once these are kept." in said
    assert str(refusal.value) == (
        f"{task.id} was not accepted: its work keeps submodule commits you do not keep (extra-00, "
        f"extra-01, extra-02 and 10 more), and accepting deletes the task. In a terminal, vivibox accept "
        f"{task.id} says how to keep them."
    )


def test_a_check_that_runs_out_of_time_is_a_refusal_that_says_so(project, monkeypatch):
    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    at_final(task, project)
    monkeypatch.setattr(submodules, "SECONDS", 0)
    said = refused(task, project)
    assert "vivibox did not finish checking the work's submodules in 0 s: there may be more." in said
    assert said.startswith(f"{task.id} was not accepted: {NOT_KNOWN}\n")
    assert "Then accept the work again" not in said


@pytest.mark.parametrize("where", ["in the work's .gitmodules", "in a commit the writer made in a submodule"])
def test_a_gitmodules_the_writer_wrote_is_read_as_data(project, env, monkeypatch, where):
    """A .gitmodules may name another file to include, any file of this machine: one of yours,
    or a named pipe, which git would wait on for good. The writer's .gitmodules is read without
    its includes: the check answers at once, the pipe is not opened, and nothing a file it
    names says is taken for what the work says."""
    monkeypatch.setattr(submodules, "SECONDS", 20)  # were the pipe opened: a failure, not a hang
    pipe, yours = env / "pipe", env / "yours.gitconfig"
    os.mkfifo(pipe)
    yours.write_text('[submodule "extra"]\n\turl = git@yours.example:private/elsewhere.git\n')
    include = f"[include]\n\tpath = {pipe}\n\tpath = {yours}\n"
    commit = git(project.deep, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    if where == "in the work's .gitmodules":
        added(task, "extra", commit, "https://example.com/extra.git", more=include)
        at_final(task, project, risky=True)
        path, mine = "extra", project.source / "extra"
    else:
        inside = SimpleNamespace(repo=task.repo / "lib")
        added(inside, "extra", commit, "https://example.com/extra.git", more=include)
        git(inside.repo, "add", ".gitmodules")
        above = written_in(inside.repo)
        at_final(task, project, risky=True)  # a .gitmodules in the submodule's work tree is one too
        git(project.source / "lib", "fetch", "-q", "--no-tags", str(inside.repo), f"{above}:refs/heads/kept")
        path, mine = "lib/extra", project.source / "lib" / "extra"
    began = time.monotonic()
    said = refused(task, project)
    assert time.monotonic() - began < 10, "the check waited on a file the writer's .gitmodules names"
    assert f"{path} at {commit}: the work adds this submodule" in said
    assert commands(said) == [f"git clone -- https://example.com/extra.git {mine}"]
    assert "yours.example" not in said


def test_the_check_fetches_nothing_and_every_git_of_it_has_a_time_limit(project, monkeypatch):
    """Each git the check runs is told to fetch nothing lazily (a submodule of yours that is a
    partial clone would go to its remote for a commit it lacks), has a time limit, and reads a
    .gitmodules without its includes."""
    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    at_final(task, project)
    ran, real = [], repo.run_git
    monkeypatch.setattr(repo, "run_git", lambda args, **how: ran.append((args, how)) or real(args, **how))
    with pytest.raises(submodules.Unkept):
        work = git(task.repo, "rev-parse", "HEAD")
        submodules.check(task.id, project.source, task.repo, task.read_state().base_commit, work)
    assert len(ran) >= 6
    for args, how in ran:
        assert how["env"] == {"GIT_NO_LAZY_FETCH": "1"} and 0 < how["timeout"] <= submodules.SECONDS, args
        assert "config" not in args or "--no-includes" in args, args
        assert str(task.repo) not in " ".join(args), "asked of the task's clone"


def test_a_submodule_of_yours_that_is_a_partial_clone_is_not_sent_to_its_remote(env, project):
    """git in a partial clone fetches what it lacks from the remote it came from, a commit asked
    after among it. The check asks without that: the commit is not there, and stays not there."""
    remote = make_repo(env / "served")
    git(remote, "config", "uploadpack.allowFilter", "true")
    git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    partial = env / "partial"
    git(env, "clone", "-q", "--filter=blob:none", f"file://{remote}", str(partial))
    commit = newer_at(remote)
    yours = submodules.at(partial, soon())
    assert yours.keeps(commit) == submodules.MISSING
    missing = subprocess.run(
        ["git", "-C", str(partial), "cat-file", "-e", commit], env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    )
    assert missing.returncode != 0, "the check fetched the commit from the submodule's remote"


def test_the_fetch_from_the_tasks_clone_works_whatever_protocol_your_settings_name(project, env, monkeypatch):
    """protocol.version = 0 in your settings, and the commit no longer where the clone's
    submodule stands: a git speaking the old protocol is refused a commit by its id. The
    printed command names the protocol itself."""
    global_git_config(monkeypatch, env / "home", IDENTITY + LOCAL_SUBMODULES + "[protocol]\n\tversion = 0\n")
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    written_in(task.repo / "lib", "the writer went on\n")  # the clone's submodule stands elsewhere now
    said = refused(task, project)
    as_told(commands(said)[1])
    assert git(lib, "rev-parse", ref_of(task, commit)) == commit


def test_the_views_accept_key_says_the_refusal_briefly_and_keeps_the_task(project):
    """On the smallest terminal the view promises, 80 by 24: the notification says what was
    refused, which submodule, and the one command that says the rest; no fetch command in it."""
    from test_tui import run
    from ux import screen_text

    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    at_final(task, project)
    before = everything(task, project)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await pilot.press("enter")  # Accept, in the dialog that asks
        await app.workers.wait_for_complete()
        await pilot.pause()
        said = " ".join(screen_text(app).replace("▌", " ").split())
        assert f"{task.id} was not accepted: its work keeps submodule commits you do not keep (lib)" in said
        assert "and accepting deletes the task" in said
        assert f"In a terminal, vivibox accept {task.id} says how to keep them." in said
        assert "fetch" not in said

    run(scenario, size=(80, 24), notifications=True)
    assert everything(task, project) == before and task.root.exists()


def test_a_name_that_is_not_plain_is_never_part_of_a_command(project):
    """The names are the writer's. One a shell would read as more than a name is quoted whole;
    one that is shown as other than itself (a line break, an escape) gets no command at all."""
    yours = submodules.at(project.source, soon())
    spaced = submodules.Kept(
        "a dir/$(touch x)", "0" * 40, submodules.MISSING, yours, yours, "a dir/$(touch x)"
    )
    said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [spaced])
    assert "\ngit -C '/yours/a dir/$(touch x)' fetch --no-recurse-submodules\n" in said
    assert "'/task/repo/a dir/$(touch x)' " + "0" * 40 in said
    assert not submodules.plain("lib\nrm -rf x") and not submodules.plain("../elsewhere")
    assert not submodules.plain("lib/.git") and not submodules.plain("/abs") and submodules.plain("a dir/b")
    hidden = submodules.Kept("lib\x1b[8m", "0" * 40, submodules.UNCHECKED, None, yours, "lib\x1b[8m")
    said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [hidden])
    assert "git " not in said and "send the work back with a reply" in said and "\x1b" not in said
    assert "Then accept the work again" not in said
    assert "\x1b" not in submodules.briefly("demo-1", [hidden])


def test_only_a_repository_of_that_very_folder_counts_as_your_copy(project, env):
    """git answers for the repository above a folder whose .git is no repository, and follows a
    link out of your checkout: neither is taken for your copy of a submodule, so the commit is
    looked for in nothing but the submodule itself."""
    source = project.source
    yours = submodules.at(source, soon())
    head = git(source, "rev-parse", "HEAD")
    assert yours.submodule("lib", "lib").tree == source / "lib" and yours.keeps(head) == ""
    assert yours.modules(head) == {"lib": ("lib", str(project.lib)), "tools": ("tools", str(project.tools))}
    (source / "hollow" / ".git").mkdir(parents=True)
    assert submodules.at(source / "hollow", soon()) is None and yours.submodule("hollow", "") is None
    elsewhere = make_repo(env / "elsewhere")
    (source / "link").symlink_to(elsewhere)
    assert submodules.at(source / "link", soon()) is not None, "a repository, but not in your checkout"
    assert yours.submodule("link", "") is None
    assert yours.keeps("0" * 40) == submodules.MISSING
    assert yours.gitlinks("0" * 40) is None, "a commit git cannot list is said, not taken for none"
    found = submodules.Found()
    submodules.look(yours, head, ["0" * 40], found)
    assert [one.kind for one in found.unkept] == [submodules.UNCHECKED]
    # A name that is no text (the writer's own bytes): listed, and never taken for a commit to follow.
    odd = git(source, "mktree", "--missing", input=b"160000 commit " + head.encode() + b"\tli\xffb\n")
    assert yours.gitlinks(odd) == {"li\\xffb": f"not text: {head}"}
    found = submodules.Found()
    submodules.look(yours, "", [odd], found)
    assert [one.kind for one in found.unkept] == [submodules.UNCHECKED]


def calls(node: ast.AST) -> list[tuple[int, str]]:
    found = []
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        func = call.func
        owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else ""
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        found.append((call.lineno, f"{owner}.{name}" if owner else name))
    return sorted(found)


def test_no_way_of_accepting_work_skips_the_check():
    """Work is accepted in one function, review.finish: the command line, the view's key, a
    box's work and accepting as a branch all end there. In it the check comes before the first
    thing accepting does, and what applies the work, makes its branch or ends the task is
    called from nowhere else. A tripwire for a refactoring; the tests above are the proof."""
    package = Path(submodules.__file__).parent
    accepting = {"apply_work", "repo.create_branch", "actions.remove", "remember", "task.transition"}
    elsewhere = {}
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            if (path.name, function.name) == ("review.py", "finish"):
                made = calls(function)
                check = next(line for line, name in made if name == "submodules.check")
                first = min(line for line, name in made if name in accepting | {"cli_usage.count"})
                assert check < first, "the check runs before anything of the accepting"
                continue
            source = ast.get_source_segment(path.read_text(), function) or ""
            for name in ("apply_work(", "transition(State.DONE"):
                if name in source and not source.startswith(f"def {name}"):
                    elsewhere.setdefault(path.name, []).append(f"{function.name}: {name}")
    assert elsewhere == {}, "work is applied or a task ended outside review.finish"
