"""K2: every event a task writes goes into vivibox.db first, then onto its events.jsonl, which
stays what every reader reads (plan P1.2). The database is a copy nothing decides on yet: one
that cannot be written leaves the event out of it, never out of the log, and never stops the
writer; one the code gets wrong does."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vivibox import actions, review, task
from vivibox.store import db, events
from vivibox.task import Task

TS = "2026-10-20T10:00:00.000+00:00"


@pytest.fixture
def where(tmp_path, monkeypatch) -> Path:
    """vivibox.db of this test alone, and the process acting as the person."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(task, "_actor", "you")
    return tmp_path / "data" / "vivibox" / "vivibox.db"


@pytest.fixture
def one(tmp_path) -> Task:
    t = Task(tmp_path / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    return t


def rows(where: Path) -> list[tuple]:
    with sqlite3.connect(where) as conn:
        return conn.execute("SELECT seq, ts, task, actor, type, data FROM events ORDER BY seq").fetchall()


def log(t: Task) -> list[dict]:
    return [json.loads(line) for line in (t.meta / "events.jsonl").read_bytes().splitlines()]


def test_an_event_goes_into_the_database_as_it_goes_onto_the_log(where, one):
    one.event("gate", iteration=2, passed=False, failed_commands=["npm test"], seconds=1.5, why=None)
    [written] = log(one)
    [(seq, ts, task_id, actor, type_, data)] = rows(where)
    assert (seq, ts, task_id, actor, type_) == (1, written["ts"], "demo-1", "you", "gate")
    assert json.loads(data) == written["data"]
    assert written == {
        "ts": ts,
        "task": "demo-1",
        "type": "gate",
        "data": {
            "iteration": 2,
            "passed": False,
            "failed_commands": ["npm test"],
            "seconds": 1.5,
            "why": None,
        },  # fmt: skip
    }


def test_the_log_is_written_as_it_always_was(where, one):
    """The line of events.jsonl is record()'s, byte for byte: the database changes nothing in it."""
    one.event("paused", problem="a\u2028b \x85 ż")
    line = (one.meta / "events.jsonl").read_bytes()
    entry = json.loads(line)
    assert line == task.record(entry)
    assert entry["data"] == {"problem": "a\u2028b \x85 ż"}


def test_events_keep_their_order_in_both(where, one):
    for n in range(5):
        one.event("turn_retry", wait=n)
    assert [e["data"]["wait"] for e in log(one)] == [0, 1, 2, 3, 4]
    found = rows(where)
    assert [r[0] for r in found] == [1, 2, 3, 4, 5]
    assert [json.loads(r[5])["wait"] for r in found] == [0, 1, 2, 3, 4]
    assert [r[1] for r in found] == [e["ts"] for e in log(one)]


def test_each_task_s_events_are_its_own(where, tmp_path, one):
    other = Task(tmp_path / "tasks" / "demo-2")
    other.meta.mkdir(parents=True)
    one.event("goal", goal="one")
    other.event("goal", goal="two")
    assert [(r[2], json.loads(r[5])["goal"]) for r in rows(where)] == [("demo-1", "one"), ("demo-2", "two")]


def test_the_actor_is_the_process_s(where, one, monkeypatch):
    for actor in ("supervisor", "vivibox", "you"):
        task.acting_as(actor)
        one.event("box")
    assert [r[3] for r in rows(where)] == ["supervisor", "vivibox", "you"]
    with pytest.raises(ValueError, match="no actor"):
        task.acting_as("cli-agent:claude-code")


def test_the_command_line_acts_as_you_and_the_supervisor_as_itself(env, monkeypatch):
    from vivibox import cli

    monkeypatch.setattr(task, "_actor", "vivibox")
    assert cli.main(["stats"]) == 0
    assert task._actor == "you"
    assert cli.main(["supervise", "nobody-1"]) == 1
    assert task._actor == "supervisor", "said before the task is read: every event of it is the supervisor's"


def test_an_event_of_a_task_that_is_gone_goes_the_same_way(where):
    """review.note writes onto the archive's log: the one write of an event, into the database
    first."""
    archive = actions.archive_path("demo-3")
    archive.mkdir(parents=True)
    review.note("demo-3", "rules_left", problem="left")
    [written] = [json.loads(line) for line in (archive / "events.jsonl").read_bytes().splitlines()]
    [(_, ts, task_id, actor, type_, data)] = rows(where)
    assert (ts, task_id, actor, type_, json.loads(data)) == (
        written["ts"], "demo-3", "you", "rules_left", {"problem": "left"}
    )  # fmt: skip


# --- data past 8192 bytes: cut in the database, whole on the log, and said to be cut. ---


def test_data_that_fits_is_kept_whole(where, one):
    one.event("paused", problem="ż" * 4000)  # 8000 bytes of text: fits
    assert json.loads(rows(where)[0][5]) == {"problem": "ż" * 4000}


def test_the_limit_is_of_bytes_not_characters(where, one):
    """5000 characters, 10000 bytes: cut to what fits in bytes, each character whole."""
    one.event("paused", problem="ż" * 5000)
    data = rows(where)[0][5]
    assert len(data.encode()) <= db.DATA_LIMIT < len(data.encode()) + 2
    kept = json.loads(data)
    assert kept["truncated"] is True and set(kept["problem"]) == {"ż"}
    assert log(one)[0]["data"] == {"problem": "ż" * 5000}, "the log keeps all of it"


def test_the_largest_field_is_cut_first_and_the_rest_kept(where, one):
    one.event("review", round=3, problem="x" * 9000, blocking=1, not_blocking=2)
    kept = json.loads(rows(where)[0][5])
    assert {k: kept[k] for k in ("round", "blocking", "not_blocking", "truncated")} == {
        "round": 3, "blocking": 1, "not_blocking": 2, "truncated": True
    }  # fmt: skip
    assert kept["problem"] == "x" * len(kept["problem"])
    assert list(kept) == ["round", "problem", "blocking", "not_blocking", "truncated"]


def test_a_list_keeps_as_many_of_its_first_items_as_fit(where, one):
    paths = [f"src/{n:05}.py" for n in range(2000)]
    one.event("risky_approved", paths=paths, shown="abc")
    kept = json.loads(rows(where)[0][5])
    assert kept["truncated"] is True and kept["shown"] == "abc"
    assert 0 < len(kept["paths"]) < 2000 and kept["paths"] == paths[: len(kept["paths"])]


def test_what_is_neither_text_nor_a_list_is_left_out_whole(where, one):
    one.event("settings", roles={f"role{n}": "m" * 100 for n in range(100)}, mode="single")
    assert json.loads(rows(where)[0][5]) == {"mode": "single", "truncated": True}


def test_cutting_one_field_may_be_enough_and_then_no_other_is_cut(where):
    data = {"a": "x" * 6000, "b": "y" * 6000, "c": "z" * 10}
    kept = events.fitted(data)
    assert kept["b"] == "y" * 6000 or kept["a"] == "x" * 6000, "only one of the two was cut"
    assert kept["c"] == "z" * 10 and len(events.encoded(kept).encode()) <= db.DATA_LIMIT
    assert events.fitted({"a": "x"}) == {"a": "x"}


def test_text_that_is_no_utf_8_is_kept_as_its_escapes(where, one):
    """A name as the file system gave it, not UTF-8: JSON's escapes for it, as on the log, and
    read back as the same name."""
    name = b"caf\xe9".decode("utf-8", "surrogateescape")
    one.event("mirror", problem=name)
    assert json.loads(rows(where)[0][5]) == {"problem": name} == log(one)[0]["data"]


# --- the database that cannot be written: left out, counted, said once it can. ---


def busy(where: Path, monkeypatch) -> sqlite3.Connection:
    """Another process's write lock held on vivibox.db, and the waits for it made short."""
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 20)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    holder = db.connect(where, made_by="other")
    holder.execute("BEGIN IMMEDIATE")
    return holder


