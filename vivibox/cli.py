"""vivibox command line."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from . import (
    actions,
    context,
    gate,
    harness,
    image,
    keys,
    manual,
    providers,
    repo,
    stats,
    timeline,
    ui,
    version,
)
from . import init as project_init
from .config import ConfigError, config_dir, load_config, load_project
from .plan import KINDS, PlanError, parse_plan
from .pod import PodError
from .providers import is_provider_key
from .states import State
from .supervise import cmd_supervise
from .task import Task, find_task, list_tasks


def cmd_new(args: argparse.Namespace) -> int:
    description = sys.stdin.read() if args.goal == "-" else args.goal
    roles = {}
    for pair in args.model:
        role, sep, model = pair.partition("=")
        if not sep or not model.strip():
            raise ConfigError(f"--model {pair}: expected role=model, e.g. writer=deepseek/deepseek-v4-pro")
        roles[role.strip()] = actions.parse_choice(model)
    task = actions.create(
        args.project,
        description,
        auto=args.auto,
        kind=args.kind,
        roles=roles,
        no_build=args.no_build,
        base_ref=args.base,
    )
    print(f"Created {task.id} from {task.read_state().base_commit[:10]}")
    for note in actions.context_notes(task):
        print(f"Note: {note}")
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
        print(f"There is no {config_dir() / 'config.toml'} yet; run vivibox to set it up.")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """What happened to a task, one line each, on your clock."""
    task, _ = actions.load(args.task)
    print(timeline.render(task), end="")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """What the events of every task, live and finished, add up to: the numbers to look at before
    changing a prompt."""
    config = load_config()
    sources = []
    for task in list_tasks(config.tasks_dir):
        if not args.project or task.read_state().project == args.project:
            sources.append((task.events(), True))
    live = {t.id for t in list_tasks(config.tasks_dir)}
    for entry in actions.history(limit=None):
        if entry["id"] in live or (args.project and entry.get("project") != args.project):
            continue
        sources.append((stats.read_events(actions.archive_path(entry["id"]) / "events.jsonl"), False))
    found = stats.collect(sources, since=args.since or "")
    if args.json:
        print(json.dumps(stats.as_dict(found), indent=2))
    else:
        print(stats.report(found), end="")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    style = ui.Style(ui.use_color())
    if args.task:
        task = find_task(config.tasks_dir, args.task)
        running = actions.supervisor_running(task)
        print(ui.task_detail(task, _criteria, config.max_iterations, args.events, style, running), end="")
        return 0
    tasks = list_tasks(config.tasks_dir)
    live = {task.id for task in tasks}
    finished = [e for e in actions.history() if e["id"] not in live]
    if not tasks and not finished:
        print("No tasks. Create one with 'vivibox new <project> \"<goal>\"'.")
        return 0
    shown = ui.task_list(
        tasks, _criteria, config.max_iterations, style, running=actions.supervisor_running, finished=finished
    )
    print(shown, end="")
    return 0


def cmd_image_build(args: argparse.Namespace) -> int:
    ref, built = image.build(pull=args.pull, force=args.force)
    print(f"{'Built' if built else 'Up to date'}: {ref}")
    return 0


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


def carry_on(task: Task) -> None:
    """Your decision was "go on": with nobody working on the task, that means starting it."""
    if model := actions.carry_on(task):
        print(f"Started {task.id} ({model}): nobody was working on it.")


def cmd_start(args: argparse.Namespace) -> int:
    """Starts the task; one started before goes on from where it was, as the view's s does."""
    task, _ = actions.load(args.task)
    resume = any(e["type"] == "started" for e in task.events())
    model = actions.start(args.task, resume=resume)
    print(f"Started {args.task} ({model}). Watch the agent: vivibox attach {args.task} (Ctrl-q leaves).")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    actions.stop(task, force=args.force)
    lost = " by force; the turn under way is lost," if args.force else ";"
    print(f"Stopped {task.id}{lost} its work and history are kept. Continue with 'vivibox start {task.id}'.")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    st = task.read_state()
    if not args.yes:
        doing = ui.view(task, st, actions.supervisor_running(task), load_config().max_iterations).status
        answer = input(f"Delete {task.id} ({doing}) and all its work, without accepting it? [y/N] ")
        if answer.strip().lower() != "y":
            return 1
    if worktree := actions.remove(task, project):
        print(f"Deleted the review copy {worktree}.")
    print(f"Deleted {task.id}; a line in the history, and its archive, stay.")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    command = actions.attach_command(args.task)
    subprocess.run(command, env=actions.outside_tmux())
    if not task.read_state().box:  # a box's shell keeps its state between visits
        actions.close_agent_view(args.task)
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    st = task.read_state()
    if st.box and st.state is State.IMPLEMENT:
        if where := actions.close_box(task, project):
            print(f"{task.id} closed; its work is ready for your review in {where}")
            print(f"Accept it with: vivibox accept {task.id}")
        else:
            print(f"{task.id} closed; it changed risky files: vivibox risky {task.id}")
    elif st.state is State.CHECKPOINT_PLAN:
        actions.accept_plan(task, project)
        print(f"Plan accepted; {task.id} moves on to implementation.")
        carry_on(task)
    elif st.state is State.CHECKPOINT_COMMAND:
        actions.accept_command(task, project, args.verify or "")
        kept = load_project(project.name).verify[0]
        print(f"{project.name} is verified with `{kept}` from now on; {task.id} is verifying.")
        carry_on(task)
    elif st.state is State.CHECKPOINT_FINAL:
        done = actions.finish(task, project, branch_only=args.branch)
        report_finished(done)
    else:
        raise gate.GateError(f"{task.id} is in {st.state}; nothing to accept (use 'vivibox reply')")
    return 0


