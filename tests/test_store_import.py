"""The import of the tasks' events.jsonl into vivibox.db (plan P1.3, ADR-0051): by each event's
identity and a cursor of bytes per file, never by what two events look like; every log stays as
it is and stays the record every reader reads."""

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vivibox import actions, cli, task
from vivibox.store import db, events, importer
from vivibox.task import Task

MADE_BY = "0.2.3.dev1+gabc1234"
TS = "2026-10-20T10:00:00.000+00:00"


@pytest.fixture
def where(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(task, "_actor", "you")
    return tmp_path / "data" / "vivibox" / "vivibox.db"


@pytest.fixture
def one(tmp_path) -> Task:
    t = Task(tmp_path / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    return t


def logged(t: Task) -> Path:
    return t.meta / "events.jsonl"


def old(task_id: str = "demo-1", type_: str = "goal", ts: str = TS, **data) -> bytes:
    """A line as a vivibox before identities wrote it: no id, no actor."""
    return task.record({"ts": ts, "task": task_id, "type": type_, "data": data})


def run(t_or_logs, where: Path) -> importer.Report:
    logs = t_or_logs if isinstance(t_or_logs, list) else [(t_or_logs.id, logged(t_or_logs))]
    return importer.run(logs, made_by=MADE_BY, where=where)


def table(where: Path) -> list[tuple]:
    """(seq, id, actor, type, data, log_offset, placed_seq) of every event."""
    with sqlite3.connect(where) as conn:
        found = conn.execute(
            "SELECT seq, id, actor, type, data, log_offset, placed_seq FROM events ORDER BY seq"
        ).fetchall()
    return [(s, i, a, t, json.loads(d), o, p) for s, i, a, t, d, o, p in found]


def cursors(where: Path) -> list[tuple]:
    with sqlite3.connect(where) as conn:
        return conn.execute("SELECT path, hash, byte_offset FROM imports ORDER BY path").fetchall()


def known(where: Path) -> int:
    with sqlite3.connect(where) as conn:
        return conn.execute("SELECT n FROM knowledge").fetchone()[0]


def line_id(offset: int, raw: bytes) -> str:
    return "line:" + hashlib.sha256(str(offset).encode() + b"\n" + raw).hexdigest()


# --- identity: the line's own, or one worked out from where it is and what it holds. ---


def test_a_line_without_an_id_is_named_by_its_offset_and_its_bytes(where, one):
    first, second = old(goal="a"), old("demo-1", "box")
    logged(one).write_bytes(first + second)
    report = run(one, where)
    assert (report.files, report.inserted, report.placed, report.refused) == (1, 2, 0, [])
    assert table(where) == [
        (1, line_id(0, first[:-1]), events.UNKNOWN_ACTOR, "goal", {"goal": "a"}, 0, 1),
        (2, line_id(len(first), second[:-1]), "unknown-before-wave-b", "box", {}, len(first), 2),
    ]
    assert cursors(where) == [
        (str(logged(one)), hashlib.sha256(first + second).hexdigest(), len(first + second))
    ]


def test_two_identical_lines_are_two_events(where, one):
    same = old(goal="twice")
    logged(one).write_bytes(same + same)
    run(one, where)
    ids = [r[1] for r in table(where)]
    assert ids == [line_id(0, same[:-1]), line_id(len(same), same[:-1])] and ids[0] != ids[1]


def test_a_blank_line_is_no_record_and_its_bytes_count(where, one):
    first, second = old(goal="a"), old(goal="b")
    logged(one).write_bytes(first + b"  \t\n\n" + second)
    run(one, where)
    offset = len(first) + 5
    assert [(r[1], r[5]) for r in table(where)] == [
        (line_id(0, first[:-1]), 0),
        (line_id(offset, second[:-1]), offset),
    ]


def test_a_log_and_its_copy_in_the_archive_are_the_same_events(where, one, tmp_path):
    logged(one).write_bytes(old(goal="a") + old(goal="b"))
    archived = tmp_path / "archive" / "demo-1" / "events.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(logged(one).read_bytes() + old("demo-1", "rules_left", problem="x"))
    first = run([("demo-1", logged(one))], where)
    then = run([("demo-1", archived)], where)
    assert (first.inserted, then.inserted, then.placed) == (2, 1, 0), "the copy's two are known already"
    assert len(table(where)) == 3
    assert [c[0] for c in cursors(where)] == sorted([str(logged(one)), str(archived)]), "a cursor each"


def test_a_line_with_an_id_keeps_it_and_its_actor(where, one):
    entry = {
        "ts": TS,
        "task": "demo-1",
        "type": "goal",
        "data": {"goal": "a"},
        "id": "f" * 32,
        "actor": "supervisor",
    }
    logged(one).write_bytes(task.record(entry))
    run(one, where)
    assert table(where) == [(1, "f" * 32, "supervisor", "goal", {"goal": "a"}, 0, 1)]


def test_the_data_is_cut_to_fit_as_the_writer_cuts_it(where, one):
    logged(one).write_bytes(old("demo-1", "paused", problem="x" * 9000))
    run(one, where)
    [row] = table(where)
    assert (
        row[4]["truncated"] is True and len(json.dumps(row[4], ensure_ascii=False).encode()) <= db.DATA_LIMIT
    )
    assert json.loads(logged(one).read_bytes())["data"] == {"problem": "x" * 9000}, "the log is left whole"


def test_data_still_past_the_limit_is_the_code_s_fault_not_a_skipped_record(where, one, monkeypatch):
    logged(one).write_bytes(old(goal="a") + old("demo-1", "paused", problem="x" * 9000))
    monkeypatch.setattr(events, "fitted", lambda data: data)
    with pytest.raises(ValueError, match="8192"):
        run(one, where)
    assert table(where) == [] and cursors(where) == [], "the file's transaction rolled back whole"


# --- the four states of an event, by its identity. ---


def test_an_event_in_the_database_and_on_the_log_is_imported_as_nothing(where, one):
    for n in range(3):
        one.event("turn_retry", wait=n)
    before, number = table(where), known(where)
    report = run(one, where)
    assert (report.inserted, report.placed) == (0, 0)
    assert table(where) == before and known(where) == number, "no fact, no number"


def test_an_event_only_on_the_log_comes_in_with_its_id_and_actor(where, one, monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 20)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    one.event("goal", goal="before")
    holder = db.connect(where, made_by="other")
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(task, "_actor", "supervisor")
    one.event("goal", goal="held out")
    holder.execute("ROLLBACK")
    holder.close()
    written = [json.loads(line) for line in logged(one).read_bytes().splitlines()]
    report = run(one, where)
    assert (report.inserted, report.placed) == (1, 0)
    offset = logged(one).read_bytes().index(b"\n") + 1
    assert table(where)[-1] == (3, written[1]["id"], "supervisor", "goal", {"goal": "held out"}, offset, 3)


def full(path, record):
    raise OSError(28, "No space left on device")


def test_an_event_only_in_the_database_is_left_as_it_is_and_out_of_the_history(where, one, monkeypatch):
    one.event("goal", goal="logged")
    with monkeypatch.context() as m, pytest.raises(OSError):
        m.setattr(task, "append_record", full)
        one.event("goal", goal="database only")
    before = table(where)
    report = run(one, where)
    assert (report.inserted, report.placed) == (0, 0) and table(where) == before
    assert before[-1][5:] == (None, None), "no place in the log: no line was ever written"
    assert len(logged(one).read_bytes().splitlines()) == 1, "the import writes no line"


def test_an_event_whose_place_was_owed_by_a_process_that_died_is_placed_by_the_import(
    where, one, monkeypatch
):
    held = []
    append = task.append_record

    def then_held(path, record):
        offset = append(path, record)
        monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 20)
        monkeypatch.setattr(db.time, "sleep", lambda s: None)
        held.append(db.connect(where, made_by="other"))
        held[0].execute("BEGIN IMMEDIATE")
        return offset

    monkeypatch.setattr(task, "append_record", then_held)
    one.event("goal", goal="unplaced")
    held[0].execute("ROLLBACK")
    events._unplaced.clear()  # the process is gone, and what it owed with it
    [(seq, eid, *_rest, offset, placed)] = table(where)
    assert (seq, offset, placed) == (1, None, None)
    report = run(one, where)
    assert (report.inserted, report.placed) == (0, 1)
    assert table(where)[0][5:] == (0, 2), "placed with a number of its own, after the event's"


def test_a_database_made_again_is_rebuilt_from_the_logs_with_the_same_identities(where, one, tmp_path):
    logged(one).write_bytes(old(goal="before identities") + old(goal="before identities"))
    one.event("goal", goal="with one")
    run(one, where)
    first = sorted((r[1], r[2], r[3], json.dumps(r[4]), r[5]) for r in table(where))
    for name in ("vivibox.db", "vivibox.db-wal", "vivibox.db-shm"):
        (where.parent / name).unlink(missing_ok=True)
    report = run(one, where)
    assert report.inserted == 3
    assert sorted((r[1], r[2], r[3], json.dumps(r[4]), r[5]) for r in table(where)) == first
    assert [r[0] for r in table(where)] == [1, 2, 3] and [r[6] for r in table(where)] == [1, 2, 3]


def test_the_loss_said_by_a_writer_is_neither_read_nor_made_by_the_import(where, one, monkeypatch):
    events._lost[where] = 2
    one.event("goal", goal="after a loss")
    run(one, where)
    losses = [r for r in table(where) if r[3] == events.LOST]
    assert [(r[2], r[4], r[5], r[6]) for r in losses] == [("you", {"count": 2}, None, None)]
    assert len(losses[0][1]) == 32


def test_a_place_the_log_contradicts_stops_the_file_there(where, one):
    entry = {"ts": TS, "task": "demo-1", "type": "goal", "data": {}, "id": "a" * 32, "actor": "you"}
    first = old(goal="first")
    logged(one).write_bytes(first + task.record(entry) + old(goal="after"))
    with closing_conn(where) as conn, db.transaction(conn):
        events.insert(conn, entry, log_offset=7)
    report = run(one, where)
    assert report.refused == [
        f"{logged(one)}:2: not imported from byte {len(first)} on: the event {'a' * 32} is at byte 7 already"
    ]
    assert [r[1] for r in table(where)] == ["a" * 32, line_id(0, first[:-1])]
    assert cursors(where)[0][2] == len(first)


def closing_conn(where: Path):
    from contextlib import closing

    return closing(db.connect(where, made_by=MADE_BY))


# --- the cursor: a prefix of the file, the import's alone, moved with the rows it covers. ---


def test_a_second_run_over_the_same_files_does_nothing(where, one):
    logged(one).write_bytes(old(goal="a") + old(goal="b"))
    run(one, where)
    before, marks, number = table(where), cursors(where), known(where)
    report = run(one, where)
    assert (report.inserted, report.placed, report.refused, report.waiting) == (0, 0, [], [])
    assert (table(where), cursors(where), known(where)) == (before, marks, number)


def test_what_was_appended_since_is_imported_from_the_cursor_on(where, one):
    logged(one).write_bytes(old(goal="a"))
    run(one, where)
    with logged(one).open("ab") as f:
        f.write(old(goal="b"))
    assert run(one, where).inserted == 1
    assert [r[4] for r in table(where)] == [{"goal": "a"}, {"goal": "b"}]
    assert cursors(where)[0][2] == len(logged(one).read_bytes())


@pytest.mark.parametrize(
    "change, why",
    [
        (lambda data: b"[" + data[1:], "rewritten"),
        (lambda data: data[:10], "shorter"),
    ],
)
def test_a_log_changed_under_the_import_is_refused_and_nothing_moves(where, one, change, why):
    logged(one).write_bytes(old(goal="a") + old(goal="b"))
    run(one, where)
    before, marks = table(where), cursors(where)
    logged(one).write_bytes(change(logged(one).read_bytes()) + old(goal="c"))
    report = run(one, where)
    said = (
        f"{logged(one)}: not imported: its first {marks[0][2]} bytes are not the ones imported before "
        f"({why}); put the log back as it was, or remove vivibox.db with its -wal and -shm to import "
        "every log again"
    )
    assert report.refused == [said] and report.inserted == 0
    assert (table(where), cursors(where)) == (before, marks)


def test_a_last_line_not_yet_ended_waits(where, one):
    tail = b'{"ts": "' + TS.encode()
    logged(one).write_bytes(old(goal="a") + tail)
    report = run(one, where)
    assert report.inserted == 1 and report.refused == []
    said = f"{logged(one)}: the last {len(tail)} bytes are not a whole record yet; imported once they are"
    assert report.waiting == [said]
    assert cursors(where)[0][2] == len(old(goal="a"))
    with logged(one).open("ab") as f:
        f.write(b'", "task": "demo-1", "type": "box", "data": {}}\n')
    assert run(one, where).inserted == 1 and len(table(where)) == 2


def changed(**fields) -> bytes:
    """A good record of demo-1 with fields changed, a field of None left out."""
    record = {"ts": TS, "task": "demo-1", "type": "x", "data": {}, **fields}
    return json.dumps({k: v for k, v in record.items() if v is not None}).encode()


@pytest.mark.parametrize(
    "bad, reason",
    [
        (b"not json", "not JSON"),
        (b"[1, 2]", "not a JSON object"),
        (changed(ts=None), "no ts"),
        (changed(ts="2026-10-20T10:00:00"), "ts has no time zone"),
        (changed(ts="yesterday"), "ts is not an ISO time"),
        (changed(ts=5), "ts is not an ISO time"),
        (changed(type=None), "no type"),
        (changed(type=""), "no type"),
        (changed(task=None), "no task"),
        (changed(task="demo-2"), "the event of demo-2 in the log of demo-1"),
        (changed(data=None), "no data"),
        (changed(data=[1]), "data is not a JSON object"),
        (changed(id="line:ab"), "id is not 32 hex digits"),
        (changed(id="A" * 32), "id is not 32 hex digits"),
        (changed(actor=""), "actor is not a name"),
        (changed(actor=3), "actor is not a name"),
    ],
)
def test_a_record_that_is_no_event_stops_its_file_on_itself(where, one, bad, reason):
    first, after = old(goal="a"), old(goal="after")
    logged(one).write_bytes(first + b"\n" + bad + b"\n" + after)
    report = run(one, where)
    offset = len(first) + 1
    assert report.refused == [f"{logged(one)}:3: not imported from byte {offset} on: {reason}"]
    assert [r[4] for r in table(where)] == [{"goal": "a"}], "the records before it, and none after"
    assert cursors(where) == [(str(logged(one)), hashlib.sha256(first + b"\n").hexdigest(), offset)]
    assert run(one, where).refused == report.refused, "said again until the line is an event"


def test_one_file_refused_leaves_the_others_imported(where, one, tmp_path):
    other = Task(tmp_path / "tasks" / "demo-2")
    other.meta.mkdir(parents=True)
    logged(other).write_bytes(old("demo-2", goal="b"))
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_bytes(old(goal="a"))
    logged(one).symlink_to(elsewhere)
    report = run([("demo-1", logged(one)), ("demo-2", logged(other))], where)
    assert report.refused == [f"{logged(one)}: not imported: not a plain file"]
    assert [r[4] for r in table(where)] == [{"goal": "b"}]


def test_a_task_with_no_log_yet_is_nothing_to_say(where, one):
    report = run(one, where)
    assert (report.files, report.refused) == (0, []) and table(where) == []


def test_the_cursor_of_a_file_that_is_gone_goes_too(where, one, tmp_path):
    logged(one).write_bytes(old(goal="a"))
    run(one, where)
    logged(one).unlink()
    run([], where)
    assert cursors(where) == [] and len(table(where)) == 1, "its events stay"


def test_the_import_touches_no_task_row(where, one):
    logged(one).write_bytes(old(goal="a"))
    run(one, where)
    with sqlite3.connect(where) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


# --- the order of the history: the log's, never the clock's. ---


def test_the_history_is_in_the_log_s_order_whatever_the_times_say(where, one):
    """Process A took its time first, B wrote its line first: the history is B, A, as the log
    and every reader of it have them."""
    b, a = (
        old(type_="b", ts="2026-10-20T10:00:01.000+00:00"),
        old(type_="a", ts="2026-10-20T10:00:00.000+00:00"),
    )
    logged(one).write_bytes(b + a)
    run(one, where)
    with sqlite3.connect(where) as conn:
        order = conn.execute(
            "SELECT type FROM events WHERE log_offset IS NOT NULL ORDER BY log_offset, seq"
        ).fetchall()
    assert order == [("b",), ("a",)]


def test_an_event_imported_after_later_ones_takes_its_place_in_the_log(where, one, monkeypatch):
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 20)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    holder = db.connect(where, made_by="other")
    holder.execute("BEGIN IMMEDIATE")
    one.event("goal", goal="first, held out")
    holder.execute("ROLLBACK")
    holder.close()
    one.event("goal", goal="second")
    known_before = known(where)
    view_before = view_at(where, known_before)
    run(one, where)
    with sqlite3.connect(where) as conn:
        order = conn.execute(
            "SELECT seq, data FROM events WHERE task = 'demo-1' ORDER BY log_offset, seq"
        ).fetchall()
    assert [json.loads(d)["goal"] for _, d in order] == ["first, held out", "second"]
    assert order[0][0] > order[1][0], "known later, first in the history"
    assert view_at(where, known_before) == view_before, "what was known before the import is unchanged"


