"""Files that run code on the host without you asking: on IDE import, on opening a folder, on entering
a directory in a shell, or through git. Changes to them need your approval before you open the task
in IntelliJ, and before the task can be done. Test configuration is on the list for another
reason: it decides which tests run, and the gate cannot tell a tidy-up from a test switched off.

The working tree is compared, not commits: IntelliJ reads whatever is on disk.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

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
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
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


def _digest(path: Path) -> str:
    if not path.is_symlink():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    # The link and what it points to: a link to another file in the repo changes when that file does.
    digest = "symlink:" + os.readlink(path)
    try:
        if path.is_file():
            digest += ":" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return digest


def scan(repo: Path, extra: tuple[str, ...] | list[str] = ()) -> dict[str, str]:
    """Risky files in the working tree, with a digest of each; nested git repositories too."""
    patterns = (*DEFAULT_PATTERNS, *extra)
    found: dict[str, str] = {}
    for root, dirs, files in os.walk(repo):
        here = Path(root)
        rel_dir = here.relative_to(repo).as_posix()
        if ".git" in dirs or ".git" in files:
            if here != repo:
                found[f"{rel_dir}/.git"] = NESTED_GIT
            if ".git" in dirs:
                dirs.remove(".git")
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        # Symlinked directories are not followed by os.walk; list them as entries to be compared.
        for name in sorted([*files, *(d for d in dirs if (here / d).is_symlink())]):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            is_link_dir = name in dirs
            # A symlinked directory is risky if a directory of that name would be (.idea -> elsewhere).
            probe = f"{rel}/x" if is_link_dir else rel
            if name != ".git" and any(matches(probe, p) for p in patterns):
                found[rel] = _digest(here / name)
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

    def approve(self) -> dict[str, str]:
        """Takes the current state as approved (at task start: the state of your repository)."""
        current = scan(self.repo, self.extra)
        files = self.dir / "files"
        if files.exists():
            shutil.rmtree(files)
        for rel, digest in current.items():
            src = self.repo / rel
            if digest != NESTED_GIT and not src.is_symlink():
                (files / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, files / rel)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.record.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return current

    def changes(self) -> list[Change]:
        before, now = self.approved(), scan(self.repo, self.extra)
        result = [Change(p, "added") for p in now if p not in before]
        result += [Change(p, "changed") for p in now if p in before and now[p] != before[p]]
        result += [Change(p, "removed") for p in before if p not in now]
        return sorted(result, key=lambda c: c.path)

    def diff(self, change: Change) -> str:
        old = self.dir / "files" / change.path
        new = self.repo / change.path
        head = f"{change.path}: {change.kind}"
        if NESTED_GIT in (self.approved().get(change.path), scan_one(new)):
            return f"{head} ({NESTED_GIT}; IntelliJ would offer to add it as a VCS root)\n"
        if new.is_symlink():
            return f"{head}, symlink to {os.readlink(new)}\n"
        try:
            a = old.read_text().splitlines(keepends=True) if old.exists() else []
            b = new.read_text().splitlines(keepends=True) if new.exists() and new.is_file() else []
        except UnicodeDecodeError:
            return f"{change.path}: {change.kind} (binary)\n"
        return "".join(difflib.unified_diff(a, b, f"approved/{change.path}", f"task/{change.path}"))


def scan_one(path: Path) -> str | None:
    return NESTED_GIT if path.name == ".git" and path.exists() else None
