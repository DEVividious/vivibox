"""Terminal output for people: colours when writing to a terminal, plain text otherwise."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import cli_session, prepare
from .states import State
from .task import Task, TaskState

CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def use_color(stream=None) -> bool:
    """https://no-color.org; also off when piped, so grep and scripts see plain text."""
    stream = stream or sys.stdout
    return "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb" and stream.isatty()


class Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return f"\033[{';'.join(CODES[s] for s in styles)}m{text}\033[0m"


def width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def shorten(text: str, limit: int) -> str:
    """Cuts to fit at a space, because a word cut in half reads like something went wrong. Falls back
    to cutting mid-word when the last space is so far back that whole words would be lost."""
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 0)]
    word, _, _ = cut.rpartition(" ")
    return (word if len(word) >= limit // 2 else cut).rstrip() + "…"


def clock(ts: str) -> str:
    """An event's time of day on your clock. Events are kept in UTC, and a time cut out of one is
    hours away from the clock on your screen."""
    return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")


def lasting(ts: str, now: datetime | None = None) -> str:
    """How long since ts, as a duration: "for 3 min", "for 2 h"; "" under a minute."""
    since = ago(ts, now)
    return "" if since == "just now" else f"for {since.removesuffix(' ago')}"


def ago(ts: str, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now(UTC)) - datetime.fromisoformat(ts)).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


# The states whose turns are planning; every other turn is implementing, fixing, or a demo.
PLANNING_STATES = {str(State.PLAN), str(State.CHECKPOINT_PLAN)}


@dataclass(frozen=True)
class Spend:
    """What a task cost, planning and implementation apart: the stronger model plans and the
    cheaper one writes, and the split shows whether that is where the money goes."""

    planning: float = 0.0
    implementation: float = 0.0
    # The reviewer's turns: another prompt and another model, so a figure of its own.
    review: float = 0.0
    # False for a box, which never plans: one number, not a split whose left side is always zero.
    split: bool = True
    # What your subscription's use would have cost on an API key, by stage (planning,
    # implementation, review, conversation): never in the figures above (ADR-0036).
    subscription: dict[str, float] = field(default_factory=dict)
    # The stages in which a model with no list price was used, or the transcript went unread:
    # their sums are short of the whole.
    subscription_unknown: frozenset[str] = frozenset()
    # The tokens the agent's CLI used on the task, every kind together.
    subscription_tokens: int = 0
    # How much of the plan's windows Codex had used when last counted, in percent ("5h", "week").
    subscription_limits: dict[str, float] = field(default_factory=dict)

    @property
    def subscription_total(self) -> float:
        return round(sum(self.subscription.values()), 6)

    @property
    def on_subscription(self) -> bool:
        return bool(self.subscription or self.subscription_unknown)

    def subscription_text(self) -> str:
        return sub_money(self.subscription_total, bool(self.subscription_unknown))

    @property
    def total(self) -> float:
        return round(self.planning + self.implementation + self.review, 6)

    def __bool__(self) -> bool:
        return bool(self.planning or self.implementation)

    def __str__(self) -> str:
        if not self.split:
            return f"${self.total:.2f}"
        text = f"${self.planning:.2f} + ${self.implementation:.2f}"
        return f"{text} + ${self.review:.2f}" if self.review else text


def count(n: int, noun: str) -> str:
    """ "1 turn", "2 turns", "0 turns": every count the view, the logs and the notifications say."""
    return f"{n} {noun}{'' if n == 1 else 's'}"


def money(amount: float) -> str:
    return f"${amount:.2f}"


def sub_money(amount: float, unknown: bool = False) -> str:
    """What a subscription's use would have cost on a key, marked so it reads apart from money
    spent without colour: "$0.41 sub"; "≥$0.41 sub" when a model had no price, "? sub" for
    nothing priced at all."""
    if unknown and not amount:
        return "? sub"
    return f"{'≥' if unknown else ''}{money(amount)} sub"


