"""Accepting work that moved a submodule is refused while the commit it keeps is kept by no
repository of yours: accepting deletes the task, and the task's clone may hold the only copy.

Real git throughout, no pod: a project with submodules checked out, a task whose writer moves
one, and you, running the commands the refusal names, in your own shell.
"""

import ast
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_counted
from conftest import IDENTITY, LOCAL_SUBMODULES, global_git_config, make_repo
from test_counted import BLOCK, BLOCKS, PRINTS_ON, ended, within
from test_git_environment import snapshot

from vivibox import actions, box, gate, repo, submodules
from vivibox.cli import main
from vivibox.config import load_project
from vivibox.states import State

WRITER = ("-c", "user.name=Writer", "-c", "user.email=writer@example.com")
stand_in = test_counted.stand_in  # the fixture, for the tests here: a git that prints what a test says


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


def at_final(task, project, risky: bool = False, reviewed: bool = True) -> None:
    """The task as it waits for you with its work: its pointers committed, verified as far as
    this goes, its risky files approved by you when it changed any, the review copy made
    (reviewed), which brings its work to your repository under refs/vivibox/<id>."""
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
    if reviewed:
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


# The clone of an address from a .gitmodules: from a folder where the work has no file, and with
# no file taken for a repository, so that git takes the address for a remote and nothing else.
CLONE = "git -C / -c protocol.file.allow=never clone"
FROM_TASK = "GIT_NO_LAZY_FETCH=1 git"  # the fetch from the task's clone: nothing fetched lazily there
# What the check's git is told: fetch nothing lazily, and read each commit as it is kept,
# whatever a replace ref of yours shows in its place.
AS_KEPT = {"GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


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
    --init finds it there. The remote here is a folder, which vivibox puts in no command: it
    is said as what the work's .gitmodules names, the agent's and not checked, and you clone
    from where you know the submodule to be."""
    source, extra = project.source, make_repo(env / "extra")
    commit = git(extra, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    added(task, "extra", commit, str(extra))
    at_final(task, project, risky=True)  # .gitmodules changed: yours to approve first
    said = refused(task, project)
    assert said.splitlines()[1:3] == [
        f"extra at {commit}: the work adds this submodule, and your checkout has no copy of it. As its "
        f"address the work's .gitmodules said «{extra}»: the agent wrote it, and vivibox has not checked "
        f"it and puts it in no command. Clone it to {source / 'extra'} from where you know the submodule "
        "to be, so that you keep the commit:",
        f"git -C / clone -- '<its remote>' {source / 'extra'}",
    ]
    as_told(commands(said)[0].replace("<its remote>", str(extra)))
    actions.finish(task, project.demo)
    assert not task.root.exists()
    git(source, "commit", "-qm", "The work")
    git(source, "submodule", "update", "--init")
    assert git(source / "extra", "rev-parse", "HEAD") == commit
    assert git(source, "status", "--porcelain") == ""


ADDRESSES = ["https://example.com/org/extra.git", "ssh://git@example.com:2222/org/extra.git",
             "git://example.com/extra.git", "git@example.com:org/extra.git", "ssh://example.com/~me/x.git",
             "git@example.com:/srv/git/extra.git", "https://example.com:8443/a/b_c/d-e+f.git/"]  # fmt: skip
NO_ADDRESSES = ["../relative.git", "./here", "/a/folder/on/this/machine", "file:///a/folder", "ext::sh -c x",
                "fd::3", "-oProxyCommand=x", "--upload-pack=x", "ssh://-oProxyCommand=x/y", "-x@host:path",
                "https://example.com/a b", "https://example.com/$(touch x)", "https://example.com/a;b",
                "https://example.com/a'b", "https://example.com/a\x1b[8mb", "http://example.com/plain",
                ""]  # fmt: skip
# What git, ssh or you would read as other than the address it looks like: a host, a user or a
# part of the path that is an option to the program it is handed to; a transport helper by any
# name; a password, or a user that reads as the host (https://host@elsewhere); a character
# written as %XX (a line break among them, which an older git hands to a credential helper);
# no path, or no host; a line break at its end.
NO_ADDRESSES += ["ssh://git@-oProxyCommand/x", "ssh://example.com/-x", "git@example.com:-x", "git@-host:x",
                 "ssh://example.com/a/-b", "https://example.com/--upload-pack", "foo::https://example.com/x",
                 "git@example.com::x", "ext@example.com::sh", "https://me:secret@example.com/x.git",
                 "ssh://git:secret@example.com/x.git", "https://github.com@elsewhere.example/org/x.git",
                 "git://me@example.com/x.git", "ssh://a@b@example.com/x", "github.com@elsewhere.example:x",
                 "https://example.com/a%0ab", "https://example.com/a%20b", "https://example.com",
                 "https://example.com/", "git@example.com", "git@example.com:", "https:///x",
                 "https://example.com:x/y", "https://[::1]/x", "https://example.com/x?y=1",
                 "https://example.com/x#y", "https://example.com/x\n", "HTTPS://example.com/x",
                 "https://example.com/a//b", "https://example.com/a\\b", "git@exa_mple.com:x"]  # fmt: skip


def test_only_an_address_that_runs_nothing_here_is_put_in_a_command(project):
    """The address is the writer's. One of a protocol that only fetches (https, ssh, git) or of
    the form user@host:path, with no option, space or character a shell reads, is printed in
    the clone command; anything else (a transport that runs a command or waits on a
    descriptor, a folder of this machine, an option) is in no command: it is shown whole, as
    text, in the sentence that says whose it is."""
    assert [a for a in ADDRESSES if not submodules.ADDRESS.fullmatch(a)] == []
    assert [a for a in NO_ADDRESSES if submodules.ADDRESS.fullmatch(a)] == []
    commit = git(project.lib, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    added(task, "extra", commit, ADDRESSES[3])
    added(task, "other", commit, "ext::sh -c 'touch MARK'")
    at_final(task, project, risky=True)
    said = refused(task, project)
    assert commands(said) == [
        f"{CLONE} -- {ADDRESSES[3]} {project.source / 'extra'}",
        f"git -C / clone -- '<its remote>' {project.source / 'other'}",
    ]
    assert said.count("ext::") == 1 and "the work's .gitmodules said «ext::sh -c 'touch MARK'»: " in said


# No host is reached by a command pasted in a test: an https address goes to a proxy that is not
# there, and ssh and git:// to a program that fails. What git says then names the address it
# went for, which is how a test knows git took it for a remote.
NO_HOST = {"https_proxy": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "no_proxy": "",
           "NO_PROXY": "", "GIT_SSH_COMMAND": "false", "GIT_PROXY_COMMAND": "false"}  # fmt: skip


def pasted(command: str, where: Path) -> subprocess.CompletedProcess:
    """A command of the refusal pasted into your shell in the folder where, with no host to reach."""
    return subprocess.run(
        ["sh", "-c", command], cwd=where, env={**os.environ, **NO_HOST}, capture_output=True, text=True
    )


def a_bundle(folder: Path) -> tuple[Path, str]:
    """A repository of the writer's own packed into one file, and its commit: what git clones
    from, with no network, when it is handed the file's path."""
    made = make_repo(folder / "made")
    git(made, "bundle", "create", "-q", str(folder / "made.bundle"), "HEAD", "main")
    return folder / "made.bundle", git(made, "rev-parse", "HEAD")


def planted(bundle: Path, folder: Path, address: str) -> None:
    """The bundle in folder at the path that spells address: git clone takes an argument for a
    path before it takes it for an address, and https://host/x is also the path https:/host/x."""
    at = folder / (f"{address}.bundle" if address.endswith("/") else address)
    at.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(bundle, at)


def test_the_printed_clone_never_takes_a_file_of_the_work_for_the_address(project, tmp_path):
    """The writer commits a repository of its own, packed, at the path that spells an address,
    and adds a submodule with that address. The review copy has the file. You read the
    refusal there, look at the address, which is the right one, and paste the command: git by
    itself would clone the file, from no host, and the next accept would pass on what the agent
    made. The printed command reaches the address as a remote and nothing else: here no host
    answers, so it fails, your checkout has no such folder, and the accept is still refused."""
    source, address = project.source, "https://example.com/org/extra.git"
    task = actions.create("demo", "Goal")
    bundle, commit = a_bundle(tmp_path)
    planted(bundle, task.repo, address)
    added(task, "extra", commit, address)
    at_final(task, project, risky=True)
    said = refused(task, project)
    copy = repo.review_worktree_path(source, task.root)
    assert (copy / "https:" / "example.com" / "org" / "extra.git").is_file(), "the review copy has the file"
    control = pasted(f"git clone -- {address} {tmp_path / 'control'}", copy)
    assert control.returncode == 0 and git(tmp_path / "control", "rev-parse", "HEAD") == commit, (
        "git by itself takes the file for the address"
    )
    done = pasted(commands(said)[0], copy)
    assert done.returncode != 0 and not (source / "extra").exists(), "the command cloned a file of the work"
    assert f"unable to access '{address}/'" in done.stderr, "git went for the host"
    assert refused(task, project) == said


WHERE = ["the review copy", "your checkout", "the folder the submodule goes in"]


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("address", ADDRESSES)
def test_a_path_that_spells_the_address_is_never_what_the_clone_command_reaches(
    project, tmp_path, address, where
):
    """Every form of address vivibox puts in a command, with a file at the path that spells it
    in each folder the command may be pasted in: the review copy, your checkout, the folder
    the submodule's own goes in. git by itself clones the file there. The printed command does
    not, whatever folder it is run in: it fails for want of a host, and makes no folder."""
    source = project.source
    folder = {WHERE[0]: tmp_path / "review-copy", WHERE[1]: source, WHERE[2]: source / "lib"}[where]
    bundle, commit = a_bundle(tmp_path)
    planted(bundle, folder, address)
    control = pasted(f"git clone -- {shlex.quote(address)} {tmp_path / 'control'}", folder)
    assert control.returncode == 0, f"no file is taken for {address} here: the case shows nothing"
    yours = submodules.at(source, soon())
    one = submodules.Kept("lib/extra", commit, submodules.ADDED, None, yours, "extra", address)
    said = submodules.refusal("demo-1", source, Path("/task/repo"), [one])
    done = pasted(commands(said)[0], folder)
    assert done.returncode != 0 and not (source / "lib" / "extra").exists(), "it cloned the file"
    assert "not allowed" not in done.stderr, "refused as a file, which is not having gone for the host"
    assert commands(said) == [
        f"git -C / -c protocol.file.allow=never clone -- {shlex.quote(address)} {source / 'lib' / 'extra'}"
    ]
    # Either half alone holds: from a folder where the work has no file, and with no file taken
    # for a repository wherever it is run.
    for half in ("git -C / clone", "git -c protocol.file.allow=never clone"):
        alone = pasted(f"{half} -- {shlex.quote(address)} {tmp_path / 'alone'}", folder)
        assert alone.returncode != 0 and not (tmp_path / "alone").exists(), half


def test_the_place_for_an_address_you_know_is_no_file_for_a_shell_to_open(project, tmp_path):
    """Where vivibox puts no address in the command, the command has a place for yours. Pasted
    as it is, a shell took < and > for files to read and to write: with a file named its in
    the folder (the review copy holds what the agent made), it wrote an empty file where the
    submodule goes and ran git clone on a path named remote. The place is one word to a shell,
    git is asked for a repository of that name and finds none, from a folder where the work has
    no file; with your address in it, the command clones as before."""
    source, copy = project.source, tmp_path / "review-copy"
    bundle, commit = a_bundle(tmp_path)
    copy.mkdir()
    (copy / "its").write_text("")
    shutil.copy(bundle, copy / "remote.bundle")
    yours = submodules.at(source, soon())
    one = submodules.Kept("extra", commit, submodules.ADDED, None, yours, "extra", "/a/folder")
    (command,) = commands(submodules.refusal("demo-1", source, Path("/task/repo"), [one]))
    done = pasted(command, copy)
    assert done.returncode != 0 and not (source / "extra").exists() and not (copy / "remote").exists()
    assert command == f"git -C / clone -- '<its remote>' {source / 'extra'}"
    assert pasted(command.replace("<its remote>", str(tmp_path / "made")), copy).returncode == 0
    assert git(source / "extra", "rev-parse", "HEAD") == commit


def test_an_address_the_agent_wrote_is_said_to_be_the_agents_and_not_checked(project):
    """The clone command carries an address from the work's .gitmodules. The line above it
    says whose it is (the agent wrote it), that vivibox has not checked it, to look at it
    before running the command, and where to clone from if it is not where the submodule is.
    Quoted for the shell where a character of it is one a shell reads (a tilde)."""
    source, commit = project.source, git(project.lib, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    added(task, "extra", commit, "https://example.com/org/extra.git")
    added(task, "home", commit, "ssh://example.com/~me/home.git")
    at_final(task, project, risky=True)
    assert refused(task, project).splitlines()[1:5] == [
        f"extra at {commit}: the work adds this submodule, and your checkout has no copy of it. Clone it "
        f"to {source / 'extra'}, so that you keep the commit. The address in the command is the one the "
        "work's .gitmodules names: the agent wrote it, and vivibox has not checked it. Look at it before "
        "you run the command, which takes it for a remote wherever it is run, never for a file of that "
        "name; clone from where you know the submodule to be if that is elsewhere:",
        f"{CLONE} -- https://example.com/org/extra.git {source / 'extra'}",
        f"home at {commit}: the work adds this submodule, and your checkout has no copy of it. Clone it "
        f"to {source / 'home'}, so that you keep the commit. The address in the command is the one the "
        "work's .gitmodules names: the agent wrote it, and vivibox has not checked it. Look at it before "
        "you run the command, which takes it for a remote wherever it is run, never for a file of that "
        "name; clone from where you know the submodule to be if that is elsewhere:",
        f"{CLONE} -- 'ssh://example.com/~me/home.git' {source / 'home'}",
    ]


LONG = "https://example.com/" + "a-very-long-part/" * 400 + "$(touch MARK)"
SHOWN_AS_TEXT = {
    "long": (LONG, LONG),
    "an escape sequence and a line of its own": (
        "https://example.com/x\x1b[2K\ngit clone https://elsewhere.example/y /tmp/y",
        "https://example.com/x‹U+001B›[2K‹U+000A›git clone https://elsewhere.example/y /tmp/y",
    ),
    "the marks it is shown between": ("ext::sh -c x» and vivibox checked it «", None),
    "none": ("", None),
}


@pytest.mark.parametrize("what", SHOWN_AS_TEXT)
def test_an_address_vivibox_puts_in_no_command_is_shown_whole_as_text(project, what):
    """An address that is not one for a command is still the person's to see: whole, however
    long, never cut and never with an ellipsis; with each character that has nothing to see
    as its code point, so it stays on its line and moves nothing on the terminal; between
    marks it cannot close, inside the sentence that says the agent wrote it and vivibox
    neither checked it nor puts it in a command. The command keeps a place for the address
    you know. A .gitmodules that names none is said so."""
    yours = submodules.at(project.source, soon())
    url, shown = SHOWN_AS_TEXT[what]
    one = submodules.Kept("extra", "0" * 40, submodules.ADDED, None, yours, "extra", url)
    said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [one])
    entry, command, last = said.splitlines()[1:]
    start = f"extra at {'0' * 40}: the work adds this submodule, and your checkout has no copy of it. "
    end = "Clone it to /yours/extra from where you know the submodule to be, so that you keep the commit:"
    assert entry.startswith(start) and entry.endswith(" " + end)
    assert (command, last) == ("git -C / clone -- '<its remote>' /yours/extra", "Then accept the work again.")
    told = entry.removeprefix(start).removesuffix(" " + end)
    if what == "none":
        assert told == "The work's .gitmodules names no address for it."
        return
    assert told.startswith("As its address the work's .gitmodules said «") and told.endswith(
        "»: the agent wrote it, and vivibox has not checked it and puts it in no command."
    )
    assert told.count("«") == told.count("»") == 1, "the address closed the marks it is shown between"
    assert "…" not in said and "..." not in said and "\x1b" not in said
    if shown:
        assert f"«{shown}»" in told


def test_a_refused_accept_takes_back_the_ref_it_brought_and_no_other(project):
    """Accepting brings the work to your repository, under refs/vivibox/<id>, before it checks
    it. Where no review copy had brought it (the ref was not there), a refusal takes the ref
    back: your refs are as they were, however often you try. The ref of another task stays,
    and so does one that was there before the attempt. The refs you made in your submodules
    with the refusal's commands (refs/vivibox/<id>/<commit>) are yours: never deleted."""
    source = project.source
    other = actions.create("demo", "Another task")
    written_in(other.repo / "tools")
    at_final(other, project)
    task = actions.create("demo", "Goal")
    commit, _ = written_in(task.repo / "lib"), written_in(task.repo / "tools")
    at_final(task, project, reviewed=False)
    before = git(source, "for-each-ref")
    assert f"refs/vivibox/{other.id}" in before and f"refs/vivibox/{task.id}" not in before
    with pytest.raises(submodules.Unkept) as said:
        actions.finish(task, project.demo)
    assert git(source, "for-each-ref") == before, "the refusal left a ref it brought"
    as_told(commands(said.value.whole)[1])  # lib's commit, kept under a ref of yours; tools is left
    kept = git(source / "lib", "for-each-ref", f"refs/vivibox/{task.id}")
    assert commit in kept
    with pytest.raises(submodules.Unkept):
        actions.finish(task, project.demo)
    assert git(source, "for-each-ref") == before and git(source / "lib", "for-each-ref").count(kept) == 1
    actions.prepare_review(task, project.demo)
    reviewed = git(source, "for-each-ref")
    assert f"refs/vivibox/{task.id}" in reviewed
    with pytest.raises(submodules.Unkept):
        actions.finish(task, project.demo)
    assert git(source, "for-each-ref") == reviewed, "a ref that was there before the attempt stays"


def ref_of_the_work(task, source: Path) -> str:
    """Where refs/vivibox/<id> is in your repository: its line of git for-each-ref, "" for none."""
    return git(source, "for-each-ref", f"refs/vivibox/{task.id}")


def test_a_ref_that_was_there_at_another_commit_is_put_back_where_it_was(project):
    """You looked at work in progress (vivibox review), which left refs/vivibox/<id> on that
    commit and the review copy showing it; the writer went on. Accepting brings the last
    commit under the ref, and is refused: the ref is back on the commit the review copy
    shows, not left on one the copy does not."""
    source = project.source
    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    first = committed(task, "Work in progress")
    actions.prepare_review(task, project.demo)
    (task.repo / "more.txt").write_text("more\n")
    committed(task, "The rest")
    at_final(task, project, reviewed=False)
    before = git(source, "for-each-ref")
    assert f"{first} commit\trefs/vivibox/{task.id}" in before
    with pytest.raises(submodules.Unkept):
        actions.finish(task, project.demo)
    assert git(source, "for-each-ref") == before, "the ref was left on a commit the review copy does not show"


@pytest.mark.parametrize("stopped", ["refused for staged changes of yours", "interrupted"])
def test_an_accept_that_stops_for_anything_takes_back_the_ref_it_brought(project, monkeypatch, stopped):
    """Not only the submodule check stops an accept after it brought the work: your staged
    changes do, and so does Ctrl-C. Whatever stops it before the work is accepted, the ref it
    brought goes, and the task is as it was."""
    source = project.source
    task = actions.create("demo", "Goal")
    (task.repo / "calc.py").write_text("x = 1\n")
    at_final(task, project, reviewed=False)
    before = git(source, "for-each-ref")
    if stopped == "interrupted":

        def interrupted(*args, **how):
            raise KeyboardInterrupt

        monkeypatch.setattr(submodules, "check", interrupted)
        with pytest.raises(KeyboardInterrupt):
            actions.finish(task, project.demo)
    else:
        (source / "yours.txt").write_text("staged\n")
        git(source, "add", "yours.txt")
        with pytest.raises(gate.GateError, match="has staged changes"):
            actions.finish(task, project.demo)
    assert ref_of_the_work(task, source) == "", "the accept left the ref it brought"
    assert git(source, "for-each-ref") == before and task.read_state().state is State.CHECKPOINT_FINAL


def test_a_review_copy_opened_while_an_accept_checks_keeps_its_ref(project, monkeypatch):
    """Nothing holds the task while an accept checks it, for up to a minute: the view may open
    the review copy meanwhile, which brings the work under the same ref and shows it. The
    accept, refused, found no ref when it began; the ref is the review copy's now, and stays:
    the panel's list of changed files and its diff read it."""
    source = project.source
    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    at_final(task, project, reviewed=False)
    check = submodules.check

    def meanwhile(*args, **how):
        actions.prepare_review(task, project.demo)  # the view's o, in another process
        return check(*args, **how)

    monkeypatch.setattr(submodules, "check", meanwhile)
    assert ref_of_the_work(task, source) == ""
    with pytest.raises(submodules.Unkept):
        actions.finish(task, project.demo)
    assert ref_of_the_work(task, source), "the ref was taken from under the review copy"
    assert "lib" in actions.changed_files(task, project.demo)


def test_a_ref_something_else_moved_meanwhile_is_not_the_accepts_to_put_back(project, monkeypatch):
    """The accept undoes what it did to the ref and nothing else: where the ref is no longer
    on the commit this accept brought, something else put it there, and it is left."""
    source = project.source
    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    at_final(task, project, reviewed=False)
    check, elsewhere = submodules.check, git(source, "rev-parse", "HEAD")

    def meanwhile(*args, **how):
        git(source, "update-ref", f"refs/vivibox/{task.id}", elsewhere)
        return check(*args, **how)

    monkeypatch.setattr(submodules, "check", meanwhile)
    with pytest.raises(submodules.Unkept):
        actions.finish(task, project.demo)
    assert ref_of_the_work(task, source) == f"{elsewhere} commit\trefs/vivibox/{task.id}"


def test_a_ref_git_could_not_put_back_is_said_with_what_removes_it(project, monkeypatch, capsys):
    """git refuses to change the ref (your repository's refs are locked, say): it stays where
    the accept put it, and the refusal says so, where it is, and what sets it right."""
    source = project.source
    task = actions.create("demo", "Goal")
    written_in(task.repo / "lib")
    at_final(task, project, reviewed=False)
    work, real = git(task.repo, "rev-parse", "HEAD"), repo.git

    def no_update(*args, **how):
        if args[0] == "update-ref":
            return subprocess.CompletedProcess(args, 1, "", "fatal: cannot lock ref")
        return real(*args, **how)

    monkeypatch.setattr(repo, "git", no_update)
    assert main(["accept", task.id]) == 1
    said = capsys.readouterr().err.splitlines()
    assert said[-2] == "Then accept the work again." and said[-1] == (
        f"refs/vivibox/{task.id} in {source} stays on {work}, where this accept brought the work: git could "
        f"not put it back. vivibox review {task.id} takes it for the review copy, and it goes with the task."
    )


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
        assert how["env"] == AS_KEPT and 0 < how["timeout"] <= submodules.SECONDS
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


def moved_at_remote(remote: Path, name: str, to: str, said: str) -> str:
    """The submodule's remote moves its own submodule `name` to a commit, with a commit."""
    git(remote / name, "fetch", "-q", "origin")
    git(remote / name, "checkout", "-q", to)
    git(remote, "commit", "-qam", said)
    return git(remote, "rev-parse", "HEAD")


def test_a_commit_the_history_needs_that_neither_the_remote_nor_the_task_has_says_the_way_out(project):
    """lib's remote went through a commit that points lib/deep at a commit its remote has
    dropped since, and on to one that points at its newest. The work only moves lib to the
    remote's last commit. You fetch lib, then lib/deep: the dropped commit comes with neither,
    and the task's clone never had it. The range stays: refused. The refusal says, after the
    two commands, what is left when neither has the commit: a reply to the task."""
    lib = project.source / "lib"
    git(project.deep, "checkout", "-q", "-b", "gone")
    (project.deep / "README.md").write_text("on a branch the remote drops\n")
    git(project.deep, "commit", "-qam", "Soon gone")
    gone = git(project.deep, "rev-parse", "HEAD")
    through = moved_at_remote(project.lib, "deep", gone, "Through a commit that will go")
    git(project.deep, "checkout", "-q", "main")
    git(project.deep, "branch", "-q", "-D", "gone")
    git(project.deep, "reflog", "expire", "--expire=now", "--all")
    cleaned_up(project.deep)
    newest = newer_at(project.deep)
    moved_at_remote(project.lib, "deep", newest, "On to the newest")
    task = actions.create("demo", "Goal")
    last = moved_to_remotes(task, "lib")
    at_final(task, project)
    said = refused(task, project)
    assert f"lib at {last} is not in your submodule." in said
    as_told(commands(said)[0])  # from lib's remote
    said = refused(task, project)
    assert f"lib/deep at {newest} is not in your submodule." in said
    as_told(commands(said)[0])  # from lib/deep's remote: the newest comes, the dropped one cannot
    said = refused(task, project)
    way_out = (
        f'If neither has it, send the work back with a reply (vivibox reply {task.id} "…") for work '
        f"that does not keep lib at {through}."
    )
    assert said.splitlines()[1:] == [
        f"lib/deep at {gone}{needed_by('lib', through)} is not in your submodule. Fetch it from the "
        f"submodule's remote, or from the task's clone ({ref_of(task, gone)} keeps it there, and may be "
        "deleted once its remote has it):",
        f"git -C {lib / 'deep'} fetch --no-recurse-submodules",
        from_task(task, lib / "deep", task.repo / "lib" / "deep", gone),
        way_out,
        "Then accept the work again.",
    ]
    as_told(commands(said)[0])
    served = subprocess.run(["sh", "-c", commands(said)[1]], capture_output=True)
    assert served.returncode != 0 and refused(task, project) == said, "neither has it, and nothing changed"


def test_a_submodule_only_the_history_has_is_not_said_to_be_added_by_the_work(project):
    """lib's remote added a submodule in one commit and removed it in the next; the work moves
    lib to the last. Neither the commit the task started from nor the one the work keeps has
    the submodule: it is the history's, said so, with the clone that keeps its commit."""
    lib = project.source / "lib"
    git(project.lib, "submodule", "add", "-q", str(project.tools), "old-dep")
    git(project.lib, "commit", "-qm", "A library it will drop")
    had, commit = git(project.lib, "rev-parse", "HEAD"), git(project.tools, "rev-parse", "HEAD")
    git(project.lib, "rm", "-qf", "old-dep")
    git(project.lib, "commit", "-qm", "Dropped")
    task = actions.create("demo", "Goal")
    moved_to_remotes(task, "lib")
    at_final(task, project)
    as_told(commands(refused(task, project))[0])  # lib's last commit, from its remote
    said = refused(task, project)
    assert "the work adds" not in said
    assert (
        f"lib/old-dep at {commit}{needed_by('lib', had)}: that history has a submodule here, and your "
        f"checkout has no copy of it. As its address that history's .gitmodules said «{project.tools}»: "
        "the agent may have written it, and vivibox has not checked it and puts it in no command. Clone "
        f"it to {lib / 'old-dep'} from where you know the submodule to be, so that you keep the commit; "
        "where the work leaves no submodule there, git shows the folder as one it does not track:"
    ) in said
    assert commands(said) == [f"git -C / clone -- '<its remote>' {lib / 'old-dep'}"]
    as_told(commands(said)[0].replace("<its remote>", str(project.tools)))
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


def test_the_git_that_tells_a_missing_start_from_a_failed_listing_is_one_of_the_checks(project, monkeypatch):
    """Your lib lacks the commit your project had it at: the listing of its history fails, and
    one more git says which of the two refusals it is (cat-file, of that commit, in your
    lib). It runs as every git of the check does: in your repository and never the task's
    clone, with nothing to read from, fetching nothing, within the check's time and limit."""
    source, lib = project.source, project.source / "lib"
    stood, newer = git(lib, "rev-parse", "HEAD"), newer_at(project.lib)
    git(source, "update-index", "--cacheinfo", f"160000,{newer},lib")
    git(source, "commit", "-qm", "Pulled; the submodule not updated")
    task = actions.create("demo", "Goal")
    git(task.repo / "lib", "checkout", "-q", stood)
    at_final(task, project)
    ran, real = [], repo.run_git
    monkeypatch.setattr(repo, "run_git", lambda args, **how: ran.append((args, how)) or real(args, **how))
    work = git(task.repo, "rev-parse", "HEAD")
    with pytest.raises(submodules.Unkept):
        submodules.check(task.id, source, task.repo, task.read_state().base_commit, work)
    assert [args for args, _ in ran if "cat-file" in args] == [
        ["-C", str(lib), "cat-file", "-e", f"{newer}^{{commit}}"]
    ]
    for args, how in ran:
        assert str(task.repo) not in " ".join(args) and how["stdin"] == subprocess.DEVNULL, args
        assert how["env"] == AS_KEPT and 0 < how["timeout"] <= submodules.SECONDS, args
        assert how["limit"] == submodules.LIMIT, args


def test_what_a_commit_of_the_history_needs_says_so_whatever_is_in_the_way(project):
    """A commit only a submodule's history needs, in a submodule you have not checked out: the
    way out for where neither its remote nor the task has it follows the commands, as it does
    where the submodule is checked out. And where your submodule lacks the commit it was at
    when the task started, the entry says that, with the fetch, and names what needs it."""
    yours = submodules.at(project.source, soon())
    below, then, start = "5" * 40, "4" * 40, "6" * 40
    named = f"lib/deep at {below}{needed_by('lib', then)}"
    out = submodules.Kept(
        "lib/deep", below, submodules.NOT_CHECKED_OUT, None, yours, "deep", via=("lib", then)
    )
    said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [out]).splitlines()
    assert said[1].startswith(f"{named}: your checkout has no copy of this submodule. Check it out, then ")
    assert said[2:4] == [
        f"git -C {project.source} submodule update --init -- deep",
        "git -C /yours/lib/deep fetch --no-recurse-submodules",
    ]
    assert said[5:] == [
        'If neither has it, send the work back with a reply (vivibox reply demo-1 "…") for work that does '
        f"not keep lib at {then}.",
        "Then accept the work again.",
    ]
    lacks = submodules.Kept(
        "lib/deep", below, submodules.NO_START, yours, yours, "deep", via=("lib", then), since=start
    )
    assert submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [lacks]).splitlines()[1:] == [
        f"{named}: your submodule does not have {start}, the commit lib had it at when the task started, "
        "so vivibox cannot tell which commits of its history are new. Fetch it from the submodule's remote:",
        "git -C /yours/lib/deep fetch --no-recurse-submodules",
        "Then accept the work again.",
    ]


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
            f"git --no-replace-objects -C {lib} log --format=%H --raw -r -c --root --no-renames "
            "--ignore-submodules=none "
            f"--no-show-signature --no-color {start}..{made.last} --"
        ]
        as_told(commands(said)[0])  # yours to run: here it lists, since git itself was not in the way
        why = "The git that lists them says why; accept the work again once it lists them:"
        assert f"{why}\n{commands(said)[0]}" in said
        # The git printed only says why: nothing is done by running it, so no "Then".
        assert "send the work back" not in said and "Then accept the work again" not in said
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


