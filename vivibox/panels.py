"""The panels beside a live task's: a project's, a finished or deleted task's; the projects the
view offers; and the view's own choices kept between runs (folded projects, what is shown).
Pure functions of files on disk, as panel.py's are; the app in tui.py draws them.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from pathlib import Path

from . import actions, display, gate, ui
from .app_support import in_terminal
from .config import ConfigError, config_dir, load_project
from .plan import body as plan_body


def view_state_path() -> Path:
    return actions.history_path().with_name("view.json")


def load_view() -> dict:
    """Your choices about the list, the ones that outlive the view: the projects you folded away,
    whether the tasks you accepted or deleted are shown."""
    try:
        return json.loads(view_state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_view(**choices) -> None:
    state = {**load_view(), **choices}
    path = view_state_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state) + "\n")


def load_collapsed() -> set[str]:
    return set(load_view().get("collapsed", []))


def save_collapsed(names: set[str]) -> None:
    save_view(collapsed=sorted(names))


def project_detail(name: str, tasks: int, problem: str) -> str:
    """A project as the panel shows it: where it is, how it is built and tested, and what would
    keep its tasks from starting."""
    path = config_dir() / "projects" / f"{name}.toml"
    lines = [f"### {name}", ""]
    if problem:
        lines += [f"**{problem}.**", ""]
    lines += [
        "**Next:** `n` new task · `e` its settings · `o` open the repository in your IDE"
        + (" · `x` forget the project" if not tasks else ""),
        "",
        f"`{path}`",
        "",
        display.fenced(read(path), "toml"),
    ]
    return "\n".join(lines)


def edit_in_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or "vi"
    in_terminal([*editor.split(), str(path)])


def project_repos() -> list[Path]:
    """The folder you started in and your projects' repositories: where a project's own
    opencode.json would be."""
    found = [Path.cwd()]
    for name in projects():
        with contextlib.suppress(ConfigError, OSError):
            found.append(load_project(name).repo)
    return list(dict.fromkeys(found))


def projects() -> list[str]:
    """The projects you can work in; one whose repository is gone is not offered."""
    broken = actions.broken_projects()
    return [p.stem for p in actions.project_files() if p.stem not in broken]


def read(path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def deleted_detail(entry: dict) -> str:
    """A task you deleted: what it was for and how far it got; nothing of it is left."""
    when = ui.when_deleted(entry["deleted"])
    return "\n".join(
        [
            f"### {entry['id']} · deleted",
            "",
            f"*{entry['project']} · {ui.ago(entry['finished'])} · {ui.finished_cost(entry)}*",
            "",
            entry["title"],
            "",
            f"Deleted{f' {when}' if when else ''}; its files and its work went with it.",
            "",
            "Press `x` to delete it from the history, `H` to hide deleted tasks.",
        ]
    )


def finished_detail(entry: dict) -> str:
    """A task you accepted: what it left in your repository; or one you deleted."""
    if entry.get("deleted"):
        return deleted_detail(entry)
    if entry["conflicts"]:
        named = ", ".join(display.code(display.name(c)) for c in entry["conflicts"])
        where = f"with conflicts in {named}, also on `{entry['branch']}`"
    elif entry["branch"]:
        where = f"on branch `{entry['branch']}`"
    else:
        where = "in your checkout"
    # Tasks accepted before criteria were kept have none; say so rather than show an empty list.
    met = entry.get("criteria")
    delivered = (
        ["**It was accepted as meeting:**", "", *(f"- ☑ {text}" for text in met), ""]
        if met
        else ["*Its criteria were not recorded; it finished before vivibox kept them.*", ""]
    )
    kept = actions.archive_path(entry["id"])
    plan = read(kept / gate.ACCEPTED_PLAN) or read(kept / "plan.md")
    archived = (
        [f"Its plan, its events and its logs are kept in `{kept}`.", "", "#### The plan", "", plan_body(plan)]
        if plan
        else []
    )
    return "\n".join(
        [
            f"### {entry['id']} · done",
            "",
            f"*{entry['project']} · {ui.ago(entry['finished'])} · {ui.finished_cost(entry)}*",
            "",
            entry["title"],
            "",
            f"Its work is {where}, from commit `{entry['commit']}`.",
            "",
            *delivered,
            "Press `l` for its timeline, logs and reviews, `x` to delete it from the history, `h` to hide"
            " accepted tasks.",
            "",
            *archived,
        ]
    )
