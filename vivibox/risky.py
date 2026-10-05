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
import hashlib
import json
import os
import re
import shlex
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import baseline, clone_read, display
from . import repo as git_repo

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
# repository (by the targets on the way out), a file past clone_read.HASH_LIMIT, and one past what
# the scan had left of its budget (clone_read.Budget).
OUT_LINK, TOO_LARGE, UNCHECKED = "symlink out of the repository:", "too large to check:", "not checked: "
# And of what is no file to show: a special file (by its kind), and one vivibox could not open.
SPECIAL, UNREADABLE, LINK = "not a plain file: ", "not readable: ", "symlink:"


# Whose the files are that a verification's risky check had no budget left for (Approvals.verified,
# GateResult.risky_stopped: each such path, and whose); anything else is why vivibox cannot tell.
WRITERS, YOURS = "the writer's", "yours"


def _stopped(stop: dict[str, str]) -> str:
    return (
        f"{len(stop)} risky file{'s' if len(stop) > 1 else ''} not checked, scan budget exceeded "
        f"({clone_read.size_of(clone_read.SCAN_BUDGET)})"
    )


def stop_said(stop: dict[str, str], task_id: str) -> str:
    """What vivibox tells you when the verification's risky check had no budget left for a file,
    from verified's own record of it, never from what a build printed: the count and whose, never
    the agent's names. "" when it had budget enough. One sentence, whole in a notification."""
    if not stop:
        return ""
    whose = next(iter(stop.values()))
    if whose == YOURS:
        return (
            f"{_stopped(stop)}: your own risky files are that large, so no task of this project can be "
            f"approved: vivibox delete {task_id}"
        )
    if whose == WRITERS:
        return (
            f"{_stopped(stop)}: reply, and the writer undoes its large risky changes "
            f"(vivibox risky {task_id} lists them)"
        )
    # whose: why it could not read your repository at the task's base, which says what to repair.
    return (
        f"{_stopped(stop)}: vivibox could not tell whose they are: {whose}; once that is repaired, "
        f"verify again (vivibox verify-again {task_id}), and where it cannot be, delete the task "
        f"(vivibox delete {task_id})"
    )


def stop_for_the_writer(stop: dict[str, str]) -> str:
    """The same for the writer: which files, and that you decide, never what you are told to do."""
    if not stop:
        return ""
    named = ", ".join(sorted(stop)[:5]) + (f" and {len(stop) - 5} more" if len(stop) > 5 else "")
    return f"{_stopped(stop)} ({named}): the user decides what happens to them"


SHA256 = re.compile(r"[0-9a-f]{64}")


def _content(digest: str) -> str | None:
    """The digest of the bytes an approval of digest takes: a plain file's, or what a link inside
    the repository points to; None when it takes none (a link to a folder or to nothing, a link
    out of the repository, a FIFO, a removal)."""
    if SHA256.fullmatch(digest):
        return digest
    tail = digest[-65:]
    return (
        tail[1:] if digest.startswith("symlink:") and tail[:1] == ":" and SHA256.fullmatch(tail[1:]) else None
    )


class Unapprovable(Exception):
    """Risky files no approval can take, with why and what fixes it."""


class NotAsShown(Unapprovable):
    """The risky files, or the approval they are compared with, are not what you were shown:
    nothing of them is approved until you have looked again."""


# A copy kept for diffs that the record's digest is not of (from before copies were held to it).
NOT_THE_COPY = "vivibox's copy of what was approved is not what was approved"
# What NotAsShown says; whoever asked adds how the files are looked at again there.
NOT_AS_SHOWN = "the risky files changed since they were shown, so nothing was approved"


def identity(
    approved: dict[str, str], now: dict[str, str], modes: dict[str, str], ran: Iterable[str] = ()
) -> str:
    """What a view of the risky files shows, as one digest: the approval its diffs are made
    against (each approved path and its digest) and the files as the view's scan found them (each
    path with the digest scan gave it, which says what it is, a file's content, a link's target
    and what it leads to, a special file's kind, and with whether a plain file can be run, which
    the view says: modes; ran: the files the approval before recorded as ones that can), in
    the order of their paths, whatever order they were found in. The same definition as the
    approval's own record, which is that scan: an approval takes the files only while its scan
    and the approval before it give this digest again. Whole, never a prefix: the task writes
    the files, and two states that share a short prefix can be searched for."""
    stated = [sorted(approved.items()), sorted((path, d, modes.get(path, "")) for path, d in now.items())]
    return hashlib.sha256(json.dumps([*stated, sorted(ran)]).encode()).hexdigest()