def tokens_short(n: int) -> str:
    """ "950", "201.5k", "1.2M"."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def finished_spend(entry: dict) -> Spend:
    """A finished task's cost as the list shows a live one; tasks finished before the split was
    kept show their total as one figure."""
    total = entry.get("cost", 0)
    subscription = dict(entry.get("subscription") or {})
    if "planning" not in entry:
        return Spend(0.0, total, split=False, subscription=subscription)
    review = entry.get("review", 0)
    return Spend(
        entry["planning"], round(total - entry["planning"] - review, 6), review, subscription=subscription
    )


def history_subscription(spent: Spend) -> dict:
    """The subscription's sums for a finished task's history line; nothing when there were none."""
    return (
        {"subscription": {k: round(v, 4) for k, v in spent.subscription.items()}}
        if spent.subscription
        else {}
    )


def with_subscription(spent: Spend) -> str:
    """Money spent, then the subscription's use apart: "$0.00 + $0.06 · $0.41 sub"."""
    return f"{spent} · {spent.subscription_text()}" if spent.on_subscription else str(spent)


def finished_cost(entry: dict) -> str:
    return with_subscription(finished_spend(entry))


def marked_cost_cells(spent: Spend | None) -> list[tuple[str, bool]]:
    """PLAN, IMPL and REVIEW as the lists show them, each with whether it is the subscription's:
    a stage done in an agent's CLI (nothing spent on keys in it) shows what the CLI used there."""
    if spent is None:
        return [("-", False)] * 3
    cells = []
    for stage, amount, shown in (
        ("planning", spent.planning, bool(spent) and spent.split),
        ("implementation", spent.implementation, bool(spent)),
        ("review", spent.review, bool(spent.review)),
    ):
        unknown = stage in spent.subscription_unknown
        if not amount and (stage in spent.subscription or unknown):
            cells.append((sub_money(spent.subscription.get(stage, 0.0), unknown), True))
        else:
            cells.append((money(amount) if shown else "-", False))
    return cells


def cost_cells(spent: Spend | None) -> tuple[str, str, str]:
    """PLAN, IMPL and REVIEW: one figure each, "-" for nothing; a box's one figure under IMPL."""
    plan, impl, review = (text for text, _ in marked_cost_cells(spent))
    return plan, impl, review


def _stage(state: str | None) -> str:
    if state in PLANNING_STATES:
        return "planning"
    return "review" if state == str(State.REVIEW) else "implementation"


def cost(task: Task) -> Spend:
    """What vivibox itself spent on the task, from its turn events. For a box that is only the
    turn that works out how to run the app; what you run in it by hand is on your own keys."""
    planning = implementation = review = 0.0
    subscription: dict[str, float] = {}
    unknown: set[str] = set()
    tokens = 0
    limits: dict[str, float] = {}
    for event in task.events():
        data = event["data"]
        if event["type"] == "cli_usage":
            stage = data.get("stage") or "conversation"
            if data.get("cost") is None:
                unknown.add(stage)
            else:
                subscription[stage] = subscription.get(stage, 0.0) + data["cost"]
            tokens += sum(n for n in (data.get("tokens") or {}).values() if isinstance(n, int))
            limits = data.get("limits") or limits
            continue
        if event["type"] != "turn":
            continue
        spent = data.get("cost") or 0
        stage = _stage(data.get("state"))
        if data.get("metered") is False:
            subscription[stage] = subscription.get(stage, 0.0) + spent
        elif stage == "planning":
            planning += spent
        elif stage == "review":
            review += spent
        else:
            implementation += spent
    st = task.read_state()
    # The turn under way, so far: its event comes when it ends, and a long turn is exactly when
    # watching the figure move is the only sign the agent is at work.
    if live := task.live_turn():
        if str(st.state) in PLANNING_STATES:
            planning += live["cost"]
        elif st.state is State.REVIEW:
            review += live["cost"]
        else:
            implementation += live["cost"]
    return Spend(
        round(planning, 6),
        round(implementation, 6),
        round(review, 6),
        split=not st.box,
        subscription={k: round(v, 6) for k, v in subscription.items()},
        subscription_unknown=frozenset(unknown),
        subscription_tokens=tokens,
        subscription_limits=dict(limits),
    )


