"""After git pull, the view and every supervisor keep the code they started with. They must say so."""

from pathlib import Path

import pytest

from vivibox import code


@pytest.fixture
def package(tmp_path: Path, monkeypatch) -> Path:
    """A stand-in for the installed package; the cache is cleared on the way out too, or the
    next test's view would start from this package's signature."""
    root = tmp_path / "vivibox"
    root.mkdir()
    (root / "tui.py").write_text("print(1)\n")
    (root / "templates").mkdir()
    (root / "templates" / "plan.md").write_text("# plan\n")
    monkeypatch.setattr(code, "ROOT", root)
    code.signature.cache_clear()
    yield root
    code.signature.cache_clear()


def test_the_signature_follows_what_is_on_disk(package):
    root = package
    before = code.signature()
    (root / "tui.py").write_text("print(2)\n")
    code.signature.cache_clear()
    assert code.signature() != before, "a changed file is a changed vivibox"
    (root / "tui.py").write_text("print(1)\n")
    code.signature.cache_clear()
    assert code.signature() == before, "content, not time: a touch changes nothing"


def test_a_supervisor_records_what_it_runs_and_the_view_sees_it_age(package):
    root = package
    meta = package.parent / ".task"
    meta.mkdir()
    assert not code.older_supervisor(meta), "nothing recorded, nothing to say"
    code.record(meta)
    assert not code.older_supervisor(meta)
    (root / "tui.py").write_text("print(2)\n")
    code.signature.cache_clear()
    assert code.older_supervisor(meta)
