"""The code index's private copy of the commits, made and brought up to date by the index
container I itself: the one operation on that copy that vivibox asks for, fixed here. It is
I's entrypoint (podspec.index_sync_command), and the provisioning run's: the copy made before the
program starts, never under it. The host gives it nothing but the copy's path, the commit the
host's read-only view stands on and the program's command to run after it; for another commit,
the host starts I again.

It runs in I, which is taken to be compromised by the repository: nothing it does is trusted,
what it prints is never read, and the host learns only whether I goes on running. The copy's
.git is written in I, so git runs on it here and never on the host. The source is the host's view
of the commits, mounted read-only at SOURCE; the copy is reset to exactly the commit the host
named, and nothing else stays: a file the commit deleted or renamed away, an untracked or an
ignored one, a committed link (written as a plain file holding its path, as in the planner's
view).

Plain Python and git, no import of vivibox's: the host passes this file's text to python3 -I.
"""

import os
import shutil
import subprocess
import sys

SOURCE = "/source"
# git with no configuration but the copy's own and the settings below, which outrank it: no
# hook, no file-system monitor (a command git would run), links as plain files.
SETTINGS = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.symlinks=false",
)  # fmt: skip
ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LC_ALL": "C",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
}
# Exit statuses the host may tell apart in a test; any status but 0 is a copy not made.
USAGE, OTHER_COMMIT = 2, 3


def git(copy: str, *args: str, out: bool = False) -> str:
    done = subprocess.run(
        ["git", *SETTINGS, *args], cwd=copy, env=ENV, stdin=subprocess.DEVNULL, check=True,
        stdout=subprocess.PIPE if out else None,
    )  # fmt: skip
    return done.stdout.decode("ascii", "replace").strip() if out else ""


def sync(copy: str, commit: str) -> int:
    """The copy at copy made, or brought, to commit, which the source's HEAD must be."""
    if not os.path.isdir(os.path.join(copy, ".git")):
        git(copy, "init", "--quiet", "--template=", ".")
    fetch = ("fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--depth=1")
    git(copy, *fetch, "--end-of-options", SOURCE, "HEAD")
    if git(copy, "rev-parse", "--verify", "FETCH_HEAD^{commit}", out=True) != commit:
        return OTHER_COMMIT
    git(copy, "reset", "--quiet", "--hard", "--no-recurse-submodules", "FETCH_HEAD")
    git(copy, "clean", "-ffdxq")
    return 0


def emptied(copy: str) -> None:
    """Everything in the copy removed, the copy's folder itself kept (it is a mount point); a
    link is removed, not followed."""
    for name in os.listdir(copy):
        path = os.path.join(copy, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def main(argv: list[str]) -> int:
    """<copy> <commit> [-- <program> ...]: the copy synced; then, when one is given and the copy
    is made, the program in this process's place. At that start, a copy that cannot be synced
    (its .git broken) is emptied and made again from the source, once: no one else would, since
    the host never reads the copy and learns nothing of why I ended."""
    if len(argv) < 2 or (len(argv) > 2 and (argv[2] != "--" or len(argv) == 3)):
        return USAGE
    copy, commit, program = argv[0], argv[1], argv[3:]
    try:
        status = sync(copy, commit)
    except (OSError, subprocess.CalledProcessError):
        if not program:
            return 1
        try:
            emptied(copy)
            status = sync(copy, commit)
        except (OSError, subprocess.CalledProcessError):
            return 1
    if status or not program:
        return status
    os.execvp(program[0], program)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
