"""git on the host runs without the git variables of your shell. git takes them before its
working directory and -C: carried by the shell vivibox was started from (inside a git hook,
under a tool that exports them), they would aim vivibox's git at another repository, index or
object store of yours, or hand it configuration and programs to run.

Every class of host call is run here with each such variable exported and pointing at another
real repository, and then: the call answered for its own repository, the other repository is
byte for byte what it was, no hook or program named through the environment ran, and no git
vivibox started was handed the variable (a `git` first on PATH writes down what it is given).
"""

import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import GIT_CONFIG, HOME, IDENTITY, global_git_config, make_repo, suites_own

from vivibox import about, baseline, context, keys_work, reading, repo, version

REAL_GIT = shutil.which("git")
BRANCH = repo.branch_name("demo-1")
# The one git on the host that reaches your remotes, by its arguments, and what of your shell it
# alone is handed (repo.TO_YOUR_REMOTES): how you reach them.
SUBMODULES = "submodule update --init --recursive --quiet"
HOW_YOU_REACH_THEM = ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_ASKPASS", "GIT_SSL_CAINFO",
                      "GIT_SSL_CAPATH", "GIT_SSL_CERT", "GIT_SSL_KEY", "GIT_PROXY_COMMAND")  # fmt: skip
# Run by git for the commands vivibox gives it, were a hooksPath of yours to reach it.
HOOKS = ("pre-commit", "post-commit", "post-checkout", "post-merge", "reference-transaction",
         "post-index-change", "pre-auto-gc", "post-rewrite")  # fmt: skip
# Programs git looks up where GIT_EXEC_PATH says: a fetch from a folder serves it with the
# first, and git submodule is the second.
EXEC = ("git-upload-pack", "git-submodule")


def git(path: Path, *args: str) -> str:
    """The tests' own git: the real one, with no git variable but the suite's configuration."""
    own = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    own["GIT_CONFIG_GLOBAL"] = str(GIT_CONFIG)
    done = subprocess.run([REAL_GIT, "-C", str(path), *args], env=own, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@dataclass
class Site:
    """Your repository, another repository of yours, and what a variable of your shell can name."""

    mine: Path
    other: Path
    left: Path  # where a hook, a program or a trace of yours leaves its mark: empty at the end
    program: Path
    hooks: Path
    config: Path
    templates: Path
    exec_path: Path
    spy: Path  # the folder first on PATH, whose git writes down its GIT_* variables
    log: Path

    def ran(self) -> list[tuple[str, list[str]]]:
        """Every git vivibox started, as (its arguments, the GIT_* variables it was handed)."""
        if not self.log.exists():
            return []
        runs = (run.splitlines() for run in self.log.read_text().split("== git ")[1:])
        return [(lines[0], lines[1:]) for lines in runs]

    def logged(self) -> list[list[str]]:
        """The GIT_* variables of every git vivibox started, one list per git."""
        return [variables for _, variables in self.ran()]


def script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def site(tmp_path) -> Site:
    mine = make_repo(tmp_path / "mine")
    (mine / "link").symlink_to("README.md")
    (mine / "src").mkdir()
    (mine / "src" / "calc.py").write_text("x = 1\n")
    git(mine, "add", ".")
    git(mine, "commit", "-q", "-m", "A link and a module")
    (mine / "draft.txt").write_text("not committed\n")

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "theirs")
    git(other, "config", "user.name", "Somebody Else")
    git(other, "config", "user.email", "else@example.com")
    (other / "theirs.txt").write_text("committed there\n")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "Their commit")
    (other / "staged.txt").write_text("in their index, not committed\n")
    git(other, "add", "staged.txt")
    (other / "theirs.txt").write_text("changed, not committed\n")
    (other / "untracked.txt").write_text("untracked, and theirs\n")

    left = tmp_path / "left"
    left.mkdir()
    mark = f'echo "$0" >> "{left}/ran"'
    program = script(tmp_path / "yours" / "program", mark)
    hooks = tmp_path / "yours" / "hooks"
    templates = tmp_path / "yours" / "templates"
    exec_path = tmp_path / "yours" / "exec"
    for name in HOOKS:
        script(hooks / name, mark)
        script(templates / "hooks" / name, mark)
    for name in EXEC:
        script(exec_path / name, f"{mark}\nexit 1")
    config = tmp_path / "yours" / "gitconfig"
    config.write_text(
        f"[user]\n\tname = From The Environment\n\temail = environment@example.com\n"
        f"[core]\n\thooksPath = {hooks}\n[diff]\n\texternal = {program}\n"
    )
    log = tmp_path / "git.log"
    spy = tmp_path / "spy"
    script(spy / "git", f'{{ echo "== git $*"; env | grep "^GIT_"; }} >> "{log}"\nexec "{REAL_GIT}" "$@"')
    return Site(mine, other, left, program, hooks, config, templates, exec_path, spy, log)


