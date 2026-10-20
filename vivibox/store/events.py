"""K2: the events of the tasks, a row each in vivibox.db.

An event is written here first and onto its task's events.jsonl after (task.write_event), and
the log stays what every reader reads until a step moves it: nothing decides on these rows yet
but the export, `vivibox events <id> --json`, which gives them back in the log's shape.

    seq         the number of the fact "this event" among all the database learnt (knowledge.n)
    id          the event's identity, the same on its line: 32 hex digits drawn by the writer
                before anything is written; for a line from before lines had one, "line:" and the
                sha256 of its offset and its bytes (line_id)
    log_offset  the byte its line begins at in its task's events.jsonl (and in that log's copy in
                the archive, which begins with the same bytes); NULL while the database does not
                know it, and for an event with no line (one whose line was never written, audit.lost)
    placed_seq  the number of the fact "its line begins at log_offset", later than seq when the
                place was learnt after the event (by the writer once the line is written, or by
                the import), the same when the import learnt both at once
    ts      when, ISO 8601 with its offset (task.now)
    task    the task's id; NULL for an event of no single task (audit.lost)
    actor   who did it: you, supervisor, vivibox (the process that wrote it, task.acting_as);
            cli-agent:<tool> once something says an agent's CLI drove it
    type    the type as the log has it, unchanged: no table between the two. A type new from
            here on is named with dots: sandbox.*, resource.profile.changed, store.*, audit.lost
    data    the event's fields, JSON, at most DATA_LIMIT bytes (fitted)

The data of a type with dots carries, where it means something and never for its own sake:
source (where it came from: a role, you, host), target (where it went), ref (what it is about:
a commit, a handoff file, a log, a run_id, a domain), and what ties it to another event, named
after its thing (run_id, request_id, round) or the parent's seq. None is asked of every event;
the first writer of a type says which it carries here.

Every fact takes the next number of one counter, in the transaction that writes it, so the
numbers only go up, none is given twice, and one whose transaction rolls back is not used. What
the database knew of a task's history at number N is the events with seq <= N and placed_seq <= N,
in the order (log_offset, seq): the log's, never the clock's. Nothing written changes it: a later
event or place has a number above N, and an event and its place are never changed (the schema's
triggers). An event with no place by N is outside the history at N.

What could not be written for the database (another process held it past every wait, the file
cannot be opened or is no database) is counted for that database, in this process, and said
with the first event written there after: audit.lost{count}, of no task, in the same
transaction. The log has every event lost here, for the import to bring in. A database made
again where one was removed is no loss seen here: nothing failed to write, and the import
rebuilds what it held from the log.

A place that could not be written for the database (StoreBusy, StoreUnavailable) once the line
was is owed by this process, the event and its offset, and written before the next place it
writes there, each with a number of its own; a place owed by a process that ended is the
import's to write (store/importer.py), from the same line.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import closing
from pathlib import Path

from . import db

LOST = "audit.lost"
# The actor of a line from before lines had one: said as unknown, never guessed from the type.
UNKNOWN_ACTOR = "unknown-before-wave-b"

_lost: dict[Path, int] = {}  # events this process could not write, per database
_unplaced: dict[Path, dict[str, int]] = {}  # places it could not write, per database: id, offset
# Held over the whole of a write, from reading what is owed to settling it: two threads of one
# process (the view's) never say the same loss twice, nor write the same place.
_writing = threading.Lock()
MARK = "truncated"


class Misplaced(Exception):
    """An event the database has at another byte than the one its line is at: never changed."""

    def __init__(self, event_id: str, found: int, offset: int):
        self.event_id, self.found, self.offset = event_id, found, offset
        super().__init__(f"the event {event_id} is at byte {found} already")


def new_id() -> str:
    return uuid.uuid4().hex


def line_id(offset: int, record: bytes) -> str:
    """The identity of a line without one: its offset, in decimal, and its bytes but the line
    feed. The same in a log and in its copy, which begins with the same bytes, and in a database
    made again; two lines alike at two offsets are two events."""
    return "line:" + hashlib.sha256(str(offset).encode("ascii") + b"\n" + record).hexdigest()


def number(conn) -> int:
    """The next number of what the database knows, inside the caller's transaction."""
    return conn.execute("UPDATE knowledge SET n = n + 1 WHERE one = 1 RETURNING n").fetchall()[0][0]


def encoded(data: dict) -> str:
    """data as the database keeps it: JSON, its text as it is (not escaped to ASCII); a name the
    file system gave that is not UTF-8 as the escapes JSON has for it, read back as the same."""
    return json.dumps(data, ensure_ascii=False).encode("utf-8", "backslashreplace").decode("utf-8")


def _size(value) -> int:
    return len(encoded(value).encode("utf-8"))


def fitted(data: dict) -> dict:
    """data as it fits DATA_LIMIT bytes: as it is when it does. Else cut field by field, the
    largest first, until it fits: a text to as much of its start as fits, a list to as many of
    its first items, anything else, and a field of which not even an empty start fits, left out;
    and truncated: true, which takes the place of a field of the event's own of that name. With
    every field left out what is left is the mark alone, so the result always fits. The log
    keeps it whole."""
    if _size(data) <= db.DATA_LIMIT:
        return data
    out = {**data, MARK: True}
    for key in sorted((k for k in data if k != MARK), key=lambda k: _size(data[k]), reverse=True):
        if _size(out) <= db.DATA_LIMIT:
            break
        value = data[key]
        cut = isinstance(value, str | list | tuple)
        if cut:
            out[key] = value[:0]  # in its place: the fields keep their order
        if not cut or _size(out) > db.DATA_LIMIT:
            del out[key]
            continue
        kept, past = 0, len(value)  # the longest start that fits, by halving
        while kept < past:
            middle = (kept + past + 1) // 2
            if _size({**out, key: value[:middle]}) <= db.DATA_LIMIT:
                kept = middle
            else:
                past = middle - 1
        out[key] = value[:kept]
    return out


