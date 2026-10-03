"""How a task is shared between the planner, the writer and the reviewer (ADR: orchestration
modes). A mode says which agent plays each role, whether the writer reviews its own work before
the verification, and who reads the work after a green gate: a reviewer of its own in the review
container, or the planner, as the supervisor, in its control container. A planner that is an
agent other than the writer's runs in that container, in a network of its own (Mode.control).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import claudecode, manual, opencode
from .config import DEFAULT_ORCHESTRATION, ORCHESTRATION_MODES, Config, Role, current_mode
from .task import Task

ROLES = ("planner", "writer", "reviewer")


@dataclass(frozen=True)
class Mode:
    name: str
    # Each role, and the agent (a role's name: its conversation and its model) that plays it.
    agents: dict[str, str]
    # The writer reviews its own work in a turn after each of its turns, before the verification.
    self_review: bool = False
    # A reviewer of its own reads the work after a green verification, in the review container.
    separate_reviewer: bool = False
    # The planner, with the plan still in its conversation, reads the work after a green gate in
    # its control container, on its copy of the writer's commits.
    supervisor: bool = False

    def brief_of(self, agent: str) -> str:
        """The brief an agent opens with: the roles it plays, planner-writer-reviewer at most."""
        return "-".join(role for role in ROLES if self.agents[role] == agent)

    @property
    def control(self) -> bool:
        """Whether the planner is an agent other than the writer's: its conversation is then one
        the writer must not reach, and it runs in a control container of its own, in a network
        of its own. An agent that plans and writes in one conversation has nothing to keep apart."""
        return self.agents["planner"] != self.agents["writer"]

    def reviews(self) -> bool:
        """Whether a reviewer other than the writer reads the work at all."""
        return self.separate_reviewer or self.supervisor


MODES = {
    "single_agent": Mode(
        "single_agent", {"planner": "planner", "writer": "planner", "reviewer": "planner"}, self_review=True
    ),
    "planner_executor": Mode(
        "planner_executor", {"planner": "planner", "writer": "writer", "reviewer": "writer"}, self_review=True
    ),
    "planner_writer_reviewer": Mode(
        "planner_writer_reviewer",
        {"planner": "planner", "writer": "writer", "reviewer": "reviewer"},
        separate_reviewer=True,
    ),
    "supervisor_worker": Mode(
        "supervisor_worker",
        {"planner": "planner", "writer": "writer", "reviewer": "planner"},
        supervisor=True,
    ),
}
assert set(MODES) == set(ORCHESTRATION_MODES)
DEFAULT = MODES[DEFAULT_ORCHESTRATION]


def recorded(task: Task | None) -> Mode | None:
    """The flow the task was created with, as its state records it; None for no task, and for
    one whose state names none (made without actions.create)."""
    chosen = task.read_state().orchestration if task else ""
    return MODES.get(current_mode(chosen)) if chosen else None


def mode_of(task: Task | None, config: Config) -> Mode:
    """A task's flow is the one it was created with (actions.create records it), whatever
    config.toml says since: config.toml's flow is for new tasks, and for a caller without a task."""
    return recorded(task) or MODES[current_mode(config.orchestration)]


def recorded_control(task: Task | None) -> bool | None:
    """Whether the task's recorded flow has a control container, from the task alone, no
    configuration read; None when it records none."""
    mode = recorded(task)
    if mode is None:
        return None
    return mode.control and not task.read_state().box


def has_control(task: Task | None, config: Config) -> bool:
    """Whether the task has a control container and the planner's network: the one answer the
    pod, the keys, the harnesses and the window ask. A box has no planner at all."""
    if task is not None and task.read_state().box:
        return False
    return mode_of(task, config).control


def max_rounds_of(task: Task | None, config: Config) -> int:
    chosen = task.read_state().max_rounds if task else 0
    return chosen or config.max_rounds


def problem(mode_name: str, planner: Role, plan_in_cli: bool = False) -> str:
    """Why a mode cannot run on this planner, or "" when it can. An agent that plans and writes in
    one conversation cannot be you in your own chat, nor a tool that cannot write. A supervisor can
    be you only in an agent's CLI, which reads the work where a chat in a browser cannot."""
    mode = MODES[current_mode(mode_name)]
    if mode.agents["writer"] == "planner" and planner.harness != opencode.NAME:
        return (
            f"{mode_name} needs a planner that can write: put the planner on an opencode model "
            "(provider/model), or pick planner_executor"
        )
    if mode.supervisor and planner.harness not in (opencode.NAME, claudecode.NAME) and not plan_in_cli:
        return (
            f"{mode_name} needs a planner that runs on a model, not {manual.NAME}: with a manual "
            "planner pick planner_executor or planner_writer_reviewer, or plan in an agent's CLI "
            "(vivibox new --plan-in-cli), which then reviews each round"
        )
    return ""