def lost(where: Path) -> list[tuple]:
    return [(r[2], r[3], json.loads(r[5])) for r in rows(where) if r[4] == "audit.lost"]


def test_a_database_held_by_another_leaves_the_event_on_the_log_and_the_writer_going(where, one, monkeypatch):
    one.event("goal", goal="before")
    holder = busy(where, monkeypatch)
    one.event("goal", goal="held out")
    one.event("goal", goal="held out too")
    assert [e["data"]["goal"] for e in log(one)] == ["before", "held out", "held out too"]
    holder.execute("ROLLBACK")
    holder.close()
    one.event("goal", goal="after")
    assert [e["data"]["goal"] for e in log(one)] == ["before", "held out", "held out too", "after"]
    found = rows(where)
    assert [(r[4], json.loads(r[5])) for r in found] == [
        ("goal", {"goal": "before"}),
        ("audit.lost", {"count": 2}),
        ("goal", {"goal": "after"}),
    ]
    assert found[1][1] == found[2][1], "said with the first event written after"
    assert lost(where) == [(None, "you", {"count": 2})], "of no single task"
    one.event("goal", goal="later")
    assert len(lost(where)) == 1, "said once"


def test_a_file_that_is_no_database_is_left_out_the_same(where, one):
    where.parent.mkdir(parents=True)
    where.write_bytes(b"not a database, " * 100)
    one.event("goal", goal="unreadable")
    assert where.read_bytes() == b"not a database, " * 100
    assert [e["data"]["goal"] for e in log(one)] == ["unreadable"]
    where.unlink()
    one.event("goal", goal="back")
    assert [(r[4], json.loads(r[5])) for r in rows(where)] == [
        ("audit.lost", {"count": 1}),
        ("goal", {"goal": "back"}),
    ]