def view_at(where: Path, n: int) -> tuple[list, int]:
    with sqlite3.connect(where) as conn:
        placed = conn.execute(
            "SELECT id FROM events WHERE seq <= ? AND placed_seq <= ? ORDER BY log_offset, seq", (n, n)
        ).fetchall()
        outside = conn.execute(
            "SELECT count(*) FROM events WHERE seq <= ? AND (placed_seq IS NULL OR placed_seq > ?)", (n, n)
        ).fetchone()[0]
    return placed, outside


# --- what stops a run, and runs at once. ---


def test_a_stale_database_is_said_and_left_as_it_is(where, one):
    from test_store_db import made_v1, v1_of

    made_v1(where, rows=1)
    before = v1_of(where)
    logged(one).write_bytes(old(goal="a"))
    report = run(one, where)
    assert report.problem.startswith(
        f"{where} is a copy of the events from before each had an identity (1 event)"
    )
    assert report.inserted == 0 and v1_of(where) == before


def test_a_database_held_past_every_wait_is_said_and_nothing_changes(where, one, monkeypatch):
    logged(one).write_bytes(old(goal="a"))
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 20)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    holder = db.connect(where, made_by="other")
    holder.execute("BEGIN IMMEDIATE")
    report = run(one, where)
    holder.execute("ROLLBACK")
    assert report.busy and report.problem == (
        f"{where}: another vivibox is writing to it; the import tries again at the next start"
    )
    assert table(where) == [] and cursors(where) == []


