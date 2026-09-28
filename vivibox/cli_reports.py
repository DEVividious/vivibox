"""Reports from task events and current disk, memory and CPU measurements."""

import argparse
import json
import sys

from . import actions, stats, usage
from .config import load_config
from .task import list_tasks


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


def cmd_usage(args: argparse.Namespace) -> int:
    """How long each role and the verification took, per task: live ones, then finished ones."""
    problems: list[str] = []
    caches: dict[str, int] = {}
    rows = usage.gather(
        finished=not args.live,
        project=args.project or "",
        measure=True,
        problems=problems,
        shared_caches=caches,
    )
    if args.json:
        print(
            json.dumps(
                {"tasks": usage.as_dicts(rows), "shared_caches": None if problems else caches}, indent=2
            )
        )
    else:
        print(usage.report(rows, None if problems else caches), end="")
    for problem in problems:
        print(f"CPU, RAM and DISK not measured: {problem}", file=sys.stderr)
    return 0
