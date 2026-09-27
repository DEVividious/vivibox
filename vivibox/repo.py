"""The task's own clone of your repository, and bringing its work back.

The agent commits in the clone, so it writes to .git. Git on the host (yours, or IntelliJ in the
background) runs commands from .git/config (core.fsmonitor, filters, hooksPath) and from .git/hooks.
Those files are written here, from an allowlist, and mounted read-only into the agent container;
.git itself is a separate mount so the agent cannot swap it for a directory of its own.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .pod import Mount

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
PROTECTION_RECORD = "git-protection.json"


class RepoError(Exception):
    pass


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    if cwd is not None and not Path(cwd).is_dir():
        # A folder that was moved or deleted since it was configured; say so instead of a stack trace.
        raise RepoError(f"{cwd} does not exist")
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RepoError(f"git {' '.join(args[:3])}…: {p.stderr.strip()}")
    return p


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
        raise RepoError(
            "Git identity is missing. Set your name and email:\n"
            'git config --global user.name "Your Name"\n'
            'git config --global user.email "you@example.com"\n'
            f"Or run these without --global in {source}."
        )
    return Identity(name, email)


def git_dirs(repo: Path) -> list[Path]:
    """The repository's git dir and the git dirs of its submodules."""
    top = repo / ".git"
    modules = top / "modules"
    nested = sorted(p.parent for p in modules.rglob("config")) if modules.is_dir() else []
    return [top, *(d for d in nested if (d / "HEAD").exists())]


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


def _fingerprint(repo: Path) -> dict[str, str]:
    result = {}
    for d in git_dirs(repo):
        rel = d.relative_to(repo).as_posix()
        result[f"{rel}/config"] = hashlib.sha256((d / "config").read_bytes()).hexdigest()
        result[f"{rel}/hooks"] = ",".join(sorted(p.name for p in (d / "hooks").iterdir()))
    return result


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
    git("submodule", "update", "--init", "--recursive", "--quiet", cwd=repo)
    if origin:
        git("remote", "set-url", "origin", origin, cwd=repo)
    else:
        git("remote", "remove", "origin", cwd=repo)
    for d in git_dirs(repo):
        _sanitize(d)
    # What tools in the pod write into the repository and nobody means to commit: Serena's project
    # file and caches. Here, not in the project's .gitignore, which is yours.
    (repo / ".git" / "info").mkdir(exist_ok=True)
    with (repo / ".git" / "info" / "exclude").open("a") as f:
        f.write("\n# vivibox: written by tools in the task's pod\n.serena/\n")
    (meta / PROTECTION_RECORD).write_text(json.dumps(_fingerprint(repo), indent=2) + "\n")
    who = identity(source)
    (meta / "gitconfig").write_text(
        f"[user]\n\tname = {who.name}\n\temail = {who.email}\n[init]\n\tdefaultBranch = main\n"
    )
    return base


def check_protection(repo: Path, meta: Path) -> None:
    """Refuses to go on if protected git files changed; run before any git command on the host."""
    expected = json.loads((meta / PROTECTION_RECORD).read_text())
    if _fingerprint(repo) != expected:
        raise RepoError(f"protected git files in {repo} changed; not touching it from the host")


def agent_mounts(repo: Path, meta: Path) -> list[Mount]:
    mounts = [Mount(str(repo / ".git"), str(repo / ".git"))]
    for d in git_dirs(repo):
        for name in ("config", "hooks"):
            mounts.append(Mount(str(d / name), str(d / name), read_only=True))
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
        raise RepoError(f"{path} has changes you made ({', '.join(changed[:3])}); discard them first")
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
    """Brings the task's commits into your repository under refs/vivibox/<id>. Returns the commit."""
    check_protection(repo, meta)
    refspec = f"+refs/heads/{branch_name(task_id)}:{review_ref(task_id)}"
    git("fetch", "--quiet", "--no-tags", str(repo), refspec, cwd=source)
    return git("rev-parse", review_ref(task_id), cwd=source).stdout.strip()


def create_branch(source: Path, task_id: str, commit: str) -> str:
    """The accepted work becomes branch vivibox/<id> in your repository."""
    git("branch", "--force", branch_name(task_id), commit, cwd=source)
    return branch_name(task_id)


def drop_review_ref(source: Path, task_id: str) -> None:
    git("update-ref", "-d", review_ref(task_id), cwd=source, check=False)
