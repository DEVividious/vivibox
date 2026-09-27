"""Task state machine."""

from enum import StrEnum


class State(StrEnum):
    PLAN = "plan"
    CHECKPOINT_PLAN = "checkpoint:plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    # The reviewer reads the work the gate passed; only when a reviewer is configured.
    REVIEW = "review"
    # The writer proposed the command the task is verified with, in a project that has none: you
    # keep it for the project, change it, or send the writer back, before the first verification.
    CHECKPOINT_COMMAND = "checkpoint:command"
    # The agent changed files that run on IDE import (build files, hooks); waits for user approval.
    APPROVAL_RISKY = "approval:risky"
    # Verification still failing after the round limit.
    CHECKPOINT_BLOCKED = "checkpoint:blocked"
    CHECKPOINT_FINAL = "checkpoint:final"
    DONE = "done"


CHECKPOINTS = {
    State.CHECKPOINT_PLAN,
    State.CHECKPOINT_COMMAND,
    State.CHECKPOINT_BLOCKED,
    State.CHECKPOINT_FINAL,
}

TRANSITIONS: dict[State, set[State]] = {
    State.PLAN: {State.CHECKPOINT_PLAN, State.APPROVAL_RISKY},
    # Plan accepted, or rejected with a comment.
    State.CHECKPOINT_PLAN: {State.IMPLEMENT, State.PLAN},
    # To the blocked checkpoint when the agent asks you something instead of finishing.
    State.IMPLEMENT: {State.VERIFY, State.CHECKPOINT_COMMAND, State.CHECKPOINT_BLOCKED, State.APPROVAL_RISKY},
    # The command kept, and the verification runs; or sent back with a comment for another.
    State.CHECKPOINT_COMMAND: {State.VERIFY, State.IMPLEMENT},
    State.VERIFY: {
        State.IMPLEMENT,
        State.REVIEW,
        State.APPROVAL_RISKY,
        State.CHECKPOINT_BLOCKED,
        State.CHECKPOINT_FINAL,
    },
    # Blocking notes send the writer back; none, or the last round, and the work goes on to you;
    # to the verification again where the commits changed since it.
    State.REVIEW: {State.IMPLEMENT, State.VERIFY, State.APPROVAL_RISKY, State.CHECKPOINT_FINAL},
    # Approved: continue to the checkpoint the risky change was blocking.
    # Rejected: back to the agent.
    State.APPROVAL_RISKY: CHECKPOINTS | {State.PLAN, State.IMPLEMENT},
    # Your reply sends the agent back; "verify again" runs the checks once more without it, for
    # when what failed was outside the code (a token, Docker, a service).
    State.CHECKPOINT_BLOCKED: {State.IMPLEMENT, State.VERIFY},
    State.CHECKPOINT_FINAL: {State.DONE, State.IMPLEMENT},
    State.DONE: set(),
}


class TransitionError(Exception):
    pass


def check_transition(current: State, target: State) -> None:
    if target not in TRANSITIONS[current]:
        raise TransitionError(f"Transition {current} -> {target} is not allowed")


def waits_for_user(state: State) -> bool:
    return state in CHECKPOINTS or state is State.APPROVAL_RISKY