# What your shell may carry, by the variable that names the case, as what is exported: each
# value is another repository's, a program's or a file's, and none is a value vivibox sets itself.
CASES = {
    # git rev-parse --local-env-vars: where the repository, its index and its objects are
    "GIT_DIR": lambda s: {"GIT_DIR": f"{s.other}/.git"},
    "GIT_WORK_TREE": lambda s: {"GIT_WORK_TREE": str(s.other)},
    "GIT_DIR and GIT_WORK_TREE": lambda s: {"GIT_DIR": f"{s.other}/.git", "GIT_WORK_TREE": str(s.other)},
    "GIT_INDEX_FILE": lambda s: {"GIT_INDEX_FILE": f"{s.other}/.git/index"},
    "GIT_OBJECT_DIRECTORY": lambda s: {"GIT_OBJECT_DIRECTORY": f"{s.other}/.git/objects"},
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": lambda s: {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": f"{s.other}/.git/objects"
    },
    "GIT_COMMON_DIR": lambda s: {"GIT_COMMON_DIR": f"{s.other}/.git"},
    "GIT_IMPLICIT_WORK_TREE": lambda s: {"GIT_IMPLICIT_WORK_TREE": "0"},
    "GIT_GRAFT_FILE": lambda s: {"GIT_GRAFT_FILE": f"{s.other}/.git/info/grafts"},
    "GIT_NO_REPLACE_OBJECTS": lambda s: {"GIT_NO_REPLACE_OBJECTS": "1"},
    "GIT_REPLACE_REF_BASE": lambda s: {"GIT_REPLACE_REF_BASE": "refs/elsewhere/"},
    "GIT_PREFIX": lambda s: {"GIT_PREFIX": "src/"},
    "GIT_SHALLOW_FILE": lambda s: {"GIT_SHALLOW_FILE": f"{s.other}/.git/shallow"},
    # configuration given through the environment
    "GIT_CONFIG": lambda s: {"GIT_CONFIG": str(s.config)},
    "GIT_CONFIG_PARAMETERS": lambda s: {"GIT_CONFIG_PARAMETERS": f"'core.hooksPath={s.hooks}'"},
    "GIT_CONFIG_COUNT": lambda s: {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(s.hooks),
        "GIT_CONFIG_KEY_1": "diff.external",
        "GIT_CONFIG_VALUE_1": str(s.program),
    },
    "GIT_CONFIG_GLOBAL": lambda s: {"GIT_CONFIG_GLOBAL": str(s.config)},
    "GIT_CONFIG_SYSTEM": lambda s: {"GIT_CONFIG_SYSTEM": str(s.config)},
    "GIT_CONFIG_NOSYSTEM": lambda s: {"GIT_CONFIG_NOSYSTEM": "true"},
    # where git looks for a repository, and what it takes a ref or an attribute from
    "GIT_NAMESPACE": lambda s: {"GIT_NAMESPACE": "elsewhere"},
    "GIT_CEILING_DIRECTORIES": lambda s: {"GIT_CEILING_DIRECTORIES": str(s.mine.parent)},
    "GIT_DISCOVERY_ACROSS_FILESYSTEM": lambda s: {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "true"},
    "GIT_ATTR_SOURCE": lambda s: {"GIT_ATTR_SOURCE": "refs/heads/theirs"},
    "GIT_DEFAULT_HASH": lambda s: {"GIT_DEFAULT_HASH": "sha256"},
    "GIT_LITERAL_PATHSPECS": lambda s: {"GIT_LITERAL_PATHSPECS": "0"},
    "GIT_GLOB_PATHSPECS": lambda s: {"GIT_GLOB_PATHSPECS": "1"},
    "GIT_ICASE_PATHSPECS": lambda s: {"GIT_ICASE_PATHSPECS": "1"},
    # programs and files git runs or writes by name
    "GIT_EXEC_PATH": lambda s: {"GIT_EXEC_PATH": str(s.exec_path)},
    "GIT_TEMPLATE_DIR": lambda s: {"GIT_TEMPLATE_DIR": str(s.templates)},
    "GIT_EXTERNAL_DIFF": lambda s: {"GIT_EXTERNAL_DIFF": str(s.program)},
    "GIT_DIFF_OPTS": lambda s: {"GIT_DIFF_OPTS": "--unified=0"},
    "GIT_PAGER": lambda s: {"GIT_PAGER": str(s.program)},
    "GIT_EDITOR": lambda s: {"GIT_EDITOR": str(s.program)},
    "GIT_SEQUENCE_EDITOR": lambda s: {"GIT_SEQUENCE_EDITOR": str(s.program)},
    # how you reach your remotes: handed to the fetch of a new clone's submodules alone
    "GIT_SSH": lambda s: {"GIT_SSH": str(s.program)},
    "GIT_SSH_COMMAND": lambda s: {"GIT_SSH_COMMAND": str(s.program)},
    "GIT_SSH_VARIANT": lambda s: {"GIT_SSH_VARIANT": "plink"},
    "GIT_ASKPASS": lambda s: {"GIT_ASKPASS": str(s.program)},
    "GIT_PROXY_COMMAND": lambda s: {"GIT_PROXY_COMMAND": str(s.program)},
    "GIT_SSL_CAINFO": lambda s: {"GIT_SSL_CAINFO": str(s.config)},
    "GIT_SSL_CAPATH": lambda s: {"GIT_SSL_CAPATH": str(s.hooks)},
    "GIT_SSL_CERT": lambda s: {"GIT_SSL_CERT": str(s.config)},
    "GIT_SSL_KEY": lambda s: {"GIT_SSL_KEY": str(s.config)},
    # what widens or shows what a remote may do: handed to none
    "GIT_ALLOW_PROTOCOL": lambda s: {"GIT_ALLOW_PROTOCOL": "ext:file"},
    "GIT_SSL_NO_VERIFY": lambda s: {"GIT_SSL_NO_VERIFY": "true"},
    "GIT_CURL_VERBOSE": lambda s: {"GIT_CURL_VERBOSE": "1"},
    "GIT_TRACE": lambda s: {"GIT_TRACE": f"{s.left}/trace"},
    "GIT_TRACE2": lambda s: {"GIT_TRACE2": f"{s.left}/trace2"},
    "GIT_TRACE2_EVENT": lambda s: {"GIT_TRACE2_EVENT": f"{s.left}/trace2-event"},
    "GIT_TRACE_SETUP": lambda s: {"GIT_TRACE_SETUP": f"{s.left}/trace-setup"},
    "GIT_TRACE_PACKET": lambda s: {"GIT_TRACE_PACKET": f"{s.left}/trace-packet"},
    # who a commit is by, and when: the host's own commit is vivibox's
    "GIT_AUTHOR_NAME": lambda s: {"GIT_AUTHOR_NAME": "Somebody Else"},
    "GIT_AUTHOR_EMAIL": lambda s: {"GIT_AUTHOR_EMAIL": "else@example.com"},
    "GIT_AUTHOR_DATE": lambda s: {"GIT_AUTHOR_DATE": "2001-02-03T04:05:06"},
    "GIT_COMMITTER_NAME": lambda s: {"GIT_COMMITTER_NAME": "Somebody Else"},
    "GIT_COMMITTER_EMAIL": lambda s: {"GIT_COMMITTER_EMAIL": "else@example.com"},
    "GIT_COMMITTER_DATE": lambda s: {"GIT_COMMITTER_DATE": "2001-02-03T04:05:06"},
    # what vivibox sets itself, at another value
    "GIT_OPTIONAL_LOCKS": lambda s: {"GIT_OPTIONAL_LOCKS": "true"},
    "GIT_NO_LAZY_FETCH": lambda s: {"GIT_NO_LAZY_FETCH": "0"},
    # a variable git has yet to gain: taken out by its name's beginning, not from a list
    "GIT_NOT_KNOWN_YET": lambda s: {"GIT_NOT_KNOWN_YET": str(s.other)},
}
every_variable = pytest.mark.parametrize("case", CASES)


