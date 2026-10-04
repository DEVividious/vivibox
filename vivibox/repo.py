"""The task's own clone of your repository, and bringing its work back.

The agent commits in the clone, so it writes to .git. Git on the host runs commands from
.git/config (core.fsmonitor, filters, hooksPath) and from .git/hooks. Those files are written here,
from an allowlist, and mounted read-only into the agent container; .git itself is a separate mount so
the agent cannot swap it for a directory of its own. What else git follows from the clone (commondir,
alternates, a submodule's .git) is recorded with them (protection), and git on the host runs in the
clone only through in_clone, which compares it with the record first.
"""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import display, protection
from .podspec import Mount

# Settings a fresh clone needs; everything else in the source repo's local config is dropped.
CONFIG_ALLOWLIST = (
    "core.repositoryformatversion",
    "core.filemode",
    "core.bare",
    "core.logallrefupdates",
    "core.ignorecase",
    "core.precomposeunicode",
    # Submodule git dirs point back at their working tree.
    "core.worktree",
    "extensions.objectformat",
    "remote.origin.url",
    "remote.origin.fetch",
)
SUBMODULE_KEYS = (".url", ".active")
PROTECTION_RECORD = protection.RECORD


class RepoError(Exception):
    pass


class Blocked(RepoError):
    """The task added to its clone what git on the host goes into (protection.Added): no git
    runs there until it is gone, which is the task's to see to; found: those additions."""

    def __init__(self, said: str, found: list[protection.Added]):
        super().__init__(said)
        self.found = found

    @property
    def why(self) -> BlockedBy:
        """What it says, as a verification's reason, with the additions for the writer's feedback."""
        why = BlockedBy(self)
        why.found = self.found
        return why


class BlockedBy(str):
    """Why a verification could not run, as you read it, and the additions that block the clone
    (found), of which the writer is told how each is taken out (feedback.blocked)."""

    found: list[protection.Added] = []  # noqa: RUF012 (set on each; never appended to)


class GitTimeout(RepoError):
    """git did not answer in time in the task's clone: what asked stops there, and never goes on as
    if git had answered."""


# Seconds git on the host may take in the task's clone, which the agent writes. What vivibox asks
# there answers in well under a second on an ordinary repository and in seconds on a large one;
# a named pipe where git reads its settings or refs makes git wait forever.
CLONE_TIMEOUT = 60.0
# Seconds git may take to copy the clone (the gate's clone of it in its container and the
# submodules; on the host, a reader's view of its commits, reading.view): it copies every object
# without hard links and checks the tree out, and fetches submodules over the network, which on a
# repository of gigabytes takes a minute or more.
COPY_TIMEOUT = 600.0


# git's own variables that pass from your shell to git on the host; every other GIT_* is taken
# out (git_environment), whatever git names next.
PASSED_ON = (
    # Only ever stops git asking for a password on the terminal, which the view is drawn on; it
    # names no path and no program.
    "GIT_TERMINAL_PROMPT",
)
# And, to the one git vivibox sends to your remotes, how you reach them: the fetch of a new
# clone's submodules (prepare), from the URLs named by the .gitmodules of the commit the task
# starts from, when that commit is your own (yours); never to a later git on what the agent
# wrote. None of these says which repository, index, object store or configuration
# git works on, and each has a twin in your configuration files that this git reads anyway
# (core.sshCommand, core.askPass, http.sslCAInfo). Never GIT_ALLOW_PROTOCOL, which widens what a
# URL there may do, nor GIT_SSL_NO_VERIFY, nor a trace.
TO_YOUR_REMOTES = (
    "GIT_SSH",  # the ssh program you reach them with,
    "GIT_SSH_COMMAND",  # or its whole command line,
    "GIT_SSH_VARIANT",  # and which ssh that is
    "GIT_ASKPASS",  # the program that hands git your password (an IDE's terminal exports one)
    "GIT_SSL_CAINFO",  # the certificate authorities you trust (a proxy's, say),
    "GIT_SSL_CAPATH",  # or the folder of them
    "GIT_SSL_CERT",  # your client certificate
    "GIT_SSL_KEY",  # and its key
    "GIT_PROXY_COMMAND",  # the program that connects through your proxy
)


