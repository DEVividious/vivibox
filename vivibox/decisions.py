"""Your decisions on a task: the plan and the command accepted, a plan brought in from your own
chat, a reply to the agent, a verification run again, risky files approved. Called through
`actions`, which re-exports them and through which they reach the rest; neither the view nor the
command line has task logic of its own."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from . import actions, comments, feedback, gate, handoff, manual, prompts, proposal, repo, supervisor
from .config import ConfigError, Project, config_dir
from .risky import Approvals, Shown
from .states import State
from .task import Task


def accept_plan(task: Task, project: Project) -> None:
    st = task.read_state()
    if st.state is not State.CHECKPOINT_PLAN:
        raise gate.GateError(f"{task.id} has no plan waiting for you")
    if st.awaiting_plan:
        raise gate.GateError(f"{task.id} has no plan yet; bring yours in with: vivibox plan import {task.id}")
    supervisor.accept_plan(task, "plan accepted")


def accept_command(task: Task, project: Project, command: str = "") -> None:
    """Keeps the command the writer proposed, or the one you give instead, for the project, and
    sends the task on to its first verification. The proposed one is the one the task came to
    the checkpoint with, as the supervisor read it then and every place has shown it since
    (proposal.at_checkpoint): never what the writer's file says by now."""
    st = task.read_state()
    if st.state is not State.CHECKPOINT_COMMAND:
        raise gate.GateError(f"{task.id} has no command waiting for you")
    recorded = proposal.at_checkpoint(task)
    if not command.strip() and recorded is None:
        raise gate.GateError(
            f"{task.id} has no command on record at this checkpoint (an earlier vivibox brought it "
            f'here), so none is kept unseen; give the one to keep: vivibox accept {task.id} --verify "…"'
        )
    chosen = command.strip() or recorded
    if not chosen:
        raise gate.GateError(
            f'no command came from the writer; give one: vivibox accept {task.id} --verify "…",'
            " or send it back with a reply"
        )
    save_verify(project, [chosen])
    supervisor.accept_command(task, chosen, "command accepted")


def plan_prompt(task: Task, cli: bool = False) -> str:
    """The prompt for planning this task in your own chat, in a browser or in a CLI."""
    # Planned in an agent's CLI: the browser prompt has no report on the repository to carry.
    cli = cli or task.read_state().plan_in_cli
    path = task.meta / (manual.PROMPT_CLI if cli else manual.PROMPT)
    if not path.exists():
        raise gate.GateError(f"{task.id} has no plan prompt; its planner is not manual")
    return path.read_text()


def answer_path(task: Task) -> Path:
    return task.meta / manual.ANSWER


def import_plan(task: Task, answer: str | None = None) -> str:
    """Your chat's plan becomes the task's plan, for you to accept. Without an answer, the one
    already in the answer file: what a CLI wrote there, or what you pasted into it."""
    if task.read_state().state is not State.CHECKPOINT_PLAN:
        raise gate.GateError(f"{task.id} is not at its plan; a plan can be brought in only there")
    path = answer_path(task)
    if answer is not None:
        path.write_text(answer)
    if not path.exists() or not path.read_text().strip():
        raise gate.GateError(f"no plan to bring in; paste your chat's answer into {path}")
    return manual.import_answer(task)


# What a project with nothing to build or test is told, and told about, in one wording.
NO_BUILD = "no build: the verification checks the criteria and the commits only"
# What an empty verification means, wherever it is shown.
WRITER_PROPOSES = "the next task's writer proposes it, for you to accept"


def _toml(text: str) -> str:
    """text as one basic string of TOML, whatever it holds: a quote, a backslash and a control
    character escaped (JSON's escapes are TOML's), DEL too, which JSON leaves and TOML refuses."""
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _beside(path: Path, data: bytes) -> Path:
    """data in a new file beside path, to be put in its place whole: on the same disk, with the
    mode path has."""
    made = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        made.write_bytes(data)
        shutil.copymode(path, made)
    except BaseException:  # a full disk: nothing half written stays beside the file
        made.unlink(missing_ok=True)
        raise
    return made


# Times a write looks again at a file somebody else saved while it was made, before it gives up.
LOOKS = 3


def save_verify(
    project: Project, commands: list[str], no_build: bool = False, only_unset: bool = False
) -> bool:
    """Keeps what the plan chose, or what you picked, so the project's next task does not decide
    again: the commands, or that there is nothing to build (verify = false). A command may be a
    writer's (--auto keeps one without you): it is written as a string and never as text of
    the file, so nothing in it ends the list or its line, and put in place as it is, never as a
    pattern's replacement, where a backslash would be read.

    The file is written beside itself and put in place whole (a write that fails, or a command
    that is no text, leaves it as it was), by one keep at a time (the projects' folder is held
    while it is made), and only if it is still the bytes that were read: saved by you meanwhile,
    it is read again. only_unset: for a keep nobody is there to see: nothing is written, and
    False returned, when the file as it is at the write names a command or says there is
    nothing to build. Yours is never written over."""
    path = Path(os.path.realpath(config_dir() / "projects" / f"{project.name}.toml"))
    line = "verify = false" if no_build else "verify = [" + ", ".join(_toml(c) for c in commands) + "]"
    found = re.compile(r"^verify = (\[.*?\]|false)", re.MULTILINE | re.DOTALL)
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(folder, fcntl.LOCK_EX)
        for _ in range(LOOKS):
            raw = path.read_bytes()
            text = raw.decode()
            if only_unset and _verify_set(text, path):
                return False
            made = _beside(path, found.sub(lambda _: line, text, count=1).encode())
            try:
                if path.read_bytes() == raw:
                    made.replace(path)
                    return True
            finally:
                made.unlink(missing_ok=True)
        raise ConfigError(f"{path} was saved again each time vivibox went to write it; try again")
    finally:
        os.close(folder)  # and the hold with it


