"""A task's own Docker networks, one per role that has one (the writer's today): a /28 of the
configured pool, which no other network on this machine holds, on a bridge of vivibox's naming
(names.bridge). vivibox picks the range and its gateway and keeps them (the allocation): the
host's rules, the sidecar's address and host.docker.internal come from there, never from what
Docker says of a network (invariant 13). Mixed into Pod, whose _run, task_id, network,
network_pool and allocation it uses."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass

from . import names
from .config import TASK_NETWORK_BITS


class PodError(Exception):
    pass


@dataclass(frozen=True)
class RoleNetwork:
    """One role's network of a task, as vivibox allocated it."""

    role: str
    subnet: ipaddress.IPv4Network
    bridge: str

    @property
    def gateway(self) -> str:
        """The host's address on the bridge: what host.docker.internal is in the pod."""
        return str(self.subnet[1])

    @property
    def address(self) -> str:
        """The sidecar's, pinned: it goes into your configuration files, so it stays the same."""
        return str(self.subnet[2])


class PodNetwork:
    def taken_subnets(self) -> list[ipaddress.IPv4Network]:
        """Every subnet Docker has handed out, here or to anything else on this machine. Only ever
        a reason to pass a range by; what vivibox uses is its own choice."""
        ids = self._run("docker", "network", "ls", "-q").stdout.split()
        if not ids:
            return []
        listed = self._run(
            "docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids
        ).stdout
        taken = []
        for word in listed.split():
            try:
                taken.append(ipaddress.IPv4Network(word, strict=False))
            except ValueError:
                continue  # an IPv6 subnet, or anything else that is not one of ours to avoid
        return taken

    # How many ranges a task tries when others started at the same time take them first.
    NETWORK_ATTEMPTS = 8

    def free_subnet(self) -> ipaddress.IPv4Network:
        pool = ipaddress.IPv4Network(self.network_pool)
        taken = self.taken_subnets()
        for candidate in pool.subnets(new_prefix=TASK_NETWORK_BITS):
            if not any(candidate.overlaps(other) for other in taken):
                return candidate
        raise PodError(
            f"no free address range left in {pool}: every /{TASK_NETWORK_BITS} is in use. "
            "Remove tasks you have finished with, or widen network.pool in config.toml"
        )

    def _allocated(self) -> dict[str, ipaddress.IPv4Network]:
        """The ranges recorded for each role, from the allocation file (or memory, without one)."""
        if self.allocation is None:
            return dict(self._in_memory)
        try:
            data = json.loads(self.allocation.read_text())
            return {n["role"]: ipaddress.IPv4Network(n["subnet"]) for n in data["networks"]}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _record(self, role: str, subnet: ipaddress.IPv4Network) -> None:
        found = {**self._allocated(), role: subnet}
        if self.allocation is None:
            self._in_memory = found
            return
        data = {"networks": [{"role": r, "subnet": str(s)} for r, s in sorted(found.items())]}
        part = self.allocation.with_name(self.allocation.name + ".part")
        part.write_text(json.dumps(data))
        os.replace(part, self.allocation)

    def recorded_network(self, role: str = "w") -> RoleNetwork | None:
        """The role's network as allocated, while its range is still in the pool; None before."""
        subnet = self._allocated().get(role)
        if subnet is None or not subnet.subnet_of(ipaddress.IPv4Network(self.network_pool)):
            return None
        return RoleNetwork(role, subnet, names.bridge(self.task_id, role))

    def networks(self) -> list[RoleNetwork]:
        """Every role network of the task that is allocated, in the roles' order."""
        return [n for role in names.ROLES if (n := self.recorded_network(role))]

    def ensure_network(self) -> RoleNetwork:
        """The writer's network, made with the range recorded for it, or a free one. Its address is
        then its own, so its ports are nobody else's."""
        recorded = self.recorded_network()
        there = self._run("docker", "network", "inspect", self.network, check=False).returncode == 0
        if there and recorded:
            return recorded
        if there:
            # Made before its range was vivibox's to keep, or the pool moved: nothing is on it, as
            # this runs only for a pod without its sidecar.
            self._run("docker", "network", "rm", self.network, check=False)
        bridge = names.bridge(self.task_id, "w")
        # Tasks started at once can each find the same range free; Docker gives it to one, and the
        # others try the next.
        said = ""
        for attempt in range(self.NETWORK_ATTEMPTS):
            subnet = recorded.subnet if recorded and attempt == 0 else self.free_subnet()
            made = self._run(
                "docker", "network", "create", "--subnet", str(subnet), "--gateway", str(subnet[1]),
                "-o", f"com.docker.network.bridge.name={bridge}", self.network, check=False,
            )  # fmt: skip
            if made.returncode == 0:
                self._record("w", subnet)
                return RoleNetwork("w", subnet, bridge)
            said = (made.stderr or made.stdout).strip()
            if "overlap" not in said.lower():
                raise PodError(f"docker network create {self.network}: {said}")
        raise PodError(f"docker network create {self.network}: {said}")