def git_environment(own: dict[str, str] | None = None, *, to_remotes: bool = False) -> dict[str, str]:
    """The environment of every git vivibox runs on the host: this process's own without git's
    variables (every GIT_* but PASSED_ON), then what vivibox sets for the call (own), which
    wins. git takes them before its working directory and -C, so one your shell carries
    (inside a git hook, under a tool that exports them) would aim vivibox's git at another
    repository, index or object store of yours (GIT_DIR, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY),
    hand it configuration that runs a program (GIT_CONFIG_COUNT, GIT_CONFIG_GLOBAL), or name
    one outright (GIT_EXTERNAL_DIFF, GIT_EXEC_PATH). Your configuration files are read where
    git keeps them. to_remotes: for the one git vivibox sends to your remotes, TO_YOUR_REMOTES
    pass too; said by name, so that every place that says it can be counted."""
    passed = PASSED_ON + (TO_YOUR_REMOTES if to_remotes else ())
    kept = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in passed}
    return {**kept, **(own or {})}


def run_git(args: list[str] | tuple[str, ...], **how) -> subprocess.CompletedProcess:
    """subprocess.run of git on the host, in git_environment; how: what run takes, env being
    what vivibox sets for the call and to_remotes what git_environment takes. For a caller that
    reads git's answer in its own way (as bytes, within its own time); the rest call git or
    in_clone."""
    env = git_environment(how.pop("env", None), to_remotes=how.pop("to_remotes", False))
    return subprocess.run(["git", *args], env=env, **how)


def git(
    *args: str, cwd: Path | None = None, check: bool = True, timeout: float | None = None,
    clone: Path | None = None, env: dict[str, str] | None = None, to_remotes: bool = False,
) -> subprocess.CompletedProcess:  # fmt: skip
    """timeout: seconds git may take, or no limit; past it, GitTimeout says what git was waiting on
    in clone (cwd when not given). env: variables over git_environment, for a git that must
    not read your configuration (control.refresh). to_remotes: this git is sent to your remotes
    (git_environment); never one with a timeout, which reads the task's clone."""
    if to_remotes and timeout is not None:
        raise ValueError("git on the task's clone is not handed how you reach your remotes")
    if cwd is not None and not Path(cwd).is_dir():
        # A folder that was moved or deleted since it was configured; say so instead of a stack trace.
        raise RepoError(f"{cwd} does not exist")
    if timeout is None:
        p = run_git(args, cwd=cwd, capture_output=True, text=True, env=env, to_remotes=to_remotes)
    else:
        own = (env,) if env else ()  # a stand-in for _bounded may know no environment
        p = _bounded(["git", *args], cwd, timeout, Path(clone or cwd or "."), *own)
    if check and p.returncode != 0:
        # The arguments and what git says of them may carry a name the agent chose in the clone.
        raise RepoError(f"git {display.name(' '.join(args[:3]))}…: {display.said(p.stderr)}")
    return p


