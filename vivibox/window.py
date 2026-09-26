"""The window on the agent, and on the verification while it runs: a tmux session of vivibox's
own that w opens and Ctrl-q leaves. Reached through actions, like everything the view and the
command line do; what it calls of the task primitives goes through actions too, so a test that
stands in for one stands in here."""

from __future__ import annotations

import os
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from . import actions, brief, opencode
from .pod import PodError
from .states import State
from .task import Task, TaskState

# The agent view runs on a tmux server of its own: your own tmux sessions and key bindings stay as they
# are, and Ctrl-q leaves the view from anywhere in it.
TMUX = ["tmux", "-L", "vivibox", "-f", "/dev/null"]
LEAVE_KEY = "C-q"
# '-E true' replaces the detaching client with a command that does nothing. Without it tmux
# prints "[detached (from session ...)]" after leaving its own screen, so the line lands on the
# normal one and is still in your scrollback once vivibox closes.
LEAVE_BINDING = ["bind-key", "-n", LEAVE_KEY, "detach-client", "-E", "true"]


def outside_tmux() -> dict[str, str]:
    """The environment for a window on the agent: without $TMUX, which a view started inside tmux
    carries and which makes tmux refuse to attach, though the agent's server is another one."""
    return {k: v for k, v in os.environ.items() if k != "TMUX"}


def tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([*TMUX, *args], capture_output=True, text=True, check=check)


def tmux_session(task_id: str) -> str:
    return f"vivibox-{task_id}"


def tmux_has(target: str) -> bool:
    return actions.tmux("has-session", "-t", target).returncode == 0


def leave_key() -> None:
    actions.tmux(*LEAVE_BINDING)


def close_agent_view(task_id: str) -> None:
    """The window on the agent, gone once you leave it: an `opencode attach` nobody looks at
    renders its interface at a third of a core; w opens it again in a second."""
    actions.tmux("kill-session", "-t", actions.tmux_session(task_id))


# The command a task's tmux session shows, kept in the session's environment: the planner's
# conversation and the writer's are two, and a window opened during planning must not go on
# showing the planner once the writer is at work.
SHOWS = "VIVIBOX_SHOWS"


def shown(session: str) -> str:
    out = actions.tmux("show-environment", "-t", session, SHOWS).stdout or ""
    return out.partition("=")[2].strip() if out.startswith(f"{SHOWS}=") else ""


def agent_view(task: Task, command: list[str]) -> None:
    """A tmux session showing the agent, opened when missing or when it shows another conversation;
    closing it never touches the agent."""
    session = actions.tmux_session(task.id)
    wanted = shlex.join(command)
    if actions.tmux_has(session):
        if actions.shown(session) == wanted:
            # The keys live on the server, which outlives any one session, so a server still
            # running from before carries an older binding. Setting it again is cheap.
            actions.leave_key()
            return
        actions.tmux("kill-session", "-t", session)
    actions.tmux("new-session", "-d", "-s", session, "-n", "agent", wanted, check=True)
    actions.tmux("set-environment", "-t", session, SHOWS, wanted)
    for option in (
        LEAVE_BINDING,
        ["set-option", "-g", "status-right", " Ctrl-q: back to vivibox "],
        ["set-option", "-g", "status-left", f" {task.id} "],
        ["set-option", "-g", "status-left-length", "40"],
    ):
        actions.tmux(*option)


def watchable_sessions(task: Task, st: TaskState | None = None) -> list[tuple[str, str]]:
    """The opencode conversations there are to look at, as (role, session): only opencode has a
    window to attach to, and a claude-code turn is watched through its log. The writer's first,
    then the planner's, which stays readable once the writer is at work."""
    st = st or task.read_state()
    return [
        (role, st.sessions[role])
        for role in brief.ROLES[::-1]
        if st.sessions.get(role) and actions.role_of(task, role).harness == opencode.NAME
    ]


def watchable_session(task: Task, st: TaskState | None = None, role: str = "") -> str:
    """The conversation w shows: the role's you name, else the first there is; "" for none."""
    found = dict(actions.watchable_sessions(task, st))
    return found.get(role, "") if role else next(iter(found.values()), "")


def verification_log(task: Task, st: TaskState | None = None) -> Path | None:
    """The log of the verification under way: the newest written since the task started
    verifying. None before the gate has opened it, and when the task is not verifying."""
    st = st or task.read_state()
    if st.state is not State.VERIFY:
        return None
    # A second's slack: the log is opened right after the transition, and mtimes are coarse.
    since = datetime.fromisoformat(st.updated).timestamp() - 1
    logs = [p for p in (task.meta / "log").glob("verify-*.log") if p.stat().st_mtime >= since]
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


# The name w takes for the verification's log, beside the roles' conversations.
VERIFICATION = "verification"


def view_command(task: Task, st: TaskState, role: str = "") -> list[str]:
    """What w shows: the verification's log while it runs, else the agent's opencode window; the
    conversation of the role you name, whatever runs."""
    if not actions.supervisor_running(task):
        raise PodError(f"{task.id}: the agent is not working now; nothing to watch")
    if st.state is State.VERIFY and role in ("", VERIFICATION):
        log = actions.verification_log(task, st)
        if log is None:
            raise PodError(
                f"{task.id}: the verification is starting and has no log yet; try again in a moment"
            )
        return ["vivibox", "follow-verification", task.id, str(log)]
    session = actions.watchable_session(task, st, role)
    if not session:
        raise PodError(f"{task.id}: the agent is not working now; nothing to watch")
    return opencode.OpenCode(actions.task_pod(task.id)).attach_command(session)


def attach_command(task_id: str, role: str = "") -> list[str]:
    task, _ = actions.load(task_id)
    st = task.read_state()
    if st.box:
        return actions.box_shell_command(task_id)
    actions.agent_view(task, actions.view_command(task, st, role))
    return [*TMUX, "attach-session", "-t", actions.tmux_session(task_id)]