def snapshot(folder: Path) -> dict[str, bytes | str]:
    """Every file under folder with its bytes, a link with what it names: the index, the
    objects, the refs, the configuration and the working tree alike."""
    found = {}
    for path in sorted(folder.rglob("*")):
        name = path.relative_to(folder).as_posix()
        if path.is_symlink():
            found[name] = f"-> {os.readlink(path)}"
        elif path.is_file():
            found[name] = path.read_bytes()
        else:
            found[name] = "folder"
    return found


@contextmanager
def carried(site: Site, monkeypatch, case: str):
    """Your shell with the case's variables exported, for the calls inside; then what must hold
    whatever they were. What became of your other repository is said first, also when a call
    inside failed: a git that stopped half-way may have written there before it did."""
    exported = CASES[case](site)
    before = snapshot(site.other)
    with monkeypatch.context() as shell:
        for name, value in exported.items():
            shell.setenv(name, value)
        shell.setenv("PATH", f"{site.spy}{os.pathsep}{os.environ['PATH']}")
        try:
            yield
        finally:
            after = snapshot(site.other)
            names = before.keys() | after.keys()
            changed = sorted(name for name in names if before.get(name) != after.get(name))
            assert not changed, f"your other repository changed: {changed}"
            assert not sorted(p.name for p in site.left.iterdir()), "a hook, a program or a trace of yours"
    ran = site.ran()
    assert ran, "no git of vivibox's ran: the calls were not what is tested"
    reached = sorted(
        f"{name}={value} (git {command})"
        for command, variables in ran
        for name, value in exported.items()
        # how you reach your remotes goes to the git that reaches them, and to no other
        if f"{name}={value}" in variables and not (name in HOW_YOU_REACH_THEM and command == SUBMODULES)
    )
    assert not reached, f"git on the host was handed your shell's {reached}"


