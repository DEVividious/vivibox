"""Before work is accepted: every submodule commit the work keeps is one you keep.

Accepting deletes the task, its clone with it. Work that moved a submodule to another commit
brings only the pointer to your repository (repo.fetch_to goes into no submodule), so a commit
the writer made inside a submodule, or one your submodule never fetched, may exist nowhere but
in the clone that is about to go. check refuses the accepting until each such commit is kept by
a repository of yours for that submodule, and says how to keep it there.

What is compared: the submodule pointers (gitlinks) of what will be kept (the work's last
commit; every commit of the work, when the accepting keeps them as a branch) against those of
the commit the task started from; each one added or changed, and under it the pointers that
submodule's own commit adds or changes, as far down as you keep the commits. Kept means
reachable from HEAD or a ref of the submodule as you have it checked out, or as your repository
kept it (.git/modules/<name>): a commit that is there as an object alone goes with git's next
clean-up. Asked of your repositories alone, never of the task's clone, by git that reads
objects, fetches nothing and follows no include of a .gitmodules, within a time limit.

A submodule commit you keep brings its history with it: the commits between the one the task
started from and the one the work keeps stay in your submodule, and one of them may point a
submodule of its own at a commit the last one no longer points at. So that history is listed,
once, and each commit of it that adds or moves a pointer is looked at as the last one is. For
a submodule the work adds there is no commit to count from: the history looked at is the one
no branch of a remote in your copy of it reaches (refs/remotes), since what a remote of yours
has was not written in the task's clone alone. A history that cannot be listed is refused.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import display, gate, repo

COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
GITLINK = "160000"
# Submodules in submodules, followed this far down: deeper is refused, not waited on.
DEEPEST = 8
# Seconds the whole check may take, as git on what the agent wrote elsewhere; past it, refused.
SECONDS = repo.CLONE_TIMEOUT
# Submodules a refusal says in full; the rest are counted.
LISTED = 10
# The listing of a history: each commit's id, then what it changed, a line for a path with
# its modes. A merge: what differs from every parent (-c). Each setting of yours that changes
# what git log prints is said here, so that none hides a pointer or adds a line.
HISTORY = ("log", "--format=%H", "--raw", "-r", "-c", "--root", "--no-renames")
HISTORY += ("--ignore-submodules=none", "--no-show-signature", "--no-color")
# git here fetches nothing: a submodule of yours that is a partial clone would otherwise go to
# its remote for a commit it lacks.
NOT_LAZY = {"GIT_NO_LAZY_FETCH": "1"}
# An address vivibox puts in a command for you to run: a URL of a protocol that runs nothing
# here, or user@host:path; no option, no space, nothing a shell reads as more than a word.
ADDRESS = re.compile(r"(?:(?:https|ssh|git)://|[A-Za-z0-9_][A-Za-z0-9._-]*@)[A-Za-z0-9][A-Za-z0-9._~:/@%+-]*")

MISSING = "missing"  # you have the submodule, not the commit
LOOSE = "loose"  # the commit is in your submodule, and nothing keeps it there
NOT_CHECKED_OUT = "not checked out"  # the submodule is your project's, with no copy of it here
ADDED = "added"  # the work adds the submodule
UNCHECKED = "unchecked"  # vivibox could not look: said, and refused
NO_START = "no start"  # your submodule lacks the commit it was at when the task started
UNLISTED = "unlisted"  # git in your submodule did not list its history
# The kinds that say a commit is not kept; the rest say vivibox does not know.
NOT_KEPT = (MISSING, LOOSE, NOT_CHECKED_OUT, ADDED)


class Unkept(gate.GateError):
    """Work that would be accepted while a submodule commit it keeps is not kept by you. Its
    text is the short form, for a notification; whole: with every submodule and command."""

    def __init__(self, short: str, whole: str) -> None:
        super().__init__(short)
        self.whole = whole


class Late(Exception):
    """The check ran out of its time. Its text: what it was reading then, where that is known."""


@dataclass(frozen=True)
class Repository:
    """A repository of yours: your checkout, or a submodule of it. tree: its work tree, None for
    one your repository only kept (.git/modules/<name>). until: when the check must be over."""

    tree: Path | None
    git_dir: Path
    until: float

    def where(self) -> tuple[str, str]:
        """What tells git to answer for this repository."""
        return ("-C", str(self.tree)) if self.tree else ("--git-dir", str(self.git_dir))

    def git(self, *args: str) -> tuple[int, bytes]:
        """git's exit code and what it printed, as bytes: the names in a commit are the
        writer's, and need not be text."""
        return _git([*self.where(), *args], self.until)

    def keeps(self, commit: str) -> str:
        """ "" when commit is reachable from HEAD or a ref here; LOOSE when it is here and
        nothing reaches it; MISSING when it is not here."""
        code, loose = self.git("rev-list", "--max-count=1", f"{commit}^{{commit}}", "--not", "--all")
        return MISSING if code else LOOSE if loose.strip() else ""

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

    def history(self, before: str, commit: str) -> list[str] | None:
        """The commits of commit's history that add or move a submodule pointer (a merge: to
        where no parent of it points), commit itself left out: those since before, or, with
        no before, those no branch of a remote here reaches. None when git cannot list them,
        or prints what is no such listing. One git, whatever the history holds, and what it
        printed read once through, for as long as the check has left (Late past it)."""
        code, listed = self.git(*listing(before, commit))
        if code != 0:
            return None
        found, last, counted = [], "", True  # counted: last is in found, or is not to be
        for n, line in enumerate(listed.split(b"\n")):
            if n % 1024 == 0 and time.monotonic() >= self.until:
                raise Late
            if not line:
                continue
            parents = len(line) - len(line.lstrip(b":"))
            if not parents:
                last = line.decode("ascii", "replace")
                if not COMMIT.fullmatch(last):
                    return None
                counted = last == commit
                continue
            # :<mode of each parent> <mode> <id of each parent> <id> <what changed>, a tab, the path
            fields = line[parents:].split(b"\t")[0].split()
            if not last or len(fields) != 2 * parents + 3:
                return None
            if fields[parents] == GITLINK.encode() and not counted:
                found.append(last)
                counted = True
        return found

    def modules(self, commit: str) -> dict[str, tuple[str, str]]:
        """What commit's .gitmodules says of each submodule, as path: (name, url). Read as
        data: no include of it is followed (the file is the writer's where the commit is,
        and an include names any file on this machine, a pipe to wait on among them)."""
        code, listed = self.git("config", "--no-includes", "--blob", f"{commit}:.gitmodules", "-z", "--list")
        said: dict[str, dict[str, str]] = {}
        for entry in filter(None, _text(listed).split("\0") if code == 0 else []):
            key, _, value = entry.partition("\n")
            section, _, variable = key.rpartition(".")
            if section.startswith("submodule."):
                said.setdefault(section.removeprefix("submodule."), {})[variable] = value
        return {of["path"]: (name, of.get("url", "")) for name, of in said.items() if "path" in of}

    def submodule(self, path: str, name: str) -> Repository | None:
        """Your repository of the submodule at path: the one checked out there, else the one
        kept under name, which the commit you had gave that path; None for neither."""
        if self.tree:
            there = self.tree / path
            if there.resolve().is_relative_to(self.tree.resolve()) and (there / ".git").exists():
                return at(there, self.until)
        kept = self.git_dir / "modules" / name
        if (
            name
            and plain(name)
            and kept.is_dir()
            and _git(["--git-dir", str(kept), "rev-parse"], self.until)[0] == 0
        ):
            return Repository(None, kept, self.until)
        return None


