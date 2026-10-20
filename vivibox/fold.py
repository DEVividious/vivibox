"""A task's events folded once, into what its readers add up: what its turns cost on keys and what
went on a subscription, by stage (ADR-0036); what an agent's CLI used; how long each role's turns
and the verifications took; how the gate went. The list's cost, the panel's roles, u and `vivibox
usage`, and `vivibox stats` all read a turn and a CLI's use here, the one way. The turn under way
is not an event (turn.json): the readers that show it add it apart.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .states import State

# The states whose turns are planning; every other turn is implementing, fixing, or a demo.
PLANNING_STATES = {str(State.PLAN), str(State.CHECKPOINT_PLAN)}
# A role's turns come from the states it works in, for events written before turns named their role.
ROLE_OF_STATE = {**dict.fromkeys(PLANNING_STATES, "planner"), str(State.REVIEW): "reviewer"}
# The column of u a role's time is shown in.
COLUMN_OF_ROLE = {"planner": "plan", "writer": "write", "reviewer": "review"}
# What a failed gate run can be refused for, in the order the report lists them, with the summary
# key each comes from (gate.GateResult.summary).
REASONS = (
    ("failed_commands", "a command failed"),
    ("missing_criteria", "criteria not met"),
    ("commit_problems", "commit problems"),
    ("switched_off_tests", "tests switched off"),
    ("uncommitted", "uncommitted files"),
    ("no_red_evidence", "tests without red evidence"),
    ("hidden_characters", "hidden characters"),
    ("environment", "outside the code"),
    ("risky_stopped", "scan budget exceeded"),
    ("build_skipped", "build not run"),
)


def stage_of(state: str | None) -> str:
    """The stage a turn taken in this state is a cost of: planning, review or implementation."""
    if state in PLANNING_STATES:
        return "planning"
    return "review" if state == str(State.REVIEW) else "implementation"


def seconds_between(start: str, end: str | datetime) -> float:
    end = end if isinstance(end, datetime) else datetime.fromisoformat(end)
    return max((end - datetime.fromisoformat(start)).total_seconds(), 0.0)


@dataclass(frozen=True)
class Turn:
    """A turn's event as it was written; a figure it has not, 0."""

    role: str | None
    state: str | None
    kind: str | None
    agent: str | None
    cost: float
    tokens: int
    # False: taken on a subscription, which is never money (ADR-0036); a turn from before the
    # field was on a key.
    metered: bool
    ok: bool | None

    @property
    def stage(self) -> str:
        return stage_of(self.state)

    @property
    def played(self) -> str:
        """The role it named, else the one of its state; a writer's, for neither."""
        return self.role or ROLE_OF_STATE.get(self.state, "writer")


@dataclass(frozen=True)
class CliUse:
    """What an agent's CLI used in a stage of the task (cli_usage.py); cost None: no price."""

    stage: str
    cost: float | None


@dataclass
class Folded:
    turns: list[Turn] = field(default_factory=list)
    cli: list[CliUse] = field(default_factory=list)
    # Dollars, summed as the events came: on keys by stage, and all of it; what went on a
    # subscription by stage (the turns taken on one and the CLI's use), and all of it.
    on_keys: dict[str, float] = field(default_factory=dict)
    spent: float = 0.0
    subscription: dict[str, float] = field(default_factory=dict)
    subscription_spent: float = 0.0
    # The stages with a CLI's use that had no price: their sums are short of the whole.
    unknown: set[str] = field(default_factory=set)
    # The tokens the CLI used, every kind together, and Codex's windows as it last said them.
    cli_tokens: int = 0
    cli_limits: dict[str, float] = field(default_factory=dict)
    # Seconds by u's column: each role's turns, turn_started to turn, and the verifications.
    seconds: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(("plan", "write", "review", "gate"), 0.0)
    )
    # (column, ts) of a turn started and not ended.
    open_turn: tuple[str, str] | None = None
    # When the task first reached done; "" before.
    done: str = ""
    gates: int = 0
    gates_passed: int = 0
    # The gate run that passed first, counted from 1; 0 for none.
    passed_at: int = 0
    reasons: Counter = field(default_factory=Counter)
    retries: int = 0


def fold(events: list[dict]) -> Folded:
    folded = Folded()
    verifying = ""
    for event in events:
        kind, data = event["type"], event.get("data", {})
        if kind == "turn":
            turn = Turn(
                data.get("role"),
                data.get("state"),
                data.get("kind"),
                data.get("agent"),
                data.get("cost") or 0,
                data.get("tokens") or 0,
                data.get("metered") is not False,
                data.get("ok"),
            )
            folded.turns.append(turn)
            if turn.metered:
                folded.on_keys[turn.stage] = folded.on_keys.get(turn.stage, 0.0) + turn.cost
                folded.spent += turn.cost
            else:
                folded.subscription[turn.stage] = folded.subscription.get(turn.stage, 0.0) + turn.cost
                folded.subscription_spent += turn.cost
            if folded.open_turn:
                column, since = folded.open_turn
                folded.seconds[column] += seconds_between(since, event["ts"])
                folded.open_turn = None
        elif kind == "turn_started":
            role = data.get("role") or ROLE_OF_STATE.get(data.get("state"), "writer")
            folded.open_turn = (COLUMN_OF_ROLE.get(role, "write"), event["ts"])
        elif kind == "cli_usage":
            use = CliUse(data.get("stage") or "conversation", data.get("cost"))
            folded.cli.append(use)
            if use.cost is None:
                folded.unknown.add(use.stage)
            else:
                folded.subscription[use.stage] = folded.subscription.get(use.stage, 0.0) + use.cost
                folded.subscription_spent += use.cost
            folded.cli_tokens += sum(n for n in (data.get("tokens") or {}).values() if isinstance(n, int))
            folded.cli_limits = data.get("limits") or folded.cli_limits
        elif kind == "state":
            if data.get("current") == "verify":
                verifying = event["ts"]
            elif data.get("current") == "done":
                folded.done = folded.done or event["ts"]
        elif kind == "gate":
            folded.gates += 1
            if data.get("passed"):
                folded.gates_passed += 1
                folded.passed_at = folded.passed_at or folded.gates
            else:
                folded.reasons.update(said for key, said in REASONS if data.get(key))
            # Before gates said how long they took, a verification ran from entering verify.
            if isinstance(data.get("seconds"), (int, float)):
                folded.seconds["gate"] += data["seconds"]
            elif verifying:
                folded.seconds["gate"] += seconds_between(verifying, event["ts"])
            verifying = ""
        elif kind == "turn_retry":
            folded.retries += 1
    return folded