@dataclass
class Made:
    """A task of the site's: its clone, its metadata, the commit it started from and the
    writer's commit on its branch."""

    root: Path
    clone: Path
    meta: Path
    base: str
    work: str


def a_task(site: Site, root: Path, bring_back: bool = False) -> Made:
    meta = root / ".task"
    meta.mkdir(parents=True)
    base = repo.prepare(site.mine, root / "repo", "demo-1", meta)
    work = written(root / "repo", "x = 2\n")
    if bring_back:
        repo.fetch_to(site.mine, root / "repo", meta, "demo-1")
    return Made(root, root / "repo", meta, base, work)


def written(clone: Path, text: str) -> str:
    """The writer's commit in the clone, as its container makes it."""
    (clone / "src" / "calc.py").write_text(text)
    git(clone, "-c", "user.name=Writer", "-c", "user.email=writer@example.com", "commit", "-qam", "Work")
    return git(clone, "rev-parse", "HEAD")


@every_variable
def test_git_in_your_repository_answers_for_it(site, monkeypatch, case):
    """What vivibox asks of your checkout before a task exists: who you are, your branches."""
    with carried(site, monkeypatch, case):
        who = repo.identity(site.mine)
        found = repo.branches(site.mine)
        start = repo.start_branch(site.mine)
        changed = repo.local_changes(site.mine)
    assert who == repo.Identity("Test User", "test@example.com"), "your identity in this repository"
    assert found == [("Current (main)", "HEAD")] and start == "main"
    assert changed == ["draft.txt"]


@every_variable
def test_a_tasks_clone_is_made_from_your_repository(site, tmp_path, monkeypatch, case):
    meta = tmp_path / "task" / ".task"
    meta.mkdir(parents=True)
    clone = tmp_path / "task" / "repo"
    with carried(site, monkeypatch, case):
        base = repo.prepare(site.mine, clone, "demo-1", meta)
    assert base == git(site.mine, "rev-parse", "HEAD")
    assert git(clone, "rev-parse", "HEAD") == base and git(clone, "branch", "--show-current") == BRANCH
    assert git(clone, "cat-file", "-t", base) == "commit", "the clone has its own objects"
    assert git(clone, "status", "--porcelain") == "" and (clone / "src" / "calc.py").read_text() == "x = 1\n"
    assert list((clone / ".git" / "hooks").iterdir()) == []
    assert "name = Test User" in (meta / "gitconfig").read_text()


