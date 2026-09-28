"""The skill that lets an agent's CLI on your machine (Claude Code, Codex) drive vivibox for you:
vivibox skill install copies it where each CLI reads skills (ADR-0034). A copy rather than a link,
with the version beside it, so an update of vivibox can tell a copy it has left behind."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

from . import version

NAME = "vivibox"
# Beside SKILL.md in a copy: the vivibox that installed it. A folder without it is not ours.
VERSION_FILE = ".vivibox-version"


@dataclass(frozen=True)
class Place:
    cli: str
    home: str  # the CLI's own folder, which says it is installed
    skills: str  # where it reads the skills of every project


# Codex reads ~/.codex/skills too; one copy, in the folder OpenAI documents, keeps it from
# listing the skill twice.
PLACES = (
    Place("Claude Code", ".claude", ".claude/skills"),
    Place("Codex", ".codex", ".agents/skills"),
)


@dataclass(frozen=True)
class Copy:
    cli: str
    path: Path
    version: str  # "" for a folder vivibox did not put there
    current: bool


def _source() -> str:
    return (files("vivibox") / "skills" / NAME / "SKILL.md").read_text()


def copies(home: Path | None = None) -> list[Copy]:
    """The skill folders there are, in every CLI's place, ours or not."""
    home = home or Path.home()
    found = []
    for place in PLACES:
        path = home / place.skills / NAME
        if not path.is_dir():
            continue
        stamp = path / VERSION_FILE
        installed = stamp.read_text().strip() if stamp.exists() else ""
        try:
            current = bool(installed) and (path / "SKILL.md").read_text() == _source()
        except OSError:
            current = False
        found.append(Copy(place.cli, path, installed, current))
    return found


def install(home: Path | None = None) -> list[tuple[Path, str]]:
    """Copies the skill for every CLI installed here; each place with what was done there."""
    home = home or Path.home()
    done = []
    for place in PLACES:
        if not (home / place.home).is_dir() and not (home / place.skills).is_dir():
            continue
        path = home / place.skills / NAME
        if path.exists() and not (path / VERSION_FILE).exists():
            done.append((path, f"left alone: a skill named {NAME} that vivibox did not install is there"))
            continue
        with as_file(files("vivibox") / "skills" / NAME) as source:
            shutil.rmtree(path, ignore_errors=True)
            shutil.copytree(source, path)
        (path / VERSION_FILE).write_text(version.current() + "\n")
        done.append((path, f"installed for {place.cli}"))
    return done


def uninstall(home: Path | None = None) -> list[tuple[Path, str]]:
    """Removes the copies vivibox installed; a folder it did not install stays."""
    done = []
    for copy in copies(home):
        if copy.version:
            shutil.rmtree(copy.path)
            done.append((copy.path, "removed"))
        else:
            done.append((copy.path, "left alone: vivibox did not install it"))
    return done
