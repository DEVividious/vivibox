"""Which build this is, for a bug report to start with: a tag, or the commit past it. From git
when vivibox runs from a checkout, as an editable install does (uv tool install -e): the metadata
keeps the version of the day it was installed, and a pull since would go unnamed. Else the version
git gave the package when it was built."""

import re
import subprocess
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed
from pathlib import Path

# The repository this module is in, when it is in one.
SOURCE = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-(\d+)-g([0-9a-f]+)$")


def from_checkout(repo: Path) -> str:
    """The version the build would get here, as uv-dynamic-versioning gives it: X.Y.Z on a tag
    vX.Y.Z, X.Y.(Z+1).devN+<commit> N commits past it, 0.0.1.devN+<commit> with no tag yet; ""
    when repo is not a git checkout."""
    if not (repo / ".git").exists():
        return ""

    def git(*args: str) -> str:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""

    try:
        described = git("describe", "--tags", "--long", "--match", "v[0-9]*", "--abbrev=7")
        if m := TAG.match(described):
            major, minor, patch, distance, sha = m.groups()
            if distance == "0":
                return f"{major}.{minor}.{patch}"
            return f"{major}.{minor}.{int(patch) + 1}.dev{distance}+{sha}"
        count, sha = git("rev-list", "--count", "HEAD"), git("rev-parse", "--short=7", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return f"0.0.1.dev{count}+{sha}" if count and sha else ""


@cache  # every command line names it; git once per process
def current() -> str:
    if here := from_checkout(SOURCE):
        return here
    try:
        return installed("vivibox")
    except PackageNotFoundError:  # run from a checkout that was never installed
        return "0+unknown"