def test_a_database_that_cannot_be_opened_is_left_out_the_same(where, one):
    where.mkdir(parents=True)  # a folder where the file goes
    one.event("goal", goal="cannot open")
    where.rmdir()
    one.event("goal", goal="back")
    assert [e["data"]["goal"] for e in log(one)] == ["cannot open", "back"]
    assert lost(where) == [(None, "you", {"count": 1})]


def test_a_database_removed_between_events_is_made_again(where, one):
    one.event("goal", goal="first")
    for name in ("vivibox.db", "vivibox.db-wal", "vivibox.db-shm"):
        (where.parent / name).unlink(missing_ok=True)
    one.event("goal", goal="second")
    assert [e["data"]["goal"] for e in log(one)] == ["first", "second"]
    assert [json.loads(r[5]) for r in rows(where)] == [{"goal": "second"}], "the first is on the log only"


def test_a_newer_vivibox_s_database_is_never_written_and_the_log_is(where, one):
    """This vivibox writes nothing there, now or later: no loss to count."""
    one.event("goal", goal="before")
    with sqlite3.connect(where) as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA + 1}")
    before = where.read_bytes()
    one.event("goal", goal="newer")
    assert where.read_bytes() == before
    assert [e["data"]["goal"] for e in log(one)] == ["before", "newer"]
    with sqlite3.connect(where) as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA}")
    one.event("goal", goal="after")
    assert lost(where) == []


def test_a_loss_is_counted_for_the_database_it_was_meant_for(where, one, tmp_path, monkeypatch):
    holder = busy(where, monkeypatch)
    one.event("goal", goal="held out")
    holder.execute("ROLLBACK")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    one.event("goal", goal="elsewhere")
    assert lost(tmp_path / "elsewhere" / "vivibox" / "vivibox.db") == []
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    one.event("goal", goal="back")
    assert lost(where) == [(None, "you", {"count": 1})]


def test_an_event_that_is_no_json_is_written_nowhere(where, one):
    one.event("goal", goal="before")
    with pytest.raises(TypeError):
        one.event("goal", goal=object())
    assert len(log(one)) == 1 and len(rows(where)) == 1


def test_an_event_the_database_refuses_is_a_fault_not_a_loss(where, one, monkeypatch):
    """What the schema refuses is the code's mistake: it stops the writer, as a payload that is
    no JSON does, and nothing is counted as lost."""
    one.event("goal", goal="before")
    monkeypatch.setattr(task, "_actor", None)
    with pytest.raises(sqlite3.IntegrityError):
        one.event("goal", goal="no actor")
    assert len(log(one)) == 1, "the database first: the log is not written"
    monkeypatch.setattr(task, "_actor", "you")
    one.event("goal", goal="after")
    assert lost(where) == []


def test_a_writer_killed_inside_its_write_leaves_seq_without_a_gap(where, one):
    one.event("goal", goal="before")
    code = textwrap.dedent(
        f"""
        import time
        from contextlib import closing
        from pathlib import Path
        from vivibox.store import db, events
        with closing(db.connect(Path({str(where)!r}), made_by="child")) as conn, db.transaction(conn):
            events.insert(conn, {{"ts": "t", "task": "demo-1", "type": "goal", "data": {{}}}}, "you")
            print("holding", flush=True)
            time.sleep(60)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "holding"
    os.kill(holder.pid, signal.SIGKILL)
    assert holder.wait(timeout=10) == -signal.SIGKILL
    one.event("goal", goal="after")
    seqs = [(r[0], json.loads(r[5])) for r in rows(where)]
    assert seqs == [(1, {"goal": "before"}), (2, {"goal": "after"})]
    with sqlite3.connect(where) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


# --- nothing reads it but the export. ---


def test_the_task_s_events_are_read_from_its_log_never_the_database(where, one):
    one.event("goal", goal="logged")
    with sqlite3.connect(where) as conn:
        conn.execute('UPDATE events SET data = \'{"goal": "changed"}\'')
        conn.execute(
            "INSERT INTO events (ts, task, actor, type, data) VALUES (?, 'demo-1', 'you', 'box', '{}')", (TS,)
        )
    assert one.events() == log(one)
    assert [e["data"] for e in one.events()] == [{"goal": "logged"}]