def _git(args: list[str], until: float) -> tuple[int, bytes]:
    """git on the host (repo.run_git), fetching nothing, for as long as the check has left."""
    left = until - time.monotonic()
    if left <= 0:
        raise Late
    try:
        done = repo.run_git(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=left, env=NOT_LAZY)
    except subprocess.TimeoutExpired:
        raise Late from None
    return done.returncode, done.stdout


def listing(before: str, commit: str) -> list[str]:
    """What git is asked for the history of commit since before: Repository.history."""
    return [*HISTORY, *([f"{before}..{commit}"] if before else [commit, "--not", "--remotes"]), "--"]


def _text(printed: bytes) -> str:
    """What git printed, when it is text; "" when it is not."""
    try:
        return printed.decode()
    except UnicodeDecodeError:
        return ""


def at(tree: Path, until: float) -> Repository | None:
    """The repository whose work tree is tree, and no other: a folder with a .git that is no
    repository would have git answer for the one above it."""
    top, toplevel = _git(["-C", str(tree), "rev-parse", "--show-toplevel"], until)
    kept, git_dir = _git(["-C", str(tree), "rev-parse", "--absolute-git-dir"], until)
    if top or kept or Path(_text(toplevel).strip()).resolve() != tree.resolve():
        return None
    return Repository(tree, Path(_text(git_dir).strip()), until)


