"""The shape of the code, as AGENTS.md states it: a module past 800 lines is split before a feature
is added to it. Checked here so the debt is seen when it is made, not after."""

from pathlib import Path

LIMIT = 800


def test_no_module_is_past_the_limit():
    package = Path(__file__).parent.parent / "vivibox"
    big = {
        path.name: lines
        for path in sorted(package.glob("*.py"))
        if (lines := len(path.read_text().splitlines())) > LIMIT
    }
    assert not big, f"split before adding to it: {big}"
