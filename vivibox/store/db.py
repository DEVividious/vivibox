"""vivibox.db: where it is, its schema, the version it is at, and how it is opened and written.

    $XDG_DATA_HOME/vivibox/vivibox.db (~/.local/share/vivibox/vivibox.db)

Beside the history and the archive, never in the tasks' folder, and never in a folder a container
mounts. The folder and the file are yours alone (0700, 0600).

The schema's version is SQLite's user_version. A migration only adds, and may run again over what
it made: a table or a column comes with the first module that writes it. A database a newer
vivibox made is read and never written: connect refuses it (StoreNewer), read opens it with every
write refused.

Schema 2 (ADR-0051) gives every event an identity (events.id, never empty, never twice) and the
place of its line in its task's events.jsonl (log_offset, set once), and numbers each fact the
database learns, an event or the place of one, from one counter (knowledge.n): events.seq and
events.placed_seq are such numbers, so what the database knew at any number never changes. A
database made new is made at schema 2. One at schema 1 (vivibox 0.2.x past 0.2.2, before
identities) is copied whole beside itself (<name>.v1) and then made schema 2, when it holds no
event; one that holds any is left as it is (StoreStale): its events cannot be told apart from the
log's, which has every one of them, so it is to be removed and built again from the logs.

Every connection waits for another process's write lock up to BUSY_TIMEOUT_MS inside SQLite;
transaction then tries again, ATTEMPTS times in all, and says StoreBusy. Only the BEGIN is tried
again, never the body: a transaction holds the lock for its writes and for nothing else.

Inside available(), a database that cannot be used (the file cannot be opened, is no database,
is damaged, or the disk refuses it) says StoreUnavailable, and one held past every wait
StoreBusy; any other error of SQLite's is the code's, and raised as it is.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 2
BUSY_TIMEOUT_MS = 5000
ATTEMPTS = 3
BACKOFF = 0.1  # seconds before the second try, doubled before each one after it
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
DATA_LIMIT = 8192  # bytes of an event's data, JSON

# What every schema keeps as it was made at schema 1, made IF NOT EXISTS: the migration may run
# again over it.
KEPT = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        version INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    ) STRICT""",
    # How much of a file was imported: its first byte_offset bytes, whose hash this is. The
    # import's alone (store/importer.py).
    """CREATE TABLE IF NOT EXISTS imports (
        path TEXT PRIMARY KEY,
        hash TEXT NOT NULL,
        byte_offset INTEGER NOT NULL
    ) STRICT""",
)
# Schema 2's own. knowledge: one row, n the number of the last fact the database learnt. events:
# seq the number of the fact "this event", placed_seq that of "its line begins at log_offset"
# (both or neither); task the task's id, or NULL for an event of no single task; no foreign key,
# so an event outlives its task's row, and comes before it from an archive. The limit of data is
# of bytes, not characters. An event once written is never changed, and its place is set once.
EVENTS = (
    """CREATE TABLE IF NOT EXISTS knowledge (
        one INTEGER PRIMARY KEY CHECK (one = 1),
        n INTEGER NOT NULL
    ) STRICT""",
    "INSERT INTO knowledge (one, n) VALUES (1, 0) ON CONFLICT (one) DO NOTHING",
    f"""CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY,
        id TEXT NOT NULL UNIQUE CHECK (length(id) > 0),
        ts TEXT NOT NULL,
        task TEXT,
        actor TEXT NOT NULL,
        type TEXT NOT NULL,
        data TEXT NOT NULL CHECK (length(CAST(data AS BLOB)) <= {DATA_LIMIT}),
        log_offset INTEGER CHECK (log_offset IS NULL OR log_offset >= 0),
        placed_seq INTEGER,
        CHECK ((log_offset IS NULL) = (placed_seq IS NULL)),
        CHECK (placed_seq IS NULL OR placed_seq >= seq)
    ) STRICT""",
    """CREATE TRIGGER IF NOT EXISTS events_immutable BEFORE UPDATE OF seq, id, ts, task, actor, type, data
        ON events BEGIN SELECT RAISE(ABORT, 'an event is never changed'); END""",
    """CREATE TRIGGER IF NOT EXISTS events_placed_once BEFORE UPDATE OF log_offset, placed_seq ON events
        WHEN OLD.log_offset IS NOT NULL BEGIN SELECT RAISE(ABORT, 'a position is set once'); END""",
)


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


class StoreUnavailable(StoreError):
    pass


