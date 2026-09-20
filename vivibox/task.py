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
    # One harness conversation each: a planner on claude-code and a writer on opencode do not share
    # a session, and handing one the other's id starts an error, not a conversation.
    sessions: dict[str, str] = field(default_factory=dict)
    # Accept the agent's plan without stopping for you (vivibox new --auto).
    auto_plan: bool = False


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
        """After your decision the agent gets a fresh iteration budget."""
        st = self.read_state()
        st.iteration = 1
        self._write_state(st)

    def set_session(self, harness: str, session: str) -> None:
        """This harness's session; empty forgets it, and the next turn starts a new one from the
        plan and handoff files."""
        st = self.read_state()
        if session:
            st.sessions[harness] = session
        else:
            st.sessions.pop(harness, None)
        self._write_state(st)

    def set_paused(self, paused: bool) -> None:
        st = self.read_state()
        st.paused = paused
        self._write_state(st)
        self.event("paused" if paused else "resumed")

    def set_auto_plan(self, auto: bool) -> None:
        st = self.read_state()
        st.auto_plan = auto
        self._write_state(st)
        self.event("auto_plan", enabled=auto)

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
        # Tasks written before roles carry one session, and it was always opencode's.
        if "sessions" not in data and data.get("session"):
            data["sessions"] = {"opencode": data["session"]}
        # Fields a newer vivibox added are ignored: a running supervisor may be older than the CLI.
        return TaskState(**{k: v for k, v in data.items() if k in TaskState.__dataclass_fields__})

    def _write_state(self, st: TaskState) -> None:
        path = self.meta / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(st), indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

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
        if target is State.IMPLEMENT and previous in (State.VERIFY, State.CHECKPOINT_BLOCKED):
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
