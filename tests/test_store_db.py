"""vivibox.db: its place, its schema, its pragmas, the version it is at, and what it does when
another process holds it or one dies holding it. The product writes its events there through
store/events.py alone (tests/test_store_events.py)."""

import os
import signal
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_pod import FakeDocker

from vivibox.store import db

MADE_BY = "0.2.3.dev1+gabc1234"

# The schema v2 is, as sqlite_master keeps it: five tables. Nothing whose writer does not exist
# yet. A key of text is never NULL (STRICT); events.seq is a number of what the database learnt
# (knowledge), not AUTOINCREMENT, so there is no sqlite_sequence.
TABLES = {"meta", "tasks", "events", "imports", "knowledge"}
COLUMNS = {
    "meta": [("key", "TEXT", 1, 1), ("value", "TEXT", 1, 0)],
    "tasks": [
        ("id", "TEXT", 1, 1),
        ("project", "TEXT", 1, 0),
        ("version", "INTEGER", 1, 0),
        ("state_json", "TEXT", 1, 0),
        ("created", "TEXT", 1, 0),
        ("updated", "TEXT", 1, 0),
    ],
    "events": [
        ("seq", "INTEGER", 0, 1),
        ("id", "TEXT", 1, 0),
        ("ts", "TEXT", 1, 0),
        ("task", "TEXT", 0, 0),
        ("actor", "TEXT", 1, 0),
        ("type", "TEXT", 1, 0),
        ("data", "TEXT", 1, 0),
        ("log_offset", "INTEGER", 0, 0),
        ("placed_seq", "INTEGER", 0, 0),
    ],
    "imports": [("path", "TEXT", 1, 1), ("hash", "TEXT", 1, 0), ("byte_offset", "INTEGER", 1, 0)],
    "knowledge": [("one", "INTEGER", 0, 1), ("n", "INTEGER", 1, 0)],
}

# The schema a vivibox of schema 1 made (B4), as it made it: what a migration finds.
V1 = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, version INTEGER NOT NULL,
        state_json TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, task TEXT, actor TEXT NOT NULL,
        type TEXT NOT NULL, data TEXT NOT NULL CHECK (length(CAST(data AS BLOB)) <= 8192)
    ) STRICT""",
    "CREATE TABLE imports (path TEXT PRIMARY KEY, hash TEXT NOT NULL, byte_offset INTEGER NOT NULL) STRICT",
)


def schema(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """(name, type, not null, primary key) of every column of every table."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {t: [(r[1], r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}


