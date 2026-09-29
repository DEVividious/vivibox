"""What the events of every task say, added up: how many attempts the gate took, what a turn
costs per role and per state, how often a turn fails, and what the gate refused work for. The
numbers to look at before changing a word of a prompt."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import ui

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
    ("build_skipped", "build not run"),
)


@dataclass
class Stats:
    tasks: int = 0
    live: int = 0
    # Per task that passed the gate: how many runs it took.
    iterations: list[int] = field(default_factory=list)
    never_passed: int = 0
    gates: int = 0
    gates_passed: int = 0
    reasons: Counter = field(default_factory=Counter)
    turns: int = 0
    failed_turns: int = 0
    retries: int = 0
    # role -> [cost, tokens, turns]; the same by state.
    by_role: dict[str, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0.0, 0, 0]))
    by_state: dict[str, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0.0, 0, 0]))

    # What agents' CLIs used on your subscription, at list prices: never in cost (ADR-0036).
    subscription_used: float = 0.0
    # The CLI's uses with no price: a model not in the table, or a transcript not read.
    unpriced: int = 0

    @property
    def subscription(self) -> float:
        return round(self.subscription_used, 4)

    @property
    def cost(self) -> float:
        return round(sum(v[0] for v in self.by_role.values()), 4)


def add_task(stats: Stats, events: list[dict], live: bool, since: str = "") -> None:
    """One task's events into the totals; since (an ISO date) leaves out what came before it."""
    events = [e for e in events if e.get("ts", "") >= since]
    if not events:
        return
    stats.tasks += 1
    stats.live += live
    runs = 0
    passed_at = 0
    for event in events:
        kind, data = event["type"], event.get("data", {})
        if kind == "gate":
            runs += 1
            stats.gates += 1
            if data.get("passed"):
                stats.gates_passed += 1
                passed_at = passed_at or runs
            else:
                for key, said in REASONS:
                    if data.get(key):
                        stats.reasons[said] += 1
        elif kind == "turn":
            stats.turns += 1
            stats.failed_turns += data.get("ok") is False
            role = data.get("role") or data.get("kind") or "?"
            state = data.get("state") or data.get("kind") or "?"
            for bucket in (stats.by_role[role], stats.by_state[state]):
                bucket[0] += data.get("cost") or 0
                bucket[1] += data.get("tokens") or 0
                bucket[2] += 1
        elif kind == "cli_usage":
            if data.get("cost") is None:
                stats.unpriced += 1
            else:
                stats.subscription_used += data["cost"]
        elif kind == "turn_retry":
            stats.retries += 1
    if passed_at:
        stats.iterations.append(passed_at)
    elif runs:
        stats.never_passed += 1


def collect(sources: Iterable[tuple[list[dict], bool]], since: str = "") -> Stats:
    """sources: (events, whether the task is live), one per task."""
    stats = Stats()
    for events, live in sources:
        add_task(stats, events, live, since)
    return stats


def read_events(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _per_turn(buckets: dict[str, list[float]]) -> list[tuple[str, int, float, int]]:
    return [
        (name, int(turns), round(cost / turns, 4), int(tokens / turns))
        for name, (cost, tokens, turns) in sorted(buckets.items())
        if turns
    ]


def as_dict(stats: Stats) -> dict:
    return {
        "tasks": stats.tasks,
        "live": stats.live,
        "finished": stats.tasks - stats.live,
        "iterations_to_pass": {
            "median": statistics.median(stats.iterations) if stats.iterations else None,
            "max": max(stats.iterations) if stats.iterations else None,
            "tasks_passed": len(stats.iterations),
            "tasks_never_passed": stats.never_passed,
        },
        "gates": {
            "runs": stats.gates,
            "passed": stats.gates_passed,
            "reasons": dict(stats.reasons.most_common()),
        },
        "turns": {
            "count": stats.turns,
            "failed": stats.failed_turns,
            "retried": stats.retries,
            "cost": stats.cost,
        },
        "subscription": {"cost": stats.subscription, "unpriced": stats.unpriced},
        "per_turn": {
            "by_role": [
                {"role": r, "turns": n, "cost": c, "tokens": t} for r, n, c, t in _per_turn(stats.by_role)
            ],
            "by_state": [
                {"state": s, "turns": n, "cost": c, "tokens": t} for s, n, c, t in _per_turn(stats.by_state)
            ],
        },
    }


def report(stats: Stats) -> str:
    """The numbers as text, for a terminal."""
    if not stats.tasks:
        return "No events yet: nothing to count.\n"
    lines = [f"Tasks: {stats.tasks} ({stats.live} live, {stats.tasks - stats.live} finished)"]
    if stats.iterations:
        lines.append(
            f"Verification runs to pass: median {statistics.median(stats.iterations):g}, "
            f"max {max(stats.iterations)} ({len(stats.iterations)} tasks passed"
            + (f", {stats.never_passed} never did)" if stats.never_passed else ")")
        )
    elif stats.never_passed:
        lines.append(f"Verification runs to pass: no task passed yet ({stats.never_passed} tried)")
    failed = (
        f"{stats.failed_turns} failed ({stats.failed_turns * 100 // stats.turns}%)" if stats.turns else ""
    )
    lines.append(
        f"Turns: {stats.turns}"
        + (f", {failed}" if failed else "")
        + f", {stats.retries} retried, ${stats.cost:.2f}"
    )
    if stats.subscription or stats.unpriced:
        lines.append(
            "Subscription (agent CLI, at list prices): "
            + ui.sub_money(stats.subscription, bool(stats.unpriced))
            + (f", {stats.unpriced} with no price" if stats.unpriced else "")
        )
    for title, buckets in (("by role", stats.by_role), ("by state", stats.by_state)):
        rows = _per_turn(buckets)
        if not rows:
            continue
        width = max(len(name) for name, *_ in rows)
        lines += ["", f"Per turn, {title}:", f"  {'':{width}}  turns   $/turn  tokens/turn"]
        lines += [f"  {name:{width}}  {n:5}  {cost:7.3f}  {tokens:11}" for name, n, cost, tokens in rows]
    if stats.gates:
        lines += ["", f"Verification: {stats.gates} runs, {stats.gates_passed} passed"]
        if stats.reasons:
            width = max(len(said) for said in stats.reasons)
            lines += [f"  {said:{width}}  {count}" for said, count in stats.reasons.most_common()]
    return "\n".join(lines) + "\n"
