"""The supervise command, run by start in the background: the supervisor as the command line
wires it, to the project's gate, your notifications and the review copy.
"""

from __future__ import annotations

import argparse

from . import actions, gate, supervisor
from .config import load_config
from .risky import Approvals
from .task import Task


def cmd_supervise(args: argparse.Namespace) -> int:
    config = load_config()
    task, project = actions.load(args.task)
    pod = actions.task_pod(task.id)
    harness = actions.harness_for("writer", pod, task)

    def agent_window(st) -> None:
        # Your view of the agent, ready once its conversation exists; reopened if you closed it.
        if session := actions.watchable_session(task, st):
            actions.agent_view(task, harness.attach_command(session))

    sup = make_supervisor(task, project, pod, config, agent_window)
    actions.supervising(task)
    print(f"Supervising {task.id}. Your decisions: vivibox accept|reply {task.id}", flush=True)
    agent_window(task.read_state())  # a resumed task already has its session
    sup.run(on_step=agent_window)
    return 0


def make_supervisor(task: Task, project, pod, config, agent_window=lambda st: None) -> supervisor.Supervisor:
    """The supervisor as the command line runs it: the gate on the project's commands, your
    notifications, the review copy. The behavioural tests build the same one and step it."""
    harness = actions.harness_for("writer", pod, task)
    planner = actions.harness_for("planner", pod, task)
    ports = supervisor.Ports(
        run_gate=lambda t: gate.run_gate(
            t,
            pod,
            actions.verify_commands(t, project),
            project.risky_extra,
            project.java,
            timeout=project.verify_timeout or config.verify_timeout,
            no_build=project.no_build,
        ),  # fmt: skip
        risky_changes=lambda: Approvals(task.meta, task.repo, project.risky_extra).changes(),
        notify=lambda task_id, message, kind="": supervisor.notify(
            task_id, message, config.desktop_notifications, actions.buttons(task, project, config, kind)
        ),
        prepare_review=lambda: actions.prepare_review(task, project),
        save_verify=lambda commands, no_build: actions.save_verify(project, commands, no_build),
        session_started=agent_window,
    )
    return supervisor.Supervisor(
        task,
        harness,
        ports,
        max_iterations=config.max_iterations,
        project_verify=project.verify,
        project_no_build=project.no_build,
        planner=planner,
        source=project.repo,
    )
    print(f"{task.id} is done.")
    return 0