class StoreStale(StoreError):
    """A database of schema 1 that holds events: they have no identity the log shares, so it is
    neither migrated nor written, and is to be removed."""

    def __init__(self, where: Path, rows: int):
        self.rows = rows
        super().__init__(
            f"{where} is a copy of the events from before each had an identity "
            f"({rows} event{'' if rows == 1 else 's'}): events.jsonl has every one of them, and this "
            f"copy cannot be matched to it. Remove {where} with its -wal and -shm; vivibox builds it "
            "again from the logs"
        )


# The primary result codes that say the file cannot be used now, not that the code is wrong.
UNAVAILABLE = {
    sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_NOTADB,
    sqlite3.SQLITE_CORRUPT,
    sqlite3.SQLITE_IOERR,
    sqlite3.SQLITE_FULL,
    sqlite3.SQLITE_READONLY,
    sqlite3.SQLITE_PERM,
}


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


def has_events(conn: sqlite3.Connection, where: Path) -> bool:
    """Whether a database being read holds events of schema 2 or later: False for one of schema 1
    that holds none (no migration has run on it yet); StoreStale for one that holds some."""
    if (found := _version(conn)) >= 2:
        return True
    if found == 1 and (rows := conn.execute("SELECT count(*) FROM events").fetchone()[0]):
        raise StoreStale(where, rows)
    return False


def backup(target: Path, where: Path | None = None) -> None:
    """The database as it is now, whole and on its own, into target, a file that is not there:
    what vivibox backup makes. Never a copy of the file itself, which leaves out the -wal."""
    where = where or path()
    if not where.exists():
        raise StoreError(f"no database at {where}")
    if target.exists() or target.is_symlink():
        raise StoreError(f"{target} exists; vivibox backup writes only a new file")
    with available(where), closing(_open(where, "rw")) as conn:
        try:
            copy(conn, target)
        except (OSError, sqlite3.Error) as e:
            raise StoreError(f"{target}: {e.strerror if isinstance(e, OSError) and e.strerror else e}") from e


def copy(conn: sqlite3.Connection, target: Path) -> None:
    """What conn's database last committed, into target, safely: VACUUM INTO a new file of a
    name of its own beside target, made 0600 and synced, then put in target's place at once
    (os.replace) and the folder synced. An earlier target stays whole until then, and nothing
    but the temporary file is left of a copy that fails: an OSError or SQLite's error, raised."""
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    # Claimed empty first, which VACUUM INTO takes, so that it is ours to remove, and 0600.
    os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
    try:
        conn.execute("VACUUM INTO ?", (str(temporary),))
        _sync(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        os.replace(temporary, target)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise
    _sync(target.parent, os.O_RDONLY | os.O_DIRECTORY)


def _sync(where: Path, flags: int) -> None:
    fd = os.open(where, flags | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def available(where: Path) -> Iterator[None]:
    """The body, with what says the database at where cannot be used now as StoreUnavailable
    (UNAVAILABLE, or the file system refusing its folder or file) or StoreBusy (held past
    SQLite's wait, where transaction does not try again: opening, migrating)."""
    try:
        yield
    except OSError as e:
        raise StoreUnavailable(f"{where}: {e.strerror or e}") from e
    except sqlite3.Error as e:
        code = getattr(e, "sqlite_errorcode", None)
        if code is not None and code & 0xFF == sqlite3.SQLITE_BUSY:
            raise StoreBusy(f"{where}: another vivibox is writing to it") from e
        if code is not None and code & 0xFF in UNAVAILABLE:
            raise StoreUnavailable(f"{where}: {e}") from e
        raise


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
    """The database from the version found to SCHEMA, in one transaction that holds the write lock
    throughout: another process opening it at the same time waits, then finds it migrated. Schema
    1 is copied before it is changed (copy, on another connection: VACUUM cannot run inside a
    transaction, and reads past this one's lock), or left as it is when it holds events
    (StoreStale). Anything that stops the copy rolls the whole back: StoreUnavailable."""
    with transaction(conn):
        found = _version(conn)
        if found > SCHEMA:
            raise StoreNewer(where, found, SCHEMA)
        if found == SCHEMA:
            return
        if found == 1:
            if rows := conn.execute("SELECT count(*) FROM events").fetchone()[0]:
                raise StoreStale(where, rows)
            try:
                with closing(_open(where, "rw")) as other:
                    copy(other, where.with_name(where.name + ".v1"))
            except (OSError, sqlite3.Error) as e:
                said = e.strerror if isinstance(e, OSError) and e.strerror else e
                raise StoreUnavailable(f"{where}: its copy before the migration failed: {said}") from e
            conn.execute("DROP TABLE events")
        for statement in (*KEPT, *EVENTS):
            conn.execute(statement)
        conn.executemany(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            [("created_by", made_by), ("created_at", datetime.now(UTC).isoformat(timespec="seconds"))],
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA}")
