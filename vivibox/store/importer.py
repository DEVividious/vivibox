"""The import of the tasks' events.jsonl into vivibox.db (plan P1.3, ADR-0051): every event of
every log there once, by its identity, never by what it looks like; nothing of a log is changed,
and nothing of the database but what the import adds (store/events.take).

A log is read whole under its writers' lock (shared), so no record in part is seen, then taken
into the database in one transaction from its cursor on: the row of imports for its path, which
is the import's alone, says how many of its first bytes were imported (byte_offset, always just
past a line feed) and what they were (hash, the sha256 of them). Its events go in in the log's
order, each new one with its place, each known one without a place given one, and the cursor is
moved in the same transaction: the cursor is never past an event the database does not have, and
a run stopped anywhere leaves it where it was. A log read before another import moved its cursor
past what was read is read again inside the transaction, and judged by those bytes. A log whose
imported bytes are no longer what they were (cut short, written over) is refused whole: the
record changed under the database, and a person says which is right.

A record is a line ending in a line feed; a line of blanks is none, though its bytes count; the
end of a log without a line feed is no record yet, and waits. A record that is no event stops its
log there: those before it are imported, nothing after it, and it is said with its line, its byte
and why, at every run until it is an event. Its identity is its own id, or, for a line from
before lines had one, line_id of its offset and bytes; its actor its own, or UNKNOWN_ACTOR.

What a run could not do is said in its Report, never as an event: it would be said again at every
start, and the cursors say what is in already. audit.lost is no business of the import's.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db, events

ID = re.compile(r"[0-9a-f]{32}")  # the whole of the id: fullmatch


@dataclass
class Report:
    files: int = 0  # logs read
    inserted: int = 0  # events new to the database
    placed: int = 0  # events it had with no place, whose place it learnt
    refused: list[str] = field(default_factory=list)  # a log, or the rest of one, not imported: why
    waiting: list[str] = field(default_factory=list)  # a last record not whole yet
    problem: str = ""  # what stopped the run: the database held, of schema 1 with events, unusable
    busy: bool = False  # held by another past every wait
    stale: bool = False  # of schema 1, with events: to be removed


class NotEvent(Exception):
    """A record that is no event of its log: why."""


class NotPlain(Exception):
    pass


def run(logs: Iterable[tuple[str, Path]], *, made_by: str, where: Path | None = None) -> Report:
    """Each (task id, path of its events.jsonl) into the database, in order; then the cursors of
    the paths that are gone go too (an event of theirs stays, and is known again by its identity
    should the file come back)."""
    where = where or db.path()
    report = Report()
    try:
        with db.available(where), closing(db.connect(where, made_by=made_by)) as conn:
            for task_id, log in logs:
                _log(conn, task_id, Path(os.path.abspath(log)), report)
            with db.transaction(conn):
                for (path,) in conn.execute("SELECT path FROM imports").fetchall():
                    if not os.path.lexists(path):
                        conn.execute("DELETE FROM imports WHERE path = ?", (path,))
    except db.StoreBusy:
        report.busy = True
        report.problem = (
            f"{where}: another vivibox is writing to it; the import tries again at the next start"
        )
    except db.StoreError as e:  # unusable, a newer vivibox's, of schema 1 with events
        report.stale = isinstance(e, db.StoreStale)
        report.problem = str(e)
    return report


def _log(conn, task_id: str, path: Path, report: Report) -> None:
    if (data := _snapshot(path, report)) is None:
        return
    report.files += 1
    with db.transaction(conn):
        cursor = conn.execute("SELECT hash, byte_offset FROM imports WHERE path = ?", (str(path),)).fetchone()
        start = cursor[1] if cursor else 0
        # Read before another import read more and moved the cursor on: read again, under the
        # database's lock, and judge by what the log holds now. A writer holds the log's lock and
        # never waits for the database while it does, so this waits for no one that waits.
        if len(data) < start and (data := _snapshot(path, report)) is None:
            return
        if cursor and (len(data) < start or _hash(data[:start]) != cursor[0]):
            why = "shorter" if len(data) < start else "rewritten"
            report.refused.append(
                f"{path}: not imported: its first {start} bytes are not the ones imported before ({why}); "
                "put the log back as it was, or remove vivibox.db with its -wal and -shm "
                "to import every log again"
            )
            return
        if (end := _records(conn, task_id, path, data, start, report)) != start:
            conn.execute(
                "INSERT INTO imports (path, hash, byte_offset) VALUES (?, ?, ?)"
                " ON CONFLICT (path) DO UPDATE SET hash = excluded.hash, byte_offset = excluded.byte_offset",
                (str(path), _hash(data[:end]), end),
            )


def _snapshot(path: Path, report: Report) -> bytes | None:
    """The log as _read gives it; None for one there is nothing to import from, said where it
    is refused (no plain file, unreadable) and not where it is not there (no event yet, or gone)."""
    try:
        return _read(path)
    except FileNotFoundError:
        return None
    except NotPlain:
        report.refused.append(f"{path}: not imported: not a plain file")
    except OSError as e:
        report.refused.append(f"{path}: not imported: {e.strerror or e}")
    return None


def _records(conn, task_id: str, path: Path, data: bytes, start: int, report: Report) -> int:
    """The records of data from start on into the database: where the import ends, just past
    the last line taken, or at the first byte of a record that is no event."""
    at, line = start, data.count(b"\n", 0, start) + 1
    while (end := data.find(b"\n", at)) != -1:
        raw = data[at:end]
        if raw.decode("utf-8", "surrogateescape").strip():
            try:
                found = events.take(conn, _event(raw, task_id, at), at)
            except (NotEvent, events.Misplaced) as e:
                report.refused.append(f"{path}:{line}: not imported from byte {at} on: {e}")
                return at
            report.inserted += found == "new"
            report.placed += found == "placed"
        at, line = end + 1, line + 1
    if (tail := data[at:]).strip():
        report.waiting.append(
            f"{path}: the last {len(tail)} bytes are not a whole record yet; imported once they are"
        )
    return at


def _event(raw: bytes, task_id: str, offset: int) -> dict:
    """The event a record of task_id's log at offset is, as store/events.insert takes one."""
    try:
        line = json.loads(raw.decode("utf-8", "surrogateescape"))
    except ValueError:
        raise NotEvent("not JSON") from None
    if not isinstance(line, dict):
        raise NotEvent("not a JSON object")
    if "ts" not in line:
        raise NotEvent("no ts")
    when = _time(line["ts"])
    if when is None:
        raise NotEvent("ts is not an ISO time")
    if when.tzinfo is None:
        raise NotEvent("ts has no time zone")
    if not isinstance(line.get("type"), str) or not line["type"]:
        raise NotEvent("no type")
    if not isinstance(line.get("task"), str):
        raise NotEvent("no task")
    if line["task"] != task_id:
        raise NotEvent(f"the event of {line['task']} in the log of {task_id}")
    if "data" not in line:
        raise NotEvent("no data")
    if not isinstance(line["data"], dict):
        raise NotEvent("data is not a JSON object")
    if "id" in line and not (isinstance(line["id"], str) and ID.fullmatch(line["id"])):
        raise NotEvent("id is not 32 hex digits")
    if "actor" in line and not (isinstance(line["actor"], str) and line["actor"].strip()):
        raise NotEvent("actor is not a name")
    return {
        "id": line.get("id") or events.line_id(offset, raw),
        "ts": line["ts"],
        "task": task_id,
        "actor": line.get("actor", events.UNKNOWN_ACTOR),
        "type": line["type"],
        "data": line["data"],
    }


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _read(path: Path) -> bytes:
    """The whole log, under its writers' lock, so no record in part; a link or anything but a
    plain file is NotPlain, never followed or waited on."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise NotPlain
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as found:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotPlain
        fcntl.flock(fd, fcntl.LOCK_SH)
        return found.read()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
