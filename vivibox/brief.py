"""What an agent is told before any turn: the brief common to every role, and the role's own.

The common part goes to the harness as its standing instructions. The role's part is the first
message of that role's conversation: a planner and a writer on one opencode server share the
server's instructions, and a system prompt per agent there would replace opencode's own, which is
what teaches a model to use the tools. A message costs nothing and says it once per conversation.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

ROLES = ("planner", "writer")


def _template(name: str) -> str:
    return files("vivibox").joinpath(f"templates/{name}").read_text()


def common(task_id: str, repo: str | Path, branch: str, verify: list[str] | tuple = ()) -> str:
    """The brief every role gets, with the task's own facts filled in."""
    return _template("instructions.md").format(
        task_id=task_id,
        repo=repo,
        branch=branch,
        verify="\n".join(f"  - `{c}`" for c in verify),
    )


def role_text(role: str) -> str:
    """What one role owns and is held to, on top of the common brief."""
    if role not in ROLES:
        raise ValueError(f"no such role: {role}")
    return _template(f"roles/{role}.md")