# What the task needs, in words, and the commands for your next step.
WAITING = {
    State.CHECKPOINT_PLAN: ("review the plan", ["vivibox accept {id}", 'vivibox reply {id} "…"']),
    State.CHECKPOINT_COMMAND: (
        "review the command",
        ["vivibox accept {id}", 'vivibox accept {id} --verify "…"', 'vivibox reply {id} "…"'],
    ),
    State.CHECKPOINT_FINAL: ("review the work", ["vivibox accept {id}", 'vivibox reply {id} "…"']),
    State.CHECKPOINT_BLOCKED: (
        "needs your help",
        ['vivibox reply {id} "…"', "vivibox verify-again {id}", "vivibox attach {id}"],
    ),
    State.APPROVAL_RISKY: ("approve risky files", ["vivibox risky {id}"]),
}
WORKING = {
    State.PLAN: "planning",
    State.IMPLEMENT: "implementing",
    State.VERIFY: "verifying",
    State.REVIEW: "reviewing",
}


def when_deleted(state: str) -> str:
    """What a deleted task was doing, in the list's own words; "" for a state no longer known."""
    try:
        was = State(state)
    except ValueError:
        return ""
    if was in WORKING:
        return f"while {WORKING[was]}"
    if was is State.CHECKPOINT_BLOCKED:
        return "while it needed your help"
    return f"while it waited for you to {WAITING[was][0]}" if was in WAITING else ""


def environment_problem(task: Task) -> str:
    """What outside the code stopped the last verification, or "" when it ran."""
    for event in reversed(task.events()):
        if event["type"] == "gate":
            return event["data"].get("environment", "")
    return ""


def why_blocked(task: Task) -> Path | None:
    """The file that says why a blocked task needs you: the agent's question, or what the last
    verification found."""
    handoff = task.meta / "handoff"
    return next((p for p in (handoff / "question.md", handoff / "verify-feedback.md") if p.exists()), None)


# At these checkpoints stopping the pod leaves the person's review decision in place.
POD_ONLY_STOP = {
    State.CHECKPOINT_PLAN,
    State.CHECKPOINT_COMMAND,
    State.CHECKPOINT_FINAL,
    State.APPROVAL_RISKY,
}


def group(st: TaskState) -> str:
    if st.state is State.DONE:
        return "Done"
    if st.paused and not st.problem and st.state not in POD_ONLY_STOP:
        return "Stopped"
    if st.state in WAITING:
        return "Waiting for you"
    return "Stopped" if st.paused else "Working"


def planner_asks(task: Task, st: TaskState) -> bool:
    """The planner wrote a question instead of a plan (the goal is met already, say): the plan
    checkpoint has only the template to show, and waits for your answer. A plan you write yourself
    under e makes it a plan to review again."""
    from . import gate  # the gate reads the view's words; imported here, not at the top
    from .plan import PlanError, parse_plan

    if st.state is not State.CHECKPOINT_PLAN or st.awaiting_plan:
        return False
    if not (task.meta / "handoff" / "question.md").exists():
        return False
    try:
        gate.check_plan(parse_plan(task.plan_path.read_text()))
    except (OSError, PlanError, gate.GateError):
        return True
    return False


# A manual planner's checkpoint before you brought a plan: nothing to review yet.
AWAITING_PLAN = ("plan it yourself", ["vivibox plan prompt {id}", "vivibox plan import {id}"])
# The review of a round is the agent's in your CLI (ADR-0035): at work there, not waiting for you.
AWAITING_REVIEW = ("review in your CLI", ["vivibox review {id} --prompt", "vivibox review {id} --import"])


def task_number(task_id: str) -> int:
    """demo-10 is 10: compared as text it would come before demo-9."""
    _, _, number = task_id.rpartition("-")
    return int(number) if number.isdigit() else 0


def activity(st: TaskState, max_rounds: int) -> str:
    """max_rounds: the task's own limit, or config.toml's (orchestration.max_rounds_of)."""
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        return AWAITING_PLAN[0]
    if st.awaiting_review and st.state is State.REVIEW:
        return AWAITING_REVIEW[0]
    if st.state in WAITING:
        return WAITING[st.state][0]
    if st.state is State.DONE:
        return "done"
    text = WORKING[st.state]
    if st.state in (State.IMPLEMENT, State.VERIFY) and st.rounds:
        why = f": {st.round_reason}" if st.round_reason else ""
        text += f" (round {st.rounds}/{max_rounds}{why})"
    return text


