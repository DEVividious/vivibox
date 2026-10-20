"""K2: the events of the tasks, a row each in vivibox.db, numbered by seq in the order written.

An event is written here first and onto its task's events.jsonl after (task.write_event), and
the log stays what every reader reads until a step moves it: nothing decides on these rows yet
but the export, `vivibox events <id> --json`, which gives them back in the log's shape.

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

What could not be written for the database (another process held it past every wait, the file
cannot be opened or is no database) is counted for that database, in this process, and said
with the first event written there after: audit.lost{count}, of no task, in the same
transaction. The log has every event lost here, for the import to bring in.
"""

from __future__ import annotations

import json
import threading
from contextlib import closing
from pathlib import Path

from . import db

LOST = "audit.lost"

_lost: dict[Path, int] = {}  # events this process could not write, per database
_counting = threading.Lock()


def encoded(data: dict) -> str:
    """data as the database keeps it: JSON, its text as it is (not escaped to ASCII); a name the
    file system gave that is not UTF-8 as the escapes JSON has for it, read back as the same."""
    return json.dumps(data, ensure_ascii=False).encode("utf-8", "backslashreplace").decode("utf-8")


def _size(value) -> int:
    return len(encoded(value).encode("utf-8"))


def fitted(data: dict) -> dict:
    """data as it fits DATA_LIMIT bytes: as it is when it does. Else cut field by field, the
    largest first, until it fits: a text to as much of its start as fits, a list to as many of
    its first items, anything else left out; and truncated: true. The log keeps it whole."""
    if _size(data) <= db.DATA_LIMIT:
        return data
    out = {**data, "truncated": True}
    for key in sorted(data, key=lambda k: _size(data[k]), reverse=True):
        if _size(out) <= db.DATA_LIMIT:
            break
        value = data[key]
        if isinstance(value, str | list | tuple):
            kept, past = 0, len(value)  # the longest start that fits, by halving
            while kept < past:
                middle = (kept + past + 1) // 2
                if _size({**out, key: value[:middle]}) <= db.DATA_LIMIT:
                    kept = middle
                else:
                    past = middle - 1
            out[key] = value[:kept]
        else:
            del out[key]
    return out


def insert(conn, entry: dict, actor: str) -> int:
    """One event, inside the caller's transaction: its seq."""
    row = (entry["ts"], entry["task"], actor, entry["type"], encoded(fitted(entry["data"])))
    found = conn.execute("INSERT INTO events (ts, task, actor, type, data) VALUES (?, ?, ?, ?, ?)", row)
    return found.lastrowid


def append(entry: dict, *, actor: str, made_by: str, where: Path | None = None) -> int:
    """entry ({ts, task, type, data}, as the log has it) by actor into the database, in a
    transaction of its own, after the audit.lost that this process owes it: its seq.

    StoreBusy, StoreUnavailable: not written, and counted. StoreNewer: never written by this
    vivibox, so no loss to say. Anything else is the code's fault and raised as it is."""
    where = where or db.path()
    with _counting:
        owed = _lost.get(where, 0)
    try:
        with db.available(where), closing(db.connect(where, made_by=made_by)) as conn, db.transaction(conn):
            if owed:
                lost = {"ts": entry["ts"], "task": None, "type": LOST, "data": {"count": owed}}
                insert(conn, lost, actor)
            seq = insert(conn, entry, actor)
    except (db.StoreBusy, db.StoreUnavailable):
        with _counting:
            _lost[where] = _lost.get(where, 0) + 1
        raise
    if owed:
        with _counting:
            _lost[where] -= owed
    return seq


def of_task(task: str, where: Path | None = None) -> list[dict]:
    """The task's events in the order written, each as the log has it: {ts, task, type, data}.
    A newer vivibox's database is read too."""
    where = where or db.path()
    with db.available(where), closing(db.read(where)) as conn:
        found = conn.execute("SELECT ts, task, type, data FROM events WHERE task = ? ORDER BY seq", (task,))
        return [{"ts": ts, "task": t, "type": kind, "data": json.loads(data)} for ts, t, kind, data in found]