def test_a_commit_that_moves_two_pointers_is_listed_once(project, monkeypatch):
    """One commit of a history moves two submodules: it is one commit to look at, and is
    listed once, so that its pointers are read once."""
    lib = submodules.at(project.source / "lib", soon())
    two, one = "d" * 40, "e" * 40
    printed(monkeypatch, f"{two}\n\n{ROW}{ROW.replace('deep', 'other')}{one}\n\n{ROW}".encode())
    assert lib.history("a" * 40, "b" * 40) == [two, one]


COMMIT = "c" * 40 + "\n"
# What the stand-in for git prints, over and over: a listing as git log --raw prints one, with a
# pointer in it. Read from where it is cut, it would say a commit moves a submodule.
LISTING = COMMIT + "\n" + ROW
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


def test_a_commit_of_the_history_that_you_replaced_is_read_as_it_is_kept(project):
    """A replace ref of yours has git show another commit in place of one: here a commit of
    lib's history that points lib/deep at a commit of the writer's, replaced by one that
    points nowhere new. git's clean-up, a clone and a push keep and send the commit that was
    replaced, with its pointer: the check reads the commits as they are kept, so the pointer
    is seen, and the commit it needs is refused until you keep it."""
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    made = moved_and_back(task)
    at_final(task, project)
    kept_from(task.repo / "lib", lib, made.last)
    innocent = git(lib, "commit-tree", f"{made.then}~^{{tree}}", "-p", f"{made.then}~", "-m", "Innocent")
    git(lib, "replace", made.then, innocent)
    shown = git(lib, "log", "--raw", "--no-abbrev", "--format=%H", f"{made.then}~..{made.last}")
    assert made.then in shown and made.below not in shown, "what your git shows of that history"
    said = refused(task, project)
    assert f"lib/deep at {made.below}{needed_by('lib', made.then)} is not in your submodule." in said
    as_told(commands(said)[1])
    actions.finish(task, project.demo)
    assert not task.root.exists()