@every_variable
def test_git_in_a_tasks_clone_reads_the_clone_and_brings_its_work_to_your_repository(
    site, tmp_path, monkeypatch, case
):
    task = a_task(site, tmp_path / "task")
    with carried(site, monkeypatch, case):
        head = repo.in_clone(task.clone, "rev-parse", "HEAD").stdout.strip()
        status = repo.in_clone(task.clone, "status", "--porcelain").stdout
        committed = repo._committed(task.clone, "README.md")
        fetched = repo.fetch_to(site.mine, task.clone, task.meta, "demo-1")
        copy = repo.update_review_worktree(site.mine, task.root, fetched, task.base)
        yours = repo.local_changes(copy)
        branch = repo.create_branch(site.mine, "demo-1", fetched)
    assert head == task.work and status == "" and committed
    assert fetched == task.work and git(site.mine, "rev-parse", repo.review_ref("demo-1")) == task.work
    assert git(site.mine, "rev-parse", branch) == task.work
    assert git(copy, "rev-parse", "HEAD") == task.base and yours == []
    assert git(copy, "diff", "--cached", "--name-only") == "src/calc.py", "the work, staged in the copy"
    assert git(site.mine, "status", "--porcelain") == "?? draft.txt", "your checkout as you left it"


@every_variable
def test_a_readers_view_and_working_directory_are_the_hosts_own(site, tmp_path, monkeypatch, case):
    """A view made before is brought up to date (a planner's refresh), and a new one and a
    working directory are made: each a repository of its own, read here as the reader's
    container reads it, with no variable of yours."""
    task = a_task(site, tmp_path / "task")
    refreshed, view, work = tmp_path / "refreshed", tmp_path / "view", tmp_path / "work"
    for folder in (refreshed, view, work):
        folder.mkdir()
    reading.view(task.clone, BRANCH, refreshed, task.work, "the planner's")
    newer = written(task.clone, "x = 3\n")
    with carried(site, monkeypatch, case):
        reading.view(task.clone, BRANCH, refreshed, whose="the planner's")
        reading.workdir(work, "the reviewer's")
        reading.view(task.clone, BRANCH, view, newer, "the reviewer's")
    for folder in (refreshed, view):
        assert reading.made(folder) and (folder / ".git" / "index").is_file(), "its own index"
        assert git(folder, "rev-parse", "HEAD") == newer and git(folder, "status", "--porcelain") == ""
        assert git(folder, "cat-file", "-t", newer) == "commit", "its own objects"
        assert (folder / "src" / "calc.py").read_text() == "x = 3\n"
        assert not (folder / ".git" / "hooks").exists()
    assert reading.made(work) and git(work, "status", "--porcelain") == ""
    assert git(work, "log", "--format=%an <%ae> %cn %s") == (
        "vivibox <vivibox@localhost> vivibox The reviewer's working directory"
    )
    assert git(task.clone, "rev-parse", "HEAD") == newer and git(task.clone, "status", "--porcelain") == ""


@every_variable
def test_the_base_of_a_task_is_read_in_your_repository(site, monkeypatch, case):
    base = git(site.mine, "rev-parse", "HEAD")
    with carried(site, monkeypatch, case):
        found = baseline.read(site.mine, base)
    assert found.problem == ""
    assert found.links == {"link": "README.md"}
    assert found.sizes == {"README.md": 5, "src/calc.py": 6}


@every_variable
def test_the_files_you_mention_are_your_projects(site, monkeypatch, case):
    with carried(site, monkeypatch, case):
        paths = context.project_paths(site.mine)
        found = context.resolve("See @README.md and @draft.txt", site.mine, site.mine)
    assert paths == ["README.md", "draft.txt", "link", "src/", "src/calc.py"]
    assert found.in_project == {"README.md": "README.md"}, "committed, so the agent has it"
    assert found.copies == {"draft.txt": site.mine / "draft.txt"}, "not committed, so copied"


@every_variable
def test_the_version_and_the_repository_of_a_folder_are_that_folders(site, monkeypatch, case):
    count = git(site.mine, "rev-list", "--count", "HEAD")
    sha = git(site.mine, "rev-parse", "--short=7", "HEAD")
    with carried(site, monkeypatch, case):
        found = version.from_checkout(site.mine)
        root = about.repository_root(site.mine / "src")
    assert found == f"0.0.1.dev{count}+{sha}"
    assert root == site.mine.resolve()


@every_variable
def test_the_diff_of_the_work_is_your_repositorys_own(site, tmp_path, monkeypatch, case):
    """f: the diff vivibox writes down for your pager, from the review ref in your repository."""
    made = a_task(site, tmp_path / "task", bring_back=True)
    task = SimpleNamespace(
        id="demo-1", meta=made.meta, read_state=lambda: SimpleNamespace(base_commit=made.base)
    )
    with carried(site, monkeypatch, case):
        command = keys_work.diff_pager(task, SimpleNamespace(repo=site.mine))
    text = Path(command[-1]).read_text()
    assert "-x = 1" in text and "+x = 2" in text, "git's own diff of the work"
    assert "@@ -1 +1 @@" in text and "src/calc.py" in text


