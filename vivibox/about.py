"""vivibox info: what an agent's CLI needs to know before it makes a task for you, from a folder in
your repository: the project there, the flows and which of them a planner in the CLI can run, what
each role runs on, and the vivibox version."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import cli_session, manual, skill, version
from .config import ORCHESTRATION_MODES, Role, load_config, load_project
from .orchestration import problem
from .projects import project_at

# The shape of info --json; raised when a field changes or goes, not when one is added.
JSON_VERSION = 1


def repository_root(path: Path) -> Path | None:
    found = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(found.stdout.strip()).resolve() if found.returncode == 0 else None


def gather(path: Path) -> dict:
    path = path.expanduser().resolve()
    config = load_config()
    project, hint = None, ""
    root = repository_root(path) if path.is_dir() else None
    if root is None:
        hint = f"{path} is not in a git repository"
    elif name := project_at(root):
        found = load_project(name)
        project = {"name": name, "repo": str(root), "verify": list(found.verify)}
    else:
        hint = f"vivibox init {root}"
    in_cli = Role(manual.NAME, "")
    flows = []
    for name, mode in ORCHESTRATION_MODES.items():
        why_not = problem(name, in_cli, plan_in_cli=True)
        flows.append(
            {
                "name": name,
                "title": mode.label,
                "summary": mode.summary,
                "best_for": mode.best_for,
                "plan_in_cli": not why_not,
                "why_not": why_not,
            }
        )
    return {
        "version": JSON_VERSION,
        "vivibox": version.current(),
        "project": project,
        "hint": hint,
        "default_flow": config.orchestration,
        "flows": flows,
        "roles": {name: {"harness": r.harness, "model": r.model} for name, r in config.roles.items()},
        # The copies of the skill an agent's CLI reads, and whether each is this vivibox's.
        # The agent's CLI session a task made from here records (ADR-0036); None outside one.
        "cli_session": cli_session.detect(os.environ, Path.cwd()) or None,
        "skill": [
            {"cli": c.cli, "path": str(c.path), "version": c.version, "current": c.current}
            for c in skill.copies()
        ],
    }


def describe(found: dict) -> str:
    """The same for you, in lines."""
    project = found["project"]
    if project:
        verify = " && ".join(project["verify"]) or "none yet: the first task's writer proposes it"
        lines = [f"Project {project['name']} ({project['repo']}), verified with: {verify}"]
    else:
        lines = [f"No project here: {found['hint']}"]
    lines += ["", "Flows:"]
    for flow in found["flows"]:
        default = " (default)" if flow["name"] == found["default_flow"] else ""
        lines.append(f"  {flow['name']}: {flow['title']}{default}, best for {flow['best_for']}")
        if not flow["plan_in_cli"]:
            lines.append(f"    not with a planner in your CLI: {flow['why_not']}")
    lines += ["", "Roles:"]
    for name, role in found["roles"].items():
        lines.append(f"  {name}: {role['model'] or 'you'} ({role['harness']})")
    for copy in found["skill"]:
        state = "current" if copy["current"] else "left behind: vivibox skill install updates it"
        lines.append(f"The skill for {copy['cli']}: {copy['path']}, {state}")
    if found["cli_session"]:
        lines.append(f"A task planned from here records {cli_session.describe(found['cli_session'])}")
    lines += ["", f"vivibox {found['vivibox']}"]
    return "\n".join(lines) + "\n"