def next_commands(st: TaskState) -> list[str]:
    if st.paused and st.state not in POD_ONLY_STOP and st.state is not State.DONE:
        decisions = WAITING[st.state][1] if st.state is State.CHECKPOINT_BLOCKED else []
        return [f"vivibox start {st.id}", *(c.format(id=st.id) for c in decisions)]
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        return [c.format(id=st.id) for c in AWAITING_PLAN[1]]
    if st.state in WAITING:
        return [c.format(id=st.id) for c in WAITING[st.state][1]]
    if st.paused:
        return [f"vivibox start {st.id}"]
    if st.state is State.DONE:
        return [f"vivibox rm {st.id}"]
    return [f"vivibox attach {st.id}"]


COLORS = {"Waiting for you": "yellow", "Working": "cyan", "Stopped": "dim", "Done": "green"}
ORDER = list(COLORS)


@dataclass(frozen=True)
class TaskView:
    """What a task is doing and what it needs, worked out once: the list, the details panel and
    the command line all show this, so they cannot call one task three different things."""

    status: str
    group: str
    # Where it sorts: your decisions first, then what failed, then what is not running, then the rest.
    rank: int
    # The reason behind a status that says something went wrong, in the failing tool's words.
    problem: str = ""
    commands: tuple[str, ...] = ()


WAITS, WORKS, STOPPED, DONE = ORDER
# Within "Waiting for you": a decision of yours, something that failed, something nobody is running.
# A decision whose pod you stopped sorts with the decisions: it is still yours to make.
DECISION, STOPPED_DECISION, FAILED, IDLE, AT_WORK, PARKED, FINISHED = range(7)


def _decision(task: Task, st: TaskState, max_rounds: int) -> TaskView:
    """What waits for you. With its pod stopped (s at a review) the decision stays yours, and the
    row says the pod is down: "review the work · stopped"."""
    status, commands = activity(st, max_rounds), tuple(next_commands(st))
    if st.box:
        # Nobody in a box to reply to; its work is yours to accept or to delete.
        commands = tuple(c for c in commands if "reply" not in c)
    elif planner_asks(task, st):
        status, commands = "agent asks", tuple(c for c in commands if "reply" in c)
    elif st.state is State.CHECKPOINT_BLOCKED:
        if (task.meta / "handoff" / "question.md").exists():
            status = "agent asks"
        elif environment_problem(task):
            status = "verification could not run"
        else:
            status = f"verification failed {st.iteration}×"
    if st.paused:
        return TaskView(f"{status} · stopped", WAITS, STOPPED_DECISION, commands=commands)
    return TaskView(status, WAITS, DECISION, commands=commands)


def view(task: Task, st: TaskState, running: bool, max_rounds: int) -> TaskView:
    """running: whether the task's supervisor is alive. Whatever will not move without you waits
    for you; "Stopped" is only what you stopped yourself."""
    if st.state is State.DONE:
        return TaskView("done", DONE, FINISHED, commands=(f"vivibox delete {st.id}",))
    if st.box and st.state is State.IMPLEMENT:
        # Nothing runs in a box but you; open or stopped is all there is to say of it. Closing a
        # box is something else: a, which brings its work to review.
        if st.paused:
            return TaskView("box stopped", STOPPED, PARKED, commands=(f"vivibox start {st.id}",))
        return TaskView(
            "box open", WORKS, AT_WORK, commands=(f"vivibox attach {st.id}", f"vivibox accept {st.id}")
        )
    if st.paused and st.state not in POD_ONLY_STOP:
        if st.problem:
            what, _, why = st.problem.partition(": ")
            return TaskView(what, WAITS, FAILED, why, (f"vivibox start {st.id}",))
        return TaskView("stopped", STOPPED, PARKED, commands=tuple(next_commands(st)))
    if st.state in WAITING:
        return _decision(task, st, max_rounds)
    if st.problem:
        what, _, why = st.problem.partition(": ")
        return TaskView(what, WAITS, FAILED, why, (f"vivibox start {st.id}",))
    if not running:
        if any(e["type"] == "started" for e in task.events()):
            return TaskView("not running", WAITS, IDLE, commands=(f"vivibox start {st.id}",))
        return TaskView("not started", WAITS, IDLE, commands=(f"vivibox start {st.id}",))
    if st.awaiting_review and st.state is State.REVIEW:
        commands = tuple(c.format(id=st.id) for c in AWAITING_REVIEW[1])
        return TaskView(AWAITING_REVIEW[0], WORKS, AT_WORK, commands=commands)
    if st.state is State.IMPLEMENT and prepare.underway(task):
        # The writer's turn waits for the project's preparation; nobody implements yet.
        return TaskView("preparing", WORKS, AT_WORK, commands=(f"vivibox attach {st.id}",))
    return TaskView(activity(st, max_rounds), WORKS, AT_WORK, commands=(f"vivibox attach {st.id}",))