@dataclass(frozen=True)
class Shown:
    """A view of the risky files: each change with its diff, and the identity an approval of
    exactly that carries; "" when a change could not be shown whole or cannot be approved, which
    no approval covers."""

    diffs: list[str]
    identity: str


def _digest(repo: Path, path: Path, budget: clone_read.Budget | None = None) -> str:
    """A plain file's content. A link's target and, while it stays in the repository, what it
    points to: a link to another file there changes when that file does. A link out of it, by its
    targets alone: what it points at is never read (Approvals.unapprovable). Anything else (a FIFO,
    a device) by its kind, never opened: it is never the same as a file you approved. Past what is
    left of budget, a file or a link is not checked, and says so."""
    target = clone_read.link(repo, path)
    try:
        content = clone_read.sha256(repo, path, clone_read.HASH_LIMIT, budget)
    except clone_read.Refused as e:
        if e.kind == clone_read.OVER_BUDGET:
            return UNCHECKED + e.kind
        if e.kind == clone_read.OUT:
            return OUT_LINK + " -> ".join(e.links)
        if e.kind == clone_read.too_large(clone_read.HASH_LIMIT):
            return TOO_LARGE + e.kind
        if target is None:
            return f"{SPECIAL}{e.kind}"
        content = ""  # a link to a FIFO, or round a loop: its target alone
    except OSError as e:
        if target is None:  # no permission, or gone since it was listed: never the scan's end
            return f"{UNREADABLE}{e.strerror or type(e).__name__}"
        content = ""  # a link to a folder, or to nothing
    return content if target is None else f"{LINK}{target}" + (f":{content}" if content else "")


def _unshown(digest: str) -> bool:
    """Whether digest is of something vivibox cannot show you whole, or cannot check: what no
    approval covers when the work brought it (_cannot)."""
    bare = digest.startswith(LINK) and _content(digest) is None  # by its target alone
    return bare or digest.startswith((OUT_LINK, TOO_LARGE, UNCHECKED, SPECIAL, UNREADABLE))


def _leads_to(repo: Path, path: Path) -> str:
    """What a link that is recorded by its target alone leads to, in words."""
    try:
        clone_read.read_bytes(repo, path, 0)
    except IsADirectoryError:
        return "a folder"
    except FileNotFoundError:
        return "nothing"
    except clone_read.Refused as e:
        return e.kind
    except OSError:
        return "what vivibox cannot read"
    return "a file"


def _spent(who: str) -> str:
    """Why a pass read no more of the clone, and what undoes it without touching your files.
    who: the one that takes a change back, and how you get them to (repo.undoes)."""
    return (
        f"the risky files together are more than vivibox reads at a time "
        f"({clone_read.size_of(clone_read.SCAN_BUDGET)}); {who} restores the large risky "
        "files it changed as they were when the task started, and removes the ones it added"
    )


def _unknown(base: baseline.Baseline) -> str:
    return (
        f"; vivibox could not tell whether it was in your repository when the task started: {base.problem}"
        if base.problem
        else ""
    )


# What you can do about a change vivibox could not show you as text and you want (a new wrapper jar).
YOURSELF = "if you want the change, make it yourself in your repository once you accept the rest of the task"


def _undo(path: str, base: baseline.Baseline) -> str:
    """What the writer does to take its change to path back: never anything to a file of yours."""
    if base.problem:
        return "restores it as it was when the task started, or removes it if the task added it"
    there = path in base.sizes or path in base.links  # a file or a link of yours
    return "restores it as it was when the task started" if there else "removes it"


