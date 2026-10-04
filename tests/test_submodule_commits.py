"""Accepting work that moved a submodule is refused while the commit it points at is in no
repository of yours: accepting deletes the task, and the task's clone may hold the only copy.

Real git throughout, no pod: a project with a submodule checked out, a task whose writer moves
it, and you, running the commands the refusal names, in your own shell.
"""

import ast
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import IDENTITY, LOCAL_SUBMODULES, global_git_config, make_repo
from test_git_environment import snapshot

from vivibox import actions, box, gate, submodules
from vivibox.cli import main
from vivibox.config import load_project
from vivibox.states import State

WRITER = ("-c", "user.name=Writer", "-c", "user.email=writer@example.com")


def git(path: Path, *args: str, input: bytes = b"") -> str:
    """Your own git, and the writer's in the task's clone: with your settings."""
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, input=input)
    assert done.returncode == 0, done.stderr
    return done.stdout.decode().strip()


@pytest.fixture
def project(env, monkeypatch) -> SimpleNamespace:
    """The project demo with a submodule, lib, checked out in your repository; lib has a
    submodule of its own, deep, checked out too."""
    global_git_config(monkeypatch, env / "home", IDENTITY + LOCAL_SUBMODULES)
    source = load_project("demo").repo
    deep, lib = make_repo(env / "deep"), make_repo(env / "lib")
    git(lib, "submodule", "add", "-q", str(deep), "deep")
    git(lib, "commit", "-qm", "A library of the library's")
    git(source, "submodule", "add", "-q", str(lib), "lib")
    git(source, "submodule", "update", "-q", "--init", "--recursive")
    git(source, "commit", "-qm", "A library")
    return SimpleNamespace(source=source, lib=lib, deep=deep, demo=load_project("demo"))


def written_in(folder: Path, text: str = "the writer's\n") -> str:
    """The writer's commit in a repository of the task's clone."""
    (folder / "README.md").write_text(text)
    git(folder, *WRITER, "commit", "-qam", "The writer's")
    return git(folder, "rev-parse", "HEAD")


def at_final(task, project, risky: bool = False) -> None:
    """The task as it waits for you with its work: its pointers committed, verified as far as
    this goes, its risky files approved by you when it changed any, the review copy made."""
    git(task.repo, "add", "-A")
    git(task.repo, *WRITER, "commit", "-qm", "Move the library")
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
    events, its clone, the review copy), your refs, your checkout and your submodules' commits."""
    source = project.source
    return {
        "task": snapshot(task.root),
        "refs": git(source, "for-each-ref"),
        "head": git(source, "rev-parse", "HEAD"),
        "status": git(source, "status", "--porcelain"),
        "submodules": git(source, "submodule", "status", "--recursive"),
        "history": actions.history(),
    }


def refused(task, project, how=("accept",)) -> str:
    """What accepting says when it refuses: nothing is accepted, and nothing changed."""
    before = everything(task, project)
    with pytest.raises(submodules.Unkept) as said:
        actions.finish(task, project.demo, branch_only="--branch" in how)
    assert everything(task, project) == before, "a refusal changed the task or your repository"
    assert task.read_state().state is State.CHECKPOINT_FINAL
    return str(said.value)


FROM_TASK = "GIT_NO_LAZY_FETCH=1 git"  # the fetch from the task's clone: nothing fetched lazily there


def commands(said: str) -> list[str]:
    return [line for line in said.splitlines() if line.startswith(("git ", FROM_TASK))]


def as_told(command: str) -> None:
    """A command of the refusal, pasted into your shell."""
    done = subprocess.run(["sh", "-c", command], env=dict(os.environ), capture_output=True, text=True)
    assert done.returncode == 0, f"{command}: {done.stderr}"


def test_a_commit_made_inside_a_submodule_is_accepted_once_you_have_it(project, capsys):
    """The story from end to end, on the command line: the writer commits inside the submodule
    and moves the pointer; accepting refuses, naming the submodule and the commit; the task is
    there, unchanged; the command that fetches from the task's clone is run; accepting again
    succeeds and the task goes; your git submodule update then gives you the writer's change."""
    source, lib = project.source, project.source / "lib"
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    before = everything(task, project)

    assert main(["accept", task.id]) == 1
    said = capsys.readouterr().err
    what, why, _ = said.partition("The task's clone may hold the only copy, and accepting deletes the task.")
    assert what.startswith(f"vivibox: {task.id} was not accepted: its work points at a submodule commit")
    assert why and said.rstrip().endswith(
        f"Then accept the work again. In a terminal, vivibox accept {task.id} says this whole."
    )
    assert f"lib at {commit} is not in your submodule." in said, "the submodule and the whole commit id"
    assert commands(said) == [
        f"git -C {lib} fetch --no-recurse-submodules",
        f"{FROM_TASK} -C {lib} fetch --no-tags --no-recurse-submodules {task.repo / 'lib'} {commit}",
    ]
    assert everything(task, project) == before and task.root.exists(), "the task, as it was"

    as_told(commands(said)[1])
    assert main(["accept", task.id]) == 0
    assert not task.root.exists(), "accepted, and the task gone with its clone"
    git(source, "commit", "-qm", "The work")
    assert git(source, "status", "--porcelain") == "M lib", "the pointer moved; your submodule not yet"
    git(source, "submodule", "update")  # your own, after the accept: vivibox never runs it for you
    assert git(lib, "rev-parse", "HEAD") == commit and (lib / "README.md").read_text() == "the writer's\n"
    assert git(source, "status", "--porcelain") == ""