def task_list(
    tasks: list[Task],
    criteria,
    max_rounds: int,
    style: Style,
    now=None,
    running=lambda task: True,
    finished: list[dict] = (),
) -> str:
    """One row per task, like kubectl get: the tasks waiting for you first, the goal fills the rest.
    finished: the history's entries, listed under the live tasks as the view lists them."""
    states = [(task, task.read_state()) for task in tasks]
    seen = [(task, st, view(task, st, running(task), max_rounds)) for task, st in states]
    seen.sort(key=lambda found: found[2].rank)
    header = ("TASK", "STATUS", "CRITERIA", "PLAN", "IMPL", "REVIEW", "CREATED", "UPDATED", "GOAL")
    rows = []
    for task, st, shown in seen:
        spent = cost(task)
        rows.append(
            (
                st.id,
                shown.status,
                criteria(task),
                *cost_cells(spent),
                ago(st.created, now),
                ago(st.updated, now),
                st.goal,
                COLORS[shown.group],
            )
        )
    for entry in finished:
        rows.append(
            (
                entry["id"],
                "deleted" if entry.get("deleted") else "done",
                "-",
                *cost_cells(finished_spend(entry)),
                ago(entry["created"], now) if entry.get("created") else "-",
                ago(entry["finished"], now),
                entry["title"],
                "dim" if entry.get("deleted") else "green",
            )
        )
    fixed = len(header) - 1  # every column but the goal, which gets what they leave
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(fixed)]
    # Piped output keeps the whole goal, for grep.
    goal_width = max(width() - sum(widths) - 3 * fixed, 20) if style.color else 10_000
    lines = ["   ".join([*(h.ljust(w) for h, w in zip(header[:fixed], widths, strict=True)), header[fixed]])]
    for *cells, goal, color in rows:
        padded = [c.ljust(w) for c, w in zip(cells, widths, strict=True)]
        padded[1] = style(padded[1], color)
        lines.append("   ".join([*padded, shorten(goal, goal_width)]))
    return "\n".join(lines) + "\n"


def task_detail(
    task: Task, criteria, max_rounds: int, events: int, style: Style, running: bool = True
) -> str:
    st = task.read_state()
    shown = view(task, st, running, max_rounds)
    meta = [ago(st.updated)] if st.box else [f"{criteria(task)} criteria", ago(st.updated)]
    spent = cost(task)
    if spent or spent.on_subscription:
        meta.append(with_subscription(spent))
    if live := task.live_turn():
        meta.append(f"last step {ago(live['at'])}")
    lines = [
        f"{style(st.id, 'bold')}  {style(shown.status, COLORS[shown.group])}"
        f"  {style(' · '.join(meta), 'dim')}",
        "",
        st.goal,
        "",
        *([] if st.box else [f"{style('Plan', 'bold')}  {task.plan_path}"]),
        *(
            [f"{style('CLI', 'bold')}   planned in {cli_session.describe(st.cli_session)}"]
            if st.cli_session
            else []
        ),
    ]
    if st.state is State.CHECKPOINT_BLOCKED and (why := why_blocked(task)):
        lines.append(f"{style('Why', 'bold')}   {why}")
    if shown.problem:
        lines.append(f"{style('Why', 'bold')}   {style(shown.problem, 'red')}")
    lines += [
        f"{style('Next', 'bold')}  " + "   ".join(shown.commands),
        "",
        style("Recent events", "bold"),
    ]
    cols = width()
    for e in task.events()[-events:]:
        when = clock(e["ts"])
        data = "  ".join(f"{k}={v}" for k, v in e["data"].items() if v not in ("", None))
        kind = style(e["type"].ljust(8), "red" if e["type"] == "error" else "cyan")
        lines.append(f"  {style(when, 'dim')}  {kind}  {shorten(data, cols - 22)}")
    return "\n".join(lines) + "\n"
