"""The sections of a task's details panel, after its header and Next: the reviewer's notes, the
acceptance criteria, the roles with their turns and cost, and the plan. The same sections in the
same order in every state, each only when it has something to say; panel.detail puts them under
what the state itself asks of you.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import gate, orchestration, reviewing, ui
from .config import ConfigError, load_config
from .plan import PlanError, parse_plan
from .plan import body as plan_body
from .roles import role_of
from .states import State
from .task import Task, TaskState

# A role's turns come from the states it works in, for events written before turns named their role.
ROLE_OF_STATE = {**dict.fromkeys(ui.PLANNING_STATES, "planner"), str(State.REVIEW): "reviewer"}


def criteria(task: Task) -> str:
    try:
        if (task.meta / gate.ACCEPTED_PLAN).exists():
            total = len(parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria)
            return f"{total - len(gate.missing_criteria(task))}/{total}"
        return f"0/{len(parse_plan(task.plan_path.read_text()).criteria)}"
    except (OSError, PlanError, gate.GateError):
        return "-"


def checklist(task: Task) -> list[str]:
    """Every criterion of the accepted plan, and which of them the agent reports as met."""
    try:
        wanted = [c.text for c in parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria]
        missing = set(gate.missing_criteria(task))
    except (OSError, PlanError, gate.GateError):
        return []
    return [f"- {'☐' if text in missing else '☑'} {text}" for text in wanted]


def read(path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def last_gate(task: Task) -> str:
    """How the last gate run went, so a checklist that has not moved still shows whether work has."""
    for event in reversed(task.events()):
        if event["type"] == "gate":
            outcome = "passed" if event["data"].get("passed") else "failed"
            return f"Verification {outcome} at {ui.clock(event['ts'])}."
    return "No verification yet."


def gate_failed(task: Task) -> bool:
    gates = [e for e in task.events() if e["type"] == "gate"]
    return bool(gates) and not gates[-1]["data"].get("passed")


def build_files_left(task: Task) -> list[str]:
    """The build files the last verification found in a project that runs nothing: a new product's
    first task made its build, and the command is yours to pick."""
    for event in reversed(task.events()):
        if event["type"] == "gate":
            return [
                f"**{source} names `{command}`**, and this project runs nothing yet: `e` on its row picks it."
                for command, source in event["data"].get("build_files") or []
            ]
    return []


def reviewers_notes(task: Task) -> list[str]:
    """The newest review, with its counts in the heading, the notes as written."""
    path = reviewing.latest(task)
    if path is None:
        return []
    text = path.read_text()
    review = reviewing.parse_review(text)
    n = reviewing.NUMBERED.match(path.name).group(1)
    return [
        "",
        f"#### Review {n}: {len(review.blocking)} blocking, {len(review.not_blocking)} not blocking",
        "",
        text.rstrip(),
    ]


def criteria_section(task: Task) -> list[str]:
    """What the task is still short of. The agent ticks these itself and the gate only checks that
    none is left open, so a tick is what the agent claims, not something vivibox saw."""
    items = checklist(task)
    if not items:
        return []
    return [
        "",
        f"#### Acceptance criteria {criteria(task)}",
        "",
        "As the agent reports them.",
        "",
        *items,
    ]


@dataclass
class Agent:
    """One conversation of the task: the roles it plays, what it runs on, its turns so far."""

    roles: str
    runs_on: str
    turns: int = 0
    cost: float = 0.0


def agents(task: Task, st: TaskState) -> list[Agent]:
    """The task's agents in the order they work, with the turns and the cost of each; an agent
    the mode has but that never took a turn is listed too, so a missing reviewer shows."""
    try:
        config = load_config()
    except ConfigError:
        return []
    mode = orchestration.mode_of(task, config)
    found: dict[str, Agent] = {}
    for role in orchestration.ROLES:
        agent = mode.agents[role]
        if agent not in config.roles:  # no reviewer configured: nobody reviews
            continue
        if agent not in found:
            chosen = role_of(task, agent, config)
            found[agent] = Agent(mode.brief_of(agent), chosen.model or "you, in your chat")
    for event in task.events():
        if event["type"] != "turn":
            continue
        data = event["data"]
        role = data.get("role") or ROLE_OF_STATE.get(data.get("state"), "writer")
        # The agent the turn names, else the one this mode gives its role; one no longer
        # configured still took the turn, and its cost is in the total.
        agent = data.get("agent") if data.get("agent") in found else mode.agents.get(role, role)
        found.setdefault(agent, Agent(role, "-"))
        found[agent].turns += 1
        found[agent].cost += data.get("cost") or 0
    # The turn under way, so far: its event comes when it ends.
    agent = mode.agents.get(ROLE_OF_STATE.get(str(st.state), "writer"))
    if (live := task.live_turn()) and agent in found:
        found[agent].cost += live.get("cost") or 0
    return list(found.values())


def roles_section(task: Task, st: TaskState) -> list[str]:
    """Who plays each role, on what, and how many turns and dollars it took: one row per
    conversation, so roles one agent plays together share a row."""
    rows = agents(task, st)
    if not rows or st.box:
        return []
    name = lambda roles: " + ".join(roles.split("-")).capitalize()  # noqa: E731
    lines = [
        "",
        "#### Roles",
        "",
        "| Role | Runs on | Turns | Cost |",
        "|---|---|---|---|",
        *(f"| {name(a.roles)} | {a.runs_on} | {a.turns} | {ui.money(a.cost)} |" for a in rows),
    ]
    if len(rows) > 1:
        lines.append(f"| Total | | {sum(a.turns for a in rows)} | {ui.money(sum(a.cost for a in rows))} |")
    return lines


def plan_section(task: Task) -> list[str]:
    """The accepted plan, last: what the task is held to, once it is no longer the decision."""
    text = read(task.meta / gate.ACCEPTED_PLAN)
    return ["", "#### The plan", "", plan_body(text)] if text else []
