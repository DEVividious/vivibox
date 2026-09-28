"""The commands an agent's CLI (Claude Code, Codex) drives vivibox with, for you: what to wait for,
in words a script reads as well as you do."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import about, actions, reviewing, skill, ui
from .config import load_config
from .waiting import JSON_VERSION, TIMED_OUT


def cmd_wait(args: argparse.Namespace) -> int:
    tasks = [actions.load(task_id)[0] for task_id in args.task]
    found = actions.wait(tasks, timeout=args.timeout)
    max_rounds = load_config().max_rounds
    shown = []
    for task, st, reason in found:
        seen = ui.view(task, st, actions.supervisor_running(task), max_rounds)
        shown.append(
            {
                "id": st.id,
                "state": str(st.state),
                "reason": reason,
                "status": seen.status,
                "problem": seen.problem or st.problem,
                "next": list(seen.commands),
            }
        )
    if args.json:
        print(json.dumps({"version": JSON_VERSION, "tasks": shown}))
    elif not found:
        print(f"Still working after {args.timeout:g} s: {', '.join(args.task)}.")
    for task in [] if args.json else shown:
        why = f" ({task['problem']})" if task["problem"] else ""
        then = f"; next: {task['next'][0]}" if task["next"] else ""
        print(f"{task['id']}: {task['status']}{why}{then}")
    return 0 if found else TIMED_OUT


def cmd_info(args: argparse.Namespace) -> int:
    found = about.gather(Path(args.path))
    print(json.dumps(found, indent=2) if args.json else about.describe(found), end="\n" if args.json else "")
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    done = skill.install() if args.action == "install" else skill.uninstall()
    if not done:
        if args.action == "install":
            names = " or ".join(f"{p.cli} (~/{p.home})" for p in skill.PLACES)
            print(f"vivibox: no agent CLI found to install the skill for: {names}", file=sys.stderr)
            return 1
        print("No copy of the skill to remove.")
    for path, what in done:
        print(f"{path}: {what}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """The review copy, as ever; with --prompt and --import, a review in your agent's CLI."""
    task, project = actions.load(args.task)
    if args.prompt:
        print(reviewing.cli_prompt(task), end="")
        return 0
    if args.answer is not None:
        text = None
        if args.answer == "-" or (not args.answer and not sys.stdin.isatty()):
            # An agent's shell has no terminal and nothing on stdin: the answer file.
            text = sys.stdin.read() or None
        elif args.answer:
            text = Path(args.answer).expanduser().read_text()
        try:
            review = reviewing.import_answer(task, text)
        except reviewing.ReviewError as e:
            print(f"vivibox: {e}", file=sys.stderr)
            return 1
        notes = ui.count(len(review.blocking), "blocking note")
        went = (
            f"{notes}; they go to the writer"
            if review.blocking
            else "no blocking notes; the work comes to the user"
        )
        print(f"Review brought in: {went}.")
        return 0
    path = actions.prepare_review(task, project)
    print(f"Review copy: {path}")
    print("The agent's work shows as uncommitted changes (IntelliJ: Commit tool window, Alt+0).")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    wait = sub.add_parser(
        "wait", help="wait until a task needs you, is done, has a problem or stopped (for an agent's CLI)"
    )
    wait.add_argument("task", nargs="+", help="task ids; the first that needs you ends the wait")
    wait.add_argument(
        "--timeout", type=float, metavar="S", help=f"give up after S seconds, with exit code {TIMED_OUT}"
    )
    wait.add_argument("--json", action="store_true", help="as JSON, for a script")
    wait.set_defaults(func=cmd_wait)

    info = sub.add_parser(
        "info", help="the project of a folder, the flows and the roles' models (for an agent's CLI)"
    )
    info.add_argument("path", nargs="?", default=".", help="a folder in the repository (default: this one)")
    info.add_argument("--json", action="store_true", help="as JSON, for a script")
    info.set_defaults(func=cmd_info)

    agent = sub.add_parser(
        "skill", help="install the skill that lets Claude Code or Codex drive vivibox for you, or remove it"
    )
    agent.add_argument("action", choices=["install", "uninstall"])
    agent.set_defaults(func=cmd_skill)

    review = sub.add_parser(
        "review",
        help="bring the work into your repository and update the review copy (automatic when ready);"
        " with --prompt and --import, review a round in your agent's CLI",
    )
    review.add_argument("task", help="task id")
    asked = review.add_mutually_exclusive_group()
    asked.add_argument("--prompt", action="store_true", help="the prompt for this round's review in your CLI")
    asked.add_argument(
        "--import",
        dest="answer",
        nargs="?",
        const="",
        metavar="FILE",
        help="bring the review in: FILE, '-' for stdin, or the answer file the prompt names",
    )
    review.set_defaults(func=cmd_review)
