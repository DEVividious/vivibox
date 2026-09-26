"""The shape of the code, as AGENTS.md states it: a module past 800 lines is split before a feature
is added to it. Checked here so the debt is seen when it is made, not after."""

import subprocess
from pathlib import Path

LIMIT = 800
ROOT = Path(__file__).parent.parent


def test_no_module_is_past_the_limit():
    package = Path(__file__).parent.parent / "vivibox"
    big = {
        path.name: lines
        for path in sorted(package.glob("*.py"))
        if (lines := len(path.read_text().splitlines())) > LIMIT
    }
    assert not big, f"split before adding to it: {big}"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def test_a_change_under_the_package_has_its_line_in_the_changelog():
    """AGENTS.md: a branch that changes vivibox/ changes CHANGELOG.md in the same change, so the
    release notes are written as the work is done. Compared with main, committed and uncommitted
    alike, so it fails before the commit; on main itself, or where main is unknown (a shallow
    checkout in CI), there is nothing to compare with."""
    branch = git("branch", "--show-current").stdout.strip()
    if branch in ("", "main") or git("rev-parse", "--verify", "-q", "main").returncode:
        return
    changed = git("diff", "--name-only", "main", "--").stdout.split()
    if any(path.startswith("vivibox/") for path in changed):
        assert "CHANGELOG.md" in changed, "a change under vivibox/ needs its line in CHANGELOG.md"
