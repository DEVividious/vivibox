"""vivibox command line."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import (
    actions,
    cli_agent,
    cli_host,
    cli_pod,
    clone_read,
    context,
    display,
    gate,
    harness,
    image,
    keys,
    providers,
    repo,
    risky,
    timeline,
    ui,
    version,
)
from . import init as project_init
from . import pod as pod_module
from .cli_decisions import cmd_accept, cmd_approve_risky, cmd_plan, cmd_reply, cmd_risky, cmd_verify_again
from .cli_host import cmd_image_build, cmd_image_check, cmd_mirror
from .cli_providers import cmd_auth, cmd_models
from .cli_reports import cmd_stats, cmd_usage
from .config import ORCHESTRATION_MODES, ConfigError, config_dir, current_mode, load_config
from .plan import KINDS, PlanError, parse_plan
from .pod import Pod, PodError
from .supervise import cmd_supervise
from .task import CURRENT, DELETE, SHOW, Task, TaskLayoutError, find_task, layout, list_tasks


def cmd_new(args: argparse.Namespace) -> int:
    description = sys.stdin.read() if args.goal == "-" else args.goal
    roles = {}
    for pair in args.model:
        role, sep, model = pair.partition("=")
        if not sep or not model.strip():
            raise ConfigError(f"--model {pair}: expected role=model, e.g. writer=deepseek/deepseek-v4-pro")
        roles[role.strip()] = actions.parse_choice(model)
    # A task that starts at once needs the agent image: said before anything is created, not
    # after, as a task that stands as "could not start". A draft needs it only when it starts.
    if not args.draft and not image.exists(image.image_ref()):
        raise PodError("the task was not created: the agent image is not built; run 'vivibox image build'")
    task = actions.create(
        args.project,
        description,
        auto=args.auto,
        kind=args.kind,
        roles=roles,
        no_build=args.no_build,
        base_ref=args.base,
        orchestration=args.flow,
        plan_in_cli=args.plan_in_cli,
    )
    print(f"Created {task.id} from {task.read_state().base_commit[:10]}")
    for note in actions.context_notes(task):
        print(f"Note: {display.under(note)}")
    print(f"Plan: {task.plan_path}")
    if args.draft:
        print(f"Edit the plan if you like, then 'vivibox start {task.id}'.")
        return 0
    args.task = task.id
    return cmd_start(args)


def _criteria(task: Task) -> str:
    """Before the plan is accepted: criteria in the plan. After: how many the agent has ticked."""
    try:
        if (task.meta / gate.ACCEPTED_PLAN).exists():
            total = len(parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria)
            return f"{total - len(gate.missing_criteria(task))}/{total}"
        return f"0/{len(parse_plan(task.plan_path.read_text()).criteria)}"
    except (OSError, PlanError):
        return "plan?"


def cmd_init(args: argparse.Namespace) -> int:
    where = Path(args.path).expanduser().resolve()
    found = actions.propose_project(where)
    name = args.name or found.name
    if args.verify:
        found.verify = [args.verify]
    if not found.verify:
        found.notes.append(f"verification: {actions.WRITER_PROPOSES}; --verify sets it now")
    if args.prepare is not None:
        found.prepare = [args.prepare] if args.prepare.strip() else []
    found.notes.append(
        f"a new task's clone runs first: {' && '.join(found.prepare) or 'nothing'}; --prepare changes it"
    )
    print(f"Project {name} for {found.repo}:")
    for note in found.notes:
        print(f"  - {display.under(note)}")
    print()
    print(display.terminal(project_init.render(found)))
    confirmed = args.yes or (
        sys.stdin.isatty() and input(f"Set up {name}? [Y/n] ").strip().lower() in ("", "y", "yes")
    )
    if not confirmed:
        print("Nothing written.")
        return 1
    target = actions.setup_project(
        where, name, found.verify, found.java, create=args.git, prepare=found.prepare
    )
    print(f"Wrote {target}. Start a task with n in 'vivibox'.")
    if not (config_dir() / "config.toml").exists():
        print(f"There is no {config_dir() / 'config.toml'} yet; run vivibox to set it up.")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """What happened to a task, one line each, on your clock."""
    task, _ = actions.load(args.task, SHOW)
    print(timeline.render(task), end="")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    style = ui.Style(ui.use_color())
    if args.task:
        task = find_task(config.tasks_dir, args.task, SHOW)
        running, unknown = still_running([task])
        print(ui.task_detail(task, _criteria, config.max_rounds, args.events, style, running(task)), end="")
        print(unknown, end="")
        return 0
    tasks = list_tasks(config.tasks_dir)
    live = {task.id for task in tasks}
    finished = [e for e in actions.history() if e["id"] not in live]
    if not tasks and not finished:
        print("No tasks. Create one with 'vivibox new <project> \"<goal>\"'.")
        return 0
    running, unknown = still_running(tasks)
    print(
        ui.task_list(tasks, _criteria, config.max_rounds, style, running=running, finished=finished), end=""
    )
    print(unknown, end="")
    return 0


# How long vivibox status waits for Docker to say which pods are up.
DOCKER_SECONDS = 5


def still_running(tasks: list[Task]) -> tuple[Callable[[Task], bool], str]:
    """Whether a task's supervisor runs; for a task this vivibox does not run, also whether its pod
    is up, which an earlier vivibox may have left. Read from the pid file and Docker, never stopped.
    With it, what to say when Docker did not answer in time: nothing is claimed then."""
    others = {Pod(t.id, Path("."), "").sidecar for t in tasks if layout(t.read_state()) != CURRENT}
    up, unknown = set(), ""
    try:
        up = set(pod_module.addresses(sorted(others), timeout=DOCKER_SECONDS))
    except subprocess.TimeoutExpired:
        unknown = (
            f"Docker did not answer in {DOCKER_SECONDS} s: vivibox could not tell whether the pods of the"
            " tasks it does not run are still up; ask again (vivibox status) once `docker ps` answers.\n"
        )
    except OSError:  # no docker, and so no pod
        pass
    return (lambda t: actions.supervisor_running(t) or Pod(t.id, Path("."), "").sidecar in up), unknown


def ensure_image() -> None:
    """The view starts with the agent image ready: built the first time, and again whenever its
    definition changed, which an update of vivibox can do."""
    ref = image.image_ref()
    if image.exists(ref):
        # Also frees the images that the last pods on them held until now.
        image.remove_old(ref)
        return
    print(f"Building the agent image {ref}; the first time takes a few minutes.", flush=True)
    image.build()
    failed = [check.name for check, ok, _ in image.run_checks(ref) if not ok]
    if failed:
        raise PodError(f"the agent image fails its checks ({', '.join(failed)}); see: vivibox image check")


def cmd_start(args: argparse.Namespace) -> int:
    """Starts the task; one started before goes on from where it was, as the view's s does."""
    task, _ = actions.load(args.task)
    resume = any(e["type"] == "started" for e in task.events())
    model = actions.start(args.task, resume=resume)
    shown = display.name(model)
    print(f"Started {args.task} ({shown}). Watch the agent: vivibox attach {args.task} (Ctrl-q leaves).")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    actions.stop(task, force=args.force)
    lost = " by force; the turn under way is lost," if args.force else ";"
    print(f"Stopped {task.id}{lost} its work and history are kept. Continue with 'vivibox start {task.id}'.")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task, DELETE)
    st = task.read_state()
    if not args.yes:
        doing = ui.view(task, st, actions.supervisor_running(task), load_config().max_rounds).status
        try:
            answer = input(f"Delete {task.id} ({doing}) and all its work, without accepting it? [y/N] ")
        except EOFError:  # run from a script: nobody to answer
            print(
                f"\nvivibox: {task.id} was not deleted: there is no terminal to confirm it on; "
                f"add --yes to delete without asking.",
                file=sys.stderr,
            )
            return 1
        if answer.strip().lower() != "y":
            return 1
    if worktree := actions.remove(task, project):
        print(f"Deleted the review copy {worktree}.")
    print(f"Deleted {task.id}; a line in the history, and its archive, stay.")
    return 0