def report_finished(done: actions.Finished) -> None:
    if done.conflicts:
        print(f"{done.task_id} is done ({done.cost}), but its work conflicts with your checkout in:")
        print("".join(f"  {f}\n" for f in done.conflicts), end="")
        print(f"Resolve them in your IDE and commit. The work is also on branch {done.branch}.")
    elif done.branch:
        print(f"{done.task_id} is done ({done.cost}): branch {done.branch} in {done.source}.")
    else:
        branch = actions.current_branch(done.source)
        print(f"{done.task_id} is done ({done.cost}). Uncommitted in {done.source} ({branch}):")
        print(done.status, end="")
        offer_commit(done.source, done.message, branch)


def offer_commit(source: Path, message: str, branch: str) -> None:
    shown = "".join(f"  {line}\n" if line else "\n" for line in message.splitlines())
    if not sys.stdin.isatty():
        example = f" with this message:\n\n{shown}" if message else ""
        print(f"Commit it when you like, e.g.: git -C {source} commit{example}")
        return
    if message:
        print(f"Commit to {branch} with this message?\n\n{shown}")
        answer = input("[Y]es, [e]dit the subject, [n]o: ").strip().lower()
    else:  # a box: the message is yours to write
        answer = input(f"Commit to {branch}? [Y]es, with a message you type, [n]o: ").strip().lower()
        answer = "e" if answer in ("", "y", "yes") else answer
    if answer in ("e", "edit"):
        subject, _, rest = message.partition("\n")
        message = input("Subject: ") + ("\n" + rest if rest else "")
    elif answer not in ("", "y", "yes"):
        print("Left uncommitted; review or change it in your IDE and commit when you are ready.")
        return
    try:
        print(f"Committed: {actions.commit_work(source, message)}")
    except gate.GateError as e:
        print(f"Not committed: {e}")