@pytest.mark.parametrize("listed", ["git fails", "git prints what is no list of commits"])
def test_work_whose_commits_git_does_not_list_is_refused_where_every_commit_is_kept(
    project, monkeypatch, listed
):
    """Kept as a branch, every commit of the work stays: here an earlier one moves lib to a
    commit of the writer's and a later one moves it back. Where git does not list the work's
    commits, the last one says nothing of the others: refused as what vivibox could not check,
    with the git that says why, never accepted on the last commit alone. An answer that is not
    commit ids, one to a line, is no list either: it is not looked at word by word as if each
    were a commit. Squashed, the last commit is all that is kept, and no commits are listed:
    accepted."""
    source = project.source
    task = actions.create("demo", "Goal")
    start = git(task.repo / "lib", "rev-parse", "HEAD")
    written_in(task.repo / "lib")
    committed(task, "Move the library")
    git(task.repo / "lib", "checkout", "-q", start)
    (task.repo / "notes.txt").write_text("the rest of the work\n")
    work = committed(task, "And back")
    at_final(task, project)
    base, real = task.read_state().base_commit, repo.run_git

    def run_git(args, **how):
        if "rev-list" in args and f"{base}..{work}" in args:
            if listed == "git fails":
                return subprocess.CompletedProcess(args, 128, b"", b"fatal: bad revision")
            return subprocess.CompletedProcess(args, 0, f"{work}\nwarning: not a commit\n".encode(), b"")
        return real(args, **how)

    monkeypatch.setattr(repo, "run_git", run_git)
    assert refused(task, project, ("accept", "--branch")).splitlines() == [
        f"{task.id} was not accepted: {NOT_KNOWN}",
        f"the work at {work}: vivibox could not list the commits of its history in your repository, so "
        "the work is not accepted with it. The git that lists them says why; accept the work again once "
        "it lists them:",
        f"git --no-replace-objects -C {source} log --format=%H --raw -r -c --root --no-renames "
        f"--ignore-submodules=none --no-show-signature --no-color {base}..{work} --",
    ]
    actions.finish(task, project.demo)
    assert not task.root.exists() and (source / "notes.txt").exists()


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