def plain(path: str) -> bool:
    """A path from a commit that vivibox follows under your folders and writes into a command:
    relative, no step out or into a .git, and nothing in it that is shown as other than itself."""
    parts = path.split("/")
    return bool(path) and not any(p in ("", ".", "..", ".git") for p in parts) and display.name(path) == path


@dataclass(frozen=True)
class Kept:
    """A submodule commit of the work that you do not keep yet. path: from your repository's
    root; held: your repository of the submodule, where there is one; above: the repository it
    is a submodule of, and inside: its path in that one; url: for one the work adds, where its
    .gitmodules says it comes from, when that is an address for a command; why: for one
    vivibox could not check; via: for one that only a submodule's history needs, the path of
    that submodule and the commit of its history that needs it; since: for one whose history
    was not listed, the commit it was to be listed from ("" for a submodule the work adds)."""

    path: str
    commit: str
    kind: str
    held: Repository | None
    above: Repository
    inside: str
    url: str = ""
    why: str = ""
    via: tuple[str, str] | None = None
    since: str = ""


@dataclass
class Found:
    """What a check found, as far as it got: a check that ran out of time says what it has."""

    unkept: list[Kept] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)


def look(
    yours: Repository,
    before: str,
    afters: list[str],
    found: Found,
    prefix: str = "",
    depth: int = 0,
    past: tuple[str, ...] = (),
    via: tuple[str, str] | None = None,
) -> None:
    """Adds to found the submodule commits the commits `afters` add or move, compared with
    `before` ("" for nothing to compare with), that no repository of yours keeps; and, where
    you keep one, those its own commit and the commits of its new history add or move. past:
    the commits of this repository's own new history to look at as `afters` are; via: what
    Kept.via says of a commit found under `afters`."""
    was = (yours.gitlinks(before) if before else None) or {}
    names = yours.modules(before) if before and was else {}
    for n, after in enumerate([*afters, *past]):
        via = (prefix.rstrip("/"), after) if n >= len(afters) else via
        now, named = yours.gitlinks(after), None  # named: what after's .gitmodules says, read once
        if now is None or depth > DEEPEST:
            deep = f"submodules more than {DEEPEST} deep are not followed" if now is not None else ""
            found.unkept.append(Kept(prefix.rstrip("/") or ".", after, UNCHECKED, None, yours, "", why=deep))
            continue
        for inside, commit in sorted(now.items()):
            path, old = prefix + inside, was.get(inside, "")
            if old == commit or (path, commit) in found.seen:
                continue
            found.seen.add((path, commit))
            if not plain(inside) or not COMMIT.fullmatch(commit):
                found.unkept.append(Kept(path, commit, UNCHECKED, None, yours, inside, via=via))
            elif (held := yours.submodule(inside, names.get(inside, ("", ""))[0])) is None and inside in was:
                found.unkept.append(Kept(path, commit, NOT_CHECKED_OUT, None, yours, inside, via=via))
            elif held is None:
                named = yours.modules(after) if named is None else named
                url = named.get(inside, ("", ""))[1]
                url = url if ADDRESS.fullmatch(url) else ""
                found.unkept.append(Kept(path, commit, ADDED, None, yours, inside, url, via=via))
            elif state := held.keeps(commit):
                kind = state if held.tree else NOT_CHECKED_OUT
                found.unkept.append(Kept(path, commit, kind, held, yours, inside, via=via))
            elif (more := _history(held, old, commit, path, depth)) is None:
                # Why, at the cost of one more git: the commit to list from is not there, or git failed.
                lacks = old and held.git("cat-file", "-e", f"{old}^{{commit}}")[0] != 0
                kind = NO_START if lacks else UNLISTED
                found.unkept.append(Kept(path, commit, kind, held, yours, inside, via=via, since=old))
            else:
                look(held, old, [commit], found, path + "/", depth + 1, tuple(more), via)