def in_clone(
    clone: Path, *args: str, check: bool = True, cwd: Path | None = None, timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:  # fmt: skip
    """git on the task's clone, or reading it from cwd, within CLONE_TIMEOUT (or timeout); only
    once guard found the clone as recorded."""
    guard(clone)
    return git(*args, cwd=cwd or clone, check=check, timeout=timeout or CLONE_TIMEOUT, clone=clone, env=env)


def guard(clone: Path) -> None:
    """check_protection, before git on the host reads the clone at clone: a task's clone is
    <task>/repo beside <task>/.task, which holds its record. Your checkout is no task's clone."""
    meta = clone.parent / ".task"
    if meta.is_dir():
        check_protection(clone, meta)


def _bounded(
    command: list[str], cwd: Path | None, timeout: float, clone: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """git in a session of its own, so that on a timeout it goes down with every process it started.
    Without optional locks: git status would write the clone's index in the form your settings ask
    for (a split one), which protection could not read."""
    p = subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=git_environment({"GIT_OPTIONAL_LOCKS": "0", **(env or {})}),
    )  # fmt: skip
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException as e:  # the timeout, or an interrupt: no git is left waiting
        with contextlib.suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        p.stdout.close()
        p.stderr.close()
        if isinstance(e, subprocess.TimeoutExpired):
            raise GitTimeout(_unanswered(command[1:], timeout, clone)) from None
        raise
    return subprocess.CompletedProcess(command, p.returncode, out, err)


# What git reads in a working tree, and every file of its own under .git.
GIT_READS = (".gitignore", ".gitattributes")
SPECIAL = {stat.S_IFIFO: "a named pipe (FIFO)", stat.S_IFSOCK: "a socket", stat.S_IFCHR: "a device",
           stat.S_IFBLK: "a device"}  # fmt: skip
# Entries looked at for the cause: a timeout's message never waits on a walk of a huge clone.
LOOKED_AT = 100_000


def special_files(clone: Path) -> list[tuple[str, str]]:
    """Files git reads in the clone that are no plain file, as (path from its root, what it is):
    git opens such a file and waits on it."""
    found, seen = [], 0
    for root, dirs, files in os.walk(clone):
        here = Path(root)
        inside_git = ".git" in here.relative_to(clone).parts
        for name in files:
            if not (inside_git or name in GIT_READS):
                continue
            try:  # the file a link leads to, as git opens it; stat never waits on a pipe
                kind = SPECIAL.get(stat.S_IFMT(os.stat(here / name).st_mode))
            except OSError:
                continue
            if kind:
                found.append(((here / name).relative_to(clone).as_posix(), kind))
        seen += len(dirs) + len(files)
        if seen > LOOKED_AT:
            break
    return found


def timed_out(args: list[str] | tuple[str, ...], seconds: float, clone: Path, slow: str = "") -> GitTimeout:
    """The error for git, run with args, that did not answer in seconds on the clone. slow: what
    takes that long with nothing wrong (a copy of a large repository), when something can."""
    return GitTimeout(_unanswered(list(args), seconds, clone, slow))


# A name from the clone, which the agent chose, as text to read (display.name).
shown = display.name


# How a timeout with no cause found ends: the step is to try again, which a caller that says its
# own way of trying again takes off (fetch_to, box.close_box).
RETRY = "; try again"


def _unanswered(args: list[str], seconds: float, clone: Path, slow: str = "") -> str:
    """What timed out, the cause where one was found (a special file, named), and what to do, in
    words; where none was found, the fact alone, what was looked for, and to try again: never a
    guess at a cause. Never a command to paste: the names are the agent's, and a shell would
    run what a name says ($(…), ;) or delete a file of yours by a path that is the clone's
    (.git/packed-refs)."""
    i = 0
    while i < len(args) - 1 and args[i].startswith("-"):
        i += 2 if args[i] in ("-c", "-C") else 1
    command = f"git {args[i] if args else ''}"
    found = special_files(clone)[:3]
    if not found and slow:
        return (
            f"{command} did not finish in {seconds:g} s in {shown(str(clone))}: {slow} can take this long, "
            f"and nothing where git reads its settings or refs is a named pipe{RETRY}"
        )
    if not found:
        return (
            f"{command} did not finish in {seconds:g} s in {shown(str(clone))}; vivibox looked there for a "
            "special file where git reads its settings or refs (a named pipe, a socket, a device) and "
            f"found none{RETRY}"
        )
    what = f"{command} did not answer in {seconds:g} s"
    # The file from the clone's root first, said to be the clone's: a notification cut short keeps it.
    named = ", ".join(f"{shown(path)} is {kind}" for path, kind in found)
    committed = [path for path, _ in found if _committed(clone, path)]
    gone = [path for path, _ in found if path not in committed]
    todo = []
    if gone:  # git never had it: nothing to put back
        todo.append("delete " + (("it" if len(gone) == 1 else "them") if not committed else _listed(gone)))
    if committed:  # deleted, it would be an uncommitted change, and a round of the writer's
        which = ("it" if len(committed) == 1 else "them") if not gone else _listed(committed)
        todo.append(f"restore {which} from the commits")
    return (
        f"{what}: in the task's clone, {named}, which git waits on forever; {'; '.join(todo)}; "
        f"the clone is {shown(str(clone))}"
    )


def _listed(paths: list[str]) -> str:
    return ", ".join(shown(p) for p in paths)


def _committed(clone: Path, path: str) -> bool:
    """Whether the clone's index has path, as committed files are; False when git cannot say in a
    few seconds (its index may be the special file), and what is in .git never is. The path is a
    name, never a pattern: a folder named * matches nothing but itself."""
    if path.split("/")[0] == ".git":
        return False
    try:
        guard(clone)
    except RepoError:
        return False
    try:
        listed = run_git(
            ["--literal-pathspecs", "ls-files", "-z", "--", path], cwd=clone, capture_output=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return listed.returncode == 0 and bool(listed.stdout)


def branch_name(task_id: str) -> str:
    return f"vivibox/{task_id}"


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def identity(source: Path) -> Identity:
    """Commits in the task are authored as you: no agent signature or co-author lines."""
    name = git("config", "user.name", cwd=source, check=False).stdout.strip()
    email = git("config", "user.email", cwd=source, check=False).stdout.strip()
    if not name or not email:
        raise RepoError(_no_identity(source))
    return Identity(name, email)


def _no_identity(source: Path) -> str:
    """What to say of a missing identity, with commands that set it where git on the host reads
    it. `git config --global` writes the file GIT_CONFIG_GLOBAL names when your shell sets it,
    and git on the host is not given that variable (git_environment): unless it names one of
    the files git reads anyway, the commands name ~/.gitconfig themselves."""
    home = Path.home()
    usual = (home / ".gitconfig", Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "git/config")
    named = os.environ.get("GIT_CONFIG_GLOBAL")
    if named is None or Path(named).resolve() in [path.resolve() for path in usual]:
        what, where = "Git identity is missing. Set your name and email:", "--global"
    else:
        what = (
            "Git identity is missing where vivibox's git reads it: your shell sets GIT_CONFIG_GLOBAL, "
            "and vivibox runs git without it. Set your name and email in ~/.gitconfig:"
        )
        where = "--file ~/.gitconfig"
    return (
        f'{what}\ngit config {where} user.name "Your Name"\n'
        f'git config {where} user.email "you@example.com"\n'
        f"Or run these without {where} in {source}."
    )


def git_dirs(repo: Path) -> list[Path]:
    """The repository's git dir and the git dirs of its submodules, found without a link."""
    return [repo / d for d in protection.git_dirs(repo)[0]]


def _sanitize(git_dir: Path) -> None:
    config = git_dir / "config"
    listed = git("config", "--file", str(config), "--null", "--list").stdout.split("\0")
    keep = []
    for entry in filter(None, listed):
        key, _, value = entry.partition("\n")
        if key in CONFIG_ALLOWLIST or (key.startswith("submodule.") and key.endswith(SUBMODULE_KEYS)):
            keep.append((key, value))
    config.write_text("")
    for key, value in keep:
        git("config", "--file", str(config), "--add", key, value)
    hooks = git_dir / "hooks"
    if hooks.exists():
        for p in sorted(hooks.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
    hooks.mkdir(exist_ok=True)


def branches(source: Path) -> list[tuple[str, str]]:
    """Branches already known locally. Current first; no network or checkout changes."""
    current = git("symbolic-ref", "--quiet", "HEAD", cwd=source, check=False).stdout.strip()
    label = current.removeprefix("refs/heads/") or "detached HEAD"
    found = git(
        "for-each-ref", "--format=%(refname)%00%(symref)", "refs/heads", "refs/remotes", cwd=source
    ).stdout
    choices = []
    for line in found.splitlines():
        ref, _, symbolic = line.partition("\0")
        if not symbolic and ref != current:
            name = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
            choices.append((name, ref))
    return [(f"Current ({label})", "HEAD"), *sorted(choices, key=lambda c: c[0].casefold())]


# A commit straight on one of these is what a team's review usually exists to prevent.
PROTECTED_BRANCHES = ("main", "master")


def start_branch(source: Path, base_ref: str = "") -> str:
    """The local branch a task starts from: the checked-out one for HEAD, else the one picked;
    "" for a detached HEAD or a remote branch, which nothing can be committed on."""
    if base_ref in ("", "HEAD"):
        return git("branch", "--show-current", cwd=source, check=False).stdout.strip()
    name = base_ref.removeprefix("refs/heads/")
    return name if branch_exists(source, name) else ""


def branch_exists(source: Path, name: str) -> bool:
    return (
        git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", cwd=source, check=False).returncode == 0
    )


def yours(source: Path, commit: str) -> bool:
    """Whether commit is your own work in your repository at source: on the branch you are on, or
    reachable from a ref of yours, which is any but refs/vivibox/, where a task's work waits
    until you accept it. A task started from such work (vivibox new --base refs/vivibox/<id>, or
    its commit by id) starts from a .gitmodules the agent may have written."""
    if git("merge-base", "--is-ancestor", commit, "HEAD", cwd=source, check=False).returncode == 0:
        return True
    refs = git("for-each-ref", "--format=%(refname)", "--contains", commit, cwd=source, check=False).stdout
    return any(not ref.startswith("refs/vivibox/") for ref in refs.split())


def prepare(source: Path, repo: Path, task_id: str, meta: Path, base_ref: str = "") -> str:
    """Clones source into repo on a new task branch. Returns the base commit."""
    if (source / ".gitattributes").exists() and "filter=lfs" in (source / ".gitattributes").read_text():
        raise RepoError("Git LFS repositories are not supported yet")
    origin = git("remote", "get-url", "origin", cwd=source, check=False).stdout.strip()
    base = git("rev-parse", "--verify", "--end-of-options", f"{base_ref or 'HEAD'}^{{commit}}",
               cwd=source).stdout.strip()  # fmt: skip
    # --no-hardlinks: the agent must not be able to modify objects shared with your repository.
    git("clone", "--quiet", "--no-hardlinks", "--no-checkout", str(source), str(repo))
    git("switch", "--quiet", "-c", branch_name(task_id), base, cwd=repo)
    # The one git vivibox sends to your remotes: as you reach them (TO_YOUR_REMOTES), when the
    # commit whose .gitmodules names them is your own.
    git("submodule", "update", "--init", "--recursive", "--quiet", cwd=repo, to_remotes=yours(source, base))
    if origin:
        git("remote", "set-url", "origin", origin, cwd=repo)
    else:
        git("remote", "remove", "origin", cwd=repo)
    for d in git_dirs(repo):
        _sanitize(d)
        for name in protection.ALTERNATES:  # empty, for the pod to mount read-only over
            protection.make_empty(repo, f"{d.relative_to(repo).as_posix()}/{name}")
    # What tools in the pod write into the repository and nobody means to commit: Serena's project
    # file and caches. Here, not in the project's .gitignore, which is yours.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    with (repo / ".git" / "info" / "exclude").open("a") as f:
        f.write("\n# vivibox: written by tools in the task's pod\n.serena/\n")
    record_protection(repo, meta)
    who = identity(source)
    (meta / "gitconfig").write_text(
        f"[user]\n\tname = {who.name}\n\temail = {who.email}\n[init]\n\tdefaultBranch = main\n"
    )
    return base


def record_protection(repo: Path, meta: Path) -> None:
    """What git follows from the clone as it is now, for check_protection to compare with."""
    try:
        protection.write(meta, protection.record(repo))
    except OSError as e:
        raise RepoError(str(e)) from None


def check_protection(repo: Path, meta: Path) -> None:
    """Refuses to go on if what git follows from the clone changed since prepare: git on the host
    would run what the agent put there (a hook, a config through commondir) or read its objects."""
    if not (meta / PROTECTION_RECORD).is_file():
        raise RepoError(
            "the task's clone has no record of its git files, so vivibox runs no git in it: delete the task"
        )
    try:
        changed = protection.check(repo, meta)
    except (OSError, ValueError) as e:  # the clone gone, or a record vivibox cannot read
        raise RepoError(f"vivibox could not check the task's clone, so it runs no git in it: {e}") from None
    if changed:
        named = named_changes(changed)
        if all(isinstance(change, protection.Added) for change in changed):
            # What ordinary work adds: the task is blocked while it is there, not lost.
            what = " and ".join(sorted({change.what for change in changed}))
            # The state and the step first: a notification holds the first 150 characters.
            raise Blocked(
                f"the task added {what} to its clone and is blocked until it is out: {undoes(meta)} "
                f"removes it, or takes it out of what git tracks. Found: {named}. git on your machine "
                "would run what its settings say, so until then vivibox runs no git in the clone, and "
                "the work is neither verified nor brought back",
                changed,
            )
        raise RepoError(
            f"git files in the task's clone changed that would make git on your machine run a program "
            f"or read another repository: {named}. vivibox runs no git in the "
            "clone now, so its work can be neither verified nor brought back: see what the agent did "
            "in its log, and delete the task"
        )


def named_changes(changed: list[str]) -> str:
    """The first three, and how many more: a notification holds no more."""
    more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
    return f"{'; '.join(changed[:3])}{more}"


# Who takes back what a task changed in its clone, and how you get them to, as the start of the
# sentence that says what they do: your reply sends the writer; a box has no writer.
WRITER, IN_A_BOX = "reply, and the writer", "whoever works in this box, which has no writer,"


def undoes(meta: Path) -> str:
    """WRITER or IN_A_BOX, for the task whose metadata folder is meta."""
    from .task import Task  # here, not at the top: task reads its version through this module

    try:
        return IN_A_BOX if Task(meta.parent).read_state().box else WRITER
    except (OSError, ValueError, KeyError, TypeError):  # no state to read: said as for a task
        return WRITER


def agent_mounts(repo: Path, meta: Path) -> list[Mount]:
    mounts = [Mount(str(repo / ".git"), str(repo / ".git"))]
    for rel, read_only in protection.mount_points(repo, meta):
        mounts.append(Mount(str(repo / rel), str(repo / rel), read_only=read_only))
    mounts.append(Mount(str(meta / "gitconfig"), "/config/.gitconfig", read_only=True))
    return mounts


def review_worktree_path(source: Path, task_root: Path) -> Path:
    """In the task's directory, which the agent does not see, and named like your repository:
    IDEs derive module names from the directory and would otherwise rewrite their project files."""
    name = source.name if source.name not in ("repo", ".task") else f"{source.name}-review"
    return task_root / name


# Files an IDE writes when it opens the review copy; its own changes there are not your work.
IDE_FILES = (".idea/", ".vscode/", ".fleet/")


def local_changes(path: Path) -> list[str]:
    """Your changes in the review copy: not staged, since the agent's work is the staged part, and
    not the IDE's own project files."""
    status = git("status", "--porcelain", "--untracked-files=all", cwd=path).stdout
    changed = [line[3:] for line in status.splitlines() if line[1] != " "]
    return [f for f in changed if not f.startswith(IDE_FILES) and not f.endswith(".iml")]


def update_review_worktree(source: Path, task_root: Path, commit: str, base: str) -> Path:
    """A worktree of your repository showing the agent's work as uncommitted changes on top of the
    commit the task started from: your IDE lists them in its Changes view, like your own work.
    The files are those of the agent's commit; only HEAD stays at base.
    """
    path = review_worktree_path(source, task_root)
    if not path.exists():
        git("worktree", "add", "--quiet", "--detach", str(path), commit, cwd=source)
    elif changed := local_changes(path):
        named = display.name(", ".join(changed[:3]))
        raise RepoError(f"{path} has changes you made ({named}); discard them first")
    else:
        _hide_ide_files(path, hide=False)
        # --force: what the IDE rewrote in its project files gives way to the new work.
        git("checkout", "--quiet", "--force", "--detach", commit, cwd=path)
    git("reset", "--quiet", "--soft", base, cwd=path)
    _hide_ide_files(path, hide=True)
    return path


def _hide_ide_files(path: Path, hide: bool) -> None:
    """IDEs rewrite their committed project files on open (vcs.xml, modules.xml). Marked
    skip-worktree, those rewrites stay out of the IDE's Changes view, which then lists only the
    agent's work; changes the agent made to these files are staged and still listed."""
    tracked = git("ls-files", "-z", "--", *(p.rstrip("/") for p in IDE_FILES), "*.iml", cwd=path).stdout
    files = [f for f in tracked.split("\0") if f]
    if files:
        flag = "--skip-worktree" if hide else "--no-skip-worktree"
        git("update-index", flag, "--", *files, cwd=path)


def remove_review_worktree(source: Path, task_root: Path) -> Path | None:
    """Also with the IDE's changes to its project files; callers check for changes of yours first."""
    path = review_worktree_path(source, task_root)
    if not path.exists():
        return None
    git("worktree", "remove", "--force", str(path), cwd=source)
    return path


def review_ref(task_id: str) -> str:
    """Not a branch: your branch list stays as it is until you accept the work."""
    return f"refs/vivibox/{task_id}"


def fetch_to(source: Path, repo: Path, meta: Path, task_id: str) -> str:
    """Brings the task's commits into your repository under refs/vivibox/<id>. Returns the commit.
    From the task's clone and nowhere else: where the work moved the pointer of a submodule you
    have checked out, git would go into that submodule and fetch from its remote (on its own,
    and always when your settings say fetch.recurseSubmodules), so it is told not to. The
    submodule's new commits are yours to fetch, with git submodule update, once you have the work."""
    check_protection(repo, meta)
    refspec = f"+refs/heads/{branch_name(task_id)}:{review_ref(task_id)}"
    try:  # run in your checkout, but upload-pack reads the clone: its refs may be a pipe the agent made
        in_clone(
            repo, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", str(repo), refspec, cwd=source
        )
    except GitTimeout as e:
        raise GitTimeout(
            f"the work could not be brought back from the task's clone: {str(e).removesuffix(RETRY)}; "
            f"then try again, or see vivibox status {task_id}"
        ) from None
    return git("rev-parse", review_ref(task_id), cwd=source).stdout.strip()


def create_branch(source: Path, task_id: str, commit: str) -> str:
    """The accepted work becomes branch vivibox/<id> in your repository."""
    git("branch", "--force", branch_name(task_id), commit, cwd=source)
    return branch_name(task_id)


def drop_review_ref(source: Path, task_id: str) -> None:
    git("update-ref", "-d", review_ref(task_id), cwd=source, check=False)