def test_a_refusal_of_a_commit_not_kept_and_of_what_was_not_checked_says_each_as_it_is(project):
    """One refusal, two submodules: tools at a commit you do not keep, and lib, whose commit
    you keep and whose history vivibox could not list. The first line is the stronger one,
    of the commit that is not kept; each entry says its own; the short form counts tools
    among the commits you do not keep and lib among what was not checked, never lib as a
    commit you lack; and the last line is there, since a command of the refusal keeps a
    commit. With lib alone there is no such line: the git printed for it only says why."""
    yours = submodules.at(project.source, soon())
    lib = submodules.Kept("lib", "1" * 40, submodules.UNLISTED, yours, yours, "lib", since="2" * 40)
    tools = submodules.Kept("tools", "3" * 40, submodules.MISSING, yours, yours, "tools")
    short = "demo-1 was not accepted: {}, and accepting deletes the task. In a terminal, vivibox accept "
    short += "demo-1 {}."
    both = (
        "its work keeps submodule commits you do not keep (tools), and vivibox could not check others (lib)"
    )
    for found in ([lib, tools], [tools, lib]):
        said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), found).splitlines()
        assert said[0].startswith("demo-1 was not accepted: its work keeps a submodule commit that no ")
        assert said[-1] == "Then accept the work again."
        assert submodules.briefly("demo-1", found) == short.format(both, "says how to keep them")
    said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [lib]).splitlines()
    assert said[0] == f"demo-1 was not accepted: {NOT_KNOWN}" and said[-1].startswith("git ")
    assert submodules.briefly("demo-1", [lib]) == short.format(
        "vivibox could not check its work's submodules (lib)", "says more"
    )
    assert submodules.briefly("demo-1", [tools]) == short.format(
        "its work keeps submodule commits you do not keep (tools)", "says how to keep them"
    )
    guidelines = (Path(__file__).parent.parent / "docs" / "ux-guidelines.md").read_text()
    assert "one refusal has both" in " ".join(guidelines.split()), "the rule for a refusal of both, in §5"


