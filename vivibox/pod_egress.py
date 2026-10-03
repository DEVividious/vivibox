"""What a pod may reach, enforced on the host: the root helper (host/vivibox-host) loads the rules
for the task's bridges, and the DNS forwarder (egress/hostd.py) answers the pod's every query.
The helper is given only what vivibox allocated (pod_network), its configuration and what the
host resolved: nothing Docker printed (invariant 13). Mixed into Pod, whose _run, task_id,
host_services, runner, network_pool and host_address it uses."""

from __future__ import annotations

import json
import re
import socket

from . import names
from .egress import hostd, resolver
from .pod_network import PodError
from .podspec import HOST_GATEWAY, Runner, run

HELPER = "/usr/local/libexec/vivibox-host"
# The one policy there is until a task's names are allowed by name (phase 4a): the internet open,
# everything private, the host and the other tasks refused.
POLICY = "open"
ETHERNET_MTU = 1500

LINK = re.compile(r"^\d+: ([^:@]+)(?:@\S+)?: <([^>]*)> mtu (\d+) .*?\blink/(\w+)")


def uplink_mtu(runner: Runner = run) -> int:
    """The MTU the host's traffic to the internet is bound by. A VPN tunnel has less than
    Ethernet's 1500 (Cloudflare WARP: 1280), and a pod, whose own interfaces have 1500, cannot
    tell: its large downloads through the tunnel stall. The route's interface counts, and so
    does every tunnel that is up: WARP steers traffic through rules and a table of its own, and
    `ip route get` still names the Wi-Fi. 1500 when there is no way to know."""
    links = {}
    p = runner(["ip", "-o", "link"])
    for line in p.stdout.splitlines() if p.returncode == 0 else []:
        if m := LINK.match(line):
            links[m.group(1)] = (set(m.group(2).split(",")), int(m.group(3)), m.group(4))
    mtus = [
        mtu for flags, mtu, kind in links.values() if kind in ("none", "ppp") and {"UP", "LOWER_UP"} <= flags
    ]
    p = runner(["ip", "-o", "route", "get", "1.1.1.1"])
    words = p.stdout.split()
    if p.returncode == 0 and "dev" in words[:-1] and words[words.index("dev") + 1] in links:
        mtus.append(links[words[words.index("dev") + 1]][1])
    return min(mtus, default=ETHERNET_MTU)


def helper(runner: Runner, *args: str, what: str = "this machine's rules for pods could not be set") -> None:
    """The root helper on an operation; PodError, saying what could not be done, on a refusal."""
    p = runner(["sudo", "-n", HELPER, *args])
    if p.returncode != 0:
        said = (p.stderr or p.stdout).strip()[-600:]
        raise PodError(
            f"{what}: {said}. If the helper or its sudo rule is missing (sudo asks for a password, or "
            "the file is not there, or it is older than this vivibox), run host/setup.sh"
        )


def helper_missing(runner: Runner = run) -> str:
    """Why the root helper does not run without a password; "" when it does. For the live tests,
    which skip without it."""
    if runner(["sudo", "-n", HELPER, "--version"]).returncode == 0:
        return ""
    return f"the root helper {HELPER} is not installed, or sudo does not allow it; run host/setup.sh"


def prepare_host(host_address: str, pool: str, runner: Runner = run) -> None:
    """This machine ready for any pod: the host's services address on vivibox0 and the base of
    the rules (the helper refuses, naming it, an address another interface or route holds), and
    the DNS forwarder answering there. No pod starts without it: its DNS would go nowhere."""
    try:
        # One at a time: two pods starting at once would both make vivibox0, and one would fail.
        with hostd.locked("host.lock"):
            helper(runner, "prepare-engine", "host", json.dumps({"host_address": host_address, "pool": pool}))
        hostd.ensure(host_address.partition("/")[0])
    except hostd.HostdError as e:
        raise PodError(f"DNS forwarder not running: {e}; no pod starts without it") from None


class RulesLeft(PodError):
    """A task gone but for this machine's rules for its network, which the helper kept (left)."""

    def __init__(self, task_id: str, left: str):
        super().__init__(f"deleted {task_id}, but {left}")
        self.left = left


class PodEgress:
    @property
    def host_ip(self) -> str:
        return self.host_address.partition("/")[0]

    def gateway(self) -> str:
        """The host's address on the writer's bridge, from the allocation."""
        net = self.recorded_network()
        if net is None:
            raise PodError(f"{self.task_id} has no network allocated yet; start it first")
        return net.gateway

    def firewall_targets(self) -> list[str]:
        """Every address a name answers with, not just the first: a service behind a load balancer
        or round-robin DNS reaches you on several, and allowing one of them fails at random."""
        targets: list[str] = []
        for s in self.host_services:
            if s.host == HOST_GATEWAY:
                found = [self.gateway()]
            else:
                try:
                    where = socket.getaddrinfo(s.host, s.port, socket.AF_INET, socket.SOCK_STREAM)
                except OSError as e:
                    raise PodError(f"cannot resolve host service {s.host}: {e}") from None
                found = sorted({r[4][0] for r in where})
            targets += [f"{ip}:{s.port}" for ip in found if f"{ip}:{s.port}" not in targets]
        return targets

    def policy(self) -> dict:
        """What the helper is given for this task: its role networks, the pool (every other task's
        range, refused), the uplink's MTU (the MSS clamp follows a VPN going on or off), the host
        services and where the forwarder listens. The networks are those with a bridge, the
        writer's and the reviewer's (pod_network.MADE_ROLES): the planner's range is reserved
        with the task and gets its rules once there is a planner's network to key them by, so
        that no chain is loaded for a bridge nothing is on."""
        nets = self.made_networks()
        if not nets:
            raise PodError(f"{self.task_id} has no network allocated yet")
        return {
            "state": "present",
            "networks": [
                {"role": n.role, "bridge": n.bridge, "subnet": str(n.subnet), "policy": POLICY} for n in nets
            ],
            "pool": self.network_pool,
            "mtu": uplink_mtu(self.runner),
            "hostsvc": self.firewall_targets(),
            "dns": self.host_ip,
            "dns_port": resolver.PORT,
        }

    def apply_firewall(self) -> None:
        helper(self.runner, "apply-policy", self.task_id, json.dumps(self.policy()))

    def remove_firewall(self) -> str:
        """The task's rules gone, for every role it may have had a network for; "" when they are,
        else what the helper said. A task id that is not one has none."""
        try:
            nets = [{"role": r, "bridge": names.bridge(self.task_id, r)} for r in names.ROLES]
        except ValueError:
            return ""
        gone = json.dumps({"state": "absent", "networks": nets})
        p = self.runner(["sudo", "-n", HELPER, "apply-policy", self.task_id, gone])
        if p.returncode == 0:
            return ""
        said = (p.stderr or p.stdout).strip()[-400:]
        return (
            f"this machine's rules for {self.task_id}'s network (bridge {nets[0]['bridge']}) were not "
            f"removed: {said}. They hold no network now; host/setup.sh installs the helper if it is missing"
        )
