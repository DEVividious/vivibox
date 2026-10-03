"""What an agent is told before any turn: the brief common to every role, and the role's own.

The common part goes to the harness as its standing instructions. The role's part is the first
message of that role's conversation: roles joined in one agent share one opencode server and its
instructions, and a system prompt per agent there would replace opencode's own, which is
what teaches a model to use the tools. A message costs nothing and says it once per conversation.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

ROLES = ("planner", "writer", "reviewer")
# The briefs of an agent that runs in a control container of its own (orchestration.Mode.control)
# and reads the work there: the planner's, and the supervisor's, which plans and then reviews.
# Their common part is reader(), not common().
READERS = ("planner", "planner-reviewer")
# The briefs there are: a role's own, and the ones for roles joined in one agent (orchestration
# modes), named by the roles they join.
BRIEFS = (*ROLES, "planner-writer-reviewer", "writer-reviewer", "planner-reviewer")


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


def reader(task_id: str, repo: str | Path, branch: str, verify: list[str] | tuple = ()) -> str:
    """The common brief of an agent that reads the work in a container of its own: where the
    project is (repo: the read-only view of the task's commits there), what the container has
    not, and where the agent writes. What common() says of the writer's container is not true
    there."""
    return _template("instructions-reader.md").format(
        task_id=task_id,
        repo=repo,
        branch=branch,
        verify="\n".join(f"  - `{c}`" for c in verify),
    )


def role_text(role: str) -> str:
    """What one role, or the roles one agent plays, owns and is held to, on top of the common brief."""
    if role not in BRIEFS:
        raise ValueError(f"no such role: {role}")
    return _template(f"roles/{role}.md")