def master(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def number(conn: sqlite3.Connection) -> int:
    """The number of the last fact the database learnt."""
    return conn.execute("SELECT n FROM knowledge").fetchone()[0]


def event(
    conn: sqlite3.Connection,
    task: str | None = "demo-1",
    actor: str | None = "vivibox",
    data: str = "{}",
    id: str | None = None,
) -> int:
    """One event, with the next number of what the database knows as its seq, the way
    store/events.py writes one; its seq."""
    seq = conn.execute("UPDATE knowledge SET n = n + 1 RETURNING n").fetchall()[0][0]
    conn.execute(
        "INSERT INTO events (seq, id, ts, task, actor, type, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seq, f"e{seq}" if id is None else id, "2026-10-20T10:00:00.000+00:00", task, actor, "created", data),
    )
    return seq


def migrated(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """The schema of a database migrated from schema 1: schema 2's, and the counter of schema 1's
    AUTOINCREMENT, which SQLite keeps, empty."""
    assert conn.execute("SELECT count(*) FROM sqlite_sequence").fetchone()[0] == 0
    return {t: c for t, c in schema(conn).items() if t != "sqlite_sequence"}


def made_v1(where: Path, rows: int = 0) -> None:
    """A database as a vivibox of schema 1 left it, in WAL, with rows events."""
    where.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(where, autocommit=True)
    conn.execute("PRAGMA journal_mode = WAL")
    for statement in V1:
        conn.execute(statement)
    conn.execute("INSERT INTO meta VALUES ('created_by', 'b4'), ('created_at', '2026-10-20T10:00:00+00:00')")
    for n in range(rows):
        conn.execute(
            "INSERT INTO events (ts, task, actor, type, data) VALUES (?, 'demo-1', 'you', 'goal', ?)",
            ("2026-10-20T10:00:00.000+00:00", f'{{"n": {n}}}'),
        )
    conn.execute("PRAGMA user_version = 1")
    conn.close()


def v1_of(where: Path) -> list[tuple]:
    """What a database of schema 1 holds: its version, its tables and its events."""
    conn = sqlite3.connect(where)
    try:
        found = [conn.execute("PRAGMA user_version").fetchone()[0], master(conn)]
        return [*found, conn.execute("SELECT * FROM events ORDER BY seq").fetchall()]
    finally:
        conn.close()


@pytest.fixture
def where(tmp_path: Path) -> Path:
    return tmp_path / "data" / "vivibox" / "vivibox.db"


def test_it_lives_in_vivibox_s_data_folder_beside_the_history(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert db.path() == tmp_path / "data" / "vivibox" / "vivibox.db"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert db.path() == tmp_path / "home" / ".local" / "share" / "vivibox" / "vivibox.db"
    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert db.path() == tmp_path / "home" / ".local" / "share" / "vivibox" / "vivibox.db", "empty is unset"


def test_a_new_database_is_schema_v2_and_nothing_more(where):
    """Made at schema 2 straight away: no copy of a schema 1 there never was."""
    conn = db.connect(where, made_by=MADE_BY)
    assert {name for kind, name, _ in master(conn) if kind == "table"} == TABLES
    others = sorted((kind, name) for kind, name, _ in master(conn) if kind != "table")
    assert others == [
        ("index", "sqlite_autoindex_events_1"),  # id UNIQUE
        ("index", "sqlite_autoindex_imports_1"),
        ("index", "sqlite_autoindex_meta_1"),
        ("index", "sqlite_autoindex_tasks_1"),
        ("trigger", "events_immutable"),
        ("trigger", "events_placed_once"),
    ]
    assert schema(conn) == COLUMNS
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA == 2
    assert conn.execute("SELECT one, n FROM knowledge").fetchall() == [(1, 0)]
    assert not list(where.parent.glob("*.v1*")), "no copy of a database that was never at schema 1"


def test_it_says_which_vivibox_made_it_and_when(where):
    conn = db.connect(where, made_by=MADE_BY)
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    assert meta.keys() == {"created_by", "created_at"}
    assert meta["created_by"] == MADE_BY
    assert meta["created_at"].endswith("+00:00") and meta["created_at"][:4].isdigit()


def test_every_connection_has_the_pragmas_of_adr_0045(where):
    db.connect(where, made_by=MADE_BY).close()
    for conn in (db.connect(where, made_by=MADE_BY), db.read(where)):
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1, "NORMAL"
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 1024 * 1024
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.in_transaction is False, "a transaction only where one is asked for"


def test_the_folder_and_the_file_are_yours_alone(where):
    where.parent.mkdir(parents=True, mode=0o755)
    os.chmod(where.parent, 0o755)
    db.connect(where, made_by=MADE_BY).close()
    assert stat.S_IMODE(where.parent.stat().st_mode) == 0o700
    for name in ("vivibox.db", "vivibox.db-wal", "vivibox.db-shm"):
        if (where.parent / name).exists():
            assert stat.S_IMODE((where.parent / name).stat().st_mode) & 0o077 == 0, name


def test_opening_it_again_changes_nothing(where):
    db.connect(where, made_by=MADE_BY).close()
    before = master(db.read(where))
    meta = dict(db.read(where).execute("SELECT key, value FROM meta"))
    conn = db.connect(where, made_by="0.3.0")
    assert master(conn) == before
    assert dict(conn.execute("SELECT key, value FROM meta")) == meta, "who made it stays who made it"


def test_the_migration_run_a_second_time_changes_nothing(where):
    conn = db.connect(where, made_by=MADE_BY)
    event(conn)
    before, meta = master(conn), dict(conn.execute("SELECT key, value FROM meta"))
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    conn = db.connect(where, made_by="0.3.0")
    assert master(conn) == before
    assert dict(conn.execute("SELECT key, value FROM meta")) == meta
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert number(conn) == 1, "what the database knows stays"


def test_a_database_of_a_newer_vivibox_is_read_and_never_written(where):
    conn = db.connect(where, made_by=MADE_BY)
    event(conn)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA + 1}")
    conn.close()
    before = where.read_bytes()

    with pytest.raises(db.StoreNewer) as raised:
        db.connect(where, made_by=MADE_BY)
    assert raised.value.found == db.SCHEMA + 1 and raised.value.known == db.SCHEMA
    assert str(raised.value) == (
        f"{where} was written by a newer vivibox (schema 3, this one knows 2): "
        "this vivibox reads it and writes nothing; update vivibox"
    )

    reader = db.read(where)
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        event(reader)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("PRAGMA user_version = 2")
    reader.close()
    assert where.read_bytes() == before
    assert sqlite3.connect(where).execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA + 1


def test_reading_makes_no_database(where):
    with pytest.raises(db.StoreError, match="no database"):
        db.read(where)
    assert not where.exists()


def test_an_event_of_no_task_and_of_a_task_with_no_row_is_taken(where):
    conn = db.connect(where, made_by=MADE_BY)
    event(conn, task=None)
    event(conn, task="gone-7")
    assert conn.execute("SELECT task FROM events ORDER BY seq").fetchall() == [(None,), ("gone-7",)]
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_list(events)").fetchall() == []


def test_an_event_without_an_actor_is_refused(where):
    conn = db.connect(where, made_by=MADE_BY)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: events.actor"):
        event(conn, actor=None)


def test_an_event_s_data_is_at_most_8192_bytes(where):
    conn = db.connect(where, made_by=MADE_BY)
    event(conn, data="x" * 8192)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        event(conn, data="x" * 8193)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        event(conn, data="ż" * 4097)  # 4097 characters, 8194 bytes
    with pytest.raises(sqlite3.IntegrityError, match="cannot store BLOB"):
        conn.execute(
            "INSERT INTO events (seq, id, ts, actor, type, data) VALUES (?, ?, ?, ?, ?, ?)",
            (99, "blob", "t", "vivibox", "created", b"{}"),
        )
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_a_transaction_commits_or_rolls_back_whole(where):
    conn = db.connect(where, made_by=MADE_BY)
    with db.transaction(conn):
        event(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        event(conn)
        event(conn, actor=None)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert conn.in_transaction is False


def test_a_writer_held_out_tries_a_bounded_number_of_times_then_says_so(where, monkeypatch):
    """busy_timeout waits inside SQLite; past it, ATTEMPTS tries in all, the pauses between them
    doubling from BACKOFF, then StoreBusy: never a wait without end, and never the body run twice."""
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 50)
    slept = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    holder = db.connect(where, made_by=MADE_BY)
    holder.execute("BEGIN IMMEDIATE")
    conn = db.connect(where, made_by=MADE_BY)
    ran = []
    with pytest.raises(db.StoreBusy, match="another vivibox is writing to it"), db.transaction(conn):
        ran.append(1)
    assert slept == [db.BACKOFF * 2**i for i in range(db.ATTEMPTS - 1)]
    assert ran == [] and conn.in_transaction is False
    holder.execute("ROLLBACK")
    with db.transaction(conn):
        event(conn)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_any_other_error_is_not_tried_again(where, monkeypatch):
    slept = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    conn = db.connect(where, made_by=MADE_BY)
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError), db.transaction(conn):
        pass
    assert slept == []


def child(where: Path, body: str) -> subprocess.Popen:
    """Another process with vivibox.db open, running body; it prints "holding" once it holds the
    write lock."""
    code = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from vivibox.store import db
        conn = db.connect(Path({str(where)!r}), made_by="child")
        """
    ) + textwrap.dedent(body)
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def test_a_second_process_waits_for_one_holding_a_transaction_and_nothing_is_lost(where):
    db.connect(where, made_by=MADE_BY).close()
    holder = child(
        where,
        """
        with db.transaction(conn):
            seq = conn.execute("UPDATE knowledge SET n = n + 1 RETURNING n").fetchall()[0][0]
            conn.execute(
                "INSERT INTO events (seq, id, ts, actor, type, data)"
                " VALUES (?, 'first', 't', 'first', 'x', '{}')",
                (seq,),
            )
            print("holding", flush=True)
            time.sleep(2)
        """,
    )
    assert holder.stdout.readline().strip() == "holding"
    conn = db.connect(where, made_by=MADE_BY)
    started = time.monotonic()
    with db.transaction(conn):
        event(conn, actor="second")
    waited = time.monotonic() - started
    assert holder.wait(timeout=10) == 0
    assert 1.0 < waited < db.BUSY_TIMEOUT_MS / 1000, "it waited for the first, inside busy_timeout"
    rows = conn.execute("SELECT seq, actor FROM events ORDER BY seq").fetchall()
    assert rows == [(1, "first"), (2, "second")]


def test_a_process_killed_inside_a_transaction_leaves_the_database_whole(where):
    conn = db.connect(where, made_by=MADE_BY)
    event(conn, actor="before")
    conn.close()
    holder = child(
        where,
        """
        conn.execute("BEGIN IMMEDIATE")
        for i in range(2000):
            seq = conn.execute("UPDATE knowledge SET n = n + 1 RETURNING n").fetchall()[0][0]
            conn.execute(
                "INSERT INTO events (seq, id, ts, actor, type, data) VALUES (?, ?, 't', 'killed', 'x', ?)",
                (seq, f"k{i}", "y" * 4000),
            )
        print("holding", flush=True)
        time.sleep(60)
        """,
    )
    assert holder.stdout.readline().strip() == "holding"
    os.kill(holder.pid, signal.SIGKILL)
    assert holder.wait(timeout=10) == -signal.SIGKILL

    conn = db.connect(where, made_by=MADE_BY)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("SELECT actor FROM events").fetchall() == [("before",)], "nothing of the killed one"
    assert number(conn) == 1, "a write that was killed takes no number"
    with db.transaction(conn):
        event(conn, actor="after")
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_no_container_mounts_the_folder_the_database_is_in(env, monkeypatch):
    """The pod's containers bind the task's folder, your repository, the gate's and the CA bundle,
    the control container the task's folders: none of them is vivibox.db or a folder above it."""
    from functools import partial

    from vivibox import actions, control, image
    from vivibox.pod import Pod

    docker = FakeDocker()
    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=docker))
    monkeypatch.setattr(image, "env", lambda ref: {})
    task = actions.create("demo", "Add divide")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    commands = [*docker.find("docker", "run"), pod.gate_command(), pod.sidecar_command(), pod.agent_command()]
    sources = {arg.split(":")[0] for cmd in commands for i, arg in enumerate(cmd) if i and cmd[i - 1] == "-v"}
    sources |= {m.source for m in control.mounts(task)}
    folders = {Path(s) for s in sources if s.startswith("/")}
    assert folders, "the guard sees the binds"
    assert db.path().parent.parent == env / "data"
    assert not [f for f in folders if db.path().is_relative_to(f)]