def test_a_pointer_no_gitmodules_gives_an_address_is_cloned_not_checked_out(project, env):
    """Your project points at a repository at a path its .gitmodules does not name, and your
    checkout has no copy of it: `git submodule update --init` answers "No url found" there.
    The refusal does not say to check it out: it says no .gitmodules names its address, and
    to clone it from where you know it to be, then the fetches; run, they make the check pass."""
    source, bare = project.source, make_repo(env / "bare")
    commit = git(bare, "rev-parse", "HEAD")
    (source / "bare").mkdir()
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},bare")
    git(source, "commit", "-qm", "A pointer .gitmodules does not name")
    base, newer = git(source, "rev-parse", "HEAD"), newer_at(bare)
    git(source, "update-index", "--cacheinfo", f"160000,{newer},bare")
    work = git(source, "commit-tree", git(source, "write-tree"), "-p", base, "-m", "The work moves it")
    git(source, "reset", "-q", base)
    told = subprocess.run(
        ["git", "-C", str(source), "submodule", "update", "--init", "--", "bare"], capture_output=True
    )
    assert told.returncode != 0 and b"No url found for submodule path 'bare'" in told.stderr
    with pytest.raises(submodules.Unkept) as refusal:
        submodules.check("demo-1", source, Path("/task/repo"), base, work)
    ref = f"refs/vivibox/demo-1/{newer[:12]}"
    assert refusal.value.whole.splitlines() == [
        "demo-1 was not accepted: its work keeps a submodule commit that no repository of yours keeps. "
        "The task's clone may hold the only copy, and accepting deletes the task.",
        f"bare at {newer}: your checkout has no copy of this submodule, and no .gitmodules of yours names "
        f"its address, so git cannot check it out. Clone it to {source / 'bare'} from where you know it "
        f"to be, then fetch the commit from the submodule's remote, or from the task's clone ({ref} keeps "
        "it there, and may be deleted once the submodule is on that commit or its remote has it):",
        f"git -C / clone -- '<its remote>' {source / 'bare'}",
        f"git -C {source / 'bare'} fetch --no-recurse-submodules",
        f"{FROM_TASK} -C {source / 'bare'} -c protocol.version=2 fetch --no-tags --no-recurse-submodules "
        f"/task/repo/bare {newer}:{ref}",
        "Then accept the work again.",
    ]
    assert str(refusal.value).startswith("demo-1 was not accepted: its work keeps submodule commits you do")
    as_told(commands(refusal.value.whole)[0].replace("<its remote>", str(bare)))
    submodules.check("demo-1", source, Path("/task/repo"), base, work)


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
    assert commands(said) == [f"{CLONE} -- https://example.com/extra.git {mine}"]
    assert "yours.example" not in said