def _cannot(
    path: str, digest: str, base: baseline.Baseline, task_id: str, heavy: bool = False,
    who: str = git_repo.WRITER, leads: str = "",
) -> str:  # fmt: skip
    """Why no approval takes path as it is, or "": a file too large to verify, or a link out of the
    repository that your repository did not have, with that target, at the task's base commit. A
    link it had is tolerated by its target alone: never followed, never read. A file the scan had
    no budget left for: heavy, when your risky files were past the budget at the base commit.
    who: the one that takes the change back (repo.undoes): the writer, or in a box you."""
    unknown, name = _unknown(base), display.name(path)
    if digest.startswith(UNCHECKED) and heavy:
        return (
            f"{name} was not checked: {digest.removeprefix(UNCHECKED)}. Your repository's risky files "
            "together were more than vivibox reads at a time "
            f"({clone_read.size_of(clone_read.SCAN_BUDGET)}) when the task started, so vivibox cannot "
            "tell whether the task changed them, and no task of this project can be approved while they "
            f"are that large: delete this task (vivibox delete {task_id})"
        )
    if digest.startswith(UNCHECKED):
        return (
            f"{name} was not checked: {digest.removeprefix(UNCHECKED)}, so vivibox cannot tell whether "
            f"the task changed it, and it cannot be approved: {_spent(who)}{unknown}"
        )
    if digest.startswith(TOO_LARGE):
        if base.sizes.get(path, 0) > clone_read.HASH_LIMIT:  # yours: nothing for the writer to do
            return (
                f"{name} is {digest.removeprefix(TOO_LARGE)}, too large for vivibox to verify safely. It "
                "was that large in your repository when the task started, so vivibox cannot tell "
                "whether the task changed it, and no task of this project can be approved while it is "
                f"that large: delete this task (vivibox delete {task_id})"
            )
        return (
            f"{name} is {digest.removeprefix(TOO_LARGE)}, too large for vivibox to verify safely, so the "
            f"task's change to it cannot be approved: {who} {_undo(path, base)}{unknown}"
        )
    if digest.startswith(SPECIAL):
        return (
            f"{name} is {digest.removeprefix(SPECIAL)}, and vivibox does not read it, so it cannot be "
            f"approved: {who} removes it or makes it a plain file{unknown}"
        )
    if digest.startswith(UNREADABLE):
        return (
            f"{name} cannot be read ({digest.removeprefix(UNREADABLE)}), so it cannot be approved: {who} "
            f"makes it a file vivibox can read, or removes it{unknown}"
        )
    # A link recorded by its target alone (to a folder, to nothing, to a special file): what it
    # leads to is neither shown nor checked, now or later. One your repository had, to the same
    # target, is not the work's change, and stays recorded as it was.
    if digest.startswith(LINK) and _content(digest) is None:
        target = digest.removeprefix(LINK)
        if base.links.get(path) == target:
            return ""
        return (
            f"{name} is a link to {leads or 'no file'} ({display.name(target)}): vivibox can show "
            f"nothing of what it leads to, so it cannot be approved: {who} removes the link or makes "
            f"it a file in the repository{unknown}"
        )
    if digest.startswith(OUT_LINK) and digest != OUT_LINK + base.links.get(path, "\0"):
        return (
            f"{name} is a link that leads out of the repository "
            f"({display.name(digest.removeprefix(OUT_LINK))}), and "
            f"vivibox cannot check what it points to, so it cannot be approved: {who} "
            f"removes the link or makes it a file in the repository{unknown}"
        )
    return ""


def untracked(repo: Path, folder: Path) -> bool:
    """Whether git tracks nothing under folder; False when git cannot say."""
    try:
        listed = git_repo.in_clone(repo, "ls-files", "--", folder.relative_to(repo).as_posix(), check=False,
                                   timeout=30)  # fmt: skip
    except (OSError, git_repo.RepoError):  # no answer in time, or a clone refused
        return False
    return listed.returncode == 0 and not listed.stdout.strip()


def ignored(repo: Path) -> set[str]:
    """What git ignores and does not track, as paths from the root, a folder with a trailing /:
    a build's output, which the commits never carry to your checkout. Nothing when git cannot say."""
    try:
        listed = git_repo.in_clone(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                                   "--directory", check=False, timeout=30)  # fmt: skip
    except (OSError, git_repo.RepoError):  # no answer in time, or a clone refused
        return set()
    return set(filter(None, listed.stdout.split("\0"))) if listed.returncode == 0 else set()


def _under(path: str, left_out: set[str]) -> bool:
    """Whether path is one of the left-out paths, or in a left-out folder."""
    parts = path.split("/")
    return path in left_out or any("/".join(parts[:i]) + "/" in left_out for i in range(1, len(parts)))