def importing(where: Path, log: Path, body: str = "") -> subprocess.Popen:
    code = (
        textwrap.dedent(
            """
        import sys, time
        from pathlib import Path
        from vivibox.store import events, importer
        """
        )
        + textwrap.dedent(body)
        + textwrap.dedent(
            f"""
        report = importer.run([("demo-1", Path({str(log)!r}))], made_by="child", where=Path({str(where)!r}))
        print(report.inserted, flush=True)
        """
        )
    )
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def test_an_import_killed_part_way_leaves_neither_rows_nor_cursor_and_the_next_finishes(where, one):
    logged(one).write_bytes(b"".join(old(goal=str(n)) for n in range(20)))
    db.connect(where, made_by=MADE_BY).close()
    child = importing(
        where,
        logged(one),
        """
        take = events.take
        def slow(conn, entry, offset):
            found = take(conn, entry, offset)
            if offset > 500:
                print("holding", flush=True)
                time.sleep(60)
            return found
        events.take = slow
        """,
    )
    assert child.stdout.readline().strip() == "holding"
    os.kill(child.pid, signal.SIGKILL)
    assert child.wait(timeout=10) == -signal.SIGKILL
    assert table(where) == [] and cursors(where) == [] and known(where) == 0
    assert run(one, where).inserted == 20
    assert len(table(where)) == 20 and cursors(where)[0][2] == len(logged(one).read_bytes())