def test_the_check_fetches_nothing_and_every_git_of_it_has_a_time_limit(project, monkeypatch):
    """Each git the check runs is told to fetch nothing lazily (a submodule of yours that is a
    partial clone would go to its remote for a commit it lacks), has a time limit and a limit
    on what is read of it, and reads a .gitmodules without its includes."""
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
        assert how["env"] == AS_KEPT and 0 < how["timeout"] <= submodules.SECONDS, args
        assert how["limit"] == submodules.LIMIT and "capture_output" not in how, args
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


@pytest.mark.parametrize(
    "word, stream",
    [("log", ""), ("log", ">&2"), ("the work", ""), ("config", ""), ("rev-list", ""), ("your project", ""),
     ("lib", "")],
    ids=["a history", "git's words on a history", "the work's files", "a .gitmodules", "the work's commits",
         "your project's own files", "your submodule's own files"],
)  # fmt: skip
def test_what_git_prints_past_the_limit_is_not_read_and_the_work_is_refused(
    project, monkeypatch, stand_in, word, stream
):
    """A listing is as long as the writer made it: a few objects make git print gigabytes.
    The check reads 64 MiB of what one git prints, on either stream, and no more (here the
    limit is 1 MiB, and the stand-in for git has 16 MiB to print and then waits). Past it,
    git is ended with every process it started, nothing of the answer cut short is used (it
    is a listing with a pointer in it), and the work is refused as what vivibox could not
    check, saying the limit, what was being read where that is known, and what to do; the
    task and your repositories are as they were. Every git of the check is read that way.

    What to do is to send the work back for less to list, where the listing is of what the work
    brings. Where it is of your own project as the task found it (its files at the commit the
    task started from, a submodule's at the commit it stood on), no work changes it and no
    reply helps: the refusal says that, and names no step that does nothing."""
    assert submodules.LIMIT == 64 * 2**20
    lib = project.source / "lib"
    task = actions.create("demo", "Goal")
    stood = git(lib, "rev-parse", "HEAD")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    kept_from(task.repo / "lib", lib, commit)  # you keep it: what is left is to read its history
    base, work = task.read_state().base_commit, git(task.repo, "rev-parse", "HEAD")
    before = everything(task, project)
    monkeypatch.setattr(submodules, "LIMIT", 16 * BLOCK)
    monkeypatch.setattr(submodules, "SECONDS", 20)  # were it read to its end: a failure, not a hang
    # The listing of a commit's files is asked for by the commit: the one that is cut short.
    listed = {"the work": work, "your project": base, "lib": stood}.get(word, word)
    git_ = stand_in(project.source.parent / "stand-in", listed, PRINTS_ON % (BLOCKS, stream), LISTING)
    with monkeypatch.context() as patched:
        patched.setenv("PATH", f"{git_.path}:{os.environ['PATH']}")
        with pytest.raises(submodules.Unkept) as said:
            within(lambda: submodules.check(task.id, project.source, task.repo, base, work, history=True))
    assert ended(git_.pids), "a process of the git that printed too much is still there"
    written = len(git_.written.read_text()) * BLOCK
    assert written <= submodules.LIMIT + 4 * BLOCK, "read past the limit: what git wrote was taken"
    where = {
        "log": " (reading the history of lib)",
        "the work": f" (listing the files of commit {work})",
        "your project": f" (listing the files of your project at {base}, as it was when the task started)",
        "lib": f" (listing the files of lib at {stood}, as it was when the task started)",
    }.get(word, "")
    stopped = "vivibox stopped checking the work's submodules at a safety limit: git printed more than "
    stopped += f"1 MiB{where}"
    step = (
        "Nothing of an answer cut short is used, so there may be more. Send the work back with a reply "
        f'(vivibox reply {task.id} "…") for work that leaves git less to list there.'
    )
    if word in ("your project", "lib"):
        step = (
            "Those files are your own, and the list of them is that large, so no work of a task of this "
            "project can be accepted while it is: a reply to the agent does not change it. The task is as "
            f"it was, with its work in its clone, {task.repo}."
        )
    assert said.value.whole.splitlines() == [f"{task.id} was not accepted: {NOT_KNOWN}", f"{stopped}. {step}"]
    assert "vivibox reply" not in step or word not in ("your project", "lib"), "a step that does nothing"
    assert str(said.value) == (
        f"{task.id} was not accepted: {stopped}, and accepting deletes the task. In a terminal, "
        f"vivibox accept {task.id} says more."
    )
    assert everything(task, project) == before and task.read_state().state is State.CHECKPOINT_FINAL


