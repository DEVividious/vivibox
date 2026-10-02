"""The command line's commands for what runs on your machine beside the tasks: the agent image,
the Docker Hub mirror and the DNS forwarder (hostd), apart from cli.py to keep it under the size
limit."""

from __future__ import annotations

import argparse
import sys

from . import image, mirror
from .config import load_config
from .egress import hostd, resolver


def cmd_image_build(args: argparse.Namespace) -> int:
    ref, built = image.build(pull=args.pull, force=args.force)
    print(f"{'Built' if built else 'Up to date'}: {ref}")
    return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    if args.action == "remove":
        mirror.remove()
        print(f"Removed {mirror.NAME} and what it held; pods pull from Docker Hub until it starts again.")
        return 0
    config = load_config()
    state, listen = mirror.found()
    if not config.hub_mirror:
        left = f" Its container {mirror.NAME} is still there; 'vivibox mirror remove' removes it."
        print(
            "The Docker Hub mirror is off: every pod pulls its images from Docker Hub. To keep them once "
            "on this machine, put hub_mirror = true under [network] in config.toml." + (left if state else "")
        )
    elif state != "running":
        print("The Docker Hub mirror is on and not running; the next task that starts starts it.")
    else:
        print(
            f"The Docker Hub mirror runs on {listen} and holds {mirror.size() or 'nothing yet'}. A task's "
            "pod pulls through it from the pod's next start."
        )
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


def cmd_hostd(args: argparse.Namespace) -> int:
    if args.action == "run":
        return hostd.serve(args.address, args.port, args.upstream)
    if args.action == "stop":
        if hostd.stop():
            print(
                "Stopped the DNS forwarder: pods resolve no names until it starts again "
                "(vivibox hostd start, or a task's start)."
            )
        else:
            print("The DNS forwarder was not running.")
        return 0
    config = load_config()
    where = f"{config.host_ip}:{resolver.PORT}"
    if args.action == "start":
        try:
            hostd.ensure(config.host_ip)
        except hostd.HostdError as e:
            print(f"vivibox: {e}", file=sys.stderr)
            return 1
    if why := hostd.problem(config.host_ip):
        print(why)
        return 1
    if pid := hostd.running():
        print(
            f"The DNS forwarder runs on {where} (process {pid}) and passes the pods' queries to this "
            "machine's resolver."
        )
    else:
        print("The DNS forwarder is not running; a task's start starts it, or: vivibox hostd start")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("hostd", help="the DNS forwarder every pod asks, on this machine: whether it runs")
    p.add_argument(
        "action", nargs="?", choices=["status", "start", "stop", "run"], default="status",
        help="start, stop; run: the forwarder itself, in the foreground (start runs it in the background)",
    )  # fmt: skip
    p.add_argument("--address", default="", help=argparse.SUPPRESS)
    p.add_argument("--port", type=int, default=resolver.PORT, help=argparse.SUPPRESS)
    p.add_argument("--upstream", default="", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_hostd)