def test_git_on_the_host_gets_what_vivibox_sets_for_it_and_nothing_else_of_gits(site, tmp_path, monkeypatch):
    """A plain git in your repository gets no GIT_* variable; git in the task's clone gets no
    optional locks; git for a reader's folder gets that folder named as its repository and its
    work tree, none of your configuration, and no lazy fetch."""
    task = a_task(site, tmp_path / "task")
    view, work = tmp_path / "view", tmp_path / "work"
    view.mkdir()
    work.mkdir()
    monkeypatch.setenv("PATH", f"{site.spy}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_EDITOR", "vi")
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)  # the one that passes, were it set here
    repo.branches(site.mine)
    assert site.logged() and all(variables == [] for variables in site.logged()), site.logged()
    site.log.unlink()
    repo.in_clone(task.clone, "status", "--porcelain")
    assert site.logged() == [["GIT_OPTIONAL_LOCKS=0"]]
    site.log.unlink()
    for folder, least, make in (
        # init, the settings, the fetch, the check of the commit, the reset and the clean
        (view, 6, lambda: reading.view(task.clone, BRANCH, view, task.work, "the reviewer's")),
        (work, 2, lambda: reading.workdir(work, "the reviewer's")),  # init and the commit
    ):
        make()
        own = {f"{k}={v}" for k, v in reading.HOST_GIT.items()}
        own |= {f"GIT_DIR={folder}/.git", f"GIT_WORK_TREE={folder}"}
        ran = site.logged()
        assert len(ran) >= least, ran
        for variables in ran:  # the fetch reads the writer's clone, so it is bounded as in_clone is
            assert set(variables) - {"GIT_OPTIONAL_LOCKS=0"} == own, variables
        site.log.unlink()
    assert reading.HOST_GIT == {
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1",
    }  # fmt: skip


@pytest.mark.parametrize("name", HOW_YOU_REACH_THEM)
def test_only_the_fetch_of_a_new_clones_submodules_is_handed_how_you_reach_your_remotes(
    site, tmp_path, monkeypatch, name
):
    """The submodules of a task's clone are fetched from your remotes, by the URLs your own
    .gitmodules names: that git gets your ssh command, your askpass, your certificates. No other
    git of the host's does, in your repository, in the clone or for a reader."""
    task, view = a_task(site, tmp_path / "task"), tmp_path / "view"
    view.mkdir()
    meta = tmp_path / "new" / ".task"
    meta.mkdir(parents=True)
    monkeypatch.setenv(name, str(site.program))
    monkeypatch.setenv("PATH", f"{site.spy}{os.pathsep}{os.environ['PATH']}")
    repo.prepare(site.mine, tmp_path / "new" / "repo", "demo-2", meta)
    repo.fetch_to(site.mine, task.clone, task.meta, "demo-1")
    reading.view(task.clone, BRANCH, view, task.work, "the reviewer's")
    baseline.read(site.mine, task.base)
    handed = [command for command, variables in site.ran() if f"{name}={site.program}" in variables]
    assert handed == [SUBMODULES], "the git that reaches your remotes, and it alone"
    assert not list(site.left.iterdir()), "no program of yours ran: nothing here is behind ssh"


def test_a_submodule_you_reach_through_your_ssh_command_is_fetched(site, tmp_path, monkeypatch):
    """A project whose submodule is behind ssh, reached by the command your shell names
    (GIT_SSH_COMMAND, as a jump host or a key of its own needs): the task's clone has it."""
    lib = make_repo(tmp_path / "lib")
    git(site.mine, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "lib")
    git(site.mine, "config", "--file", ".gitmodules", "submodule.lib.url", f"ssh://git.example.invalid{lib}")
    git(site.mine, "commit", "-qam", "A library, behind ssh")
    asked = tmp_path / "asked-of-ssh"
    # ssh as git runs it: the host, then the command for the far side, which is run here.
    ssh = script(
        tmp_path / "yours" / "ssh", f'echo "$*" >> "{asked}"\nfor far; do :; done\nexec sh -c "$far"'
    )
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    monkeypatch.setenv("PATH", f"{site.spy}{os.pathsep}{os.environ['PATH']}")
    meta = tmp_path / "task" / ".task"
    meta.mkdir(parents=True)
    clone = tmp_path / "task" / "repo"
    repo.prepare(site.mine, clone, "demo-1", meta)
    assert (clone / "lib" / "README.md").read_text() == "demo\n", "the submodule, in the task's clone"
    assert f"git.example.invalid git-upload-pack '{lib}'" in asked.read_text()
    handed = [command for command, variables in site.ran() if f"GIT_SSH_COMMAND={ssh}" in variables]
    assert handed == [SUBMODULES]


