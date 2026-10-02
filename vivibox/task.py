"""Task files in <tasks_dir>/<id>/.task/: plan, state and the append-only event log."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import version
from .config import current_mode
from .states import State, check_transition

TASK_ID = re.compile(r"^(?P<project>[a-z0-9][a-z0-9-]*)-(?P<seq>\d+)$")


# A reason is for reading in the view; the whole of a build's output belongs in its log.
MAX_PROBLEM = 2000

# The layout of a task's files and state that this vivibox runs. A change that a task created
# before it would not meet (what the task's folder holds, what its pod mounts) raises it. A task
# written under another is never run nor written here, and never migrated: one from an earlier
# vivibox is shown and can be deleted, one from a newer vivibox is only shown.
TASK_SCHEMA_VERSION = 1
CURRENT, EARLIER, NEWER = "current", "earlier", "newer"
# What a task is opened for (find_task): an operation that runs, moves or opens it needs the
# layout; showing it does not; deleting it needs a layout this vivibox knows the resources of.
RUN, SHOW, DELETE = "run", "show", "delete"


class TaskLayoutError(Exception):
    """A task of another layout, opened for what needs this one."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class TaskState:
    id: str
    project: str
    goal: str
    state: State
    # How many verifications have run.
    iteration: int
    paused: bool
    created: str
    updated: str
    base_commit: str = ""
    # The local branch the task started on, which accepted work can be committed on; "" when it
    # started on a detached HEAD or a remote branch.
    base_branch: str = ""
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
    # You plan in an agent's CLI on your machine (vivibox new --plan-in-cli), which reads your
    # checkout itself: no report on the repository is written for it, and its prompt is the CLI's.
    plan_in_cli: bool = False
    # In REVIEW with the review in an agent's CLI (ADR-0035): the prompt is written, and the task
    # waits for the review the CLI brings in.
    awaiting_review: bool = False
    # The agent's CLI session a task planned there came from (cli_session.detect): tool, id and
    # the path of its transcript, where what the session spent on the task is read (ADR-0036).
    cli_session: dict[str, str] = field(default_factory=dict)
    # Why the task is not moving, when that was not your doing: "<what happened>: <the reason in
    # the failing tool's words>". Kept until the task starts again, so the view can go on saying it.
    problem: str = ""
    # A box: the project's pod with no plan and no agent, for you to work in by hand. Its work
    # comes back the way a task's does.
    box: bool = False
    # Reviews written so far, which number their files (handoff/review-N.md).
    reviews: int = 0
    # Fix turns the writer was sent on since the plan was accepted or you last replied: after a
    # red gate or a review with blocking notes. The first implementation is none.
    rounds: int = 0
    # How this task is shared between the roles, and how many fix turns it gets: "" and 0 for
    # config.toml's.
    orchestration: str = ""
    max_rounds: int = 0
    # The commit the last green verification ran on: what the review and you are shown is it.
    verified_commit: str = ""
    # Why the writer was sent on its last fix turn (what the gate found, or the review's count),
    # for the row to say beside the round; "" before the first.
    round_reason: str = ""
    # The layout the task was created under (TASK_SCHEMA_VERSION), the only thing that says
    # whether this vivibox runs it. 0: a task from before the field (v0.1.x).
    task_schema_version: int = 0
    # The vivibox that created it (version.current()): for a bug report, never for compatibility.
    created_by_version: str = ""


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

    def reset_rounds(self) -> None:
        """After your decision the writer gets its fix turns back."""
        st = self.read_state()
        st.rounds, st.round_reason = 0, ""
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

    def set_plan_in_cli(self, in_cli: bool) -> None:
        st = self.read_state()
        st.plan_in_cli = in_cli
        self._write_state(st)

    def set_cli_session(self, session: dict[str, str]) -> None:
        st = self.read_state()
        st.cli_session = dict(session)
        self._write_state(st)
        self.event("cli_session", tool=session["tool"], id=session["id"])

    def set_awaiting_review(self, waiting: bool) -> None:
        st = self.read_state()
        st.awaiting_review = waiting
        st.updated = now()
        self._write_state(st)

    def set_awaiting_plan(self, waiting: bool) -> None:
        """Moves updated too: the state stays at the plan checkpoint either way, and a view that
        redraws only on change would keep saying there is nothing to review."""
        st = self.read_state()
        st.awaiting_plan = waiting
        st.updated = now()
        self._write_state(st)

    def set_orchestration(self, mode: str, max_rounds: int = 0) -> None:
        """This task's own way of sharing the work and its own round limit; "" and 0 follow config.toml."""
        st = self.read_state()
        st.orchestration, st.max_rounds = mode, max_rounds
        self._write_state(st)

    def set_verified_commit(self, commit: str) -> None:
        st = self.read_state()
        st.verified_commit = commit
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

    def set_base_commit(self, commit: str, branch: str = "") -> None:
        st = self.read_state()
        st.base_commit, st.base_branch = commit, branch
        self._write_state(st)
        self.event("repo", base_commit=commit)

    def read_state(self) -> TaskState:
        path = self.meta / "state.json"
        data = json.loads(path.read_text())
        if classify(data.get("task_schema_version", 0)) != CURRENT:
            return self._read_other(data, path)
        data["state"] = State(data["state"])
        # A flow chosen under a name it has since changed (config.RENAMED_MODES).
        data["orchestration"] = current_mode(data.get("orchestration", ""))
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

    def _read_other(self, data: dict, path: Path) -> TaskState:
        """The state of a task of another layout, for showing it: what it has of the fields this
        vivibox knows, of the types it knows them by; a state it does not know as written; the
        rest from the folder. Whatever else a newer vivibox keeps is left alone, never written."""
        when = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="milliseconds")
        found = TASK_ID.match(self.id)
        shape = asdict(
            TaskState(self.id, found["project"] if found else "", "", State.PLAN, 0, False, when, when)
        )
        known = {
            k: v
            for k, v in data.items()
            if k in shape and type(v) is type(shape[k]) and (k not in ("created", "updated") or _iso(v))
        }
        try:
            state = State(data.get("state"))
        except ValueError:
            state = str(data.get("state", ""))  # a newer vivibox's own, as it wrote it
        raw = data.get("task_schema_version", 0)
        return TaskState(**{**shape, **known, "id": self.id, "state": state, "task_schema_version": raw})

    def _write_state(self, st: TaskState) -> None:
        # Behind every operation: a task of another layout is never written over, which would
        # drop what a newer vivibox keeps and move a task this one does not run.
        if why := layout_problem(st):
            raise TaskLayoutError(why)
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
        # iteration counts the verifications run; rounds the fix turns the writer was sent on by
        # the gate or the review. Coming back from you is neither: your reply gave the writer its
        # rounds back (reset_rounds).
        if target is State.VERIFY:
            st.iteration += 1
        if target is State.IMPLEMENT and previous in (State.VERIFY, State.REVIEW):
            st.rounds += 1
            st.round_reason = str(data.get("why", ""))
        self._write_state(st)
        self.event("state", previous=str(previous), current=str(target), **data)
        return st


