"""Files that run code on the host without you asking: on IDE import, on opening a folder, on entering
a directory in a shell, or through git. Changes to them need your approval before you open the task
in IntelliJ, and before the task can be done. Test configuration is on the list for another
reason: it decides which tests run, and the gate cannot tell a tidy-up from a test switched off.

The working tree is compared, not commits: IntelliJ reads whatever is on disk.
"""

from __future__ import annotations

import contextlib
import difflib
import fnmatch
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import baseline, clone_read

# Pattern forms: "name" matches a file name at any depth, "dir/**" everything under a directory with
# that name at any depth, anything else the whole path relative to the repo root.
DEFAULT_PATTERNS = (
    # Maven
    "pom.xml", ".mvn/**", "mvnw", "mvnw.cmd",
    # Gradle
    "*.gradle", "*.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat", "gradle/wrapper/**",
    "buildSrc/**",
    # Node
    "package.json", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs",
    # Lockfiles decide what an install puts on your host, and one added where the project had
    # none changes how it is verified.
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    # bun's settings can preload a script into every run and point installs at another registry.
    "bunfig.toml",
    # Rust: rust-analyzer runs build scripts and proc macros as it opens the project;
    # .cargo/config can set the compiler a build runs; the toolchain file picks what rustup
    # fetches; Cargo.lock decides the dependencies, and their build scripts, like a lockfile above.
    "Cargo.toml", "Cargo.lock", "build.rs", ".cargo/**", "rust-toolchain", "rust-toolchain.toml",
    # The agent's own instructions, read by opencode and Claude Code from the repository: an
    # agent that edits them instructs the next turn, and the next task.
    "AGENTS.md", "CLAUDE.md",
    # IDEs
    ".idea/**", ".run/**", ".vscode/**",
    # Git hooks and hook managers
    ".githooks/**", ".husky/**", "lefthook.yml", "lefthook.yaml", ".pre-commit-config.yaml",
    ".gitmodules",
    # Shell environment managers that run code on entering the directory
    ".envrc", "mise.toml", ".mise.toml", ".tool-versions",
    # Test configuration: what decides which tests run, and how, switches tests off without any
    # of the words the gate looks for in added lines
    "vitest.config.*", "vite.config.*", "jest.config.*", ".mocharc*", "playwright.config.*",
    "cypress.config.*", "karma.conf.*", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
    "conftest.py", "Makefile", "junit-platform.properties",
)  # fmt: skip
SKIP_DIRS = {"node_modules", "target", ".gradle"}
# A tool's environment in the clone (uv sync's .venv, tox's): thousands of installed packages with
# their own package.json and Makefile. Skipped only while git tracks nothing in it, so none of it
# can reach your checkout; one tracked file and it is scanned like any other folder.
ENVIRONMENT_DIRS = {
    ".venv",
    "venv",
    ".tox",
    ".nox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
NESTED_GIT = "nested git repository"


def matches(path: str, pattern: str) -> bool:
    parts = path.split("/")
    if pattern.endswith("/**"):
        head = pattern[:-3]
        if "/" not in head:
            return any(fnmatch.fnmatchcase(p, head) for p in parts[:-1])
        return fnmatch.fnmatchcase(path, head + "/*")
    if "/" not in pattern:
        return fnmatch.fnmatchcase(parts[-1], pattern)
    return fnmatch.fnmatchcase(path, pattern)


# Digests of what vivibox does not read, and so cannot check: a link that leads out of the
# repository (by the targets on the way out), and a file past clone_read.HASH_LIMIT.
OUT_LINK, TOO_LARGE = "symlink out of the repository:", "too large to check:"


class Unapprovable(Exception):
    """Risky files no approval can take, with why and what fixes it."""


def _digest(repo: Path, path: Path) -> str:
    """A plain file's content. A link's target and, while it stays in the repository, what it
    points to: a link to another file there changes when that file does. A link out of it, by its
    targets alone: what it points at is never read (Approvals.unapprovable). Anything else (a FIFO,
    a device) by its kind, never opened: it is never the same as a file you approved."""
    target = clone_read.link(repo, path)
    try:
        content = clone_read.sha256(repo, path, clone_read.HASH_LIMIT)
    except clone_read.Refused as e:
        if e.kind == clone_read.OUT:
            return OUT_LINK + " -> ".join(e.links)
        if e.kind == clone_read.too_large(clone_read.HASH_LIMIT):
            return TOO_LARGE + e.kind
        if target is None:
            return f"not a plain file: {e.kind}"
        content = ""  # a link to a FIFO, or round a loop: its target alone
    except OSError:
        if target is None:
            raise
        content = ""  # a link to a folder, or to nothing
    return content if target is None else f"symlink:{target}" + (f":{content}" if content else "")


def _cannot(path: str, digest: str, base: baseline.Baseline, task_id: str) -> str:
    """Why no approval takes path as it is, or "": a file too large to verify, or a link out of the
    repository that your repository did not have, with that target, at the task's base commit. A
    link it had is tolerated by its target alone: never followed, never read."""
    unknown = (
        f"; vivibox could not tell whether it was in your repository when the task started: {base.problem}"
        if base.problem
        else ""
    )
    if digest.startswith(TOO_LARGE):
        if base.sizes.get(path, 0) > clone_read.HASH_LIMIT:  # yours: nothing for the writer to do
            return (
                f"{path} is {digest.removeprefix(TOO_LARGE)}, too large for vivibox to verify safely. It "
                "was that large in your repository when the task started, so vivibox cannot tell "
                "whether the task changed it, and no task of this project can be approved while it is "
                f"that large: delete this task (vivibox delete {task_id})"
            )
        if base.problem:
            undo = "restores it as it was when the task started, or removes it if the task added it"
        else:
            there = path in base.sizes or path in base.links  # a file or a link of yours
            undo = "restores it as it was when the task started" if there else "removes it"
        return (
            f"{path} is {digest.removeprefix(TOO_LARGE)}, too large for vivibox to verify safely, so the "
            f"task's change to it cannot be approved: reply, and the writer {undo}{unknown}"
        )
    if digest.startswith(OUT_LINK) and digest != OUT_LINK + base.links.get(path, "\0"):
        return (
            f"{path} is a link that leads out of the repository ({digest.removeprefix(OUT_LINK)}), and "
            "vivibox cannot check what it points to, so it cannot be approved: reply, and the writer "
            f"removes the link or makes it a file in the repository{unknown}"
        )
    return ""


def untracked(repo: Path, folder: Path) -> bool:
    """Whether git tracks nothing under folder; False when git cannot say."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--", folder.relative_to(repo).as_posix()],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return listed.returncode == 0 and not listed.stdout.strip()


def ignored(repo: Path) -> set[str]:
    """What git ignores and does not track, as paths from the root, a folder with a trailing /:
    a build's output, which the commits never carry to your checkout. Nothing when git cannot say."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(filter(None, listed.stdout.split("\0"))) if listed.returncode == 0 else set()


def _under(path: str, left_out: set[str]) -> bool:
    """Whether path is one of the left-out paths, or in a left-out folder."""
    parts = path.split("/")
    return path in left_out or any("/".join(parts[:i]) + "/" in left_out for i in range(1, len(parts)))


def scan(repo: Path, extra: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """Risky files in the working tree, with a digest of each; nested git repositories too. What
    git ignores and does not track is left out: it never reaches your checkout."""
    patterns = (*DEFAULT_PATTERNS, *extra)
    found: dict[str, str] = {}
    left_out = ignored(repo)
    for root, dirs, files in os.walk(repo):
        here = Path(root)
        rel_dir = here.relative_to(repo).as_posix()
        if ".git" in dirs or ".git" in files:
            if here != repo:
                found[f"{rel_dir}/.git"] = NESTED_GIT
            if ".git" in dirs:
                dirs.remove(".git")
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS
            and f"{prefix}{d}/" not in left_out
            and not (d in ENVIRONMENT_DIRS and untracked(repo, here / d))
        )
        # Symlinked directories are not followed by os.walk; list them as entries to be compared.
        for name in sorted([*files, *(d for d in dirs if (here / d).is_symlink())]):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            is_link_dir = name in dirs
            # A symlinked directory is risky if a directory of that name would be (.idea -> elsewhere).
            probe = f"{rel}/x" if is_link_dir else rel
            if name != ".git" and rel not in left_out and any(matches(probe, p) for p in patterns):
                found[rel] = _digest(repo, here / name)
    return found


@dataclass(frozen=True)
class Change:
    path: str
    kind: str  # added | changed | removed


class Approvals:
    """Approved state of risky files, kept in the task's metadata with copies for diffs."""

    def __init__(self, meta: Path, repo: Path, extra: tuple[str, ...] | list[str] = ()):
        self.dir = meta / "risky"
        self.repo = repo
        self.extra = tuple(extra)

    @property
    def record(self) -> Path:
        return self.dir / "approved.json"

    def approved(self) -> dict[str, str]:
        return json.loads(self.record.read_text()) if self.record.exists() else {}

    def unapprovable(self, now: dict[str, str]) -> dict[str, str]:
        """The risky files of now that no approval takes, each with why, against your repository
        at the task's base commit (baseline), read only when there is something to ask it."""
        if not any(d.startswith((OUT_LINK, TOO_LARGE)) for d in now.values()):
            return {}
        base, task_id = baseline.of_task(self.dir.parent), self.dir.parent.parent.name
        return {p: why for p, d in now.items() if (why := _cannot(p, d, base, task_id))}

    def approve(self) -> dict[str, str]:
        """Takes the current state as approved (at task start: the state of your repository).
        After that, refused with nothing taken while a file is unapprovable."""
        current = scan(self.repo, self.extra)
        first = not self.record.exists()
        if not first and (refused := self.unapprovable(current)):
            raise Unapprovable("; ".join(refused.values()))
        files = self.dir / "files"
        if files.exists():
            shutil.rmtree(files)
        shutil.rmtree(self.dir / "view", ignore_errors=True)  # read for this approval, and done
        for rel, digest in current.items():
            if digest != NESTED_GIT and not digest.startswith(("symlink", "not a plain file", TOO_LARGE)):
                (files / rel).parent.mkdir(parents=True, exist_ok=True)
                # Where the agent reads (/task): never a file of yours, swapped in since the scan.
                with contextlib.suppress(clone_read.Refused):
                    clone_read.copy(self.repo, self.repo / rel, files / rel)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.record.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return current

    def changes(self) -> list[Change]:
        before, now = self.approved(), scan(self.repo, self.extra)
        # Approved before git ignored it (a build's output, approved before those were left out):
        # still there, and out of the scan now, so not a removal to approve.
        left_out = ignored(self.repo)
        before = {p: d for p, d in before.items() if p in now or not _under(p, left_out)}
        result = [Change(p, "added") for p in now if p not in before]
        result += [Change(p, "changed") for p in now if p in before and now[p] != before[p]]
        result += [Change(p, "removed") for p in before if p not in now]
        # Never taken as checked, whatever the record says: for you, on every check.
        stuck = self.unapprovable(now)
        result += [Change(p, "changed") for p in stuck if p in before and now[p] == before[p]]
        return sorted(result, key=lambda c: c.path)

    def diff(self, change: Change) -> str:
        old = self.dir / "files" / change.path
        new = self.repo / change.path
        head = f"{change.path}: {change.kind}"
        if NESTED_GIT in (self.approved().get(change.path), scan_one(new)):
            return f"{head} ({NESTED_GIT}; IntelliJ would offer to add it as a VCS root)\n"
        with contextlib.suppress(OSError):  # gone since: the rest says so
            if why := self.unapprovable({change.path: _digest(self.repo, new)}):
                return f"{head}: {why[change.path]}\n"
        if new.is_symlink():
            return f"{head}, symlink to {os.readlink(new)}\n"
        try:
            b = clone_read.read_bytes(self.repo, new).decode().splitlines(keepends=True)
        except clone_read.Refused as e:
            if e.kind != clone_read.too_large(clone_read.CHECK_LIMIT):
                return f"{head} ({e})\n"
            return f"{head}: {e.kind}, too large to show here; {self._view(change.path)}\n"
        except OSError:  # gone, or a folder
            b = []
        except UnicodeDecodeError:
            return f"{change.path}: {change.kind} (binary)\n"
        try:
            a = old.read_text().splitlines(keepends=True) if old.exists() else []
        except UnicodeDecodeError:
            return f"{change.path}: {change.kind} (binary)\n"
        return "".join(difflib.unified_diff(a, b, f"approved/{change.path}", f"task/{change.path}"))

    def _view(self, path: str) -> str:
        """Where to read a risky file too large for the panel, before you approve it: a copy in the
        task's metadata, where no container writes and no link of the agent's can be, read as text.
        Opened in your IDE, a build file is imported, which is what approving it is there for."""
        view = self.dir / "view" / path
        said = "vivibox checks whether it changes, not what it says"
        try:
            view.parent.mkdir(parents=True, exist_ok=True)
            clone_read.copy(self.repo, self.repo / path, view, clone_read.HASH_LIMIT)
        except OSError as e:
            return f"{said}, and could not copy it for you to read: {e}"
        return f"{said}: read its copy as text, not in your IDE (less {view}), before you approve"


def scan_one(path: Path) -> str | None:
    return NESTED_GIT if path.name == ".git" and path.exists() else None