def _verify_set(text: str, path: Path) -> bool:
    """Whether a project file's text names a verification, or says there is none to run."""
    try:
        said = tomllib.loads(text).get("verify", [])
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return said is False or bool(said)


def reply(task: Task, comment: str, criteria: list[str] | tuple = ()) -> State:
    """Your comment goes to the agent, and the task back to it. Returns where it goes. Criteria,
    when the work has come back to you, join the accepted plan: what you found at review becomes
    something the gate holds the work to, not only a remark the agent may act on."""
    criteria = [c for c in criteria if c.strip()]
    if not comment.strip() and not criteria:
        raise gate.GateError("the comment is empty")
    st = task.read_state()
    if criteria and st.state not in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED):
        raise gate.GateError(
            "criteria are added when the work comes back to you; before that, they belong in the plan"
        )
    targets = {
        State.CHECKPOINT_PLAN: State.PLAN,
        State.CHECKPOINT_COMMAND: State.IMPLEMENT,
        State.CHECKPOINT_BLOCKED: State.IMPLEMENT,
        State.CHECKPOINT_FINAL: State.IMPLEMENT,
    }
    if st.state is State.APPROVAL_RISKY:
        # Rejecting risky changes sends the agent back to where it came from.
        target = State.PLAN if risky_target(task) is State.CHECKPOINT_PLAN else State.IMPLEMENT
    elif st.state is State.CHECKPOINT_PLAN and actions.role_of(task, "planner").harness == manual.NAME:
        # Nobody here would read it: the planner is a chat of yours, and the comment belongs there.
        raise gate.GateError(
            "you plan this task in your own chat; say it there and bring the new plan back "
            f"with: vivibox plan import {task.id}"
        )
    elif st.state in targets:
        target = targets[st.state]
    else:
        raise gate.GateError(f"{task.id} is in {st.state}; replies are for checkpoints")
    # A clone blocked by what the writer added: its turn is sent to the verification's feedback,
    # which is written here to say how that is taken out, before anything of the reply is kept.
    blocked = repo.blocked(task.repo) if target is State.IMPLEMENT and not st.box else None
    try:  # open, or refused, before any criterion is added, not after
        if blocked:
            feedback.write_blocked(task, blocked)
        with handoff.appending(task.meta / comments.FILE) as file:
            file.write(comments.entry(comment, gate.add_criteria(task, criteria) if criteria else []))
    except handoff.HandoffError as e:
        raise gate.GateError(str(e)) from e
    supervisor.put_question_away(task)
    prompt = prompts.PLAN_COMMENT_PROMPT if target is State.PLAN else prompts.COMMENT_PROMPT
    prompt = prompts.BLOCKED_REPLY_PROMPT if blocked else prompt
    supervisor.set_next_prompt(task, prompt)
    task.reset_rounds()
    task.transition(target, reason="your reply")
    return target


def verify_again(task: Task) -> None:
    """Runs the verification once more on the work as it is, without a turn of the agent. For a
    failure whose cause was outside the code: an expired token, Docker, a service that was down,
    whether the gate saw that itself or the agent asked about it. A reply would cost a turn and
    invite the agent to change code that was fine. A question the agent asked is answered by the
    build running, so it goes out of the way like an answered one."""
    if task.read_state().state is not State.CHECKPOINT_BLOCKED:
        raise gate.GateError(
            f"{task.id} is not blocked on a failed verification; only that is verified again"
        )
    kept = supervisor.put_question_away(task)
    task.transition(State.VERIFY, reason="verify again", **({"question": kept.name} if kept else {}))


def risky_target(task: Task) -> State:
    """The checkpoint the risky change was holding back, recorded when the task entered approval."""
    entered = next(e["data"] for e in reversed(task.events()) if e["type"] == "state")
    if "then" in entered:
        return State(entered["then"])
    return State.CHECKPOINT_PLAN if entered["previous"] == State.PLAN else State.CHECKPOINT_FINAL


def risky_view(task: Task, project: Project) -> Shown:
    """The changes to the risky files as they are now, each with its diff, and the identity an
    approval of exactly this view carries (risky.Shown)."""
    return Approvals(task.meta, task.repo, project.risky_extra).view()


def risky_diffs(task: Task, project: Project) -> list[str]:
    return risky_view(task, project).diffs


def approve_risky(task: Task, project: Project, shown: str) -> tuple[int, Path | None]:
    """Approves the risky files as you were shown them: shown is the identity of the view you
    looked at (risky_view), and files that are not that view's by now are refused whole
    (risky.NotAsShown), with nothing approved and the task where it was. Returns how many
    changed, and the review copy when the approval was all the finished work was waiting for."""
    changes = Approvals(task.meta, task.repo, project.risky_extra).approve_changes(shown)
    task.event("risky_approved", paths=[c.path for c in changes], shown=shown)
    if task.read_state().state is State.APPROVAL_RISKY:
        target = risky_target(task)
        task.transition(target)
        if target is State.CHECKPOINT_FINAL:
            return len(changes), actions.prepare_review(task, project)
    return len(changes), None