def test_two_imports_at_once_bring_each_event_in_once(where, one):
    logged(one).write_bytes(b"".join(old(goal=str(n)) for n in range(200)))
    db.connect(where, made_by=MADE_BY).close()
    children = [importing(where, logged(one)) for _ in range(2)]
    said = [int(c.communicate(timeout=60)[0]) for c in children]
    assert sum(said) == 200, said
    assert len(table(where)) == 200 and len({r[1] for r in table(where)}) == 200


# --- where it runs: at every start, and when asked. ---


def test_vivibox_import_says_what_it_brought_in_and_what_it_refused(env, monkeypatch, capsys):
    monkeypatch.setattr(task, "_actor", "you")
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    logged(t).write_bytes(old(goal="a") + b"oops\n")
    archived = actions.archive_path("demo-3") / "events.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(old("demo-3", goal="b"))
    assert cli.main(["import"]) == 1
    out, err = capsys.readouterr()
    assert out == "Imported 2 events from 2 logs into vivibox.db.\n"
    assert err == f"vivibox: {logged(t)}:2: not imported from byte {len(old(goal='a'))} on: not JSON\n"
    logged(t).write_bytes(old(goal="a") + old(goal="fixed"))
    assert cli.main(["import"]) == 0
    assert capsys.readouterr() == ("Imported 1 event from 2 logs into vivibox.db.\n", "")


