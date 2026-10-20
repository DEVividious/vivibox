"""vivibox events <id> --json: a task's events as vivibox.db keeps them, in the shape of its
events.jsonl, byte for byte (plan P1.2). Every event type the code writes goes in and comes out
as it was written, with no table between: the type in the database is the type on the log."""

import ast
import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from vivibox import cli, task
from vivibox.task import Task

PACKAGE = Path(task.__file__).parent


def written_types() -> set[str]:
    """The type of every event the code writes: the first argument of each .event(...) and
    review.note(id, ...), a literal, either of two (x if c else y), or a constant of its module."""
    found = set()
    for path in PACKAGE.rglob("*.py"):
        module = None
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "event" and node.args:
                arg = node.args[0]
            elif isinstance(func, ast.Name) and func.id == "note" and len(node.args) > 1:
                arg = node.args[1]
            else:
                continue
            for value in (arg.body, arg.orelse) if isinstance(arg, ast.IfExp) else (arg,):
                if isinstance(value, ast.Constant):
                    found.add(value.value)
                else:
                    assert isinstance(value, ast.Name), ast.dump(value)
                    name = ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)
                    module = module or importlib.import_module(name)
                    found.add(getattr(module, value.id))
    return found


@pytest.fixture
def one(env, monkeypatch) -> Task:
    monkeypatch.setattr(task, "_actor", "supervisor")
    t = Task(env / "tasks" / "demo-1")
    t.meta.mkdir(parents=True)
    return t


def exported(capsysbinary, task_id="demo-1") -> tuple[int, bytes, bytes]:
    code = cli.main(["events", task_id, "--json"])
    out = capsysbinary.readouterr()
    return code, out.out, out.err


def test_every_type_the_code_writes_comes_back_as_its_line(one, capsysbinary):
    types = written_types()
    assert len(types) >= 49 and {"state", "rules_left", "resource.limit.exceeded"} <= types, sorted(types)
    for n, type_ in enumerate(sorted(types)):
        one.event(type_, n=n, text='ż\u2028\x00"', ratio=0.1 + n, none=None, items=[1, "a", {"b": [True]}])
    code, out, err = exported(capsysbinary)
    assert (code, err) == (0, b"")
    assert out == (one.meta / "events.jsonl").read_bytes()
    assert [json.loads(line)["type"] for line in out.splitlines()] == sorted(types)


def test_a_name_that_is_no_utf_8_comes_back_as_the_log_has_it(one, capsysbinary):
    one.event("mirror", problem=b"caf\xe9".decode("utf-8", "surrogateescape"))
    assert exported(capsysbinary)[1] == (one.meta / "events.jsonl").read_bytes()


def test_only_the_task_s_own_events_in_the_order_written(one, env, capsysbinary):
    other = Task(env / "tasks" / "demo-2")
    other.meta.mkdir(parents=True)
    for n in range(3):
        one.event("turn_retry", wait=n)
        other.event("turn_retry", wait=n)
    assert exported(capsysbinary)[1] == (one.meta / "events.jsonl").read_bytes()
    assert exported(capsysbinary, "demo-2")[1] == (other.meta / "events.jsonl").read_bytes()


def test_an_event_cut_to_fit_says_so(one, capsysbinary):
    one.event("paused", problem="x" * 9000)
    [line] = exported(capsysbinary)[1].splitlines()
    assert json.loads(line)["data"]["truncated"] is True
    assert "truncated" not in json.loads((one.meta / "events.jsonl").read_bytes())["data"]


def test_a_task_with_no_events_kept_says_where_they_are(one, capsysbinary):
    one.event("goal", goal="kept")
    code, out, err = exported(capsysbinary, "demo-9")
    assert (code, out) == (1, b"")
    assert err.decode() == (
        "vivibox: no events of demo-9 in vivibox.db\n"
        "    vivibox timeline demo-9 reads them from the task's events.jsonl\n"
    )


def test_a_database_that_cannot_be_read_says_why(env, capsysbinary):
    where = env / "data" / "vivibox" / "vivibox.db"
    with pytest.raises(task.EventsUnread, match=f"no database at {where}"):
        task.recorded_events("demo-1")
    where.parent.mkdir(parents=True)
    where.write_bytes(b"not a database, " * 100)
    code, out, err = exported(capsysbinary)
    assert code == 1 and err.decode().startswith("vivibox: the events of demo-1 could not be read: ")
    assert "file is not a database" in err.decode()
    assert err.decode().endswith("    vivibox timeline demo-1 reads them from the task's events.jsonl\n")


def test_a_database_from_before_identities_says_to_remove_it(one, env, capsysbinary):
    from test_store_db import made_v1

    where = env / "data" / "vivibox" / "vivibox.db"
    made_v1(where, rows=1)
    code, out, err = exported(capsysbinary)
    stale = f"{where} is a copy of the events from before each had an identity (1 event)"
    assert code == 1 and err.decode().count(stale) == 2, "said by the import at the start, and by the export"


def test_a_newer_vivibox_s_database_is_read(one, env, capsysbinary):
    one.event("goal", goal="kept")
    with sqlite3.connect(env / "data" / "vivibox" / "vivibox.db") as conn:
        conn.execute("PRAGMA user_version = 99")
    assert exported(capsysbinary)[1] == (one.meta / "events.jsonl").read_bytes()


def test_the_shape_is_asked_for(env, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["events", "demo-1"])
    assert raised.value.code == 2
    assert "--json" in capsys.readouterr().err
