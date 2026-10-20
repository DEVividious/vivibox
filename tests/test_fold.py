"""fold: a task's events folded once for every reader that adds them up. The four readers that
each had a loop of their own (the list's cost, the panel's roles, u and `vivibox usage`, `vivibox
stats`) give what they gave before: each is run here against a copy of the loop it had, field by
field, on the same tasks, with the order sums are made in kept, so a figure rounded for a screen
cannot come out a cent apart."""

import random
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vivibox import display, orchestration, sections, stats, ui, usage
from vivibox.cli import main
from vivibox.config import ConfigError, load_config
from vivibox.fold import fold
from vivibox.roles import role_of
from vivibox.states import State
from vivibox.task import Task, TaskState, create_task, list_tasks, record

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=9)


# --- The loops as they were before fold (main at cdec4c0), copied, not imported. ---

OLD_PLANNING_STATES = {str(State.PLAN), str(State.CHECKPOINT_PLAN)}
OLD_ROLE_OF_STATE = {**dict.fromkeys(OLD_PLANNING_STATES, "planner"), "review": "reviewer"}
OLD_COLUMN_OF_ROLE = {"planner": "plan", "writer": "write", "reviewer": "review"}
OLD_REASONS = (
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


def _old_stage(state):
    if state in OLD_PLANNING_STATES:
        return "planning"
    return "review" if state == str(State.REVIEW) else "implementation"


def old_cost(task: Task) -> ui.Spend:
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
        stage = _old_stage(data.get("state"))
        if data.get("metered") is False:
            subscription[stage] = subscription.get(stage, 0.0) + spent
        elif stage == "planning":
            planning += spent
        elif stage == "review":
            review += spent
        else:
            implementation += spent
    st = task.read_state()
    if live := task.live_turn():
        if str(st.state) in OLD_PLANNING_STATES:
            planning += live["cost"]
        elif st.state is State.REVIEW:
            review += live["cost"]
        else:
            implementation += live["cost"]
    return ui.Spend(
        round(planning, 6),
        round(implementation, 6),
        round(review, 6),
        split=not st.box,
        subscription={k: round(v, 6) for k, v in subscription.items()},
        subscription_unknown=frozenset(unknown),
        subscription_tokens=tokens,
        subscription_limits=dict(limits),
    )


def old_of_events(task_id, project, events, live, now=None) -> usage.Usage:
    now = now or datetime.now(UTC)
    used = usage.Usage(task_id, project, live)
    started = None
    verifying = ""
    for event in events:
        kind, data, ts = event["type"], event.get("data", {}), event["ts"]
        if kind == "turn":
            if data.get("metered") is False:
                used.subscription += data.get("cost") or 0
            else:
                used.cost += data.get("cost") or 0
        elif kind == "cli_usage":
            if data.get("cost") is None:
                used.subscription_unknown = True
            else:
                used.subscription += data["cost"]
        if kind == "turn_started":
            role = data.get("role") or OLD_ROLE_OF_STATE.get(data.get("state"), "writer")
            started = (OLD_COLUMN_OF_ROLE.get(role, "write"), ts)
        elif kind == "turn" and started:
            column, since = started
            setattr(used, column, getattr(used, column) + usage.seconds_between(since, ts))
            started = None
        elif kind == "state" and data.get("current") == "verify":
            verifying = ts
        elif kind == "gate":
            if isinstance(data.get("seconds"), (int, float)):
                used.gate += data["seconds"]
            elif verifying:
                used.gate += usage.seconds_between(verifying, ts)
            verifying = ""
    if started and live:
        column, since = started
        setattr(used, column, getattr(used, column) + usage.seconds_between(since, now))
    if events:
        done = next(
            (e["ts"] for e in events if e["type"] == "state" and e["data"].get("current") == "done"), ""
        )
        end = done or (now if live else events[-1]["ts"])
        used.total = usage.seconds_between(events[0]["ts"], end)
    return used


def old_add_task(found: stats.Stats, events, live, since="") -> None:
    events = [e for e in events if e.get("ts", "") >= since]
    if not events:
        return
    found.tasks += 1
    found.live += live
    runs = 0
    passed_at = 0
    for event in events:
        kind, data = event["type"], event.get("data", {})
        if kind == "gate":
            runs += 1
            found.gates += 1
            if data.get("passed"):
                found.gates_passed += 1
                passed_at = passed_at or runs
            else:
                for key, said in OLD_REASONS:
                    if data.get(key):
                        found.reasons[said] += 1
        elif kind == "turn":
            found.turns += 1
            found.failed_turns += data.get("ok") is False
            role = data.get("role") or data.get("kind") or "?"
            state = data.get("state") or data.get("kind") or "?"
            for bucket in (found.by_role[role], found.by_state[state]):
                bucket[0] += data.get("cost") or 0
                bucket[1] += data.get("tokens") or 0
                bucket[2] += 1
        elif kind == "cli_usage":
            if data.get("cost") is None:
                found.unpriced += 1
            else:
                found.subscription_used += data["cost"]
        elif kind == "turn_retry":
            found.retries += 1
    if passed_at:
        found.iterations.append(passed_at)
    elif runs:
        found.never_passed += 1


def old_collect(sources, since="") -> stats.Stats:
    found = stats.Stats()
    for events, live in sources:
        old_add_task(found, events, live, since)
    return found


def old_agents(task: Task, st: TaskState) -> list[sections.Agent]:
    try:
        config = load_config()
    except ConfigError:
        return []
    mode = orchestration.mode_of(task, config)
    found: dict[str, sections.Agent] = {}
    for role in orchestration.ROLES:
        agent = mode.agents[role]
        if agent in found:
            continue
        if agent in config.roles:
            chosen = role_of(task, agent, config)
            found[agent] = sections.Agent(
                mode.brief_of(agent), display.words(chosen.model) or "you, in your chat"
            )
        elif agent == "reviewer" and mode.separate_reviewer:
            model = display.words(role_of(task, "writer", config).model)
            found[agent] = sections.Agent("reviewer", f"{model} (the writer's)")
    for event in task.events():
        if event["type"] != "turn":
            continue
        data = event["data"]
        role = data.get("role") or OLD_ROLE_OF_STATE.get(data.get("state"), "writer")
        agent = data.get("agent") if data.get("agent") in found else mode.agents.get(role, role)
        found.setdefault(agent, sections.Agent(role, "-"))
        found[agent].turns += 1
        if data.get("metered") is not False:
            found[agent].cost += data.get("cost") or 0
    agent = mode.agents.get(OLD_ROLE_OF_STATE.get(str(st.state), "writer"))
    if (live := task.live_turn()) and agent in found:
        found[agent].cost += live.get("cost") or 0
    return list(found.values())


# --- The tasks: the cases the code and its tests name, then many drawn at random. ---


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def events_of(*entries) -> list[dict]:
    """(kind, data) in order, a minute apart."""
    return [
        {"ts": at(60 * i), "task": "demo-1", "type": kind, "data": data}
        for i, (kind, data) in enumerate(entries)
    ]


ALL_REASONS = {key: ["x"] for key, _ in OLD_REASONS}

NAMED = {
    # usage's own: each role's turns, a gate that says its seconds and one from before they did, done.
    "roles, gates and done": events_of(
        ("created", {"project": "demo", "goal": "Goal"}),
        ("turn_started", {"state": "plan", "role": "planner"}),
        ("turn", {"state": "plan", "role": "planner", "cost": 0.01}),
        ("state", {"previous": "checkpoint:plan", "current": "implement"}),
        ("turn_started", {"state": "implement", "role": "writer"}),
        ("turn", {"state": "implement", "role": "writer", "cost": 0.02}),
        ("state", {"previous": "implement", "current": "verify"}),
        ("gate", {"passed": False, "seconds": 118.5, "failed_commands": ["npm test"]}),
        ("turn_started", {"state": "implement", "role": "writer"}),
        ("turn", {"state": "implement", "role": "writer", "cost": 0.01}),
        ("state", {"previous": "implement", "current": "verify"}),
        ("gate", {"passed": True}),
        ("turn_started", {"state": "review", "role": "reviewer"}),
        ("turn", {"state": "review", "role": "reviewer", "cost": 0.01}),
        ("state", {"previous": "checkpoint:final", "current": "done"}),
        ("turn", {"state": "implement", "role": "writer", "cost": 0.5}),  # after done
        ("state", {"current": "done"}),  # done again: the first counts
    ),
    "a turn under way": events_of(
        ("created", {}),
        ("turn_started", {"state": "implement", "role": "writer"}),
    ),
    "turns from before they named their role": events_of(
        ("created", {}),
        ("turn_started", {"state": "plan"}),
        ("turn", {"state": "plan", "cost": 0.4, "tokens": 1}),
        ("turn_started", {"state": "review"}),
        ("turn", {"state": "review", "cost": 0.05}),
        ("turn_started", {"state": "implement"}),
        ("turn", {"state": "implement", "cost": 0.06}),
    ),
    # ui's own: the split by state, answering comments on the plan is planning, demos are not.
    "the split by state": events_of(
        ("turn", {"state": "plan", "cost": 0.40, "tokens": 1}),
        ("turn", {"state": "checkpoint:plan", "cost": 0.10, "tokens": 1}),
        ("turn", {"state": "implement", "cost": 0.06, "tokens": 1}),
        ("turn", {"state": "checkpoint:final", "cost": 0.02, "tokens": 1}),
        ("turn", {"cost": 0.01, "tokens": 1, "kind": "demo"}),
    ),
    # stats' own: a pass at the second run, reasons, a failed and a retried turn.
    "stats, passed at the second run": events_of(
        ("created", {}),
        ("turn", {"state": "plan", "role": "planner", "ok": True, "cost": 0.4, "tokens": 4000}),
        ("turn", {"state": "implement", "role": "writer", "ok": True, "cost": 0.1, "tokens": 2000}),
        ("gate", {"passed": False, "failed_commands": ["npm test"], "missing_criteria": 1}),
        ("turn", {"state": "implement", "role": "writer", "ok": True, "cost": 0.3, "tokens": 6000}),
        ("gate", {"passed": True, "failed_commands": []}),
    ),
    "stats, never passed": events_of(
        ("created", {}),
        ("turn_retry", {"wait": 30, "error": "429"}),
        ("turn", {"state": "plan", "role": "planner", "ok": False, "cost": 0.0, "tokens": 0, "error": "x"}),
        ("gate", {"passed": False, "failed_commands": ["npm test"], "environment": "docker down"}),
        ("turn", {"cost": 0.05, "tokens": 500, "kind": "demo"}),
    ),
    "every reason, and a pass that names some": events_of(
        ("gate", {"passed": False, **ALL_REASONS}),
        ("gate", {"passed": False, "environment": "x", "build_skipped": True, "uncommitted": []}),
        ("gate", {"passed": True, "failed_commands": ["x"]}),
        ("gate", {"passed": False, "risky_stopped": 1}),
    ),
    # The CLI's use (ADR-0036): by stage, unpriced, tokens of every kind, Codex's limits.
    "planned in the CLI": events_of(
        (
            "cli_usage",
            {"stage": "planning", "model": "claude-opus-5-5", "tokens": {"output": 1000}, "cost": 0.41},
        ),
        ("turn", {"state": "implement", "role": "writer", "cost": 0.06, "tokens": 1}),
        ("cli_usage", {"stage": "review", "model": "m", "tokens": {}, "cost": 0.15}),
        (
            "cli_usage",
            {"stage": "conversation", "model": "m", "tokens": {"input": 7, "x": "y"}, "cost": 0.20},
        ),
        ("cli_usage", {"stage": "review", "cost": None, "problem": "transcript not read"}),
        ("cli_usage", {"stage": "planning", "cost": 0.1, "limits": {"5h": 5.0, "week": 1.0}}),
        ("cli_usage", {"stage": "planning", "cost": 0.2, "limits": {}}),  # empty keeps the last
        ("cli_usage", {"cost": 0.3}),  # no stage: conversation
        ("cli_usage", {"stage": None, "cost": None}),
    ),
    "nothing on keys": events_of(("cli_usage", {"stage": "planning", "cost": 0.41})),
    "only unpriced": events_of(("cli_usage", {"stage": "review", "cost": None})),
    # A turn on a subscription, which ui and u keep apart from money and stats does not.
    "turns on a subscription": events_of(
        ("turn", {"state": "plan", "role": "planner", "metered": False, "cost": 0.3}),
        ("cli_usage", {"stage": "planning", "cost": 0.1}),
        ("turn", {"state": "implement", "role": "writer", "metered": False, "cost": 0.2}),
        ("turn", {"state": "implement", "role": "writer", "metered": True, "cost": 0.1}),
        ("turn", {"state": "review", "role": "reviewer", "metered": False, "cost": None}),
        ("turn", {"state": "plan", "role": "planner", "metered": None, "cost": 0.7}),
    ),
    # Figures absent or empty, a role and a state that disagree, agents named and not.
    "figures absent": events_of(
        ("turn", {}),
        ("turn", {"cost": None, "tokens": None, "ok": None}),
        ("turn", {"cost": 0, "role": "", "state": ""}),
        ("turn", {"state": "review", "role": "writer", "cost": 0.2, "kind": "self-review"}),
        ("turn", {"state": "plan", "role": "reviewer", "cost": 0.3, "agent": "planner"}),
        ("turn", {"state": "implement", "role": "writer", "agent": "ghost", "cost": 0.4}),
        ("turn", {"state": "implement", "role": "oracle", "cost": 0.5}),
    ),
    "times out of order": events_of(
        ("turn", {"state": "implement", "cost": 0.1}),  # ended, never started
        ("turn_started", {"state": "implement", "role": "writer"}),
        ("turn_started", {"state": "review", "role": "reviewer"}),  # started again: the last counts
        ("turn", {"state": "review", "role": "reviewer"}),
        ("gate", {"passed": False}),  # no verify before it, no seconds
        ("state", {"current": "verify"}),
        ("gate", {"passed": True, "seconds": "soon"}),  # seconds not a number: from verify
        ("state", {"current": "verify"}),
        ("gate", {"passed": True, "seconds": 7}),
        ("gate", {"passed": False, "seconds": True}),
        ("turn_started", {"role": "oracle"}),  # a role with no column: write
    ),
    "only created": events_of(("created", {"project": "demo"})),
    "nothing": [],
    "unknown kinds between": events_of(
        ("serena", {}),
        ("turn_started", {"state": "plan", "role": "planner"}),
        ("demo_commit", {"cost": 9.0}),
        ("turn", {"state": "plan", "role": "planner", "cost": 0.25}),
        ("cost_warning", {"cost": 3}),
    ),
    # Sums whose order shows in the last bit: 0.1 + 0.2 + 0.3 is not 0.1 + (0.2 + 0.3).
    "sums whose order shows": events_of(
        *(
            ("turn", {"state": state, "role": role, "cost": cost, "tokens": 3})
            for state, role, cost in [
                ("plan", "planner", 0.1),
                ("implement", "writer", 0.2),
                ("plan", "planner", 0.2),
                ("implement", "writer", 0.3),
                ("plan", "planner", 0.3),
                ("review", "reviewer", 0.123455),
                ("implement", "writer", 1e-7),
                ("review", "reviewer", 0.000005),
            ]
        ),
        ("cli_usage", {"stage": "review", "cost": 0.1}),
        ("turn", {"state": "review", "metered": False, "cost": 0.2}),
        ("cli_usage", {"stage": "review", "cost": 0.3}),
    ),
}


def drawn(seed: int) -> list[dict]:
    """A task of events picked at random from what the writers write, with the figures sums go
    wrong on, its own seed each so a failure names the one it was."""
    pick = random.Random(seed).choice
    states = ["plan", "checkpoint:plan", "implement", "verify", "review", "checkpoint:final", None]
    costs = [None, 0, 0.0, 0.1, 0.2, 0.3, 0.123455, 1e-6, 1.15, 2]
    absent = object()

    def some(**choices):
        return {k: v for k, options in choices.items() if (v := pick(options)) is not absent}

    makers = [
        lambda: ("turn_started", some(state=states, role=["planner", "writer", "reviewer", None, absent])),
        lambda: (
            "turn",
            some(
                state=states + [absent],
                role=["planner", "writer", "reviewer", "", absent],
                kind=[absent, absent, "demo", "self-review", "history"],
                agent=[absent, "planner", "writer", "reviewer", "planner-writer", "ghost"],
                metered=[absent, True, True, False],
                ok=[absent, True, True, False],
                cost=costs + [absent],
                tokens=[absent, None, 0, 1000, 4321],
            ),
        ),
        lambda: (
            "cli_usage",
            some(
                stage=["planning", "review", "conversation", "implementation", None, absent],
                cost=[None, 0.41, 0.15, 0.1, 0.2, 0.3],
                tokens=[absent, {}, {"output": 1000, "input": 5}, {"x": "y", "cache_read": 3}],
                limits=[absent, {}, {"5h": 5.0}, {"5h": 6.0, "week": 2.0}],
            ),
        ),
        lambda: ("state", some(current=["verify", "verify", "done", "implement", "review"])),
        lambda: (
            "gate",
            {
                **some(passed=[True, False, False], seconds=[absent, 118.5, 60, "x"]),
                **dict([pick(list(ALL_REASONS.items()))]),
            },
        ),
        lambda: ("turn_retry", {"wait": 30}),
        lambda: ("serena", {}),
    ]
    return events_of(("created", {}), *(pick(makers)() for _ in range(random.Random(seed).randint(0, 40))))


TASKS = {**NAMED, **{f"drawn {seed}": drawn(seed) for seed in range(60)}}


def as_written(task: Task, events: list[dict]) -> Task:
    (task.meta / "events.jsonl").write_bytes(b"".join(record(e) for e in events))
    return task


# --- Old against new. ---


@pytest.mark.parametrize("name", TASKS)
@pytest.mark.parametrize("live", [False, True])
def test_usage_gives_what_its_loop_gave(name, live):
    events = TASKS[name]
    new = usage.of_events("demo-1", "demo", events, live=live, now=NOW)
    assert new == old_of_events("demo-1", "demo", events, live=live, now=NOW)


@pytest.mark.parametrize("name", TASKS)
def test_the_list_s_cost_is_what_its_loop_gave(name, tmp_path):
    task = as_written(create_task(tmp_path, "demo", "Goal", ""), TASKS[name])
    for state in (None, State.PLAN, State.CHECKPOINT_PLAN, State.IMPLEMENT, State.REVIEW):
        if state is not None:
            task._write_state(replace(task.read_state(), state=state))
            task.set_live_turn(0.03, 300, 2)
        new, old = ui.cost(task), old_cost(task)
        assert new == old
        assert list(new.subscription) == list(old.subscription), "a history line keeps this order"
        assert [str(new), ui.cost_cells(new), ui.with_subscription(new)] == [
            str(old),
            ui.cost_cells(old),
            ui.with_subscription(old),
        ]


def test_a_box_s_cost_is_what_its_loop_gave(tmp_path):
    task = as_written(create_task(tmp_path, "demo", "Goal", "", box=True), TASKS["the split by state"])
    assert ui.cost(task) == old_cost(task) and not ui.cost(task).split


def stats_seen(found: stats.Stats) -> tuple:
    """Everything a Stats holds, with the order of what a report lists in the order it was met."""
    return (
        stats.as_dict(found),
        stats.report(found),
        {k: v for k, v in vars(found).items() if k not in ("by_role", "by_state")},
        list(found.reasons.items()),
        list(found.by_role.items()),
        list(found.by_state.items()),
        found.cost,
        found.subscription,
    )


@pytest.mark.parametrize("since", ["", at(0), at(60 * 7), at(60 * 30), "2027"])
def test_stats_give_what_their_loop_gave(since):
    sources = [(events, i % 3 == 0) for i, events in enumerate(TASKS.values())]
    assert stats_seen(stats.collect(sources, since)) == stats_seen(old_collect(sources, since))


def test_stats_of_each_task_alone_give_what_their_loop_gave():
    for name, events in TASKS.items():
        assert stats_seen(stats.collect([(events, True)])) == stats_seen(old_collect([(events, True)])), name


def test_the_roles_agents_and_costs_are_what_their_loop_gave(env):
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    (task,) = list_tasks(load_config().tasks_dir)
    for name, events in TASKS.items():
        as_written(task, events)
        task.clear_live_turn()
        for live in (False, True):
            if live:
                task.set_live_turn(0.03, 300, 2)
            st = task.read_state()
            assert sections.agents(task, st) == old_agents(task, st), name


def test_the_old_loops_held_what_the_tests_of_their_readers_say():
    """The copies above are the loops the readers had: they give what those readers' own tests ask."""
    events = TASKS["roles, gates and done"][:15]
    used = old_of_events("demo-1", "demo", events, live=False, now=NOW)
    assert (used.plan, used.write, used.review, used.gate, used.total) == (60, 120, 60, 178.5, 840)
    found = old_collect(
        [(TASKS["stats, passed at the second run"], False), (TASKS["stats, never passed"], True)]
    )
    assert (found.turns, found.failed_turns, found.retries, found.cost) == (5, 1, 1, 0.85)


# --- fold itself: what the readers rely on it for. ---


def test_a_task_folded_in_two_parts_adds_up_to_it_folded_whole():
    """fold(a + b) on sums that do not hang on order: a task's events read in two halves add up,
    a turn started in one and ended in the other aside; turn.json is no event and is not in it."""
    for name, events in TASKS.items():
        cut = len(events) // 2
        whole, first, second = fold(events), fold(events[:cut]), fold(events[cut:])
        assert whole.turns == first.turns + second.turns, name
        assert whole.cli == first.cli + second.cli, name
        assert whole.gates == first.gates + second.gates, name
        assert whole.retries == first.retries + second.retries, name
        assert whole.unknown == first.unknown | second.unknown, name
        assert whole.cli_tokens == first.cli_tokens + second.cli_tokens, name
        assert whole.cli_limits == (second.cli_limits or first.cli_limits), name
        for stage in ("planning", "implementation", "review"):
            assert whole.on_keys.get(stage, 0) == pytest.approx(
                first.on_keys.get(stage, 0) + second.on_keys.get(stage, 0)
            ), name


def test_a_turn_is_read_one_way():
    turn = fold(
        events_of(("turn", {"state": "checkpoint:plan", "cost": None, "tokens": None, "metered": None}))
    ).turns[0]
    assert (turn.stage, turn.played, turn.cost, turn.tokens, turn.metered) == (
        "planning",
        "planner",
        0,
        0,
        True,
    )
    demo = fold(events_of(("turn", {"kind": "demo", "cost": 0.01}))).turns[0]
    assert (demo.stage, demo.played, demo.kind, demo.role, demo.state) == (
        "implementation",
        "writer",
        "demo",
        None,
        None,
    )
    on_subscription = fold(events_of(("turn", {"state": "review", "metered": False, "cost": 0.2})))
    assert on_subscription.on_keys == {} and on_subscription.subscription == {"review": 0.2}


def test_only_fold_reads_what_a_turn_or_a_cli_s_use_cost():
    """The readers took a turn's cost, whether it was on a subscription, and a CLI's use apart
    each in its own loop; one reading now, so they cannot drift apart again."""
    package = Path(__file__).parent.parent / "vivibox"
    reads = re.compile(r'\.get\("metered"\)|== "cli_usage"')
    found = sorted(
        path.name
        for path in package.rglob("*.py")
        if reads.search(path.read_text()) and path.name != "fold.py"
    )
    assert found == [], "fold.fold reads a task's turns and the CLI's use; the rest read Folded"
