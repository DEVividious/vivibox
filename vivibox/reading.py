"""What the host makes for a container that reads the work and runs nothing of it (the planner's
control container, the reviewer's): the working directory of its harness, and a read-only view
of the task's commits.

A harness takes its working directory for the project and loads the project's own configuration
from it, and git follows a repository's .git to commands. So the directory the harness works in
is a git repository of the host's making with one empty commit and nothing else, and the work
is a view beside it that git on the host makes from the task's clone: only the commits are
taken, and the view's .git is the host's. Both are mounted read-only in the one container they
are for; the writer's clone is not mounted there at all. git in that container answers for the
view from wherever it is run (git_env).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import counted, display, repo
from .config import DEFAULT_HISTORY_DEPTH

# git on the host, in a view or a working directory, reads no configuration but the folder's
# own, which is the host's making: your global and system configuration (a filter driver,
# git-lfs, a submodule setting) would otherwise run your programs on what the writer committed.
# And it fetches nothing lazily: git serving the host's fetch from a clone that names a promisor
# remote would go to that remote for an object the clone lacks, by the command the clone names;
# said here, it does not, whatever your environment says or the git's own default is.
HOST_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1"}
HEX = re.compile(r"[0-9a-f]{40,64}")
# Steps of history_depth commits each that one deepening of a copy may take: here, to the task's
# base, which a review's `git diff <base>..HEAD` needs; for a commit the planner asks for, from
# the clone and a remote together (history.ensure). And the bytes one such fetch may print.
STEPS = 20
FETCH_LIMIT = 64 * 2**20
# The view's own configuration, written by the host: a symbolic link the writer committed is
# checked out as a plain file holding the path it names. A link would be followed, in the
# reader's container, to that container's own files (its secrets), which a plan or a review
# could then quote back to the writer. git there reads the same setting and finds the view clean.
LINKS_AS_FILES = ("core.symlinks", "false")
# For git in a reader's container: a directory is a repository only when git is pointed at it
# (GIT_DIR, --git-dir), never because it looks like a bare one. A folder the writer committed
# with a HEAD, objects, refs and a config of its own is then not taken for a repository by a
# git command whose environment has lost GIT_DIR, and what its config names is not run.
NO_BARE_DISCOVERY = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.bareRepository",
    "GIT_CONFIG_VALUE_0": "explicit",
}


def git_env(view: str | Path) -> dict[str, str]:
    """What makes git in the container work on the view, at the path it is mounted there,
    wherever it is run: the harness stands in a directory of the host's, and a review's turn
    asks for `git diff <base>..HEAD` and names no place to run it. And on nothing else it was
    not pointed at (NO_BARE_DISCOVERY)."""
    return {"GIT_DIR": f"{view}/.git", "GIT_WORK_TREE": str(view), **NO_BARE_DISCOVERY}


def made(folder: Path) -> bool:
    """Whether a repository of the host's making is at folder: a plain folder with a .git that
    is a folder too, neither of them a link."""
    git_dir = folder / ".git"
    return folder.is_dir() and not folder.is_symlink() and git_dir.is_dir() and not git_dir.is_symlink()


def own_git(folder: Path) -> dict[str, str]:
    """The environment of the host's git for one folder of its own making: HOST_GIT, and that
    folder as its repository and its work tree, said outright. git takes both from the
    environment before the directory it is run in, so a GIT_DIR your shell carries (inside a
    git hook, say) would otherwise aim the reset and the clean below, which delete, at another
    repository."""
    return {**HOST_GIT, "GIT_DIR": f"{folder}/.git", "GIT_WORK_TREE": str(folder)}


def workdir(work: Path, whose: str) -> None:
    """The working directory of a reader's harness, made once: a git repository of the host's
    with one empty commit and nothing else, no hook and no file. Mounted read-only in the
    reader's container, it is where the harness looks for a project's configuration and finds
    none; a repository with a commit, so opencode knows the directory as one project from turn
    to turn without writing there. whose: "the planner's", for what is said when it cannot be made."""
    if not work.is_dir() or work.is_symlink():
        raise repo.RepoError(f"{whose} working directory has no folder at {work}; start the task again")
    if (work / ".git").exists():
        return
    own = own_git(work)
    repo.git("init", "--quiet", "--template=", "--initial-branch", "main", str(work), env=own)
    who = ("-c", "user.name=vivibox", "-c", "user.email=vivibox@localhost")
    message = f"{whose[0].upper()}{whose[1:]} working directory"
    repo.git(*who, "commit", "--quiet", "--allow-empty", "-m", message, cwd=work, env=own)


def _own_settings(src: Path) -> None:
    """The view's .git as the host keeps it, also for a view made before these were kept: no
    hook, and links as plain files. Where the setting is new to the view, its index goes, so the
    checkout that follows writes every file again, a link among them as a plain file."""
    shutil.rmtree(src / ".git" / "hooks", ignore_errors=True)
    key, value = LINKS_AS_FILES
    has = repo.git("config", "--local", "--get", key, cwd=src, check=False, env=own_git(src))
    if has.stdout.strip() != value:
        repo.git("config", "--local", key, value, cwd=src, env=own_git(src))
        (src / ".git" / "index").unlink(missing_ok=True)


def view(
    clone: Path, branch: str, src: Path, commit: str = "", whose: str = "the reader's",
    depth: int = DEFAULT_HISTORY_DEPTH, base: str = "",
) -> None:  # fmt: skip
    """A reader's copy of the committed work at src: made by git on the host, in a folder only
    that reader's container mounts, read-only. The writer's clone is read as every git of the
    host's reads it, once it is as recorded (repo.guard) and within a time limit, and only its
    commits are taken: its .git, where the writer can leave what git follows to a command, is
    never what the reader's git works in. The view's .git holds no hook, made without git's
    templates, and a committed link is a plain file there (LINKS_AS_FILES). The copy stands on
    commit when one is given (the verified one, for a review; not on the task's branch, it is
    refused), else on the task's branch as it is, and holds that commit's files and nothing
    else. It is made with depth commits of the branch, and kept as deep as it is after that;
    and, given the task's base, it always holds it (_to_base)."""
    if not src.is_dir() or src.is_symlink():
        raise repo.RepoError(f"{whose} copy of the work has no folder at {src}; start the task again")
    own = own_git(src)
    if not (src / ".git").exists():
        repo.git("init", "--quiet", "--template=", "--initial-branch", branch, str(src), env=own)
    _own_settings(src)
    # The first fetch of a view takes depth commits of the branch (history_depth), and every later
    # one the new commits alone: a refresh keeps the view as deep as it is, the host's deepening
    # of the planner's among it (history.ensure), and a view made whole before stays whole.
    made = repo.git("rev-parse", "--verify", "--quiet", "HEAD^{commit}", cwd=src, check=False, env=own)
    deep = () if made.returncode == 0 else (f"--depth={depth}",)
    try:
        repo.in_clone(
            # --update-shallow: a clone of a shallow checkout is shallow, and git refuses to bring
            # its shallow roots into another repository without it (the fetch ends with nothing
            # in FETCH_HEAD). The source is the clone on disk, never a remote.
            clone, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--update-shallow", *deep,
            str(clone), f"refs/heads/{branch}", cwd=src, timeout=repo.COPY_TIMEOUT, env=own,
        )  # fmt: skip
    except repo.GitTimeout as e:
        raise repo.GitTimeout(f"{whose} copy of the work could not be brought up to date: {e}") from None
    if base and HEX.fullmatch(base):
        _to_base(clone, branch, src, base, depth, own)
    if commit:
        if not HEX.fullmatch(commit):
            raise repo.RepoError(f"not a commit to read the work at: {commit[:60]!r}")
        known = repo.git("merge-base", "--is-ancestor", commit, "FETCH_HEAD", cwd=src, check=False, env=own)
        if known.returncode != 0:
            raise repo.RepoError(
                f"the verified commit {commit[:7]} is not on the task's branch {branch}, so "
                f"{whose} copy cannot show the work that was verified"
            )
    repo.git(
        "reset", "--quiet", "--hard", "--no-recurse-submodules", commit or "FETCH_HEAD", cwd=src, env=own
    )
    # Exactly the commit: nothing untracked or ignored stays (what the branch has deleted since
    # a view made before lost its index, a link among it). The clean is of this view and no
    # other folder (_in); git removes a link itself, never what it names, and walks into none,
    # so nothing outside the view is touched.
    repo.git("clean", "-ffdxq", cwd=src, env=own)