def cmd_plan(args: argparse.Namespace) -> int:
    """For a manual planner: the prompt to take to your chat, and bringing its plan back."""
    task, _ = actions.load(args.task)
    if args.action == "prompt":
        print(actions.plan_prompt(task, cli=args.cli), end="")
        return 0
    answer = None
    if args.file == "-" or (args.file is None and not sys.stdin.isatty()):
        answer = sys.stdin.read()
    elif args.file:
        answer = Path(args.file).expanduser().read_text()
    try:
        summary = actions.import_plan(task, answer)
    except PlanError as e:
        print(f"vivibox: that is not a plan yet: {e}", file=sys.stderr)
        print(f"\nSay this in the same chat:\n\n{manual.repair_prompt(str(e))}", file=sys.stderr)
        return 1
    print(f"Plan brought in{f': {summary}' if summary else ''}. Read it, then: vivibox accept {task.id}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    target = actions.reply(task, args.comment, args.criterion)
    added = f", with {len(args.criterion)} new criteria" if args.criterion else ""
    print(f"Sent to the agent{added}; {task.id} goes back to {ui.WORKING[target]}.")
    carry_on(task)
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
    config = load_config()
    result = gate.run_gate(
        task, pod, commands, project.risky_extra, project.java,
        timeout=project.verify_timeout or config.verify_timeout, no_build=project.no_build,
        no_command=actions.missing_command(task, project),
    )  # fmt: skip
    for command, source in result.build_files:
        print(f"NOTE  {source} names `{command}`, and the project runs nothing yet: set verify in its file")
    if result.environment:
        print(f"ENV   verification could not run: {result.environment}")
    if result.build_skipped:
        print(f"SKIP  the build was not run: {result.build_skipped}")
    if result.unchanged:
        print("SAME  nothing committed since the last verification; its result stands")
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
    for f in result.no_red_evidence:
        print(f"FAIL  no red evidence in red.md for: {f}")
    for t in result.removed_tests:
        print(f"GONE  test removed: {t}")
    for c in result.risky:
        print(f"RISK  {c.kind}: {c.path}")
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
        print(f"The last task you accepted in this project was run like this:\n\n{result.proposed}\n")
        print(f"If that still applies: vivibox demo {args.task} --yes")
        return 0
    if result.proposed:
        result = actions.use_instruction(args.task, result.proposed)
    if result.question:
        print(f"The agent asks:\n\n{result.question}\n")
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
        print(f"  {command}")
    for url in result.urls:
        print(f"\nListening: {url}")
    for listener in result.unreachable:
        print(f"\nPort {listener.port} is {listener.why_not}.\nStart it on 0.0.0.0 instead.")
    if result.starting:
        # Still installing or compiling. Calling that a failure sends you debugging a working app.
        print(f"\nStill starting, nothing listening yet. Its output so far:\n{result.log or '(none)'}")
        print(f"Look again with 'vivibox status {args.task}'.")
        return 0
    if not result.listening:
        print(f"\nIt stopped without listening. Last output:\n{result.log or '(none)'}")
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


def cmd_verify_again(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    actions.verify_again(task)
    print(f"Verifying {task.id} again, without a turn of the agent.")
    carry_on(task)
    return 0


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
    carry_on(task)
    return 0


def _yes(question: str) -> bool:
    """Yes without a terminal to ask in: a script that runs the import means it."""
    return not sys.stdin.isatty() or input(question).strip().lower() in ("y", "yes")


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
    elif args.action == "import":
        reading = providers.read_opencode(Path(args.provider or providers.DEFAULT_SOURCE))
        said = {"new": "", "replaces": "  (you have a different one)", "same": "  (same as yours, skipped)"}
        for f in reading.found:
            print(f"{f.kind:9} {f.name:20} {f.what}, key {f.key}{said[f.status]}")
        if reading.left:
            print(f"Left in the file, not for vivibox: {', '.join(reading.left)}.")
        new = [f for f in reading.found if f.status == "new"]
        differ = [f for f in reading.found if f.status == "replaces"]
        chosen = new if new and _yes(f"Import the {len(new)} new? [y/N] ") else []
        if differ and _yes(f"Overwrite yours with {', '.join(f.name for f in differ)}? [y/N] "):
            chosen += differ
        if not chosen:
            print("Nothing imported.")
            return 1
        providers.bring_over(chosen)
        print(f"Imported {', '.join(f.name for f in chosen)}; press k in vivibox to see them.")
    elif args.action == "rm":
        if not args.provider:
            raise keys.KeyStoreError("which provider? e.g. vivibox auth rm deepseek")
        print(f"Removed {args.provider}." if keys.remove(args.provider) else f"No key for {args.provider}.")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Asks opencode which models it knows for the providers you have keys for. Display only."""
    providers = [args.provider] if args.provider else list(filter(is_provider_key, keys.list_keys()))
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
        "--auto", action="store_true", help="accept the agent's plan without stopping; you review the work"
    )
    new.add_argument(
        "--no-build",
        action="store_true",
        help="nothing to build or test in this task: research, a ticket analysis",
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

    approve = sub.add_parser("approve-risky", help="approve the current risky files")
    approve.add_argument("task", help="task id")
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
        gate.GateError,
        keys.KeyStoreError,
        context.ContextError,
        harness.HarnessError,
        KeyError,
    ) as e:
        print(f"vivibox: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"vivibox: command failed ({e.returncode}): {' '.join(e.cmd)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
