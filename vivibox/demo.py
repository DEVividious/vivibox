"""Running the project for you to look at, in the task's pod, and the line between that and the
task's own work. Reached through actions, like everything the view and the command line call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    actions,
    display,
    gate,
    handoff,
    opencode,
    probe,
    repo,
)
from . import init as project_init
from .config import (
    Project,
)
from .pod import Pod, PodError
from .states import State
from .task import Task, TaskState

DEMO_FILE = "demo.md"
# Its own question and its own session: the supervisor watches handoff/question.md to decide when a
# task stops for you, so working out how to run something must never be able to halt the work itself.
DEMO_QUESTION = "demo-question.md"
DEMO_SESSION = "demo-session"
BASH_BLOCK = re.compile(r"```(?:bash|sh|shell)\n(.*?)```", re.DOTALL)
# Longer than this and the agent has written documentation instead of an instruction.
MAX_INSTRUCTION = 3000
# The commits of the agent's own named to you, newest first; the rest are counted.
NAMED_COMMITS = 10

DEMO_ASK = """Work out how to start this project so a person can open it in a browser, and write
that down. Do not change what the project does.

Read the README and the build files first; they usually say. You may run commands to check that
what you found works.

Two things decide whether it is usable:
- The app must listen on 0.0.0.0. Bound to localhost it answers inside this container only, and
  nothing outside can reach it. Most dev servers bind localhost unless told otherwise.
- Anything the app needs to come up (a database, for instance) has to be started first. You have a
  Docker daemon here, so a container is a fine way to do that.

