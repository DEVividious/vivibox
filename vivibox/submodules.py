"""Before work is accepted: every submodule commit the work points at is one you have.

Accepting deletes the task, its clone with it. Work that moved a submodule to another commit
brings only the pointer to your repository (repo.fetch_to goes into no submodule), so a commit
the writer made inside a submodule, or one your submodule never fetched, may exist nowhere but
in the clone that is about to go. check refuses the accepting until each such commit is in a
repository of yours for that submodule, and says how to put it there.

What is compared: the submodule pointers (gitlinks) of the commit that will be accepted against
those of the commit the task started from; each one added or changed, and under it the pointers
that submodule's own commit adds or changes, as far down as you have the commits. Where a
commit must be: in the submodule as you have it checked out, or in what your repository kept
of it (.git/modules/<name>). Asked of your repositories alone, by git that reads its objects
and reaches no remote; never of the task's clone.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import display, gate, repo

COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GITLINK = "160000"
# Submodules in submodules, followed this far down: deeper is refused, not waited on.
DEEPEST = 8

MISSING = "missing"  # you have the submodule, not the commit
NOT_CHECKED_OUT = "not checked out"  # the submodule is your project's, with no copy of it here
ADDED = "added"  # the work adds the submodule
UNCHECKED = "unchecked"  # vivibox could not look: said, and refused


class Unkept(gate.GateError):
    """Work that would be accepted while a submodule commit it points at is not yours yet."""


@dataclass(frozen=True)
class Repository:
    """A repository of yours: your checkout, or a submodule of it. tree: its work tree, None for
    one your repository only kept (.git/modules/<name>)."""

    tree: Path | None
    git_dir: Path

    def git(self, *args: str) -> tuple[int, bytes]:
        """git's exit code and what it printed, as bytes: the names in a commit are the
        writer's, and need not be text."""
        where = ("-C", str(self.tree)) if self.tree else ("--git-dir", str(self.git_dir))
        done = repo.run_git([*where, *args], capture_output=True)
        return done.returncode, done.stdout

    def has(self, commit: str) -> bool:
        return self.git("cat-file", "-e", f"{commit}^{{commit}}")[0] == 0

    def gitlinks(self, commit: str) -> dict[str, str] | None:
        """The submodules commit points at, as path: commit; None when git cannot list them. A
        path that is no text is kept under a name that stands for it, with something that is
        no commit id: seen as changed when it changed, and never followed."""
        code, listed = self.git("ls-tree", "-r", "-z", "--full-tree", commit)
        if code != 0:
            return None
        found = {}
        for entry in filter(None, listed.split(b"\0")):
            head, _, path = entry.partition(b"\t")
            mode, _, oid = head.decode("ascii", "replace").split()
            if mode != GITLINK:
                continue
            try:
                found[path.decode()] = oid
            except UnicodeDecodeError:
                found[path.decode("utf-8", "backslashreplace")] = f"not text: {oid}"
        return found

    def submodule(self, path: str, before: str) -> Repository | None:
        """Your repository of the submodule at path: the one checked out there, else the one
        kept under the name the commit you had (before) gave that path; None for neither."""
        if self.tree:
            there = self.tree / path
            if there.resolve().is_relative_to(self.tree.resolve()) and (there / ".git").exists():
                return at(there)
        name = self._name(path, before) if before else ""
        kept = self.git_dir / "modules" / name
        if (
            name
            and kept.is_dir()
            and repo.git("--git-dir", str(kept), "rev-parse", check=False).returncode == 0
        ):
            return Repository(None, kept)
        return None

    def url(self, path: str, commit: str) -> str:
        """Where commit's .gitmodules says the submodule at path comes from, when that is
        something to hand git clone as it stands: "" for none, for one that is relative to
        your project's own remote, and for one that would be read as an option."""
        name = self._name(path, commit)
        code, found = self.git("config", "--blob", f"{commit}:.gitmodules", "--get", f"submodule.{name}.url")
        url = _text(found).strip() if name and code == 0 else ""
        return url if url and display.name(url) == url and not url.startswith(("-", "./", "../")) else ""

    def _name(self, path: str, commit: str) -> str:
        """The name commit's .gitmodules gives the submodule at path, "" for none or for one
        that is no folder's name under .git/modules."""
        code, listed = self.git("config", "--blob", f"{commit}:.gitmodules", "-z", "--get-regexp", r"\.path$")
        for entry in filter(None, _text(listed).split("\0") if code == 0 else []):
            key, _, value = entry.partition("\n")
            name = key.removeprefix("submodule.").removesuffix(".path")
            if value == path and plain(name):
                return name
        return ""


def _text(printed: bytes) -> str:
    """What git printed, when it is text; "" when it is not."""
    try:
        return printed.decode()
    except UnicodeDecodeError:
        return ""


def at(tree: Path) -> Repository | None:
    """The repository whose work tree is tree, and no other: a folder with a .git that is no
    repository would have git answer for the one above it."""
    top = repo.git("rev-parse", "--show-toplevel", cwd=tree, check=False)
    git_dir = repo.git("rev-parse", "--absolute-git-dir", cwd=tree, check=False)
    if top.returncode or git_dir.returncode or Path(top.stdout.strip()).resolve() != tree.resolve():
        return None
    return Repository(tree, Path(git_dir.stdout.strip()))