def insert(conn, entry: dict, *, log_offset: int | None = None) -> int:
    """One event ({id, ts, task, actor, type, data}), inside the caller's transaction, with the
    next number as its seq: its seq. Where its line's offset is known already (the import), the
    place is the same fact, of the same number. Data past DATA_LIMIT after fitted, or no id, is
    the code's fault, refused here and not by the schema."""
    if not entry.get("id"):
        raise ValueError(f"{entry['type']}: an event without an id")
    data = encoded(fitted(entry["data"]))
    if (size := len(data.encode("utf-8"))) > db.DATA_LIMIT:
        raise ValueError(f"{entry['type']}: {size} bytes of data past {db.DATA_LIMIT} after fitting")
    seq = number(conn)
    placed = None if log_offset is None else seq
    row = (
        seq,
        entry["id"],
        entry["ts"],
        entry["task"],
        entry["actor"],
        entry["type"],
        data,
        log_offset,
        placed,
    )
    conn.execute(
        "INSERT INTO events (seq, id, ts, task, actor, type, data, log_offset, placed_seq)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    return seq


def placed(conn, event_id: str, offset: int) -> bool | None:
    """The event's place, inside the caller's transaction: True where it is set now (with a
    number of its own), False where it was there already, None where there is no such event.
    Misplaced where the event is at another byte, which is never changed."""
    found = conn.execute("SELECT log_offset FROM events WHERE id = ?", (event_id,)).fetchone()
    if found is None:
        return None
    if found[0] is not None:
        if found[0] != offset:
            raise Misplaced(event_id, found[0], offset)
        return False
    conn.execute(
        "UPDATE events SET log_offset = ?, placed_seq = ? WHERE id = ? AND log_offset IS NULL",
        (offset, number(conn), event_id),
    )
    return True


def take(conn, entry: dict, offset: int) -> str:
    """An event of a log, at offset, inside the caller's transaction: "new" where the database
    had no such event (written with its place), "placed" where it had it without one, "known"
    where it had it there already. No number is used for what the database knew."""
    found = placed(conn, entry["id"], offset)
    if found is None:
        insert(conn, entry, log_offset=offset)
        return "new"
    return "placed" if found else "known"


def append(entry: dict, *, made_by: str, where: Path | None = None) -> int:
    """entry ({id, ts, task, actor, type, data}) into the database, in a transaction of its own,
    after the audit.lost that this process owes it: its seq.

    StoreBusy, StoreUnavailable: not written, and counted. StoreNewer, StoreStale: never written
    by this vivibox, so no loss to say. Anything else is the code's fault and raised as it is."""
    where = where or db.path()
    with _writing:
        owed = _lost.get(where, 0)
        try:
            with db.available(where), closing(db.connect(where, made_by=made_by)) as conn:
                seq = _write(conn, entry, owed)
        except (db.StoreBusy, db.StoreUnavailable):
            _lost[where] = owed + 1
            raise
        _lost.pop(where, None)
        return seq


def _write(conn, entry: dict, owed: int) -> int:
    """The audit.lost owed, then the event, in one transaction: both or neither. The loss has an
    identity of its own, and no line."""
    with db.transaction(conn):
        if owed:
            loss = {"id": new_id(), "ts": entry["ts"], "task": None, "actor": entry["actor"], "type": LOST}
            insert(conn, {**loss, "data": {"count": owed}})
        return insert(conn, entry)


def place(event_id: str, offset: int, *, made_by: str, where: Path | None = None) -> None:
    """The event's line begins at offset: written for the database in a transaction of its own,
    after the places this process owes it, each a fact of its own number. One the database has
    no event for is let go (the database was made again: the import brings the event in).

    StoreBusy, StoreUnavailable: owed, with the event's offset, and raised. Misplaced: raised,
    and that place never owed again."""
    where = where or db.path()
    with _writing:
        owed = {**_unplaced.get(where, {}), event_id: offset}
        try:
            with (
                db.available(where),
                closing(db.connect(where, made_by=made_by)) as conn,
                db.transaction(conn),
            ):
                for each, at in owed.items():
                    placed(conn, each, at)
        except (db.StoreBusy, db.StoreUnavailable):
            _unplaced[where] = owed
            raise
        except Misplaced as e:
            del owed[e.event_id]  # the rest rolled back with it, and still owed
            if owed:
                _unplaced[where] = owed
            else:
                _unplaced.pop(where, None)
            raise
        _unplaced.pop(where, None)


def of_task(task: str, where: Path | None = None) -> list[dict]:
    """The task's events in its log's order, (log_offset, seq), and those with no place after
    them: each as its line has it, with the id and actor the writer gave it, without those of a
    line that had none ({ts, task, type, data}). A newer vivibox's database is read too."""
    where = where or db.path()
    with db.available(where), closing(db.read(where)) as conn:
        if not db.has_events(conn, where):
            return []
        found = conn.execute(
            "SELECT id, ts, task, actor, type, data FROM events WHERE task = ?"
            " ORDER BY log_offset IS NULL, log_offset, seq",
            (task,),
        )
        return [_line(*row) for row in found]


def _line(event_id: str, ts: str, task: str, actor: str, kind: str, data: str) -> dict:
    line = {"ts": ts, "task": task, "type": kind, "data": json.loads(data)}
    return line if event_id.startswith("line:") else {**line, "id": event_id, "actor": actor}