def _history(held: Repository, before: str, commit: str, path: str, depth: int) -> list[str] | None:
    """held.history, for the submodule at path that look found at depth; none is listed for
    one too deep to be followed, which look refuses. Out of time, it says what was read."""
    try:
        return held.history(before, commit) if depth < DEEPEST else []
    except Late:
        raise Late(f"reading the history of {display.name(path)}") from None


def check(task_id: str, source: Path, clone: Path, base: str, commit: str, history: bool = False) -> None:
    """Refuses (Unkept) to accept commit, the task's work brought to your repository at source,
    while a submodule commit it adds or moves since base is kept by no repository of yours, or
    one that the new history of a submodule commit you keep adds or moves.
    history: the accepting keeps every commit of the work (a branch), not its last tree alone,
    so each of them is looked at. Nothing is changed, here or in the task. clone: the task's,
    named in what to do, never read."""
    found, late = Found(), ""
    try:
        yours = at(source, time.monotonic() + SECONDS)
        if yours is None:
            said = f"{task_id} was not accepted: {source} is not a git repository vivibox can read"
            raise Unkept(said, said)
        kept = [commit]
        if history:
            code, listed = yours.git("rev-list", f"{base}..{commit}")
            kept = _text(listed).split() if code == 0 else []
        look(yours, base, kept or [commit], found)
    except Late as reading:
        late = f"vivibox did not finish checking the work's submodules in {SECONDS:g} s"
        late += f" ({reading})" if str(reading) else ""
    if found.unkept or late:
        raise Unkept(
            briefly(task_id, found.unkept, late), refusal(task_id, source, clone, found.unkept, late)
        )


def briefly(task_id: str, found: list[Kept], late: str = "") -> str:
    """The refusal for a notification, which wraps and is gone in seconds: what was refused and
    why, which submodules, and where the whole of it is said."""
    paths = list(dict.fromkeys(display.name(one.path) for one in found))
    more = f" and {len(paths) - 3} more" if len(paths) > 3 else ""
    which = f" ({', '.join(paths[:3])}{more})" if paths else ""
    known = any(one.kind in NOT_KEPT for one in found)
    what = "vivibox could not check its work's submodules" if paths else late
    what = "its work keeps submodule commits you do not keep" if known else what
    return (
        f"{task_id} was not accepted: {what}{which}, and accepting deletes the task. "
        f"In a terminal, vivibox accept {task_id} says {'how to keep them' if known else 'more'}."
    )


