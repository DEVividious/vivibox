"""vivibox.db: where it is, its schema, the version it is at, and how it is opened and written.

    $XDG_DATA_HOME/vivibox/vivibox.db (~/.local/share/vivibox/vivibox.db)

Beside the history and the archive, never in the tasks' folder, and never in a folder a container
mounts. The folder and the file are yours alone (0700, 0600).

The schema's version is SQLite's user_version. A migration only adds, and may run again over what
it made: MIGRATIONS[n] takes a database at version n to n + 1, a table or a column with the first
module that writes it. A database a newer vivibox made is read and never written: connect refuses
it (StoreNewer), read opens it with every write refused.

Every connection waits for another process's write lock up to BUSY_TIMEOUT_MS inside SQLite;
transaction then tries again, ATTEMPTS times in all, and says StoreBusy. Only the BEGIN is tried
again, never the body: a transaction holds the lock for its writes and for nothing else.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 1
BUSY_TIMEOUT_MS = 5000
ATTEMPTS = 3
BACKOFF = 0.1  # seconds before the second try, doubled before each one after it
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
DATA_LIMIT = 8192  # bytes of an event's data, JSON

V1 = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        version INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    ) STRICT""",
    # task: the task's id, or NULL for an event of no single task; no foreign key, so an event
    # outlives its task's row, and comes before it from an archive. seq is never given twice,
    # not even after the last event is gone (AUTOINCREMENT). The limit is of bytes, not characters.
    f"""CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        task TEXT,
        actor TEXT NOT NULL,
        type TEXT NOT NULL,
        data TEXT NOT NULL CHECK (length(CAST(data AS BLOB)) <= {DATA_LIMIT})
    ) STRICT""",
    # How much of a file was imported: its first byte_offset bytes, whose hash this is.
    """CREATE TABLE IF NOT EXISTS imports (
        path TEXT PRIMARY KEY,
        hash TEXT NOT NULL,
        byte_offset INTEGER NOT NULL
    ) STRICT""",
)
MIGRATIONS: tuple[tuple[str, ...], ...] = (V1,)
assert len(MIGRATIONS) == SCHEMA


class StoreError(Exception):
    pass


class StoreNewer(StoreError):
    def __init__(self, where: Path, found: int, known: int):
        self.found, self.known = found, known
        super().__init__(
            f"{where} was written by a newer vivibox (schema {found}, this one knows {known}): "
            "this vivibox reads it and writes nothing; update vivibox"
        )


class StoreBusy(StoreError):
    pass


def path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "vivibox" / "vivibox.db"


def connect(where: Path | None = None, *, made_by: str) -> sqlite3.Connection:
    """The database to read and write, made or migrated to SCHEMA first. made_by: the version of
    the vivibox that opens it, kept in meta when it makes it."""
    where = where or path()
    where.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(where.parent, 0o700)
    # Made 0600 before SQLite opens it; its -wal and -shm take the file's mode.
    os.close(os.open(where, os.O_RDWR | os.O_CREAT, 0o600))
    os.chmod(where, 0o600)
    conn = _open(where, "rw")
    try:
        if (found := _version(conn)) > SCHEMA:
            raise StoreNewer(where, found, SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
        if found < SCHEMA:
            _migrate(conn, where, made_by)
    except BaseException:
        conn.close()
        raise
    return conn


def read(where: Path | None = None) -> sqlite3.Connection:
    """The database to read, at whatever version, a newer vivibox's too: every write refused."""
    where = where or path()
    if not where.exists():
        raise StoreError(f"no database at {where}")
    conn = _open(where, "rw")
    conn.execute("PRAGMA query_only = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE, the body, COMMIT; ROLLBACK when the body raises. The write lock is taken
    first, so a transaction never fails half way for another's."""
    for attempt in range(ATTEMPTS):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as e:
            if e.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            if attempt == ATTEMPTS - 1:
                busy = f"vivibox.db: another vivibox is writing to it; tried {ATTEMPTS} times"
                raise StoreBusy(busy) from None
            time.sleep(BACKOFF * 2**attempt)
    # COMMIT and ROLLBACK said, not conn.commit(): on an autocommit connection those do nothing.
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _open(where: Path, mode: str) -> sqlite3.Connection:
    """autocommit: no transaction but the ones transaction begins."""
    conn = sqlite3.connect(
        f"{where.resolve().as_uri()}?mode={mode}", uri=True, timeout=BUSY_TIMEOUT_MS / 1000, autocommit=True
    )
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate(conn: sqlite3.Connection, where: Path, made_by: str) -> None:
    """Every migration from the version found to SCHEMA, in one transaction: another process
    opening the database at the same time waits, then finds it migrated."""
    with transaction(conn):
        found = _version(conn)
        if found > SCHEMA:
            raise StoreNewer(where, found, SCHEMA)
        for migration in MIGRATIONS[found:]:
            for statement in migration:
                conn.execute(statement)
        conn.executemany(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            [("created_by", made_by), ("created_at", datetime.now(UTC).isoformat(timespec="seconds"))],
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA}")
