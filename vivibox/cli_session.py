"""The agent's CLI a task was planned in (ADR-0036): which tool, which of its sessions, and where
that session's transcript lies, from the environment the CLI gives the commands it runs. Nothing
is asked of the agent, and nothing here opens the transcript."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

CLAUDE_CODE = "claude-code"
NAMES = {CLAUDE_CODE: "Claude Code"}

# An id is looked for as a file name: anything else in it is not an id.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def claude_folder(cwd: Path) -> str:
    """Claude Code keeps a folder's sessions under its path with every other character a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def detect(environ: Mapping[str, str], cwd: Path) -> dict[str, str]:
    """{tool, id, path} of the session these commands run in; {} outside an agent's CLI. The path
    is kept even before the file is there."""
    session = environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not SESSION_ID.fullmatch(session):
        return {}
    home = Path(environ.get("CLAUDE_CONFIG_DIR") or Path(environ.get("HOME") or Path.home()) / ".claude")
    projects = home / "projects"
    # The CLI names the folder it was started in, which a command may have left.
    found = sorted(projects.glob(f"*/{session}.jsonl"))
    path = found[0] if found else projects / claude_folder(cwd) / f"{session}.jsonl"
    return {"tool": CLAUDE_CODE, "id": session, "path": str(path)}


def describe(session: Mapping[str, str]) -> str:
    return f"{NAMES.get(session['tool'], session['tool'])} session {session['id']}"
