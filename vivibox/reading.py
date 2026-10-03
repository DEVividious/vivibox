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

import re
from pathlib import Path

from . import repo

# git on the host, in a view or a working directory, reads no configuration but the folder's
# own, which is the host's making: your global and system configuration (a filter driver,
# git-lfs, a submodule setting) would otherwise run your programs on what the writer committed.
HOST_GIT = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
HEX = re.compile(r"[0-9a-f]{40,64}")


def git_env(view: str | Path) -> dict[str, str]:
    """What makes git in the container work on the view, at the path it is mounted there,
    wherever it is run: the harness stands in a directory of the host's, and a review's turn
    asks for `git diff <base>..HEAD` and names no place to run it."""
    return {"GIT_DIR": f"{view}/.git", "GIT_WORK_TREE": str(view)}


def made(folder: Path) -> bool:
    """Whether a repository of the host's making is at folder: a plain folder with a .git that
    is a folder too, neither of them a link."""
    git_dir = folder / ".git"
    return folder.is_dir() and not folder.is_symlink() and git_dir.is_dir() and not git_dir.is_symlink()


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
    repo.git("init", "--quiet", "--template=", "--initial-branch", "main", str(work), env=HOST_GIT)
    who = ("-c", "user.name=vivibox", "-c", "user.email=vivibox@localhost")
    message = f"{whose[0].upper()}{whose[1:]} working directory"
    repo.git(*who, "commit", "--quiet", "--allow-empty", "-m", message, cwd=work, env=HOST_GIT)


def view(clone: Path, branch: str, src: Path, commit: str = "", whose: str = "the reader's") -> None:
    """A reader's copy of the committed work at src: made by git on the host, in a folder only
    that reader's container mounts, read-only. The writer's clone is read as every git of the
    host's reads it, once it is as recorded (repo.guard) and within a time limit, and only its
    commits are taken: its .git, where the writer can leave what git follows to a command, is
    never what the reader's git works in. The copy stands on commit when one is given (the
    verified one, for a review; not on the task's branch, it is refused), else on the task's
    branch as it is."""
    if not src.is_dir() or src.is_symlink():
        raise repo.RepoError(f"{whose} copy of the work has no folder at {src}; start the task again")
    if not (src / ".git").exists():
        repo.git("init", "--quiet", "--initial-branch", branch, str(src), env=HOST_GIT)
    try:
        repo.in_clone(
            clone, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", str(clone),
            f"refs/heads/{branch}", cwd=src, timeout=repo.COPY_TIMEOUT, env=HOST_GIT,
        )  # fmt: skip
    except repo.GitTimeout as e:
        raise repo.GitTimeout(f"{whose} copy of the work could not be brought up to date: {e}") from None
    if commit:
        if not HEX.fullmatch(commit):
            raise repo.RepoError(f"not a commit to read the work at: {commit[:60]!r}")
        known = repo.git(
            "merge-base", "--is-ancestor", commit, "FETCH_HEAD", cwd=src, check=False, env=HOST_GIT
        )
        if known.returncode != 0:
            raise repo.RepoError(
                f"the verified commit {commit[:7]} is not on the task's branch {branch}, so "
                f"{whose} copy cannot show the work that was verified"
            )
    repo.git(
        "reset", "--quiet", "--hard", "--no-recurse-submodules", commit or "FETCH_HEAD", cwd=src,
        env=HOST_GIT,
    )  # fmt: skip