def _to_base(clone: Path, branch: str, src: Path, base: str, depth: int, own: dict[str, str]) -> None:
    """The copy at src made deeper until it holds the task's base, which a review's `git diff
    <base>..HEAD` needs and a writer's commits past history_depth would leave out: from the
    clone alone, depth commits a step, the clone checked before each (repo.guard), each step
    within repo.CLONE_TIMEOUT and all within repo.COPY_TIMEOUT, STEPS steps at most. Past them,
    or on a step that fails, the copy is refused, as deep as it was."""
    has = lambda: repo.git("cat-file", "-e", f"{base}^{{commit}}", cwd=src, check=False, env=own)  # noqa: E731
    if has().returncode == 0:
        return
    before, until = boundary(src), time.monotonic() + repo.COPY_TIMEOUT
    try:
        for _ in range(STEPS):
            repo.guard(clone)
            step, left = boundary(src), until - time.monotonic()
            why = f"no answer within {repo.COPY_TIMEOUT:g} s" if left <= 0 else deepen(
                src, str(clone), f"refs/heads/{branch}", depth, min(repo.CLONE_TIMEOUT, left), own
            )  # fmt: skip
            if why:
                said = f"the copy could not be made deep enough to hold the base {base[:7]}"
                raise repo.RepoError(f"{said}: {why}")
            if has().returncode == 0:
                return
            if boundary(src) == step:
                break
    except BaseException:
        put_back(src, before)
        raise
    put_back(src, before)
    raise repo.RepoError(
        f"the work is more than {STEPS * depth} commits past its base {base[:7]}; raise history_depth in "
        "the configuration and start the task again"
    )


