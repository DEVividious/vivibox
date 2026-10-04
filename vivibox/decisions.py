"""Your decisions on a task: the plan and the command accepted, a plan brought in from your own
chat, a reply to the agent, a verification run again, risky files approved. Called through
`actions`, which re-exports them and through which they reach the rest; neither the view nor the
command line has task logic of its own."""

from __future__ import annotations

import re
from pathlib import Path

from . import actions, comments, feedback, gate, handoff, manual, prompts, repo, supervisor
from .config import Project, config_dir
from .risky import Approvals
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
    sends the task on to its first verification."""
    st = task.read_state()
    if st.state is not State.CHECKPOINT_COMMAND:
        raise gate.GateError(f"{task.id} has no command waiting for you")
    chosen = command.strip() or actions.proposed(task)
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


def save_verify(project: Project, commands: list[str], no_build: bool = False) -> None:
    """Keeps what the plan chose, or what you picked, so the project's next task does not decide
    again: the commands, or that there is nothing to build (verify = false)."""
    path = config_dir() / "projects" / f"{project.name}.toml"
    text = path.read_text()
    line = "verify = false" if no_build else "verify = [" + ", ".join(f'"{c}"' for c in commands) + "]"
    path.write_text(re.sub(r"^verify = (\[.*?\]|false)", line, text, count=1, flags=re.MULTILINE | re.DOTALL))


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


def risky_diffs(task: Task, project: Project) -> list[str]:
    return Approvals(task.meta, task.repo, project.risky_extra).diffs()


def approve_risky(task: Task, project: Project) -> tuple[int, Path | None]:
    """Approves the risky files as they are now. Returns how many changed, and the review copy when
    the approval was all the finished work was waiting for."""
    changes = Approvals(task.meta, task.repo, project.risky_extra).approve_changes()
    task.event("risky_approved", paths=[c.path for c in changes])
    if task.read_state().state is State.APPROVAL_RISKY:
        target = risky_target(task)
        task.transition(target)
        if target is State.CHECKPOINT_FINAL:
            return len(changes), actions.prepare_review(task, project)
    return len(changes), None