def scan(
    repo: Path, extra: tuple[str, ...] | list[str] = (), budget: clone_read.Budget | None = None
) -> dict[str, str]:
    """Risky files in the working tree, with a digest of each; nested git repositories too. What
    git ignores and does not track is left out: it never reaches your checkout. One pass: its
    reads come from budget, a scan's own when none is given."""
    budget = clone_read.Budget() if budget is None else budget
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
                found[rel] = _digest(repo, here / name, budget)
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

    @property
    def runs(self) -> Path:
        return self.dir / "executable.json"

    def approved_executable(self) -> set[str] | None:
        """The approved files that could be run when they were approved; None for an approval
        that recorded none (an earlier vivibox's), which says nothing of modes: none is compared
        with it, and the next approval records them."""
        return set(json.loads(self.runs.read_text())) if self.runs.exists() else None

    def _running(self, now: dict[str, str]) -> set[str]:
        """The files of a scan that can be run now: a plain file, or what a link inside the
        repository leads to, with a bit that says so, read as the scan reads (clone_read.mode).
        The other permission bits travel with no commit and are not part of what is shown."""
        modes = {path: clone_read.mode(self.repo, self.repo / path) for path in now}
        return {path for path, mode in modes.items() if mode and int(mode, 8) & 0o111}

    def unapprovable(self, now: dict[str, str]) -> dict[str, str]:
        """The risky files of now that no approval takes, each with why, against your repository
        at the task's base commit (baseline), read only when there is something to ask it."""
        if not any(_unshown(d) for d in now.values()):
            return {}
        base, task_id = baseline.of_task(self.dir.parent), self.dir.parent.parent.name
        heavy, who = self._heavy(base), git_repo.undoes(self.dir.parent)
        found = {}
        for path, digest in now.items():
            bare = digest.startswith(LINK) and _content(digest) is None
            leads = _leads_to(self.repo, self.repo / path) if bare else ""
            if why := _cannot(path, digest, base, task_id, heavy, who, leads):
                found[path] = why
        return found

    def _heavy(self, base: baseline.Baseline) -> bool:
        """Whether your risky files at the start, as last approved, were past the budget together,
        by their size at the base commit."""
        return sum(base.sizes.get(p, 0) for p in self.approved()) > clone_read.SCAN_BUDGET

    def approve(
        self,
        now: dict[str, str] | None = None,
        budget: clone_read.Budget | None = None,
        runs: set[str] | None = None,
    ) -> dict[str, str]:
        """Takes the current state as approved (at task start: the state of your repository), or now,
        a scan's of this pass, whose budget the copies for later diffs draw from too. After the
        start, refused with nothing taken while a file is unapprovable."""
        budget = clone_read.Budget() if budget is None else budget
        current = scan(self.repo, self.extra, budget) if now is None else now
        first = not self.record.exists()
        if not first and (refused := self.unapprovable(current)):
            raise Unapprovable("; ".join(refused.values()))
        # The copies later diffs are drawn against, made beside the ones kept and put in their
        # place whole: each is the bytes the record's digest is of (the copy says what it wrote),
        # or the approval takes nothing. A file that is another by the time it is copied was
        # not what you were shown, whatever it is again afterwards.
        files, fresh = self.dir / "files", self.dir / "files.new"
        shutil.rmtree(fresh, ignore_errors=True)
        for rel, digest in current.items():
            if digest != NESTED_GIT and not digest.startswith(
                (LINK, OUT_LINK, SPECIAL, UNREADABLE, TOO_LARGE, UNCHECKED)
            ):
                (fresh / rel).parent.mkdir(parents=True, exist_ok=True)
                # Where the agent reads (/task): never a file of yours, swapped in since the scan.
                with contextlib.suppress(clone_read.Refused):
                    if clone_read.copy(self.repo, self.repo / rel, fresh / rel, budget=budget) == digest:
                        continue
                    if not first:
                        shutil.rmtree(fresh, ignore_errors=True)
                        raise NotAsShown(NOT_AS_SHOWN)
                    (fresh / rel).unlink()  # your own repository, changed as the task was made: no copy
        shutil.rmtree(files, ignore_errors=True)
        if fresh.exists():
            fresh.rename(files)
        shutil.rmtree(self.dir / "view", ignore_errors=True)  # read for this approval, and done
        self.dir.mkdir(parents=True, exist_ok=True)
        runs = self._running(current) if runs is None else runs  # with now: that pass's own
        for path, kept in ((self.runs, sorted(runs)), (self.record, current)):
            temporary = path.with_name(path.name + ".new")
            temporary.write_text(json.dumps(kept, indent=2, sort_keys=True) + "\n")
            temporary.replace(path)  # whole, or as it was
        return current

    def _found(self, budget: clone_read.Budget) -> tuple[dict[str, str], str, set[str]]:
        """One scan of the clone, the identity of what it found against the last approval, and
        which of its files can be run."""
        now = scan(self.repo, self.extra, budget)
        runs = self._running(now)
        found = identity(self.approved(), now, dict.fromkeys(runs, "x"), self.approved_executable() or ())
        return now, found, runs

    def approve_changes(self, shown: str) -> list[Change]:
        """p: the changes since the last approval and their approval, from one scan in one budget,
        so what is counted is what is approved, and a file it could not check is not. Nor is a
        change it could not have shown you: p shows each, in this pass, as the view does. And
        only what you were shown: shown is the identity of the view you looked at
        (Shown.identity), and while this pass's scan and the approval before it do not give that
        identity again, NotAsShown, with nothing taken; none ("") is never one."""
        budget = clone_read.Budget()
        now, found, runs = self._found(budget)
        changes = self.changes(now, runs)
        if self.record.exists():
            refused = list(self.unapprovable(now).values())
            refused += [
                said.strip() for said, seen in (self._show(c, now, budget) for c in changes) if not seen
            ]
            if refused:
                raise Unapprovable("; ".join(refused))
        if not shown or found != shown:
            raise NotAsShown(NOT_AS_SHOWN)
        self.approve(now, budget, runs)
        return changes

    def verified(self, budget: clone_read.Budget | None = None) -> tuple[list[Change], dict[str, str]]:
        """The verification's risky check, its reads from budget: the changes, and each file the scan
        had no budget left for, with whose they are (stop_said); {} when there was budget enough.
        Yours to decide, never a round of the writer's: the files it could not check may be yours."""
        now = scan(self.repo, self.extra, budget)
        if not (unread := [p for p, d in now.items() if d.startswith(UNCHECKED)]):
            return self.changes(now), {}
        base = baseline.of_task(self.dir.parent)
        whose = base.problem or (YOURS if self._heavy(base) else WRITERS)
        return self.changes(now), dict.fromkeys(unread, whose)

    def view(self) -> Shown:
        """A view: every change and its diff from one scan, all of it read in one budget, and
        the identity of what that scan found, for the approval of exactly this (approve_changes);
        no identity while a change was not shown whole, or is one no approval takes."""
        budget = clone_read.Budget()
        now, found, runs = self._found(budget)
        shown = [self._show(change, now, budget) for change in self.changes(now, runs)]
        # Before the first approval nothing is held to being shown: it is your repository's own.
        whole = not self.record.exists() or (all(seen for _, seen in shown) and not self.unapprovable(now))
        return Shown([said for said, _ in shown], found if whole else "")

    def diffs(self) -> list[str]:
        return self.view().diffs

    def changes(self, now: dict[str, str] | None = None, runs: set[str] | None = None) -> list[Change]:
        """Since the last approval, by now: a scan's of this pass, or a scan of its own. A file
        whose content is as approved and that can be run where it could not, or the other way
        round, is a change too (runs: the files of now that can, read here when not given)."""
        before, now = self.approved(), scan(self.repo, self.extra) if now is None else now
        # Approved before git ignored it (a build's output, approved before those were left out):
        # still there, and out of the scan now, so not a removal to approve.
        left_out = ignored(self.repo)
        before = {p: d for p, d in before.items() if p in now or not _under(p, left_out)}
        result = [Change(p, "added") for p in now if p not in before]
        result += [Change(p, "changed") for p in now if p in before and now[p] != before[p]]
        result += [Change(p, "removed") for p in before if p not in now]
        # Never taken as checked, whatever the record says: for you, on every check.
        stuck = self.unapprovable(now)
        same = [p for p in now if p in before and now[p] == before[p]]
        result += [Change(p, "changed") for p in same if p in stuck]
        if (ran := self.approved_executable()) is not None:
            runs = self._running(now) if runs is None else runs
            result += [Change(p, "changed") for p in same if p not in stuck and (p in runs) != (p in ran)]
        return sorted(result, key=lambda c: c.path)

    def diff(
        self, change: Change, now: dict[str, str] | None = None, budget: clone_read.Budget | None = None
    ) -> str:
        """change, as now (the view's scan) has it, read in budget (the view's); hashed again only
        without now, in a budget of its own."""
        return self._show(change, now, budget)[0]

    def _show(
        self, change: Change, now: dict[str, str] | None = None, budget: clone_read.Budget | None = None
    ) -> tuple[str, bool]:
        """change's diff, and whether you could read what it is: a new or changed file, or what a
        link inside the repository points to, is shown, or copied for you whole, as text, and what
        was shown or copied is what the digest an approval records says; one that could not be, for
        its size, the budget or not being text (clone_read.Text), or that changed meanwhile, is no
        approval's. What was approved before need not be text: the new content is shown whole."""
        budget = clone_read.Budget() if budget is None else budget
        old = self.dir / "files" / change.path
        new = self.repo / change.path
        head = f"{display.name(change.path)}: {change.kind}{self._mode_said(change)}"
        if NESTED_GIT in (self.approved().get(change.path), scan_one(new)):
            return f"{head} ({NESTED_GIT}; IntelliJ would offer to add it as a VCS root)\n", True
        digest = "" if now is None else now.get(change.path, "")
        with contextlib.suppress(OSError):  # a copy for you is of the file as it is now, or none
            self.copy_for_you(change.path).unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # gone since: the rest says so
            digest = _digest(self.repo, new, budget) if now is None else digest
            if why := self.unapprovable({change.path: digest}):
                return f"{head}: {why[change.path]}\n", True  # refused for that already
        want, target = _content(digest), clone_read.link(self.repo, new)
        linked = f"{head}, symlink to {display.name(target)}\n" if target is not None else ""
        if target is not None and want is None:  # your repository's own, by its target alone
            return linked, True
        changed = "it changed while vivibox read it"
        try:  # through a link only while it stays inside the repository
            raw = clone_read.read_bytes(self.repo, new, clone_read.CHECK_LIMIT, budget)
        except clone_read.Refused as e:
            if e.kind == clone_read.OVER_BUDGET:
                return f"{linked}{head}: not shown: {e.kind}, as {self._unseen(change.path)}\n", False
            if e.kind != clone_read.too_large(clone_read.CHECK_LIMIT):
                if want is None:
                    return f"{linked}{head} ({display.name(str(e))})\n", True
                # Hashed as a file and no file now (a FIFO, a link out, swapped in since): refused.
                return (
                    f"{linked}{head}: not shown: {self._unseen(change.path, display.name(str(e)))}\n",
                    False,
                )
            view, seen = self._view(change.path, budget, want)
            return f"{linked}{head}: {e.kind}, too large to show here; {view}\n", seen
        except OSError:  # gone, or a folder
            if want is not None:
                return f"{linked}{head}: not shown: {self._unseen(change.path, changed)}\n", False
            raw = b""
        if want is not None and hashlib.sha256(raw).hexdigest() != want:
            return f"{linked}{head}: not shown: {self._unseen(change.path, changed)}\n", False
        try:
            b = display.lines(clone_read.as_text(raw))
        except clone_read.NotText as e:
            return f"{linked}{head}: not shown: {self._not_text(change.path, e)}\n", False
        note = ""
        try:
            before = old.read_bytes() if old.exists() else None
            # The copy is the approved side only while it is what the record says was approved.
            if before is not None and hashlib.sha256(before).hexdigest() != self.approved().get(change.path):
                before, note = None, f"{head}: {NOT_THE_COPY}, so the new content is shown whole\n"
            a = display.lines(clone_read.as_text(before)) if before is not None else []
        except clone_read.NotText as e:
            if change.kind == "removed":
                return f"{linked}{head} (what was approved before is not text: {e.why})\n", True
            before = f"what was approved before is not text ({e.why})"
            a, note = [], f"{head}: {before}, so the new content is shown whole\n"
        # Of the raw lines, escaped only to be read: the digest above is of raw, as it is.
        name = display.name(change.path)
        if diff := display.diff(difflib.unified_diff(a, b, f"approved/{name}", f"task/{name}")):
            return (linked + note or f"{head}\n") + diff, True
        if change.kind == "removed" and not old.exists():  # a removal, and nothing kept to show of it
            return f"{head} (vivibox kept no copy of what was approved, so there is nothing to show)\n", True
        if digest and digest == self.approved().get(change.path):  # only whether it runs changed
            return f"{linked}{head} (its content is as approved)\n", True
        if not b:  # nothing to show, and nothing in it unshown
            return f"{linked}{head} (empty)\n", True
        # Never a change taken as seen with nothing of it shown.
        nothing = "the difference is in nothing vivibox shows as lines"
        return f"{linked}{head}: not shown: {self._unseen(change.path, nothing)}\n", False

    def _mode_said(self, change: Change) -> str:
        """Whether the changed file can be run, after its change's kind: said of one that can, and
        of one that could when it was approved and cannot now."""
        ran = self.approved_executable()
        was = None if ran is None or change.kind == "added" else change.path in ran
        if change.kind != "removed" and self._running({change.path: ""}):
            return ", now executable" if was is False else ", executable"
        return ", no longer executable" if was and change.kind != "removed" else ""

    def _unseen(self, path: str, why: str = "") -> str:
        """Why the task's change to path cannot be approved when you could not read it, and what
        the writer does: takes its change back, never anything to a file of yours."""
        base = baseline.of_task(self.dir.parent)
        why = why or (
            "the risky files together are more than vivibox reads at a time "
            f"({clone_read.size_of(clone_read.SCAN_BUDGET)})"
        )
        who = git_repo.undoes(self.dir.parent)  # the writer after your reply; in a box, you
        return (
            f"{why}, so the task's change to it cannot be approved: {who} {_undo(path, base)}{_unknown(base)}"
        )

    def _not_text(self, path: str, e: clone_read.NotText) -> str:
        """Why the task's change to path, not text, cannot be approved, and what you can do."""
        return f"{self._unseen(path, f'it is {e}')}; {YOURSELF}"

    def copy_for_you(self, path: str) -> Path:
        """Where _view copies a risky file of path: the task's metadata, which no container writes,
        by a name of vivibox's own, so the command to read it holds nothing the agent named."""
        digest = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()
        return self.dir / "view" / f"{digest}.txt"  # whole: no two paths share a copy

    def _view(
        self, path: str, budget: clone_read.Budget | None = None, want: str | None = None
    ) -> tuple[str, bool]:
        """Where to read a risky file too large for the panel, before you approve it: a copy in the
        task's metadata, where no link of the agent's can be, read as text, and only text (it is
        deleted when it turns out not to be, and the change is not seen). Opened in your IDE, a
        build file is imported, which is what approving it is there for. The copy's name is
        vivibox's (copy_for_you), and less runs no LESSOPEN unpacker (-L)."""
        view = self.copy_for_you(path)
        said = "vivibox checks whether it changes, not what it says"
        try:
            view.parent.mkdir(parents=True, exist_ok=True)
            copied = clone_read.copy(
                self.repo, self.repo / path, view, clone_read.HASH_LIMIT, budget, follow=True, text=True
            )
            if want is not None and copied != want:  # not what an approval would take
                raise OSError("it changed while vivibox copied it")
        except clone_read.NotText as e:  # the copy is gone already
            return f"{said}, and could not copy it for you to read: {self._not_text(path, e)}", False
        except OSError as e:
            with contextlib.suppress(OSError):
                view.unlink(missing_ok=True)
            over = getattr(e, "kind", "") == clone_read.OVER_BUDGET
            why = self._unseen(path) if over else self._unseen(path, display.name(str(e)))
            return (
                f"{said}, and could not copy it for you to read: {e.kind + ', as ' if over else ''}{why}",
                False,
            )
        # -+R -+r: no escape sequence of the copy's reaches the terminal, whatever your LESS says.
        command = shlex.join(["less", "-L", "-+R", "-+r", str(view)])
        return f"{said}: read its copy as text, not in your IDE ({command}), before you approve", True


def scan_one(path: Path) -> str | None:
    return NESTED_GIT if path.name == ".git" and path.exists() else None