def refusal(task_id: str, source: Path, clone: Path, found: list[Kept], late: str = "") -> str:
    """What was refused, why, and what to do, for each commit: commands whole, to paste, with
    the folders they are run in and read from named in full. The first LISTED in full. The
    first line says a commit is not kept only where vivibox found one that is not."""
    known = "its work keeps a submodule commit that no repository of yours keeps. The task's clone may "
    known += "hold the only copy, and accepting deletes the task."
    unknown = "vivibox could not check that you keep every submodule commit its work keeps. The task's "
    unknown += "clone may hold the only copy of one, and accepting deletes the task."
    lines = [
        f"{task_id} was not accepted: {known if any(one.kind in NOT_KEPT for one in found) else unknown}"
    ]
    quoted = lambda path: shlex.quote(str(path))  # noqa: E731
    for one in found[:LISTED]:
        named = f"{display.name(one.path)} at {one.commit}"
        if one.via:  # not where the work leaves the submodule: said, with the commit that needs it
            above = display.name(one.via[0])
            named += f" (needed by {above} at {one.via[1]}, in the history of {above} that the work keeps)"
        if one.kind == UNCHECKED:
            why = one.why or "vivibox could not check this submodule"
            lines.append(
                f"{named}: {why}, so the work is not accepted with it; send the work back with a reply."
            )
            continue
        mine, theirs = source / one.path, clone / one.path
        ref = f"refs/vivibox/{task_id}/{one.commit[:12]}"
        fetch = f"git -C {quoted(mine)} fetch --no-recurse-submodules"
        # From the task's clone: the one commit, under a ref that keeps it; no tags, no
        # submodules; asked for by its id, which a git serving protocol 2 gives whatever your
        # settings; and a git serving it there that fetches nothing lazily (reading.HOST_GIT).
        from_task = (
            f"GIT_NO_LAZY_FETCH=1 git -C {quoted(mine)} -c protocol.version=2 fetch --no-tags "
            f"--no-recurse-submodules {quoted(theirs)} {one.commit}:{ref}"
        )
        keeps = f"{ref} keeps it there, and may be deleted once "
        keeps += "" if one.via else "the submodule is on that commit or "
        keeps += "its remote has it"
        if one.above.tree:
            init = f"git -C {quoted(one.above.tree)} submodule update --init -- {quoted(one.inside)}"
        else:  # the submodule above it is not checked out either: from the top, all the way down
            top = one.path.split("/")[0]
            init = f"git -C {quoted(source)} submodule update --init --recursive -- {quoted(top)}"
        if one.kind == NO_START:
            # The submodule's own path in the repository above it, which had it at that commit.
            owner = display.name(one.path.removesuffix(one.inside).rstrip("/")) or "your project"
            first = "" if one.held and one.held.tree else "Check the submodule out, then fetch"
            lines += [
                f"{named}: your submodule does not have {one.since}, the commit {owner} had it at when "
                "the task started, so vivibox cannot tell which commits of its history are new. "
                f"{first or 'Fetch'} it from the submodule's remote:",
                *([init] if first else []),
                fetch,
            ]
        elif one.kind == UNLISTED:
            lines += [
                f"{named}: vivibox could not list the commits of its history in your repository, so the "
                "work is not accepted with it. The git that lists them says why:",
                "git " + shlex.join([*one.held.where(), *listing(one.since, one.commit)]),  # to paste
            ]
        elif one.kind == ADDED:
            # No copy in the task's clone either: a pod cannot make a submodule's repository.
            where = "" if one.url else " (an address vivibox does not put in a command)"
            lines += [
                f"{named}: the work adds this submodule, and your checkout has no copy of it. Clone it "
                f"to {display.name(str(mine))} from the remote the work's .gitmodules names{where}, so that "
                "you keep the commit:",
                f"git clone -- {one.url or '<its remote>'} {quoted(mine)}",
            ]
        elif one.kind == LOOSE:
            lines += [
                f"{named} is in your submodule, and nothing keeps it there: git removes it when it next "
                f"cleans up. Give it a ref ({keeps}):",
                f"git -C {quoted(mine)} update-ref {ref} {one.commit}",
            ]
        elif one.kind == NOT_CHECKED_OUT:
            lines += [
                f"{named}: your checkout has no copy of this submodule. Check it out, then fetch the "
                f"commit from the submodule's remote, or from the task's clone ({keeps}):",
                init,
                fetch,
                from_task,
            ]
        else:
            lines += [
                f"{named} is not in your submodule. Fetch it from the submodule's remote, or from the "
                f"task's clone ({keeps}):",
                fetch,
                from_task,
            ]
    if len(found) > LISTED:
        lines.append(f"And {len(found) - LISTED} more submodule commits, said once these are kept.")
    if late:
        lines.append(f"{late}: there may be more.")
    if any(one.kind != UNCHECKED for one in found):
        lines.append("Then accept the work again.")
    return "\n".join(lines)
