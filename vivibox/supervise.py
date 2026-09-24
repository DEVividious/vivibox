"""The supervise command, run by start in the background: the supervisor as the command line
wires it, to the project's gate, your notifications and the review copy.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from . import actions, gate, keys, ntfy, supervisor
from .config import Config, ConfigError, load_config
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

    current = live_config(config)
    stages = ntfy.Stages(
        lambda: channel_for(current()), again=any(e["type"] == "turn" for e in task.events())
    )

    def stepped(st) -> None:
        agent_window(st)
        stages.seen(st)

    sup = make_supervisor(task, project, pod, config, agent_window, current)
    actions.supervising(task)
    print(f"Supervising {task.id}. Your decisions: vivibox accept|reply {task.id}", flush=True)
    stepped(task.read_state())  # a resumed task already has its session
    sup.run(on_step=stepped)
    return 0


def live_config(start: Config) -> Callable[[], Config]:
    """config.toml as it is now, for what may change while a task runs: a topic set under k
    reaches the task from its next message, not from its next start. A file that cannot be read
    at that moment keeps what the supervisor started with."""

    def current() -> Config:
        try:
            return load_config()
        except ConfigError:
            return start

    return current


def channel_for(config: Config, get_key=keys.get_key, stored=keys.list_keys) -> ntfy.Channel | None:
    """The ntfy topic config.toml names, with its token from the key store when there is one."""
    if not config.ntfy:
        return None
    token = get_key(ntfy.TOKEN) if ntfy.TOKEN in stored() else ""
    return ntfy.Channel(f"{config.ntfy_server}/{config.ntfy}", config.ntfy_events, token)


def notifier(task: Task, project, current: Callable[[], Config]):
    """Where the supervisor's messages go: its window and the desktop, and the ntfy topic when
    there is one, at a priority that says whether the task now waits for you or has stopped.
    current: the settings as they are at each message, so a change under k counts at once."""

    def notify(task_id: str, message: str, kind: str = "") -> None:
        config = current()
        supervisor.notify(
            task_id, message, config.desktop_notifications, actions.buttons(task, project, config, kind)
        )
        if channel := channel_for(config):
            st = task.read_state()
            channel.decision(task_id, message, waiting=waits_for_user(st.state), trouble=st.paused)

    return notify


def make_supervisor(
    task: Task,
    project,
    pod,
    config,
    agent_window=lambda st: None,
    current: Callable[[], Config] | None = None,
) -> supervisor.Supervisor:
    """The supervisor as the command line runs it: the gate on the project's commands, your
    notifications, the review copy. The behavioural tests build the same one and step it.
    current: the settings as they are at each message; the ones given, unless told otherwise."""
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
        notify=notifier(task, project, current or (lambda: config)),
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