def deepen(
    src: Path, source: str, ref: str, depth: int, seconds: float, env: dict[str, str],
    to_remotes: bool = False,
) -> str:  # fmt: skip
    """One fetch of depth commits more of ref, from source, into the copy at src: "" once it is
    done, else why not, as one line. Its input closed, what it prints counted up to FETCH_LIMIT,
    within seconds; source after --end-of-options, so that no address is taken for an option.
    to_remotes: source is your remote (repo.git_environment)."""
    args = ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--update-shallow"]
    args += [f"--deepen={depth}", "--end-of-options", source, ref]
    try:
        done = repo.run_git(
            args, cwd=src, env=env, to_remotes=to_remotes, limit=FETCH_LIMIT, timeout=seconds,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return f"no answer within {seconds:g} s"
    except counted.TooMuch:
        return f"the fetch printed more than {FETCH_LIMIT // 2**20} MiB"
    except OSError as e:
        return one_line(str(e))
    if done.returncode != 0:
        return one_line(display.said(done.stderr.decode("utf-8", "replace"))) or f"exit {done.returncode}"
    return ""


def boundary(src: Path) -> bytes | None:
    """The copy's shallow boundary as git keeps it, or None for a copy that holds every commit."""
    try:
        return (src / ".git" / "shallow").read_bytes()
    except FileNotFoundError:
        return None


def put_back(src: Path, before: bytes | None) -> None:
    """The copy's boundary as it was: what was fetched since is out of every history git shows
    there. A fetch ended past its time leaves its lock, which would refuse the next one; the
    copy is the host's alone."""
    git_dir = src / ".git"
    (git_dir / "shallow.lock").unlink(missing_ok=True)
    if boundary(src) == before:
        return
    if before is None:
        (git_dir / "shallow").unlink(missing_ok=True)
        return
    kept = git_dir / "shallow.vivibox"
    kept.write_bytes(before)
    os.replace(kept, git_dir / "shallow")


def one_line(text: str) -> str:
    """The last line of what was said, which is what git ends on, as one line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return display.name(lines[-1])[:300] if lines else ""