def test_vivibox_import_with_nothing_new_says_so(env, monkeypatch, capsys):
    monkeypatch.setattr(task, "_actor", "you")
    assert cli.main(["import"]) == 0
    assert capsys.readouterr().out == "Imported 0 events from 0 logs into vivibox.db.\n"


def test_every_command_imports_first_and_says_only_what_was_refused(env, monkeypatch, capsys):
    monkeypatch.setattr(task, "_actor", "you")
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    logged(t).write_bytes(old(goal="a") + b'{"ts": ')
    assert cli.main(["stats"]) == 0
    assert capsys.readouterr().err == "", "a record still being written is nothing to say"
    where = env / "data" / "vivibox" / "vivibox.db"
    assert [r[4] for r in table(where)] == [{"goal": "a"}]
    logged(t).write_bytes(old(goal="a") + b"oops\n")
    assert cli.main(["stats"]) == 0, "the command goes on"
    said = f"vivibox: {logged(t)}:2: not imported from byte {len(old(goal='a'))} on: not JSON\n"
    assert capsys.readouterr().err == said


def test_the_export_after_the_import_is_the_log_in_its_order(env, monkeypatch, capsysbinary):
    monkeypatch.setattr(task, "_actor", "you")
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    logged(t).write_bytes(old(goal="before identities") + old("demo-1", "box"))
    t.event("goal", goal="with one")
    assert cli.main(["events", "demo-1", "--json"]) == 0
    assert capsysbinary.readouterr().out == logged(t).read_bytes()