def test_a_commit_not_kept_and_the_limit_in_one_refusal_do_not_end_in_accept_again(
    project, monkeypatch, stand_in
):
    """The writer commits in lib, which you do not keep, and in tools, which you keep and whose
    history is past the limit. The refusal has the commands that keep lib's commit and the
    limit's line with its reply, and does not end "Then accept the work again.": that would
    end at the limit again. The short form names the limit too, and does not say that keeping
    the commit is how the work gets accepted."""
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    kept_from(task.repo / "tools", project.source / "tools", written_in(task.repo / "tools"))
    at_final(task, project)
    base, work = task.read_state().base_commit, git(task.repo, "rev-parse", "HEAD")
    monkeypatch.setattr(submodules, "LIMIT", 16 * BLOCK)
    monkeypatch.setattr(submodules, "SECONDS", 20)
    git_ = stand_in(project.source.parent / "stand-in", "log", PRINTS_ON % (BLOCKS, ""), LISTING)
    with monkeypatch.context() as patched:
        patched.setenv("PATH", f"{git_.path}:{os.environ['PATH']}")
        with pytest.raises(submodules.Unkept) as said:
            within(lambda: submodules.check(task.id, project.source, task.repo, base, work))
    assert ended(git_.pids)
    whole = said.value.whole.splitlines()
    assert (
        whole[1].startswith(f"lib at {commit} is not in your submodule.")
        and len(commands(said.value.whole)) == 2
    )
    over = "git printed more than 1 MiB (reading the history of tools)"
    assert whole[4:] == [
        f"vivibox stopped checking the work's submodules at a safety limit: {over}. Nothing of an answer cut "
        'short is used, so there may be more. Send the work back with a reply (vivibox reply demo-1 "…") '
        "for work that leaves git less to list there. The commands above keep those commits, and this "
        "work is still not accepted."
    ]
    assert str(said.value) == (
        f"{task.id} was not accepted: its work keeps submodule commits you do not keep (lib), and the check "
        f"stopped at a safety limit: {over}, and accepting deletes the task. In a terminal, vivibox accept "
        f"{task.id} says more."
    )


