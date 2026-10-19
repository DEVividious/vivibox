"""vivibox.db: its place, its schema, its pragmas, the version it is at, and what it does when
another process holds it or one dies holding it. Nothing in the product opens it yet
(tests/test_structure.py), so these are its only callers."""

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

# The schema v1 is, as sqlite_master keeps it: four tables, and the counter AUTOINCREMENT keeps
# for events.seq. Nothing whose writer does not exist yet. A key of text is never NULL (STRICT).
TABLES = {"meta", "tasks", "events", "imports", "sqlite_sequence"}
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
        ("ts", "TEXT", 1, 0),
        ("task", "TEXT", 0, 0),
        ("actor", "TEXT", 1, 0),
        ("type", "TEXT", 1, 0),
        ("data", "TEXT", 1, 0),
    ],
    "imports": [("path", "TEXT", 1, 1), ("hash", "TEXT", 1, 0), ("byte_offset", "INTEGER", 1, 0)],
}


def schema(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """(name, type, not null, primary key) of every column of every table."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {t: [(r[1], r[2], r[3], r[5]) for r in conn.execute(f"PRAGMA table_info({t})")] for t in tables}


def master(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def event(
    conn: sqlite3.Connection, task: str | None = "demo-1", actor: str | None = "vivibox", data: str = "{}"
):
    conn.execute(
        "INSERT INTO events (ts, task, actor, type, data) VALUES (?, ?, ?, ?, ?)",
        ("2026-10-20T10:00:00.000+00:00", task, actor, "created", data),
    )


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


def test_a_new_database_is_schema_v1_and_nothing_more(where):
    conn = db.connect(where, made_by=MADE_BY)
    assert {name for kind, name, _ in master(conn) if kind == "table"} == TABLES
    indexes = [name for kind, name, _ in master(conn) if kind != "table"]
    assert indexes == ["sqlite_autoindex_imports_1", "sqlite_autoindex_meta_1", "sqlite_autoindex_tasks_1"]
    assert {t: c for t, c in schema(conn).items() if t != "sqlite_sequence"} == COLUMNS
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA == 1


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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


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
        f"{where} was written by a newer vivibox (schema 2, this one knows 1): "
        "this vivibox reads it and writes nothing; update vivibox"
    )

    reader = db.read(where)
    assert reader.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        event(reader)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("PRAGMA user_version = 1")
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
            "INSERT INTO events (ts, actor, type, data) VALUES (?, ?, ?, ?)",
            ("t", "vivibox", "created", b"{}"),
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
            conn.execute("INSERT INTO events (ts, actor, type, data) VALUES ('t', 'first', 'x', '{}')")
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
            conn.execute(
                "INSERT INTO events (ts, actor, type, data) VALUES ('t', 'killed', 'x', ?)", ("y" * 4000,)
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