def test_how_you_reach_your_remotes_is_a_list_of_names_and_passes_only_when_asked(monkeypatch):
    """Nine names, each with its reason in repo.py; what widens or shows what a remote may do
    (GIT_ALLOW_PROTOCOL, GIT_SSL_NO_VERIFY, a trace) and what aims git elsewhere pass to none."""
    assert repo.TO_YOUR_REMOTES == HOW_YOU_REACH_THEM
    never = ("GIT_ALLOW_PROTOCOL", "GIT_SSL_NO_VERIFY", "GIT_TRACE", "GIT_CURL_VERBOSE", "GIT_DIR",
             "GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_AUTHOR_NAME", "GIT_EXEC_PATH")  # fmt: skip
    for name in (*HOW_YOU_REACH_THEM, *never):
        monkeypatch.setenv(name, "yours")
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    gits = lambda found: {name for name in found if name.startswith("GIT_")}  # noqa: E731
    assert gits(repo.git_environment()) == set()
    assert gits(repo.git_environment(to_remotes=True)) == set(HOW_YOU_REACH_THEM)


def test_what_vivibox_sets_for_a_git_wins_over_what_passes_from_your_shell(monkeypatch):
    for name in ("GIT_TERMINAL_PROMPT", "GIT_SSH_COMMAND", "LESS"):
        monkeypatch.setenv(name, "yours")
    own = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "LESS": "R"}
    found = repo.git_environment(own, to_remotes=True)
    assert {name: found[name] for name in own} == own


@pytest.fixture
def unnamed(tmp_path, monkeypatch) -> Path:
    """A repository with no identity of its own, on a machine whose ~/.gitconfig has none."""
    source = make_repo(tmp_path / "source")
    for key in ("user.name", "user.email"):
        git(source, "config", "--unset", key)
    global_git_config(monkeypatch, tmp_path / "home", "")
    return source


def as_told(said: str, where: Path) -> None:
    """The commands an error names, run as you would run them: in your shell, as it is."""
    for line in said.splitlines():
        if line.startswith("git config "):
            subprocess.run(["sh", "-c", line], cwd=where, env=dict(os.environ), check=True)


def test_the_identity_hint_sets_it_where_vivibox_reads_it_when_your_shell_names_its_own_file(
    unnamed, tmp_path, monkeypatch
):
    """Your shell keeps git's global settings in a file GIT_CONFIG_GLOBAL names, your identity
    in it. vivibox's git is not given that variable, so it finds no identity; `git config
    --global` would write that same file again. The error says so, and its commands lead on."""
    elsewhere = tmp_path / "elsewhere" / "gitconfig"
    elsewhere.parent.mkdir()
    elsewhere.write_text(IDENTITY)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(elsewhere))
    with pytest.raises(repo.RepoError) as refused:
        repo.identity(unnamed)
    said = str(refused.value)
    what, first, second, other = said.splitlines()
    assert what.startswith("Git identity is missing where vivibox's git reads it: ")
    assert "your shell sets GIT_CONFIG_GLOBAL, and vivibox runs git without it" in what
    assert first == 'git config --file ~/.gitconfig user.name "Your Name"' and "--global" not in said
    assert second == 'git config --file ~/.gitconfig user.email "you@example.com"'
    assert other == f"Or run these without --file ~/.gitconfig in {unnamed}."
    as_told(said, tmp_path)
    assert repo.identity(unnamed) == repo.Identity("Your Name", "you@example.com")
    assert elsewhere.read_text() == IDENTITY, "your own file is as it was"