def test_a_commit_the_submodules_remote_has_is_accepted_once_you_fetched_it(project):
    task = actions.create("demo", "Goal")
    (project.lib / "README.md").write_text("newer\n")
    git(project.lib, "commit", "-qam", "Newer, at the remote")
    git(task.repo / "lib", "fetch", "-q", "origin", "main")
    git(task.repo / "lib", "checkout", "-q", "FETCH_HEAD")
    commit = git(task.repo / "lib", "rev-parse", "HEAD")
    at_final(task, project)
    said = refused(task, project)
    assert f"lib at {commit} is not in your submodule." in said
    as_told(commands(said)[0])  # from the submodule's remote
    done = actions.finish(task, project.demo)
    assert not done.conflicts and not task.root.exists()
    git(project.source, "submodule", "update")
    assert git(project.source / "lib", "rev-parse", "HEAD") == commit


@pytest.mark.parametrize("how", [("accept",), ("accept", "--branch")], ids=" ".join)
def test_a_commit_you_have_is_accepted_at_once(project, how):
    """As before: a task with a verification command whose work moves a submodule to a commit
    your submodule holds, and one that touches no submodule at all, are accepted."""
    task = actions.create("demo", "Goal")
    (project.lib / "README.md").write_text("newer\n")
    git(project.lib, "commit", "-qam", "Newer, at the remote")
    git(project.source / "lib", "fetch", "-q")  # you have it; your work tree is where it was
    git(task.repo / "lib", "fetch", "-q", "origin", "main")
    git(task.repo / "lib", "checkout", "-q", "FETCH_HEAD")
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
        f"{FROM_TASK} -C {source / 'lib'} fetch --no-tags --no-recurse-submodules "
        f"{task.repo / 'lib'} {commit}",
    ]
    as_told(commands(said)[0])
    as_told(commands(said)[2])
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_what_your_repository_kept_of_a_submodule_counts_as_having_the_commit(project):
    """Deinitialised, its repository still under .git/modules, the commit in it: accepted."""
    source = project.source
    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    git(source / "lib", "fetch", "-q", "--no-tags", str(task.repo / "lib"), commit)
    git(source, "submodule", "deinit", "-f", "lib")
    at_final(task, project)
    actions.finish(task, project.demo)
    assert not task.root.exists()


def test_a_submodule_the_work_adds_is_refused_until_you_have_a_copy_of_it(project, env):
    """The work adds a submodule: its .gitmodules entry and its pointer, which is all a writer
    can make of one in its pod. Nothing of yours holds the commit: refused, with the command
    that clones the submodule into your checkout from the remote the work names; then accepted,
    and your git submodule update --init finds it there."""
    source, extra = project.source, make_repo(env / "extra")
    commit = git(extra, "rev-parse", "HEAD")
    task = actions.create("demo", "Goal")
    with (task.repo / ".gitmodules").open("a") as f:
        f.write(f'[submodule "extra"]\n\tpath = extra\n\turl = {extra}\n')
    (task.repo / "extra").mkdir()  # as git leaves the folder of a submodule that is not checked out
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},extra")
    at_final(task, project, risky=True)  # .gitmodules changed: yours to approve first
    said = refused(task, project)
    assert f"extra at {commit}: the work adds this submodule, and your checkout has no copy of it." in said
    assert f"Clone it to {source / 'extra'} from the remote the work's .gitmodules names" in said
    assert commands(said) == [f"git clone -- {extra} {source / 'extra'}"]
    as_told(commands(said)[0])
    actions.finish(task, project.demo)
    assert not task.root.exists()
    git(source, "commit", "-qm", "The work")
    git(source, "submodule", "update", "--init")
    assert git(source / "extra", "rev-parse", "HEAD") == commit
    assert git(source, "status", "--porcelain") == ""