def _iso(text: str) -> bool:
    """A time the view can count from: ISO, with its zone."""
    try:
        return datetime.fromisoformat(text).tzinfo is not None
    except ValueError:
        return False


def layout(st: TaskState) -> str:
    """CURRENT, EARLIER (no version, or an older one) or NEWER (a higher one, or one that is not a
    whole number, which no vivibox this one knows wrote)."""
    return classify(st.task_schema_version)


def classify(number) -> str:
    """layout() of a version as state.json holds it."""
    if isinstance(number, bool) or not isinstance(number, int) or number > TASK_SCHEMA_VERSION:
        return NEWER
    return CURRENT if number == TASK_SCHEMA_VERSION else EARLIER


def layout_problem(st: TaskState) -> str:
    """Why this vivibox does not run the task, and what to do instead; "" for a task it runs."""
    if (found := layout(st)) == EARLIER:
        return (
            f"{st.id} was created by an earlier vivibox, whose tasks this one does not run; "
            f"delete it and create it again: vivibox delete {st.id}"
        )
    if found == NEWER:
        return (
            f"{st.id} was created by a newer vivibox, whose tasks this one can neither run nor "
            "delete; use that vivibox, or update this one"
        )
    return ""


def check_layout(task: Task, use: str) -> None:
    """Refuses to open a task for what its layout does not allow here (RUN, SHOW, DELETE). Reads
    the task's state and writes nothing."""
    st = task.read_state()
    if use != SHOW and (why := layout_problem(st)) and (use == RUN or layout(st) == NEWER):
        raise TaskLayoutError(why)


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
    task._write_state(
        TaskState(
            task.id, project, goal, State.PLAN, 0, False, ts, ts,
            task_schema_version=TASK_SCHEMA_VERSION, created_by_version=version.current(),
        )
    )  # fmt: skip
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


def find_task(tasks_dir: Path, task_id: str, use: str = RUN) -> Task:
    """The task, opened for use: RUN, the default, for anything that runs, moves or opens it,
    which a task of another layout refuses (check_layout); SHOW to look at it; DELETE."""
    root = tasks_dir / task_id
    if not TASK_ID.match(task_id) or not (root / ".task" / "state.json").exists():
        raise KeyError(f"No task '{task_id}'")
    task = Task(root)
    check_layout(task, use)
    return task
