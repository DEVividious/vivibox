"""The pod's network rules on the host: the firewall helper that fences the sidecar's network
namespace, and the facts it is given. Mixed into Pod, whose _run, sidecar, host_services, runner and
network_pool it uses."""

from __future__ import annotations

import re
import socket

from .pod_network import PodError
from .podspec import HOST_GATEWAY, Runner, run

FIREWALL = "/usr/local/libexec/vivibox-netns"
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


class PodEgress:
    def gateway(self) -> str:
        hosts = self._run("docker", "exec", self.sidecar, "cat", "/etc/hosts").stdout
        for line in hosts.splitlines():
            parts = line.split()
            if len(parts) >= 2 and HOST_GATEWAY in parts[1:]:
                return parts[0]
        raise PodError(f"{HOST_GATEWAY} missing in {self.sidecar}")

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

    def apply_firewall(self) -> None:
        # The pool is not a private range, so the helper has to be told to reject it by name:
        # without that, one task could reach another task's ports. Applied on every start, so the
        # MSS clamp follows the uplink of the moment: a VPN on or off since the last start.
        mtu = uplink_mtu(self.runner)
        self._run(
            "sudo", "-n", FIREWALL, "apply", self.sidecar,
            "--pool", self.network_pool, *(("--mtu", str(mtu)) if mtu < ETHERNET_MTU else ()),
            *self.firewall_targets(),
        )  # fmt: skip
