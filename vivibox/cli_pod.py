"""The command line on a task's limits and its disk: what its containers may use, set for the
task alone over config.toml's [limits], and a disk made larger; apart from cli.py to keep it
under the size limit."""

from __future__ import annotations

import argparse
import sys

from . import actions, limits, panel_resources
from .config import load_config
from .pod import PodError
from .podspec import DISK_LARGEST, DISK_SMALLEST, LIMIT_KEYS, parse_size, size_option

# The options of `vivibox limits`: one per key of [limits] but the disk's size, which is the
# image's (vivibox disk grow).
OPTIONS = [key for key in LIMIT_KEYS if key != "disk"]


def cmd_limits(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    given = {key: value for key in OPTIONS if (value := getattr(args, key)) is not None}
    if given:
        try:
            limits.write_overrides(task.meta, given, load_config().limits)
        except ValueError as e:
            print(f"vivibox: {e}", file=sys.stderr)
            return 1
    pod = actions.task_pod(task.id)
    try:
        print(panel_resources.text(task, up=pod.running()))
    except ValueError as e:
        print(f"vivibox: {e}", file=sys.stderr)
        return 1
    if given:
        print(
            f"Set for {task.id} alone. It applies at the task's next start, which makes its containers "
            f"with the new limits: vivibox stop {task.id} if it runs, then vivibox start {task.id}."
        )
    else:
        options = ", ".join("--" + k.replace("_", "-") for k in OPTIONS)
        print(
            f"Raise one for this task alone: vivibox limits {task.id} --agent-memory 6g (any of "
            f"{options}); a disk: vivibox disk grow {task.id} 40g."
        )
    return 0


def a_count(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None


def a_number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None


def cmd_disk(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    try:
        size = parse_size(args.size)
        if not DISK_SMALLEST <= size <= DISK_LARGEST:
            raise ValueError(f"a size from {size_option(DISK_SMALLEST)} to {size_option(DISK_LARGEST)}")
    except ValueError as e:
        print(
            f"vivibox: the disk's size must be {e}" if "from" in str(e) else f"vivibox: size: {e}",
            file=sys.stderr,
        )
        return 1
    pod = actions.task_pod(task.id)
    try:
        pod.grow_disk(args.size.strip())
    except PodError as e:
        print(f"vivibox: {e}", file=sys.stderr)
        return 1
    print(f"The disk of {task.id} is {args.size.strip()} now; what runs in the pod has the space at once.")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("limits", help="what a task's containers may use: see it, or raise it for this task")
    p.add_argument("task", help="task id")
    for key in OPTIONS:
        kind = a_number if key == "pod_cpus" else a_count if key.endswith("_pids") else str
        what = {"memory": "a size, e.g. 6g", "pids": "a number of processes", "cpus": "a number of CPUs"}
        unit = key.split("_")[-1]
        flag = "--" + key.replace("_", "-")
        p.add_argument(flag, dest=key, type=kind, default=None, metavar=unit.upper(), help=what[unit])
    p.set_defaults(func=cmd_limits)
    d = sub.add_parser("disk", help="a task's disk: make it larger").add_subparsers(
        dest="action", required=True
    )
    grow = d.add_parser("grow", help="a larger disk for the task, at once; a disk never shrinks")
    grow.add_argument("task", help="task id")
    grow.add_argument("size", help="the new size, e.g. 40g")
    grow.set_defaults(func=cmd_disk)
