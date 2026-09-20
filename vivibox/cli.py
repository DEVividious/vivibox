"""vivibox command line."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from . import actions, context, gate, image, keys, opencode, repo, supervisor, ui
from . import init as project_init
from .config import ConfigError, config_dir, load_config
from .plan import KINDS, PlanError, parse_plan
from .pod import PodError
from .risky import Approvals
from .states import State
from .task import Task, find_task, list_tasks


def cmd_new(args: argparse.Namespace) -> int:
    description = sys.stdin.read() if args.goal == "-" else args.goal
    task = actions.create(args.project, description, auto=args.auto, kind=args.kind)
    print(f"Created {task.id} from {task.read_state().base_commit[:10]}")
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
        found.notes.append("no build found; the first plan you accept sets how to build and test it")
    print(f"Project {name} for {found.repo}:")
    for note in found.notes:
        print(f"  - {note}")
    print()
    print(project_init.render(found))
    confirmed = args.yes or (
        sys.stdin.isatty() and input(f"Set up {name}? [Y/n] ").strip().lower() in ("", "y", "yes")
    )
    if not confirmed:
        print("Nothing written.")
        return 1
    target = actions.setup_project(where, name, found.verify, found.java, create=args.git)
    print(f"Wrote {target}. Start a task with n in 'vivibox'.")
    if not (config_dir() / "config.toml").exists():
        print(f"There is no {config_dir() / 'config.toml'} yet; see templates/config.example.toml.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    style = ui.Style(ui.use_color())
    if args.task:
        task = find_task(config.tasks_dir, args.task)
        print(ui.task_detail(task, _criteria, config.max_iterations, args.events, style), end="")
        return 0
    tasks = list_tasks(config.tasks_dir)
    if not tasks:
        print("No tasks. Create one with 'vivibox new <project> \"<goal>\"'.")
        return 0
    print(ui.task_list(tasks, _criteria, config.max_iterations, style), end="")
    return 0


def cmd_image_build(args: argparse.Namespace) -> int:
    ref, built = image.build(pull=args.pull, force=args.force)
    print(f"{'Built' if built else 'Up to date'}: {ref}")
    return 0


def cmd_image_check(args: argparse.Namespace) -> int:
    ref = image.image_ref()
    if not image.exists(ref):
        print(f"vivibox: image {ref} is not built; run 'vivibox image build'", file=sys.stderr)
        return 1
    failed = 0
    for check, ok, out in image.run_checks(ref):
        print(f"{'PASS' if ok else 'FAIL'}  {check.name}")
        if not ok:
            failed += 1
            print("      " + out.replace("\n", "\n      "))
    print(f"\n{ref}: {failed} failed" if failed else f"\n{ref}: all checks passed")
    return 1 if failed else 0


def cmd_start(args: argparse.Namespace) -> int:
    model = actions.start(args.task, resume=getattr(args, "resume", False))
    print(f"Started {args.task} ({model}). Watch the agent: vivibox attach {args.task} (Ctrl-q leaves).")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    args.resume = True
    return cmd_start(args)


def cmd_stop(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    actions.stop(task)
    print(f"Stopped {task.id}; its work and history are kept. Continue with 'vivibox resume {task.id}'.")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    st = task.read_state()
    if not args.yes:
        answer = input(f"Remove {task.id} ({st.state}) and all its work, without accepting it? [y/N] ")
        if answer.strip().lower() != "y":
            return 1
    if worktree := actions.remove(task, project):
        print(f"Removed the review copy {worktree}.")
    print(f"Removed {task.id}.")
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    config = load_config()
    task, project = actions.load(args.task)
    pod = actions.task_pod(task.id)
    harness = opencode.OpenCode(pod)

    def agent_window(st) -> None:
        # Your view of the agent, ready once the harness session exists; reopened if you closed it.
        if st.session:
            actions.agent_view(task, harness.attach_command(st.session))

    sup = supervisor.Supervisor(
        task,
        harness,
        run_gate=lambda t: gate.run_gate(
            t, pod, actions.verify_commands(t, project), project.risky_extra, project.java
        ),
        risky_changes=lambda: Approvals(task.meta, task.repo, project.risky_extra).changes(),
        max_iterations=config.max_iterations,
        notify=lambda task_id, message, kind="": supervisor.notify(
            task_id, message, config.desktop_notifications, actions.buttons(task, project, config, kind)
        ),
        prepare_review=lambda: actions.prepare_review(task, project),
        project_verify=project.verify,
        save_verify=lambda commands: actions.save_verify(project, commands),
    )
    (task.meta / actions.SUPERVISOR_PID).write_text(str(os.getpid()))
    print(f"Supervising {task.id}. Your decisions: vivibox accept|reply {task.id}", flush=True)
    agent_window(task.read_state())  # a resumed task already has its session
    sup.run(on_step=agent_window)
    print(f"{task.id} is done.")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    command = actions.attach_command(args.task)
    os.execvp(command[0], command)


def cmd_accept(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    st = task.read_state()
    if st.state is State.CHECKPOINT_PLAN:
        actions.accept_plan(task, project)
        print(f"Plan accepted; {task.id} moves on to implementation.")
    elif st.state is State.CHECKPOINT_FINAL:
        done = actions.finish(task, project, branch_only=args.branch)
        report_finished(done)
    else:
        raise gate.GateError(f"{task.id} is in {st.state}; nothing to accept (use 'vivibox reply')")
    return 0


def report_finished(done: actions.Finished) -> None:
    if done.conflicts:
        print(f"{done.task_id} is done (${done.cost:.2f}), but its work conflicts with your checkout in:")
        print("".join(f"  {f}\n" for f in done.conflicts), end="")
        print(f"Resolve them in your IDE and commit. The work is also on branch {done.branch}.")
    elif done.branch:
        print(f"{done.task_id} is done (${done.cost:.2f}): branch {done.branch} in {done.source}.")
    else:
        branch = actions.current_branch(done.source)
        print(f"{done.task_id} is done (${done.cost:.2f}). Uncommitted in {done.source} ({branch}):")
        print(done.status, end="")
        offer_commit(done.source, done.message, branch)


def offer_commit(source: Path, message: str, branch: str) -> None:
    if not sys.stdin.isatty():
        print(f'Commit it when you like, e.g.: git -C {source} commit -m "{message}"')
        return
    answer = input(f'Commit to {branch} as "{message}"? [Y]es, [e]dit the message, [n]o: ').strip().lower()
    if answer in ("e", "edit"):
        message = input("Message: ")
    elif answer not in ("", "y", "yes"):
        print("Left uncommitted; review or change it in your IDE and commit when you are ready.")
        return
    try:
        print(f"Committed: {actions.commit_work(source, message)}")
    except gate.GateError as e:
        print(f"Not committed: {e}")


def cmd_reply(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    target = actions.reply(task, args.comment)
    print(f"Sent to the agent; {task.id} goes back to {target}.")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    path = actions.prepare_review(task, project)
    print(f"Review copy: {path}")
    print("The agent's work shows as uncommitted changes (IntelliJ: Commit tool window, Alt+0).")
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
    elif args.action == "rm":
        pod.remove()
    elif args.action == "shell":
        os.execvp("docker", ["docker", "exec", "-it", pod.agent, "bash"])
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    pod = actions.task_pod(task.id)
    pod.up()
    commands = actions.verify_commands(task, project)
    result = gate.run_gate(task, pod, commands, project.risky_extra, project.java)
    for c in result.commands:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.command}  ({c.seconds}s)")
    for c in result.missing_criteria:
        print(f"TODO  criterion not ticked: {c}")
    for p in result.commit_problems:
        print(f"FAIL  commit {p}")
    for h in result.hidden_characters:
        print(f"FAIL  invisible character at {h}")
    for f in result.uncommitted:
        print(f"FAIL  not committed: {f}")
    for c in result.risky:
        print(f"RISK  {c.kind}: {c.path}")
    print(f"\nLog: {result.log}")
    if result.risky:
        print(
            f"Risky files changed: review with 'vivibox risky {task.id}' before opening the task in IntelliJ."
        )
    return 0 if result.passed else 1


def cmd_risky(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    diffs = actions.risky_diffs(task, project)
    if not diffs:
        print("No changes to risky files since your last approval.")
        return 0
    print("\n".join(diffs))
    print(f"Approve with 'vivibox approve-risky {task.id}', or reject with a comment for the agent.")
    return 0


def cmd_approve_risky(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    count, review = actions.approve_risky(task, project)
    print(f"Approved {count} change(s) to risky files.")
    if review:
        print(f"Ready for your review in {review}")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    if args.action == "list":
        stored = keys.list_keys()
        for provider, shown in stored.items():
            print(f"{provider:20} {shown}")
        if not stored:
            print(f"No keys in {keys.store()}. Add one with: vivibox auth set <provider>")
    elif args.action == "set":
        if not args.provider:
            raise keys.KeyStoreError("which provider? e.g. vivibox auth set deepseek")
        value = getpass.getpass(f"API key for {args.provider}: ") if sys.stdin.isatty() else sys.stdin.read()
        keys.set_key(args.provider, value)
        print(f"Stored {args.provider}: {keys.masked(keys.get_key(args.provider))} in {keys.store()}")
    elif args.action == "rm":
        if not args.provider:
            raise keys.KeyStoreError("which provider? e.g. vivibox auth rm deepseek")
        print(f"Removed {args.provider}." if keys.remove(args.provider) else f"No key for {args.provider}.")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Asks opencode which models it knows for the providers you have keys for. Display only."""
    providers = [args.provider] if args.provider else list(keys.list_keys())
    if not providers:
        raise keys.KeyStoreError("no keys yet; add one with: vivibox auth set <provider>")
    env = []
    for provider in providers:
        # opencode lists a provider's models only when it has a key; a placeholder is enough to list.
        env += ["-e", f"{provider.upper().replace('-', '_').replace('.', '_')}_API_KEY=placeholder"]
    ref = image.image_ref()
    cmd = ["docker", "run", "--rm", "--tmpfs", f"/config:uid={os.getuid()},gid={os.getgid()}", *env, ref,
           "opencode", "models", *([args.provider] if args.provider else [])]  # fmt: skip
    return subprocess.run(cmd).returncode


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vivibox",
        description="Run AI agent tasks in isolated pods. Without a command: the interactive view.",
    )
    # Without a command: the interactive view.
    sub = p.add_subparsers(dest="command")

    init = sub.add_parser("init", help="set up the repository you are in as a project")
    init.add_argument("path", nargs="?", default=".", help="a directory in the repository (default: here)")
    init.add_argument("--name", help="project name (default: the repository's directory name)")
    init.add_argument(
        "--verify", help="command that builds and tests the project, instead of the detected one"
    )
    init.add_argument(
        "--git", action="store_true", help="start a git repository here first, for a project from scratch"
    )
    init.add_argument("--yes", action="store_true", help="write without asking")
    init.set_defaults(func=cmd_init)

    new = sub.add_parser("new", help="create a task and its plan")
    new.add_argument("project", help="project name (~/.config/vivibox/projects/<name>.toml)")
    new.add_argument(
        "goal",
        help="what the agent should do: a line, or a whole ticket (first line is the title; - reads stdin)",
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
        "--auto", action="store_true", help="accept the agent's plan without stopping; you review the work"
    )
    new.set_defaults(func=cmd_new)

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

    verify = sub.add_parser("verify", help="run the verification gate now")
    verify.add_argument("task", help="task id")
    verify.set_defaults(func=cmd_verify)

    risky = sub.add_parser("risky", help="show changes to files that run code on the host")
    risky.add_argument("task", help="task id")
    risky.set_defaults(func=cmd_risky)

    approve = sub.add_parser("approve-risky", help="approve the current risky files")
    approve.add_argument("task", help="task id")
    approve.set_defaults(func=cmd_approve_risky)

    for name, func, text in (
        ("start", cmd_start, "start the task's pod, agent and supervisor"),
        ("resume", cmd_resume, "continue a stopped or paused task"),
        ("stop", cmd_stop, "stop the task, keeping its work"),
        ("attach", cmd_attach, "watch or talk to the agent; Ctrl-q leaves"),
        ("supervise", cmd_supervise, "(run by start, in the background) drive the task through its states"),
    ):
        p_ = sub.add_parser(name, help=text)
        p_.add_argument("task", help="task id")
        p_.set_defaults(func=func)
    accept = sub.add_parser(
        "accept", help="accept the plan, or the finished work: it lands in your checkout, uncommitted"
    )
    accept.add_argument("task", help="task id")
    accept.add_argument(
        "--branch", action="store_true", help="put the work on branch vivibox/<id> instead, e.g. for a PR"
    )
    accept.set_defaults(func=cmd_accept)

    rm = sub.add_parser("rm", help="remove the task: clone, containers, volumes")
    rm.add_argument("task", help="task id")
    rm.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    rm.set_defaults(func=cmd_rm)

    reply = sub.add_parser("reply", help="answer or reject at a checkpoint; the comment goes to the agent")
    reply.add_argument("task", help="task id")
    reply.add_argument("comment", help="your comment")
    reply.set_defaults(func=cmd_reply)

    auth = sub.add_parser("auth", help="API keys for model providers, stored by vivibox")
    auth.add_argument("action", choices=["list", "set", "rm"])
    auth.add_argument("provider", nargs="?", help="provider, as in <provider>/<model> in config.toml")
    auth.set_defaults(func=cmd_auth)

    models = sub.add_parser("models", help="list models opencode knows for your providers")
    models.add_argument("provider", nargs="?", help="only this provider")
    models.set_defaults(func=cmd_models)

    review = sub.add_parser(
        "review", help="bring the work into your repository and update the review copy (automatic when ready)"
    )
    review.add_argument("task", help="task id")
    review.set_defaults(func=cmd_review)

    pod = sub.add_parser("pod", help="manage a task's containers directly")
    pod.add_argument(
        "action", choices=["up", "down", "rm", "shell"], help="start, stop, remove, or open a shell"
    )
    pod.add_argument("task", help="task id")
    pod.set_defaults(func=cmd_pod)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command is None:
            from . import tui  # the view's library loads only when you use it

            return tui.run()
        return args.func(args)
    except (
        ConfigError,
        PlanError,
        PodError,
        repo.RepoError,
        gate.GateError,
        keys.KeyStoreError,
        context.ContextError,
        opencode.HarnessError,
        KeyError,
    ) as e:
        print(f"vivibox: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"vivibox: command failed ({e.returncode}): {' '.join(e.cmd)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