# --- schema 2: an event's identity, its place in the log, and the numbers of what is known. ---


def test_an_event_without_an_id_or_with_an_empty_one_is_refused(where):
    conn = db.connect(where, made_by=MADE_BY)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: events.id"):
        conn.execute("INSERT INTO events (seq, ts, actor, type, data) VALUES (1, 't', 'you', 'x', '{}')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        event(conn, id="")
    event(conn, id="a")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: events.id"):
        event(conn, id="a")


def test_an_event_once_written_is_never_changed(where):
    conn = db.connect(where, made_by=MADE_BY)
    seq = event(conn)
    for column, value in (
        ("seq", 7), ("id", "other"), ("ts", "t"), ("task", "demo-2"), ("actor", "you"), ("type", "x"),
        ("data", '{"a": 1}'),
    ):  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError, match="an event is never changed"):
            conn.execute(f"UPDATE events SET {column} = ? WHERE seq = ?", (value, seq))
    assert conn.execute("SELECT seq, id, task, data FROM events").fetchall() == [(seq, "e1", "demo-1", "{}")]


def test_a_position_is_set_once_with_its_own_number(where):
    conn = db.connect(where, made_by=MADE_BY)
    seq = event(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute("UPDATE events SET log_offset = 0 WHERE seq = ?", (seq,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute("UPDATE events SET log_offset = 0, placed_seq = ? WHERE seq = ?", (seq - 1, seq))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute("UPDATE events SET log_offset = -1, placed_seq = ? WHERE seq = ?", (seq, seq))
    conn.execute("UPDATE events SET log_offset = 40, placed_seq = 5 WHERE seq = ?", (seq,))
    for change in (
        "log_offset = 40",
        "log_offset = 41",
        "placed_seq = 6",
        "log_offset = NULL, placed_seq = NULL",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="a position is set once"):
            conn.execute(f"UPDATE events SET {change} WHERE seq = ?", (seq,))
    assert conn.execute("SELECT log_offset, placed_seq FROM events").fetchall() == [(40, 5)]


def test_the_numbers_of_what_is_known_only_go_up_and_a_rolled_back_one_is_not_used(where):
    conn = db.connect(where, made_by=MADE_BY)
    with db.transaction(conn):
        event(conn)
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        event(conn)
        event(conn, actor=None)
    assert number(conn) == 1, "the transaction took its numbers back with it"
    with db.transaction(conn):
        assert event(conn) == 2
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO knowledge (one, n) VALUES (2, 0)")
    assert conn.execute("SELECT count(*) FROM knowledge").fetchone()[0] == 1


# --- the migration from schema 1: a copy first, and never over events it cannot name. ---


def test_an_empty_schema_1_database_is_copied_then_made_schema_2(where):
    made_v1(where)
    before = v1_of(where)
    conn = db.connect(where, made_by=MADE_BY)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrated(conn) == COLUMNS
    assert dict(conn.execute("SELECT key, value FROM meta"))["created_by"] == "b4", "who made it stays"
    kept = where.with_name("vivibox.db.v1")
    assert v1_of(kept) == before, "the copy is the database as it was"
    with sqlite3.connect(kept) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert sorted(p.name for p in where.parent.iterdir() if ".v1" in p.name) == ["vivibox.db.v1"], (
        "no temporary left"
    )
    conn.close()
    again = db.connect(where, made_by=MADE_BY)
    assert migrated(again) == COLUMNS and v1_of(kept) == before, "opened again: nothing more is done"


def test_a_schema_1_database_with_events_is_left_as_it_is_and_said_to_be_stale(where):
    """Its events have no identity the log shares: nothing is matched, made up or copied, and
    the database is said to be removed, to be built again from the logs."""
    made_v1(where, rows=2)
    before = v1_of(where)
    with pytest.raises(db.StoreStale) as raised:
        db.connect(where, made_by=MADE_BY)
    assert raised.value.rows == 2
    assert str(raised.value) == (
        f"{where} is a copy of the events from before each had an identity (2 events): "
        "events.jsonl has every one of them, and this copy cannot be matched to it. "
        f"Remove {where} with its -wal and -shm; vivibox builds it again from the logs"
    )
    assert v1_of(where) == before
    assert not list(where.parent.glob("*.v1*")), "no copy"
    assert isinstance(raised.value, db.StoreError)


def test_a_copy_that_fails_leaves_schema_1_and_the_earlier_copy_as_they_were(where, monkeypatch):
    made_v1(where)
    before = v1_of(where)
    kept = where.with_name("vivibox.db.v1")
    kept.write_bytes(b"an earlier copy")

    def refused(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(db.os, "replace", refused)
    with pytest.raises(db.StoreUnavailable, match="No space left on device"):
        db.connect(where, made_by=MADE_BY)
    assert v1_of(where) == before
    assert kept.read_bytes() == b"an earlier copy"
    assert sorted(p.name for p in where.parent.iterdir() if ".v1" in p.name) == ["vivibox.db.v1"], (
        "temporary gone"
    )
    monkeypatch.undo()
    db.connect(where, made_by=MADE_BY)
    assert v1_of(kept) == before, "the next opening copies and migrates"


def test_a_copy_that_sqlite_refuses_leaves_schema_1(where, monkeypatch):
    made_v1(where)
    before = v1_of(where)
    real = db._open

    def no_vacuum(at, mode):
        conn = real(at, mode)
        conn.set_authorizer(
            lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else 0
        )
        return conn

    monkeypatch.setattr(db, "_open", no_vacuum)
    with pytest.raises(db.StoreUnavailable):
        db.connect(where, made_by=MADE_BY)
    monkeypatch.undo()
    assert v1_of(where) == before
    assert not list(where.parent.glob("*.v1*"))


def test_two_processes_migrating_at_once_copy_and_migrate_once(where, monkeypatch):
    """The second waits for the first's lock, then finds schema 2 and does nothing."""
    made_v1(where)
    holder = child_before_connect(
        where,
        """
        replace = os.replace
        def slow(source, target):
            print("copying", flush=True)
            time.sleep(1.5)
            replace(source, target)
        os.replace = slow
        db.connect(where, made_by="child").close()
        """,
    )
    assert holder.stdout.readline().strip() == "copying"
    replaced = []
    monkeypatch.setattr(db.os, "replace", lambda *a: replaced.append(a))
    conn = db.connect(where, made_by=MADE_BY)
    assert holder.wait(timeout=10) == 0
    assert replaced == [], "the second made no copy"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2 and migrated(conn) == COLUMNS


def test_a_process_killed_between_the_copy_and_the_commit_leaves_schema_1(where):
    made_v1(where)
    before = v1_of(where)
    holder = child_before_connect(
        where,
        """
        replace = os.replace
        def then_held(source, target):
            replace(source, target)
            print("copied", flush=True)
            time.sleep(60)
        os.replace = then_held
        db.connect(where, made_by="child")
        """,
    )
    assert holder.stdout.readline().strip() == "copied"
    os.kill(holder.pid, signal.SIGKILL)
    assert holder.wait(timeout=10) == -signal.SIGKILL
    assert v1_of(where) == before
    assert v1_of(where.with_name("vivibox.db.v1")) == before, "the copy is whole"
    conn = db.connect(where, made_by=MADE_BY)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def child_before_connect(where: Path, body: str) -> subprocess.Popen:
    code = textwrap.dedent(
        f"""
        import os, sys, time
        from pathlib import Path
        from vivibox.store import db
        where = Path({str(where)!r})
        """
    ) + textwrap.dedent(body)
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def test_a_backup_is_a_whole_database_beside_which_nothing_else_is_written(where, tmp_path):
    conn = db.connect(where, made_by=MADE_BY)
    event(conn)
    target = tmp_path / "kept" / "copy.db"
    target.parent.mkdir()
    db.backup(target, where)
    with sqlite3.connect(target) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert copy.execute("SELECT id FROM events").fetchall() == [("e1",)]
    assert sorted(p.name for p in target.parent.iterdir()) == ["copy.db"]
    with pytest.raises(db.StoreError, match="exists"):
        db.backup(target, where)
    with pytest.raises(db.StoreError, match="no database"):
        db.backup(tmp_path / "other.db", tmp_path / "none" / "vivibox.db")