Write it to /task/handoff/demo.md as short markdown: the commands in ```bash blocks, in the order
they must run, with the app itself last. They are run from the root of the repository, and a later
task runs them again from a clone at another path, so write them from there: never a cd into this
clone's own path. Around them put only what a person needs to know, in a
line or two. This is a note on how to start the project, not documentation: if it grows past a
screen, you have misunderstood it.

If a decision is mine rather than yours - which profile, which port, which of two ways the project
can be run - do not guess. Write the question to /task/handoff/demo-question.md and leave demo.md
alone. I will answer and you can carry on. End the turn when demo.md is written, or when the
question is."""


@dataclass
class Demo:
    """What happened when the project was started for you to look at."""

    commands: list[str] = field(default_factory=list)
    # Where it came from: this task's instruction, your project file, the repository, the agent.
    source: str = ""
    address: str = ""
    listening: list[probe.Listener] = field(default_factory=list)
    log: str = ""
    question: str = ""
    # An instruction from an earlier task, waiting for you to say it still applies.
    proposed: str = ""
    # Nothing listens yet, but the command is still running: a slow install is not a failure, and
    # calling it one sends you looking for a broken app instead of waiting a moment longer.
    starting: bool = False
    # What the agent had left listening in its container and was stopped to make room, so you
    # know what went and why the app you see is the current code.
    stopped: list[str] = field(default_factory=list)
    # What the agent committed while working out how to run it, newest first: the work goes on from
    # there, and nothing else would say where those commits came from.
    committed: list[str] = field(default_factory=list)

    @property
    def urls(self) -> list[str]:
        return [f"http://{self.address}:{p.port}" for p in self.listening if p.reachable]

    @property
    def unreachable(self) -> list[probe.Listener]:
        return [p for p in self.listening if not p.reachable]


def instruction_commands(text: str) -> list[str]:
    """The commands of an instruction: its shell blocks, in order. The prose around them is yours."""
    return [line for block in BASH_BLOCK.findall(text) for line in block.strip().splitlines() if line.strip()]


def demo_instruction(task: Task) -> str:
    """Never through a link or a special file: a file of yours with shell blocks in it would be run
    in the pod, where the agent sees it. Refused, nothing is run and you are told why."""
    try:
        return handoff.read_text(task.meta / "handoff" / DEMO_FILE).strip()
    except handoff.HandoffError as e:
        raise PodError(str(e)) from e


def write_instruction(task: Task, text: str) -> Path:
    path = task.meta / "handoff" / DEMO_FILE
    try:
        handoff.write_text(path, text.rstrip() + "\n")
    except handoff.HandoffError as e:
        raise PodError(str(e)) from e
    return path


def demo_from_history(project_name: str, task: Task) -> str:
    """How the last task you accepted in this project was run, offered for this task. A record,
    never run on its own. A path into the earlier task's directory becomes the same path in this
    one's: that directory is gone once its work is accepted, and saying yes to the instruction
    must not run it against a directory that no longer exists."""
    for entry in actions.history(limit=50):
        if entry["project"] == project_name and entry.get("demo") and not entry.get("deleted"):
            return for_this_task(entry["demo"], task.root.parent / entry["id"], task.root)
    return ""


def for_this_task(text: str, earlier: Path, root: Path) -> str:
    """The earlier task's directory named as this task's, and only as a whole name: snake-1 is not
    the start of snake-10."""
    return re.sub(re.escape(str(earlier)) + r"(?![\w.-])", lambda _: str(root), text)


def demo_commands(project: Project, task: Task) -> tuple[list[str], str]:
    """This task's own instruction first, then what you set, then the repository's own answer."""
    if found := instruction_commands(demo_instruction(task)):
        return found, "task"
    if project.demo:
        return list(project.demo), "project"
    if found := project_init.detect_demo(task.repo):
        return found, "compose"
    return [], ""


def ask_agent_how_to_run(task: Task, pod: Pod, reply: str = "") -> tuple[str, str]:
    """A conversation of its own, in a session of its own, so that neither its questions nor its
    failures can touch the task's state. Returns the instruction it wrote and anything it asks."""
    folder = task.meta / "handoff"
    (folder / DEMO_QUESTION).unlink(missing_ok=True)
    kept = folder / DEMO_SESSION
    try:  # it goes to the pod as an argument the agent can read: never a link's file
        session = handoff.read_text(kept).strip() if reply else ""
    except handoff.HandoffError as e:
        raise PodError(str(e)) from e
    try:
        turn = opencode.OpenCode(pod).turn(
            reply or DEMO_ASK, session=session, title=f"{task.id}-demo", on_step=task.set_live_turn
        )
    finally:
        task.clear_live_turn()
    task.event("turn", cost=turn.cost, tokens=turn.tokens, kind="demo")  # spent, whatever follows
    if turn.session:
        try:
            handoff.write_text(kept, turn.session)
        except handoff.HandoffError as e:
            raise PodError(str(e)) from e
    if not turn.ok:
        said = display.under(turn.error or turn.text)  # the agent's words: no line of them at the left edge
        raise PodError(f"the agent could not work out how to run this: {said}")
    text = demo_instruction(task)
    if len(text) > MAX_INSTRUCTION:
        raise PodError(
            f"the agent wrote {len(text)} characters of instruction; that is documentation, not a "
            f"note on how to start the project. Ask it again, or write {DEMO_FILE} yourself"
        )
    try:  # a FIFO would hold run app; a link's file would read as the agent's question to you
        return text, handoff.read_text(folder / DEMO_QUESTION).strip()
    except handoff.HandoffError as e:
        raise PodError(str(e)) from e


def demo_allowed(st: TaskState) -> bool:
    """Whether the project may be run in the task's pod now: in a box, or once the work is back with
    you. While the agent implements or the gate verifies, the pod and its working tree are theirs:
    a second build on the same tree, or a server on a port their tests want, would get in the way.
    The view's v and the demo command ask here."""
    return bool(st.box) or st.state in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED)


def demo(task_id: str, ask: bool = True, reply: str = "", wait: float = 40) -> Demo:
    """Runs the project in the task's pod and watches for it to listen. Nothing here moves the task
    between states: working out how to run something must not be able to stop the work."""
    task, project = actions.load(task_id)
    if not demo_allowed(task.read_state()):
        raise gate.GateError(
            f"could not run the app: the agent is at work in the pod of {task_id}; "
            "run it once the work is back with you"
        )
    pod = actions.task_pod(task_id)
    pod.up()
    commands, source = demo_commands(project, task)
    question = ""
    if not commands and (earlier := demo_from_history(project.name, task)) and not reply:
        return Demo(source="history", address=pod.address(), proposed=earlier)
    committed: list[str] = []
    if not commands and (ask or reply):
        question, committed = ask_naming_commits(task, pod, reply)
        commands, source = instruction_commands(demo_instruction(task)), "agent"
    if not commands:
        return Demo([], source, pod.address(), question=question, committed=committed)
    stopped = pod.stop_leftovers()
    heard = pod.demo_start(commands, workdir=str(task.repo), wait=wait)
    still_going = not heard and pod.demo_running()
    return Demo(
        commands,
        source,
        pod.address(),
        heard,
        pod.demo_log(),
        question,
        starting=still_going,
        stopped=stopped,
        committed=committed,
    )


def ask_naming_commits(task: Task, pod: Pod, reply: str) -> tuple[str, list[str]]:
    """ask_agent_how_to_run, and what the agent committed in the task's clone meanwhile: named
    whether the turn ends well or not, since the work goes on from those commits either way. A
    failure says itself first and the commits after it; git that cannot read the clone then
    leaves the failure as it was, and says why at the next git."""
    before = _head(task)
    try:
        _, question = ask_agent_how_to_run(task, pod, reply)
    except BaseException as e:
        try:
            committed = commits_since(task, before)
        except repo.RepoError:
            committed = []
        if committed and isinstance(e, Exception):
            said = e.args[0] if e.args else e
            made = ", ".join(committed)
            raise PodError(
                f"{said}\nWhile working out how to run the app, the agent committed {made} in the task's "
                "clone; the work goes on from that commit: look at it before you accept"
            ) from e
        raise
    return question, commits_since(task, before)


def _head(task: Task) -> str:
    return repo.in_clone(task.repo, "rev-parse", "HEAD").stdout.strip()


def commits_since(task: Task, before: str) -> list[str]:
    """The commits the task's branch gained since before, newest first, short, the newest
    NAMED_COMMITS of them and how many more; the new head alone when the branch moved elsewhere.
    Recorded as an event, for the timeline."""
    after = _head(task)
    if after == before:
        return []
    listed = repo.in_clone(task.repo, "rev-list", "--abbrev-commit", "--abbrev=7", f"{before}..{after}")
    found = listed.stdout.split() or [after[:7]]
    if len(found) > NAMED_COMMITS:
        found = [*found[:NAMED_COMMITS], f"and {len(found) - NAMED_COMMITS} more"]
    task.event("demo_commit", commits=found)
    return found


def use_instruction(task_id: str, text: str, wait: float = 40) -> Demo:
    """Takes on an instruction you have read and approved, for this task only."""
    task, project = actions.load(task_id)
    write_instruction(task, text)
    return demo(task_id, ask=False, wait=wait)


def demo_stop(task_id: str) -> None:
    actions.task_pod(task_id).demo_stop()