def plain(path: str) -> bool:
    """A path from a commit that vivibox follows under your folders and writes into a command:
    relative, no step out or into a .git, and nothing in it that is shown as other than itself."""
    parts = path.split("/")
    return bool(path) and not any(p in ("", ".", "..", ".git") for p in parts) and display.name(path) == path


@dataclass(frozen=True)
class Kept:
    """A submodule commit of the work that is not yours yet. path: from your repository's root;
    held: your repository of the submodule, where there is one; above: the repository it is a
    submodule of, and inside: its path in that one; url: for one the work adds, where its
    .gitmodules says it comes from."""

    path: str
    commit: str
    kind: str
    held: Repository | None
    above: Repository
    inside: str
    url: str = ""


def unkept(yours: Repository, before: str, after: str, prefix: str = "", depth: int = 0) -> list[Kept]:
    """The submodule commits `after` adds or moves, compared with `before` ("" for nothing to
    compare with), that no repository of yours holds; with those their own commits add or move."""
    now = yours.gitlinks(after)
    if now is None or depth > DEEPEST:
        return [Kept(prefix.rstrip("/") or ".", after, UNCHECKED, None, yours, "")]
    was = (yours.gitlinks(before) if before else None) or {}
    found = []
    for inside, commit in sorted(now.items()):
        if was.get(inside) == commit:
            continue
        path = prefix + inside
        if not plain(inside) or not COMMIT.fullmatch(commit):
            found.append(Kept(path, commit, UNCHECKED, None, yours, inside))
        elif (held := yours.submodule(inside, before)) is None and inside in was:
            found.append(Kept(path, commit, NOT_CHECKED_OUT, None, yours, inside))
        elif held is None:
            found.append(Kept(path, commit, ADDED, None, yours, inside, yours.url(inside, after)))
        elif not held.has(commit):
            kind = MISSING if held.tree else NOT_CHECKED_OUT
            found.append(Kept(path, commit, kind, held, yours, inside))
        else:
            found += unkept(held, was.get(inside, ""), commit, path + "/", depth + 1)
    return found


def check(task_id: str, source: Path, clone: Path, base: str, commit: str) -> None:
    """Refuses (Unkept) to accept commit, the task's work brought to your repository at source,
    while a submodule commit it adds or moves since base is in no repository of yours. Nothing
    is changed, here or in the task. clone: the task's, named in what to do, never read."""
    yours = at(source)
    if yours is None:
        raise Unkept(f"{task_id} was not accepted: {source} is not a git repository vivibox can read")
    if found := unkept(yours, base, commit):
        raise Unkept(refusal(task_id, source, clone, found))


def refusal(task_id: str, source: Path, clone: Path, found: list[Kept]) -> str:
    """What was refused, why, and what to do, for each commit: commands whole, to paste, with
    the folders they are run in and read from named in full."""
    lines = [
        f"{task_id} was not accepted: its work points at a submodule commit your repository does not "
        "have. The task's clone may hold the only copy, and accepting deletes the task."
    ]
    quoted = lambda path: shlex.quote(str(path))  # noqa: E731
    for one in found:
        named = f"{display.name(one.path)} at {one.commit}"
        if one.kind == UNCHECKED:
            lines.append(
                f"{named}: vivibox could not check this submodule, so the work is not accepted with it; "
                "send the work back with a reply."
            )
            continue
        mine, theirs = source / one.path, clone / one.path
        fetch = f"git -C {quoted(mine)} fetch --no-recurse-submodules"
        # From the task's clone: the one commit, no tags, no submodules, and a git serving it
        # there that fetches nothing lazily, whatever its own default is (reading.HOST_GIT).
        from_task = (
            f"GIT_NO_LAZY_FETCH=1 git -C {quoted(mine)} fetch --no-tags --no-recurse-submodules "
            f"{quoted(theirs)} {one.commit}"
        )
        if one.kind == ADDED:
            # No copy in the task's clone either: a pod cannot make a submodule's repository.
            lines += [
                f"{named}: the work adds this submodule, and your checkout has no copy of it. Clone it "
                f"to {display.name(str(mine))} from the remote the work's .gitmodules names, so that you "
                "have the commit:",
                f"git clone -- {quoted(one.url) if one.url else '<its remote>'} {quoted(mine)}",
            ]
        elif one.kind == NOT_CHECKED_OUT:
            if one.above.tree:
                init = f"git -C {quoted(one.above.tree)} submodule update --init -- {quoted(one.inside)}"
            else:  # the submodule above it is not checked out either: from the top, all the way down
                top = one.path.split("/")[0]
                init = f"git -C {quoted(source)} submodule update --init --recursive -- {quoted(top)}"
            lines += [
                f"{named}: your checkout has no copy of this submodule. Check it out, then fetch the "
                "commit from the submodule's remote, or from the task's clone:",
                init,
                fetch,
                from_task,
            ]
        else:
            lines += [
                f"{named} is not in your submodule. Fetch it from the submodule's remote, or from the "
                "task's clone:",
                fetch,
                from_task,
            ]
    if any(one.kind != UNCHECKED for one in found):
        # The view's notification wraps a long line and is gone in seconds: where to read it whole.
        lines.append(f"Then accept the work again. In a terminal, vivibox accept {task_id} says this whole.")
    return "\n".join(lines)
