"""The supervise command, run by start in the background: the supervisor as the command line
wires it, to the project's gate, your notifications and the review copy.
"""

from __future__ import annotations

import argparse

from . import actions, gate, keys, ntfy, supervisor
from .config import Config, load_config
from .risky import Approvals
from .states import waits_for_user
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

    channel = channel_for(config)
    stages = ntfy.Stages(channel, again=any(e["type"] == "turn" for e in task.events())) if channel else None

    def stepped(st) -> None:
        agent_window(st)
        if stages:
            stages.seen(st)

    sup = make_supervisor(task, project, pod, config, agent_window, channel)
    actions.supervising(task)
    print(f"Supervising {task.id}. Your decisions: vivibox accept|reply {task.id}", flush=True)
    stepped(task.read_state())  # a resumed task already has its session
    sup.run(on_step=stepped)
    return 0


def channel_for(config: Config, get_key=keys.get_key, stored=keys.list_keys) -> ntfy.Channel | None:
    """The ntfy topic config.toml names, with its token from the key store when there is one."""
    if not config.ntfy:
        return None
    token = get_key(ntfy.TOKEN) if ntfy.TOKEN in stored() else ""
    return ntfy.Channel(f"{config.ntfy_server}/{config.ntfy}", config.ntfy_events, token)


def notifier(task: Task, project, config: Config, channel: ntfy.Channel | None):
    """Where the supervisor's messages go: its window and the desktop, and the ntfy topic when
    there is one, at a priority that says whether the task now waits for you or has stopped."""

    def notify(task_id: str, message: str, kind: str = "") -> None:
        supervisor.notify(
            task_id, message, config.desktop_notifications, actions.buttons(task, project, config, kind)
        )
        if channel:
            st = task.read_state()
            channel.decision(task_id, message, waiting=waits_for_user(st.state), trouble=st.paused)

    return notify


def make_supervisor(
    task: Task, project, pod, config, agent_window=lambda st: None, channel: ntfy.Channel | None = None
) -> supervisor.Supervisor:
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
        notify=notifier(task, project, config, channel),
        prepare_review=lambda: actions.prepare_review(task, project),
        save_verify=lambda commands, no_build: actions.save_verify(project, commands, no_build),
        session_started=agent_window,
    )
    return supervisor.Supervisor(
        task,
        harness,
        ports,
        max_iterations=config.max_iterations,
        cost_warning=config.cost_warning,
        cost_limit=config.cost_limit,
        project_verify=project.verify,
        project_no_build=project.no_build,
        planner=planner,
        source=project.repo,
    )