@pytest.mark.parametrize("named", ["nothing", "~/.gitconfig"])
def test_the_identity_hint_is_git_config_global_where_that_writes_what_vivibox_reads(
    unnamed, tmp_path, monkeypatch, named
):
    if named == "nothing":
        monkeypatch.delenv("GIT_CONFIG_GLOBAL")
    with pytest.raises(repo.RepoError) as refused:
        repo.identity(unnamed)
    said = str(refused.value)
    assert said.splitlines() == [
        "Git identity is missing. Set your name and email:",
        'git config --global user.name "Your Name"',
        'git config --global user.email "you@example.com"',
        f"Or run these without --global in {unnamed}.",
    ]
    as_told(said, tmp_path)
    assert repo.identity(unnamed) == repo.Identity("Your Name", "you@example.com")


def test_the_suites_home_hides_the_settings_your_shell_points_git_and_vivibox_at(tmp_path, monkeypatch):
    """A shell that exports XDG_CONFIG_HOME keeps git's global settings in git/config there, and
    vivibox's git reads them where git does. Every test starts in a home of the suite's own
    (conftest.own_home), the folders a shell names apart from HOME included: an identity and a
    hook of yours reach no git a test has vivibox run, and vivibox keeps its configuration, its
    keys, its history and what it remembers of the models in the suite's home, not in yours."""
    from vivibox import config, keys, review, roles

    named = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
    assert all(Path(os.environ[name]).is_relative_to(HOME) for name in named), "as every test starts"
    yours, ran = tmp_path / "yours", tmp_path / "ran"
    for hook in ("reference-transaction", "post-checkout"):
        script(yours / "hooks" / hook, f'echo "$0" >> "{ran}"')
    (yours / "git").mkdir()
    (yours / "git" / "config").write_text(
        f"[user]\n\tname = Yours\n\temail = yours@example.com\n[core]\n\thooksPath = {yours}/hooks\n"
    )
    source = make_repo(tmp_path / "source")
    for name in named:  # your shell, as the suite is started from it
        monkeypatch.setenv(name, str(yours))
    monkeypatch.delenv("VIVIBOX_CONFIG_DIR", raising=False)
    suites_own(monkeypatch)  # what own_home does before every test
    meta = tmp_path / "task" / ".task"
    meta.mkdir(parents=True)
    repo.prepare(source, tmp_path / "task" / "repo", "demo-1", meta)
    names = repo.git("config", "--get-all", "user.name", cwd=tmp_path / "task" / "repo").stdout
    assert names.splitlines() == ["Test User"], "the suite's identity, and no other"
    assert not ran.exists(), "a hook of your own git settings ran in a test"
    for kept in (config.config_dir(), keys.store(), review.history_path(), roles.models_cache()):
        assert kept.is_relative_to(HOME), f"{kept} is not the suite's"


def test_the_environment_of_git_keeps_everything_that_is_not_gits(monkeypatch):
    """Your proxy, your SSH agent, your pager's settings and your PATH stay; what vivibox sets
    for a call is added, over a variable of the same name."""
    for name, value in (("HTTPS_PROXY", "http://proxy:3128"), ("SSH_AUTH_SOCK", "/run/agent"),
                        ("SSH_ASKPASS", "/usr/bin/ask"), ("LESS", "R"), ("GITHUB_TOKEN", "t"),
                        ("GIT_DIR", "/elsewhere/.git"), ("GIT_OPTIONAL_LOCKS", "1")):  # fmt: skip
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    found = repo.git_environment({"GIT_OPTIONAL_LOCKS": "0"})
    assert found["PATH"] == os.environ["PATH"] and found["HOME"] == os.environ["HOME"]
    for name in ("HTTPS_PROXY", "SSH_AUTH_SOCK", "SSH_ASKPASS", "LESS", "GITHUB_TOKEN"):
        assert found[name] == os.environ[name], name
    assert {name: value for name, value in found.items() if name.startswith("GIT_")} == {
        "GIT_OPTIONAL_LOCKS": "0"
    }
    assert "GIT_DIR" in os.environ, "your own environment is left as it is"


def test_a_shell_that_tells_git_not_to_ask_on_the_terminal_is_heard(site, monkeypatch):
    """GIT_TERMINAL_PROMPT=0 only ever stops git asking for a password on the terminal, which
    vivibox's view is drawn on; it names no path and no program, so it passes."""
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PATH", f"{site.spy}{os.pathsep}{os.environ['PATH']}")
    assert repo.PASSED_ON == ("GIT_TERMINAL_PROMPT",), "each name that passes has its reason there"
    repo.branches(site.mine)
    repo.in_clone(site.mine, "status", "--porcelain")
    assert all("GIT_TERMINAL_PROMPT=0" in variables for variables in site.logged())
