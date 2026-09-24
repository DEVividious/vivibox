"""Task files in <tasks_dir>/<id>/.task/: plan, state and the append-only event log."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .states import State, check_transition

TASK_ID = re.compile(r"^(?P<project>[a-z0-9][a-z0-9-]*)-(?P<seq>\d+)$")


# A reason is for reading in the view; the whole of a build's output belongs in its log.
MAX_PROBLEM = 2000


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class TaskState:
    id: str
    project: str
    goal: str
    state: State
    iteration: int
    paused: bool
    created: str
    updated: str
    base_commit: str = ""
    # One conversation per role. A planner and a writer on the same harness would otherwise share
    # one, and a reviewer would review its own writing; a session of one harness handed to another
    # starts an error, not a conversation.
    sessions: dict[str, str] = field(default_factory=dict)
    # A model this task runs a role on, instead of the one in config.toml. Empty means the config
    # decides, which is what almost every task wants.
    models: dict[str, str] = field(default_factory=dict)
    # The tool a role runs in, when the task chose another than config.toml's: a planner you
    # normally plan with yourself can be given a model for one task, and the other way round.
    harnesses: dict[str, str] = field(default_factory=dict)
    # Accept the agent's plan without stopping for you (vivibox new --auto).
    auto_plan: bool = False
    # At the plan checkpoint with a manual planner: no plan yet, the task waits for one from you.
    awaiting_plan: bool = False
    # Why the task is not moving, when that was not your doing: "<what happened>: <the reason in
    # the failing tool's words>". Kept until the task starts again, so the view can go on saying it.
    problem: str = ""
    # A box: the project's pod with no plan and no agent, for you to work in by hand. Its work
    # comes back the way a task's does.
    box: bool = False
    # Rounds of review so far, and how the reviewer works on this task ("" for config.toml's mode).
    reviews: int = 0
    review_mode: str = ""


class Task:
    def __init__(self, root: Path):
        self.root = root
        self.meta = root / ".task"

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def plan_path(self) -> Path:
        return self.meta / "plan.md"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    def reset_iterations(self) -> None:
        """After your decision the agent gets a fresh iteration budget, and the reviewer its rounds."""
        st = self.read_state()
        st.iteration = 1
        st.reviews = 0
        self._write_state(st)

    def set_session(self, role: str, session: str) -> None:
        """This role's session; empty forgets it, and the next turn starts a new one from the
        plan and handoff files."""
        st = self.read_state()
        if session:
            st.sessions[role] = session
        else:
            st.sessions.pop(role, None)
        self._write_state(st)

    def set_role(self, role: str, harness: str = "", model: str = "") -> None:
        """This task's harness and model for a role; both empty give the role back to config.toml."""
        st = self.read_state()
        for chosen, value in ((st.harnesses, harness), (st.models, model)):
            if value:
                chosen[role] = value
            else:
                chosen.pop(role, None)
        self._write_state(st)

    def set_model(self, role: str, model: str) -> None:
        """This task's model for a role; empty gives the role back to config.toml."""
        st = self.read_state()
        if model:
            st.models[role] = model
        else:
            st.models.pop(role, None)
        self._write_state(st)

    def set_box(self) -> None:
        st = self.read_state()
        st.box = True
        self._write_state(st)
        self.event("box")

    def set_paused(self, paused: bool, problem: str = "") -> None:
        """problem: why, when it was not you who stopped it. Starting again is the end of it."""
        st = self.read_state()
        st.paused, st.problem = paused, problem[:MAX_PROBLEM] if paused else ""
        self._write_state(st)
        self.event("paused" if paused else "resumed", **({"problem": st.problem} if st.problem else {}))

    def set_problem(self, problem: str) -> None:
        """Why a task that is not paused is not moving either, such as one that could not start;
        empty once it has."""
        st = self.read_state()
        if st.problem == problem[:MAX_PROBLEM]:
            return
        st.problem = problem[:MAX_PROBLEM]
        self._write_state(st)
        if problem:
            self.event("error", message=st.problem)

    def set_auto_plan(self, auto: bool) -> None:
        st = self.read_state()
        st.auto_plan = auto
        self._write_state(st)
        self.event("auto_plan", enabled=auto)

    def set_awaiting_plan(self, waiting: bool) -> None:
        """Moves updated too: the state stays at the plan checkpoint either way, and a view that
        redraws only on change would keep saying there is nothing to review."""
        st = self.read_state()
        st.awaiting_plan = waiting
        st.updated = now()
        self._write_state(st)

    def set_review_mode(self, mode: str) -> None:
        st = self.read_state()
        st.review_mode = mode
        self._write_state(st)

    def set_reviews(self, reviews: int) -> None:
        st = self.read_state()
        st.reviews = reviews
        self._write_state(st)

    def set_goal(self, goal: str) -> None:
        """The agent's one-line summary of its plan replaces what you typed, in lists and messages."""
        st = self.read_state()
        st.goal = goal
        self._write_state(st)
        self.event("goal", goal=goal)

    def set_base_commit(self, commit: str) -> None:
        st = self.read_state()
        st.base_commit = commit
        self._write_state(st)
        self.event("repo", base_commit=commit)

    def read_state(self) -> TaskState:
        data = json.loads((self.meta / "state.json").read_text())
        data["state"] = State(data["state"])
        # Tasks written before roles carry one session, and it was always the writer's; tasks
        # written before roles owned sessions keep them under the harness's name, and opencode
        # could only be the writer's, claude-code only the planner's.
        if "sessions" not in data and data.get("session"):
            data["sessions"] = {"writer": data["session"]}
        sessions = data.get("sessions") or {}
        for harness, role in (("opencode", "writer"), ("claude-code", "planner")):
            if harness in sessions:
                sessions.setdefault(role, sessions.pop(harness))
        # Fields a newer vivibox added are ignored: a running supervisor may be older than the CLI.
        return TaskState(**{k: v for k, v in data.items() if k in TaskState.__dataclass_fields__})

    def _write_state(self, st: TaskState) -> None:
        path = self.meta / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(st), indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    # What the turn under way has cost so far; written at every step, gone when the turn ends.
    LIVE_TURN = "turn.json"

    def set_live_turn(self, cost: float, tokens: int, steps: int) -> None:
        live = {"cost": round(cost, 6), "tokens": tokens, "steps": steps, "at": now()}
        (self.meta / self.LIVE_TURN).write_text(json.dumps(live))

    def live_turn(self) -> dict | None:
        try:
            return json.loads((self.meta / self.LIVE_TURN).read_text())
        except (OSError, ValueError):
            return None

    def clear_live_turn(self) -> None:
        (self.meta / self.LIVE_TURN).unlink(missing_ok=True)

    def event(self, type_: str, **data) -> None:
        line = json.dumps({"ts": now(), "task": self.id, "type": type_, "data": data}, ensure_ascii=False)
        with (self.meta / "events.jsonl").open("a") as f:
            f.write(line + "\n")

    def events(self) -> list[dict]:
        path = self.meta / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def transition(self, target: State, **data) -> TaskState:
        st = self.read_state()
        check_transition(st.state, target)
        previous = st.state
        st.state, st.updated = target, now()
        # A failed gate uses up an attempt. Coming back from you does not: your reply has just
        # given the agent a fresh budget (reset_iterations), and counting here made it start at 2.
        if target is State.IMPLEMENT and previous is State.VERIFY:
            st.iteration += 1
        self._write_state(st)
        self.event("state", previous=str(previous), current=str(target), **data)
        return st


