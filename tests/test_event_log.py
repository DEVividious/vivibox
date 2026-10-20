"""A task's events.jsonl, the record every reader reads: one function appends to it
(task.append_record), whole records only, and says at which byte each began; a copy of it (the
archive's) never holds part of one (plan P1.3, rule 7)."""

import errno
import fcntl
import inspect
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from vivibox import actions, review, task
from vivibox.task import Task


@pytest.fixture
def log(tmp_path) -> Path:
    return tmp_path / "events.jsonl"


def test_a_record_is_appended_whole_and_its_first_byte_said(log):
    assert task.append_record(log, b'{"a": 1}\n') == 0
    assert task.append_record(log, b'{"b": "\xc5\xbc"}\n') == 9
    assert log.read_bytes() == b'{"a": 1}\n{"b": "\xc5\xbc"}\n'
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_the_offset_is_where_the_record_landed_not_the_size_after(log):
    """Another writer between this one's write and a look at the size would move the size: the
    offset is taken on the descriptor, under the lock, never from the file's size."""
    log.write_bytes(b"x" * 10 + b"\n")
    assert task.append_record(log, b"{}\n") == 11
    source = inspect.getsource(task.append_record)
    assert "stat" not in source and "st_size" not in source and "LOCK_EX" in source


RECORD = b'{"long": "record"}\n'


def writes(*plan):
    """os.write as it goes for RECORD: each step writes that many bytes of what is asked, or
    raises; any other write (the test runner's own) as it is."""
    write, steps = os.write, list(plan)

    def fake(fd, data):
        if not RECORD.endswith(bytes(data)) or not steps:
            return write(fd, data)
        step = steps.pop(0)
        if isinstance(step, OSError):
            raise step
        return write(fd, bytes(data)[:step]) if step else 0

    return fake


def test_a_link_in_its_place_is_never_followed(log, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"")
    log.symlink_to(elsewhere)
    with pytest.raises(OSError) as raised:
        task.append_record(log, b"{}\n")
    assert raised.value.errno == errno.ELOOP
    assert elsewhere.read_bytes() == b""


def test_a_short_write_is_followed_by_the_rest(log, monkeypatch):
    log.write_bytes(b"{}\n")
    monkeypatch.setattr(task.os, "write", writes(3, 100))
    assert task.append_record(log, RECORD) == 3
    assert log.read_bytes() == b"{}\n" + RECORD


def test_a_write_that_fails_part_way_takes_its_part_back(log, monkeypatch):
    """The record is the log's whole or not at all: what was written of it is cut off again
    before the lock is let go, and the writer told."""
    log.write_bytes(b"{}\n")
    monkeypatch.setattr(task.os, "write", writes(5, OSError(errno.ENOSPC, "No space left on device")))
    with pytest.raises(OSError, match="No space left"):
        task.append_record(log, RECORD)
    assert log.read_bytes() == b"{}\n"
    assert task.append_record(log, RECORD) == 3, "the next record starts where the last one ended"


def test_a_write_that_writes_nothing_is_an_error_not_a_loop(log, monkeypatch):
    monkeypatch.setattr(task.os, "write", writes(0))
    with pytest.raises(OSError):
        task.append_record(log, RECORD)
    assert log.read_bytes() == b""


def test_a_part_that_cannot_be_taken_back_is_said_to_damage_the_log(log, monkeypatch):
    log.write_bytes(b"{}\n")

    def refused(fd, length):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(task.os, "write", writes(5, OSError(errno.EIO, "Input/output error")))
    monkeypatch.setattr(task.os, "ftruncate", refused)
    with pytest.raises(task.LogDamaged) as raised:
        task.append_record(log, RECORD)
    assert str(raised.value) == (
        f"{log}: a record was cut short at byte 3 and could not be taken back (Input/output error): "
        "the log ends in part of a record"
    )
    assert not isinstance(raised.value, OSError), "never taken for a write that can be tried again"


def appender(log: Path, name: str, count: int) -> subprocess.Popen:
    """A process that appends count records of its own once told to go, and prints each offset."""
    code = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from vivibox import task
        sys.stdin.readline()
        for n in range({count}):
            pad = "x" * (n * 37 % 3000)
            record = ('{{"by": "{name}", "n": ' + str(n) + ', "pad": "' + pad + '"}}\\n').encode()
            print(task.append_record(Path({str(log)!r}), record), flush=True)
        """
    )
    return subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )


def test_two_processes_appending_at_once_each_get_the_offsets_of_their_own_records(log):
    first, second = appender(log, "a", 60), appender(log, "b", 60)
    for p in (first, second):
        p.stdin.write("go\n")
        p.stdin.flush()
    said = {
        name: [int(line) for line in p.communicate(timeout=60)[0].split()]
        for name, p in (("a", first), ("b", second))
    }
    data = log.read_bytes()
    lines = data.split(b"\n")[:-1]
    assert len(lines) == 120 and all(json.loads(line) for line in lines), "every record whole"
    for name, offsets in said.items():
        assert len(offsets) == 60
        for n, offset in enumerate(offsets):
            record = json.loads(data[offset : data.index(b"\n", offset)])
            assert (record["by"], record["n"]) == (name, n)


def test_a_copy_waits_for_a_record_being_written(log, tmp_path):
    log.write_bytes(b'{"a": 1}\n')
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, b'{"b": ')  # a writer half way through its record
    target = tmp_path / "copy.jsonl"
    copying = threading.Thread(target=task.copy_log, args=(log, target))
    copying.start()
    copying.join(timeout=0.3)
    assert copying.is_alive(), "the copy waits for the writer's lock"
    os.write(fd, b"2}\n")
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    copying.join(timeout=10)
    assert target.read_bytes() == b'{"a": 1}\n{"b": 2}\n'


def test_a_copy_of_a_log_that_is_a_link_is_refused(log, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"{}\n")
    log.symlink_to(elsewhere)
    with pytest.raises(OSError):
        task.copy_log(log, tmp_path / "copy.jsonl")
    assert not (tmp_path / "copy.jsonl").exists()


def test_the_archive_holds_every_event_the_archiving_wrote(env, monkeypatch):
    """A file the agent named that is not kept is said on the task's log; the archive copies
    the log after it, so the archive is the log and more, never less."""
    monkeypatch.setattr(task, "_actor", "you")
    t = Task(env / "tasks" / "demo-1")
    (t.meta / "handoff").mkdir(parents=True)
    t.event("created", project="demo", goal="g")
    (t.meta / "handoff" / "review-1.md").symlink_to("/etc/hostname")
    kept = review.archive(t)
    archived = (kept / "events.jsonl").read_bytes()
    assert archived == (t.meta / "events.jsonl").read_bytes()
    assert [json.loads(line)["type"] for line in archived.splitlines()] == ["created", "not_kept"]
    review.note(t.id, "rules_left", problem="left")
    assert (kept / "events.jsonl").read_bytes().startswith((t.meta / "events.jsonl").read_bytes())
    assert actions.archive_path(t.id) == kept


def test_nothing_but_append_record_appends_to_an_event_log(env, monkeypatch, tmp_path):
    """Task.event and review.note, the two writers of a task's events, both go through it."""
    monkeypatch.setattr(task, "_actor", "you")
    calls = []
    append = task.append_record
    monkeypatch.setattr(
        task, "append_record", lambda path, record: calls.append(path) or append(path, record)
    )
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    t.event("box")
    actions.archive_path("demo-2").mkdir(parents=True)
    review.note("demo-2", "rules_left", problem="left")
    assert calls == [t.meta / "events.jsonl", actions.archive_path("demo-2") / "events.jsonl"]