OVER = "git printed more than 64 MiB (reading the history of lib)"


@pytest.mark.parametrize("own", [False, True], ids=["of what the work brings", "of your own project"])
def test_a_refusal_at_the_limit_with_a_commit_not_kept_says_each_ones_step_and_names_both(project, own):
    """One refusal: tools at a commit you do not keep, and the check stopped at the limit.
    The commands keep the commit; the limit's line says its own step, or that there is none;
    and nothing says to accept again, since that ends at the limit as before. The short form
    names both, and does not say that keeping the commit is how the work gets accepted."""
    yours = submodules.at(project.source, soon())
    tools = submodules.Kept("tools", "3" * 40, submodules.MISSING, yours, yours, "tools")
    said = submodules.refusal(
        "demo-1", Path("/yours"), Path("/task/repo"), [tools], "", OVER, own
    ).splitlines()
    assert said[0].startswith("demo-1 was not accepted: its work keeps a submodule commit that no ")
    assert (
        said[1].startswith(f"tools at {'3' * 40} is not in your submodule.") and len(commands(said[2])) == 1
    )
    assert len(said) == 5 and "Then accept the work again." not in said
    stopped = f"vivibox stopped checking the work's submodules at a safety limit: {OVER}. "
    step = (
        "Nothing of an answer cut short is used, so there may be more. Send the work back with a reply "
        '(vivibox reply demo-1 "…") for work that leaves git less to list there.'
    )
    if own:
        step = (
            "Those files are your own, and the list of them is that large, so no work of a task of this "
            "project can be accepted while it is: a reply to the agent does not change it. The task is as "
            "it was, with its work in its clone, /task/repo."
        )
    assert (
        said[4]
        == f"{stopped}{step} The commands above keep those commits, and this work is still not accepted."
    )
    assert submodules.briefly("demo-1", [tools], "", OVER) == (
        "demo-1 was not accepted: its work keeps submodule commits you do not keep (tools), and the check "
        f"stopped at a safety limit: {OVER}, and accepting deletes the task. In a terminal, vivibox accept "
        "demo-1 says more."
    )
    lib = submodules.Kept("lib", "1" * 40, submodules.UNLISTED, yours, yours, "lib", since="2" * 40)
    assert submodules.briefly("demo-1", [lib], "", OVER) == (
        "demo-1 was not accepted: vivibox could not check its work's submodules (lib), and the check "
        f"stopped at a safety limit: {OVER}, and accepting deletes the task. In a terminal, vivibox accept "
        "demo-1 says more."
    )
    alone = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [], "", OVER, own).splitlines()
    assert alone[1] == f"{stopped}{step}", "no commands above: nothing said of them"


def test_a_submodule_added_below_another_is_not_said_to_be_named_by_the_works_own_gitmodules(project):
    """The work moves lib to a commit that adds a submodule, lib/new. Its address is in the
    .gitmodules of lib at that commit, which may be the remote's and not the agent's: said
    so, not as the work's own .gitmodules that the agent wrote."""
    yours = submodules.at(project.source, soon())
    for url, told in (
        (
            "https://example.com/new.git",
            "The address in the command is the one the .gitmodules of lib names: ",
        ),
        ("/a/folder", "As its address the .gitmodules of lib said «/a/folder»: "),
    ):
        one = submodules.Kept("lib/new", "7" * 40, submodules.ADDED, None, yours, "new", url)
        said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [one]).splitlines()[1]
        assert said.startswith(f"lib/new at {'7' * 40}: the work adds this submodule, and your checkout has")
        assert f"{told}the agent may have written it, and vivibox has not checked it" in said
        assert "the work's .gitmodules" not in said and "the agent wrote it" not in said
    none = submodules.Kept("lib/new", "7" * 40, submodules.ADDED, None, yours, "new", "")
    said = submodules.refusal("demo-1", Path("/yours"), Path("/task/repo"), [none]).splitlines()[1]
    assert "The .gitmodules of lib names no address for it." in said


def test_a_process_that_holds_gits_output_after_git_ended_goes_when_the_time_is_out(
    project, monkeypatch, stand_in
):
    """git ends at once, and a process it started holds the pipes it printed to: the check
    waits on them no longer than its time, says so as it says any git that was late, and ends
    that process with the rest of git's session."""
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    kept_from(task.repo / "lib", project.source / "lib", commit)
    base, work = task.read_state().base_commit, git(task.repo, "rev-parse", "HEAD")
    monkeypatch.setattr(submodules, "SECONDS", 4)
    git_ = stand_in(project.source.parent / "stand-in", "log", "exit 0")
    with monkeypatch.context() as patched:
        patched.setenv("PATH", f"{git_.path}:{os.environ['PATH']}")
        with pytest.raises(submodules.Unkept) as said:
            within(lambda: submodules.check(task.id, project.source, task.repo, base, work))
    late = "vivibox did not finish checking the work's submodules in 4 s (reading the history of lib)"
    assert f"{late}: there may be more." in said.value.whole
    assert ended(git_.pids), "the process git left behind is still there"


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