def create_task(tasks_dir: Path, project: str, goal: str, plan_template: str, after: int = 0) -> Task:
    """after: the highest number already used elsewhere, e.g. by branches in your repository."""
    tasks_dir.mkdir(parents=True, exist_ok=True)
    # Numbers are never reused: a removed task leaves its branch vivibox/<id> in your repository.
    counter = tasks_dir / f".last-{project}"
    last = int(counter.read_text()) if counter.exists() else 0
    seq = 1 + max(
        (
            int(m["seq"])
            for p in tasks_dir.iterdir()
            if (m := TASK_ID.match(p.name)) and m["project"] == project
        ),
        default=last,
    )
    seq = max(seq, last + 1, after + 1)
    while True:
        root = tasks_dir / f"{project}-{seq}"
        try:
            root.mkdir(mode=0o750)
            break
        except FileExistsError:
            seq += 1
    counter.write_text(str(seq))
    task = Task(root)
    for sub in ("handoff", "log"):
        (task.meta / sub).mkdir(parents=True)
    task.plan_path.write_text(plan_template.replace("{{goal}}", goal))
    ts = now()
    task._write_state(TaskState(task.id, project, goal, State.PLAN, 1, False, ts, ts))
    task.event("created", project=project, goal=goal)
    return task


def list_tasks(tasks_dir: Path) -> list[Task]:
    if not tasks_dir.is_dir():
        return []
    tasks = [
        Task(p)
        for p in tasks_dir.iterdir()
        if TASK_ID.match(p.name) and (p / ".task" / "state.json").exists()
    ]
    return sorted(tasks, key=lambda t: t.read_state().created)


def find_task(tasks_dir: Path, task_id: str) -> Task:
    root = tasks_dir / task_id
    if not TASK_ID.match(task_id) or not (root / ".task" / "state.json").exists():
        raise KeyError(f"No task '{task_id}'")
    return Task(root)
