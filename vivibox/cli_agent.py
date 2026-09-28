"""The commands an agent's CLI (Claude Code, Codex) drives vivibox with, for you: what to wait for,
in words a script reads as well as you do."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import about, actions, skill, ui
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