def on_a_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def cmd_attach(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    if not on_a_terminal():  # tmux could not attach, and the agent's window would stay open
        print(f"vivibox: attach needs a terminal; run it in one: vivibox attach {task.id}", file=sys.stderr)
        return 1
    command = actions.attach_command(args.task)
    subprocess.run(command, env=actions.outside_tmux())
    if not task.read_state().box:  # a box's shell keeps its state between visits
        actions.close_agent_view(args.task)
    return 0


def cmd_follow_verification(args: argparse.Namespace) -> int:
    """What the window under w runs while a verification does: not for your own shell."""
    import signal

    from . import logs

    task, _ = actions.load(args.task)
    logs.follow_verification(task, Path(args.log), sys.stdout)
    signal.pause()  # the window stays on the word until Ctrl-q, which takes it down
    return 0


def cmd_pod(args: argparse.Namespace) -> int:
    pod = actions.task_pod(args.task)
    if args.action == "up":
        if not image.exists(pod.image):
            raise PodError(f"image {pod.image} is not built; run 'vivibox image build'")
        pod.up()
        print(f"{pod.agent} running")
    elif args.action == "down":
        pod.down()
    elif args.action == "rm" and (left := pod.remove()):
        raise PodError(left)
    elif args.action == "shell":
        os.execvp("docker", ["docker", "exec", "-it", pod.agent, "bash"])
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    pod = actions.task_pod(task.id)
    pod.up()
    commands = actions.verify_commands(task, project)
    config = load_config()
    result = gate.run_gate(
        task, pod, commands, project.risky_extra, project.java,
        timeout=project.verify_timeout or config.verify_timeout, no_build=project.no_build,
        no_command=actions.missing_command(task, project), tools=project.tools,
    )  # fmt: skip
    for command, source in ((display.name(c), display.name(s)) for c, s in result.build_files):
        print(f"NOTE  {source} names `{command}`, and the project runs nothing yet: set verify in its file")
    if said := "; ".join(filter(None, [risky.stop_said(result.risky_stopped, task.id), result.environment])):
        print(f"ENV   verification could not run: {display.under(said)}")
    if result.build_skipped:
        print(f"SKIP  the build was not run: {result.build_skipped}")
    if result.unchanged:
        print("SAME  nothing committed since the last verification; its result stands")
    for c in result.commands:
        print(f"{'PASS' if c.ok else 'FAIL'}  {display.name(c.command)}  ({c.seconds}s)")
    for c in result.missing_criteria:
        print(f"TODO  criterion not ticked: {display.name(c)}")
    for p in result.commit_problems:
        print(f"FAIL  commit {display.under(p)}")
    for h in result.hidden_characters:
        h = display.name(h)
        print(f"FAIL  {h}" if h.endswith(clone_read.NOT_CHECKED) else f"FAIL  invisible character at {h}")
    for f in result.uncommitted:
        print(f"FAIL  not committed: {display.name(f)}")
    for f in result.no_red_evidence:
        print(f"FAIL  no red evidence in red.md for: {display.name(f)}")
    for t in result.removed_tests:
        print(f"GONE  test removed: {display.name(t)}")
    for c in result.risky:
        print(f"RISK  {c.kind}: {display.name(c.path)}")
    print(f"\nLog: {result.log}")
    if result.risky:
        print(
            f"Risky files changed: review with 'vivibox risky {task.id}' before opening the task in IntelliJ."
        )
    return 0 if result.passed else 1


def cmd_demo(args: argparse.Namespace) -> int:
    if args.stop:
        actions.demo_stop(args.task)
        print("Stopped.")
        return 0
    result = actions.demo(args.task, ask=not args.no_ask, reply=args.reply or "")
    if result.proposed and not args.yes:
        ran = display.block(result.proposed)
        print(f"The last task you accepted in this project was run like this:\n\n  {ran}\n")
        print(f"If that still applies: vivibox demo {args.task} --yes")
        return 0
    if result.proposed:
        result = actions.use_instruction(args.task, result.proposed)
    if result.committed:
        made = display.name(", ".join(result.committed))
        print(f"While working out how to run the app, the agent committed {made} in the task's clone; the "
              "work goes on from that commit: look at it before you accept.\n")  # fmt: skip
    if result.question:
        print(f"The agent asks:\n\n  {display.block(result.question)}\n")
        print(f'Answer with: vivibox demo {args.task} --reply "…"')
        return 1
    if not result.commands:
        print(
            "Nothing here says how to run this project. Add 'demo' to its project file, "
            'for example demo = ["npm run dev"], or let the agent work it out.'
        )
        return 1
    where = {
        "task": "this task's instruction",
        "project": "your project file",
        "compose": "the repository's compose file",
        "agent": "the agent, written to this task's instruction",
    }
    print(f"From {where.get(result.source, result.source)}:")
    for command in result.commands:
        print(f"  {display.name(command)}")
    for url in result.urls:
        print(f"\nListening: {url}")
    for listener in result.unreachable:
        print(f"\nPort {listener.port} is {listener.why_not}.\nStart it on 0.0.0.0 instead.")
    said = display.block(result.log or "(none)")  # the app's output: under vivibox's line, never beside it
    if result.starting:
        # Still installing or compiling. Calling that a failure sends you debugging a working app.
        print(f"\nStill starting, nothing listening yet. Its output so far:\n  {said}")
        print(f"Look again with 'vivibox status {args.task}'.")
        return 0
    if not result.listening:
        print(f"\nIt stopped without listening. Last output:\n  {said}")
        return 1
    return 0


def cmd_box(args: argparse.Namespace) -> int:
    """A box: the project's pod for you to work in, with no agent."""
    if args.new:
        where = Path(args.new).expanduser().resolve()
        found = actions.propose_project(where)
        name = args.project or found.name
        actions.setup_project(where, name, found.verify, found.java, create=True)
        print(f"Set up the project {name} in {where}.")
    elif not args.project:
        raise ConfigError("which project? vivibox box <project>, or --new <folder> for one from scratch")
    else:
        name = args.project
    task = actions.open_box(name)
    print(f"Opened {task.id}: a pod with the project's clone, your keys and tools, and no agent.")
    print(f"Enter it: vivibox attach {task.id} (Ctrl-q leaves; the box stays).")
    print(f"Bring its work back: vivibox accept {task.id}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vivibox",
        description="Run AI agent tasks in isolated pods. Without a command: the interactive view.",
    )
    p.add_argument("--version", action="version", version=f"vivibox {version.current()}")
    # Without a command: the interactive view.
    sub = p.add_subparsers(dest="command")

    init = sub.add_parser("init", help="set up the repository you are in as a project")
    init.add_argument("path", nargs="?", default=".", help="a directory in the repository (default: here)")
    init.add_argument("--name", help="project name (default: the repository's directory name)")
    init.add_argument(
        "--verify", help="command that builds and tests the project, instead of the detected one"
    )
    init.add_argument(
        "--prepare",
        help="command a new task's clone runs first, before the writer (a build without tests);"
        " '' for none; default: what the build files suggest",
    )
    init.add_argument(
        "--git", action="store_true", help="start a git repository here first, for a project from scratch"
    )
    init.add_argument("--yes", action="store_true", help="write without asking")
    init.set_defaults(func=cmd_init)

    new = sub.add_parser("new", help="create a task and its plan")
    new.add_argument("--base", default="", help="start from this branch or ref (default: current HEAD)")
    new.add_argument("project", help="project name (~/.config/vivibox/projects/<name>.toml)")
    new.add_argument(
        "goal",
        help="what the agent should do: a line, or a whole ticket (- reads stdin)",
    )
    new.add_argument(
        "--kind",
        choices=KINDS,
        default="feature",
        help="bug: the plan starts by reproducing it in a test",
    )
    new.add_argument(
        "--draft", action="store_true", help="only create the task, to edit its plan before 'vivibox start'"
    )
    new.add_argument(
        "--plan-in-cli",
        action="store_true",
        help="plan this task yourself in an agent's CLI (claude, codex): no report on the repository first",
    )
    new.add_argument(
        "--auto", action="store_true", help="accept the agent's plan without stopping; you review the work"
    )
    new.add_argument(
        "--no-build",
        action="store_true",
        help="nothing to build or test in this task: research, a ticket analysis",
    )
    new.add_argument(
        "--flow",
        choices=ORCHESTRATION_MODES,
        type=current_mode,  # a flow's former name too (config.RENAMED_MODES)
        default="",
        help="how this task is shared between the roles, as the Flow row in n (default: config.toml's)",
    )
    new.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="ROLE=MODEL",
        help="run a role on another model for this task: writer=deepseek/deepseek-v4-pro, "
        "planner=claude-opus-5 (Claude Code), or planner=manual to plan in your own chat",
    )
    new.set_defaults(func=cmd_new)

    stat = sub.add_parser(
        "stats", help="what the tasks' events add up to: attempts, cost per turn, why the gate refused"
    )
    stat.add_argument("--project", help="only this project's tasks")
    stat.add_argument("--since", help="only events from this date on (YYYY-MM-DD)")
    stat.add_argument("--json", action="store_true", help="as JSON, for a script")
    stat.set_defaults(func=cmd_stats)

    use = sub.add_parser("usage", help="how long each role and the verification took, per task")
    use.add_argument("--project", help="only this project's tasks")
    use.add_argument("--live", action="store_true", help="only the tasks not finished yet")
    use.add_argument("--json", action="store_true", help="as JSON, for a note or a script")
    use.set_defaults(func=cmd_usage)

    status = sub.add_parser("status", help="list tasks, or show one task")
    status.add_argument("task", nargs="?", help="task id")
    status.add_argument("--events", type=int, default=10, help="recent events to show (default 10)")
    status.set_defaults(func=cmd_status)

    img = sub.add_parser("image", help="build or check the agent image").add_subparsers(
        dest="action", required=True
    )
    build = img.add_parser("build", help="build the agent image if its definition changed")
    build.add_argument("--pull", action="store_true", help="refresh base images")
    build.add_argument("--force", action="store_true", help="rebuild even if up to date")
    build.set_defaults(func=cmd_image_build)
    img.add_parser("check", help="verify the agent image").set_defaults(func=cmd_image_check)

    mirror_cmd = sub.add_parser("mirror", help="the Docker Hub mirror the pods pull through: what it holds")
    mirror_cmd.add_argument(
        "action", nargs="?", choices=["status", "remove"], default="status",
        help="remove: the mirror's container and what it holds",
    )  # fmt: skip
    mirror_cmd.set_defaults(func=cmd_mirror)

    verify = sub.add_parser("verify", help="run the verification gate now")
    verify.add_argument("task", help="task id")
    verify.set_defaults(func=cmd_verify)

    demo = sub.add_parser("demo", help="run the project in its pod so you can look at it")
    demo.add_argument("task", help="task id")
    demo.add_argument("--stop", action="store_true", help="stop what is running")
    demo.add_argument("--reply", help="answer what the agent asked, and let it carry on")
    demo.add_argument("--yes", action="store_true", help="use the instruction from the last task")
    demo.add_argument("--no-ask", action="store_true", help="do not ask the agent when nothing is known")
    demo.set_defaults(func=cmd_demo)

    box = sub.add_parser("box", help="open the project's pod for you to work in, with no agent")
    box.add_argument("project", nargs="?", help="project name; with --new, the name of the new project")
    box.add_argument("--new", metavar="FOLDER", help="start a project from scratch in this folder first")
    box.set_defaults(func=cmd_box)

    again = sub.add_parser(
        "verify-again",
        help="a blocked task: run the verification once more, without the agent, e.g. after a token expired",
    )
    again.add_argument("task", help="task id")
    again.set_defaults(func=cmd_verify_again)

    risky = sub.add_parser("risky", help="show changes to files that run code on the host")
    risky.add_argument("task", help="task id")
    risky.set_defaults(func=cmd_risky)

    approve = sub.add_parser("approve-risky", help="approve the risky files as 'vivibox risky' showed them")
    approve.add_argument("task", help="task id")
    approve.add_argument("shown", nargs="?", default="", help="what was shown, as 'vivibox risky' names it")
    approve.set_defaults(func=cmd_approve_risky)

    for name, func, text in (
        ("timeline", cmd_timeline, "what happened to the task, one line each: states, turns, verifications"),
        ("start", cmd_start, "start the task, or a stopped one again from where it was"),
        ("resume", cmd_start, "the same as start"),
        ("stop", cmd_stop, "stop the task, keeping its work"),
        ("attach", cmd_attach, "watch or talk to the agent, or the verification as it runs; Ctrl-q leaves"),
        ("supervise", cmd_supervise, "(run by start, in the background) drive the task through its states"),
    ):
        p_ = sub.add_parser(name, help=text)
        p_.add_argument("task", help="task id")
        p_.set_defaults(func=func)
        if name == "stop":
            p_.add_argument(
                "--force", action="store_true",
                help="kill the supervisor and the containers instead of waiting; the turn under way is lost",
            )  # fmt: skip
    follow = sub.add_parser("follow-verification", help=argparse.SUPPRESS)  # what w's window runs
    follow.add_argument("task")
    follow.add_argument("log")
    follow.set_defaults(func=cmd_follow_verification)
    accept = sub.add_parser(
        "accept",
        help="accept the plan, the writer's verification command, or the finished work: it lands in"
        " your checkout, uncommitted",
    )
    accept.add_argument("task", help="task id")
    accept.add_argument(
        "--branch", action="store_true", help="put the work on branch vivibox/<id> instead, e.g. for a PR"
    )
    accept.add_argument(
        "--verify", help="at the command checkpoint: keep this verification command instead of the writer's"
    )
    accept.set_defaults(func=cmd_accept)

    for name, text in (
        (
            "delete",
            "delete the task without accepting it: clone, containers, volumes; the history keeps a line",
        ),
        ("rm", "the same as delete"),
    ):
        rm = sub.add_parser(name, help=text)
        rm.add_argument("task", help="task id")
        rm.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        rm.set_defaults(func=cmd_rm)

    reply = sub.add_parser("reply", help="answer or reject at a checkpoint; the comment goes to the agent")
    reply.add_argument("task", help="task id")
    reply.add_argument("comment", nargs="?", default="", help="your comment")
    reply.add_argument(
        "--criterion",
        action="append",
        default=[],
        help="an acceptance criterion to add, when the work has come back to you; repeat for more",
    )
    reply.set_defaults(func=cmd_reply)

    plan = sub.add_parser("plan", help="plan in your own chat (a manual planner): the prompt, and its answer")
    plan.add_argument("action", choices=["prompt", "import"])
    plan.add_argument("task", help="task id")
    plan.add_argument("file", nargs="?", help="import: the answer, '-' for stdin; default: the answer file")
    plan.add_argument("--cli", action="store_true", help="prompt: for a CLI in your checkout, not a browser")
    plan.set_defaults(func=cmd_plan)

    auth = sub.add_parser("auth", help="API keys for model providers, stored by vivibox")
    auth.add_argument("action", choices=["list", "set", "rm", "import"])
    auth.add_argument(
        "provider",
        nargs="?",
        help="provider, as in <provider>/<model> in config.toml; for import, an opencode.json"
        f" (default: {providers.DEFAULT_SOURCE})",
    )
    auth.set_defaults(func=cmd_auth)

    models = sub.add_parser("models", help="list models opencode knows for your providers")
    models.add_argument("provider", nargs="?", help="only this provider")
    models.set_defaults(func=cmd_models)

    pod = sub.add_parser("pod", help="manage a task's containers directly")
    pod.add_argument(
        "action", choices=["up", "down", "rm", "shell"], help="start, stop, remove, or open a shell"
    )
    pod.add_argument("task", help="task id")
    pod.set_defaults(func=cmd_pod)
    cli_agent.register(sub)
    cli_host.register(sub)
    cli_pod.register(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command is None:
            from . import firstrun, tui  # the view's library loads only when you use it

            ensure_image()
            firstrun.ensure_config()
            return tui.run()
        return args.func(args)
    except (
        ConfigError,
        PlanError,
        PodError,
        repo.RepoError,
        risky.Unapprovable,
        gate.GateError,
        keys.KeyStoreError,
        context.ContextError,
        harness.HarnessError,
        TaskLayoutError,
        KeyError,
    ) as e:
        print(f"vivibox: {display.terminal(str(e.args[0] if e.args else e))}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"vivibox: command failed ({e.returncode}): {display.name(' '.join(e.cmd))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