@pytest.mark.parametrize("url", ["../relative.git", "-oProxyCommand=x", "ssh://host/a\x1b[8mb"])
def test_a_remote_that_is_no_plain_address_is_never_part_of_a_command(project, url):
    """The address is the writer's: one relative to your project's remote, one git would read as
    an option, and one with an escape sequence in it are left out of the command."""
    task = actions.create("demo", "Goal")
    commit = git(project.lib, "rev-parse", "HEAD")
    with (task.repo / ".gitmodules").open("a") as f:
        f.write(f'[submodule "extra"]\n\tpath = extra\n\turl = "{url}"\n')
    (task.repo / "extra").mkdir()  # as git leaves the folder of a submodule that is not checked out
    git(task.repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},extra")
    at_final(task, project, risky=True)
    said = refused(task, project)
    assert commands(said) == [f"git clone -- <its remote> {project.source / 'extra'}"]
    assert "relative" not in said and "ProxyCommand" not in said and "\x1b" not in said


def test_a_submodule_of_a_submodule_is_checked_once_you_have_the_commit_above_it(project):
    """The writer commits in lib/deep, then in lib, then moves lib. Your lib lacks its commit:
    refused for lib. Once you have that one, its own pointer is read in your lib: refused for
    lib/deep, by its path. Once you have both, accepted; git submodule update --recursive then
    stands on the writer's commits all the way down."""
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
    nested = f"{task.repo / 'lib' / 'deep'} {below}"
    assert commands(said)[1] == (
        f"{FROM_TASK} -C {source / 'lib' / 'deep'} fetch --no-tags --no-recurse-submodules {nested}"
    )
    as_told(commands(said)[1])
    actions.finish(task, project.demo)
    assert not task.root.exists()
    git(source, "commit", "-qm", "The work")
    git(source, "submodule", "update", "--recursive")
    assert git(source / "lib", "rev-parse", "HEAD") == above
    assert git(source / "lib" / "deep", "rev-parse", "HEAD") == below
    assert (source / "lib" / "deep" / "README.md").read_text() == "the writer's, below\n"


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


def test_the_views_accept_key_says_the_refusal_and_keeps_the_task(project):
    from test_tui import run
    from ux import screen_text

    task = actions.create("demo", "Goal")
    commit = written_in(task.repo / "lib")
    at_final(task, project)
    before = everything(task, project)

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("a")
        await pilot.press("enter")  # Accept, in the dialog that asks
        await app.workers.wait_for_complete()
        await pilot.pause()
        said = screen_text(app)
        assert f"{task.id} was not accepted" in said and commit in said
        # The notification wraps its commands: it says where they are printed whole.
        assert f"vivibox accept {task.id} says" in " ".join(said.replace("▌", " ").split())

    run(scenario, size=(200, 50), notifications=True)
    assert everything(task, project) == before and task.root.exists()


def test_a_name_that_is_not_plain_is_never_part_of_a_command(project):
    """The names are the writer's. One a shell would read as more than a name is quoted whole;
    one that is shown as other than itself (a line break, an escape) gets no command at all."""
    yours = submodules.at(project.source)
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


def test_only_a_repository_of_that_very_folder_counts_as_your_copy(project, env):
    """git answers for the repository above a folder whose .git is no repository, and follows a
    link out of your checkout: neither is taken for your copy of a submodule, so the commit is
    looked for in nothing but the submodule itself."""
    source = project.source
    yours = submodules.at(source)
    head = git(source, "rev-parse", "HEAD")
    assert yours.submodule("lib", head).tree == source / "lib" and yours.has(head)
    (source / "hollow" / ".git").mkdir(parents=True)
    assert submodules.at(source / "hollow") is None and yours.submodule("hollow", "") is None
    elsewhere = make_repo(env / "elsewhere")
    (source / "link").symlink_to(elsewhere)
    assert submodules.at(source / "link") is not None, "a repository, but not in your checkout"
    assert yours.submodule("link", "") is None
    assert yours.gitlinks("0" * 40) is None, "a commit git cannot list is said, not taken for none"
    # A name that is no text (the writer's own bytes): listed, and never taken for a commit to follow.
    odd = git(source, "mktree", "--missing", input=b"160000 commit " + head.encode() + b"\tli\xffb\n")
    assert yours.gitlinks(odd) == {"li\\xffb": f"not text: {head}"}
    assert [one.kind for one in submodules.unkept(yours, "", odd)] == [submodules.UNCHECKED]
    unread = submodules.unkept(yours, head, "0" * 40)
    assert [one.kind for one in unread] == [submodules.UNCHECKED]


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
    called from nowhere else."""
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