def test_an_event_without_a_place_comes_last_in_the_export(env, monkeypatch, capsysbinary):
    monkeypatch.setattr(task, "_actor", "you")
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    t.event("goal", goal="logged")
    with monkeypatch.context() as m, pytest.raises(OSError):
        m.setattr(task, "append_record", full)
        t.event("goal", goal="database only")
    assert cli.main(["events", "demo-1", "--json"]) == 0
    lines = [json.loads(line) for line in capsysbinary.readouterr().out.splitlines()]
    assert [e["data"]["goal"] for e in lines] == ["logged", "database only"]


def test_the_view_says_what_the_import_at_its_start_refused_and_nothing_else(env, monkeypatch):
    from test_tui import run as on_the_view

    from vivibox import tui

    said = []

    def notify(app, text, **kw):
        said.append((text, kw.get("severity")))

    monkeypatch.setattr(tui.Vivibox, "notify", notify)
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    logged(t).write_bytes(old(goal="a") + b'{"ts": ')

    async def nothing(app, pilot):
        pass

    on_the_view(nothing)
    assert not [s for s in said if "import" in s[0]], "a record still being written is no news"
    logged(t).write_bytes(old(goal="a") + b"oops\n")
    on_the_view(nothing)
    refused = f"{logged(t)}:2: not imported from byte {len(old(goal='a'))} on: not JSON"
    assert (f"{refused}\nvivibox import says it again in a terminal.", "warning") in said
