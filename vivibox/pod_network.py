"""A task's own Docker networks, one per role: a /27 block of the configured pool, which no other
network on this machine holds any of, split by role (the writer's /28, the reviewer's /29, the
planner's /29), each on a bridge of vivibox's naming (names.bridge). vivibox picks the ranges and
their gateways and keeps them (the allocation): the host's rules, each role's pinned address and
host.docker.internal come from there, never from what Docker says of a network (invariant 13).
Mixed into Pod, whose _run, task_id, network, network_pool, allocation and has_control it uses."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import display, names
from .config import TASK_NETWORK_BITS

# The kernel's interfaces: whether a bridge of that name is there is asked of it, not of Docker.
SYS_NET = Path("/sys/class/net")
# How a task's block is split: the writer's role first, the widest (the sidecar, the gate and the
# nested containers are on it), then the reviewer's, a /29, then the planner's and the code
# index's provisioning run's, a /30 each: one container each, at the address after the gateway.
# A task allocated before the index had a range keeps the planner's /29 and has no index range
# (LAYOUTS).
ROLE_BITS = {"w": 28, "r": 29, "p": 30, "i": 30}
# The sets of roles a task's allocation may record: every role's, or an earlier vivibox's
# without the code index's.
LAYOUTS = (frozenset(names.ROLES), frozenset(("w", "r", "p")))
# The roles whose Docker network every task has. The planner's is made for a task whose planner
# runs in a control container of its own (PodNetwork.made_roles); in a task without one its range
# stays recorded and reserved, with no network, no bridge and no rules.
MADE_ROLES = ("w", "r")


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
        """The role's container's, pinned (the sidecar's in the writer's network, the reviewer's
        in its own): it goes into your configuration files, so it stays the same."""
        return str(self.subnet[2])


def split_block(block: ipaddress.IPv4Network) -> dict[str, ipaddress.IPv4Network]:
    """The roles' ranges of a task's block, in the roles' order, each cut from where the one
    before ended."""
    found, at = {}, int(block.network_address)
    for role in names.ROLES:
        subnet = ipaddress.IPv4Network((at, ROLE_BITS[role]))
        found[role] = subnet
        at += subnet.num_addresses
    return found


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

    # How many blocks a task tries when others started at the same time take them first.
    NETWORK_ATTEMPTS = 8

    def free_subnet(self) -> ipaddress.IPv4Network:
        """A block of the pool nothing on this machine holds any part of."""
        pool = ipaddress.IPv4Network(self.network_pool)
        taken = self.taken_subnets()
        blocks = (
            list(pool.subnets(new_prefix=TASK_NETWORK_BITS)) if pool.prefixlen <= TASK_NETWORK_BITS else []
        )
        for candidate in blocks:
            if not any(candidate.overlaps(other) for other in taken):
                return candidate
        sizes = ", ".join(f"the {names.ROLE_NAMES[r]}'s /{bits}" for r, bits in ROLE_BITS.items())
        raise PodError(
            f"no free address range left in network.pool {pool} for {self.task_id}: a task takes one "
            f"/{TASK_NETWORK_BITS} of it ({sizes}; room for {len(blocks)} "
            f"task{'' if len(blocks) == 1 else 's'}), and every one is in "
            "use. Delete tasks you have finished with (vivibox delete <id>), which frees their "
            "ranges, or widen network.pool in config.toml"
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

    def _record(self, found: dict[str, ipaddress.IPv4Network]) -> None:
        """Every role's range, kept at once: a task has all of them or none."""
        if self.allocation is None:
            self._in_memory = dict(found)
            return
        data = {"networks": [{"role": r, "subnet": str(s)} for r, s in sorted(found.items())]}
        part = self.allocation.with_name(self.allocation.name + ".part")
        part.write_text(json.dumps(data))
        os.replace(part, self.allocation)

    def allocation_problem(self) -> str:
        """Why the task's allocation is not one this vivibox runs a pod on, "" when it is: a task
        allocated the writer's network alone was made before every role had one, and is not given
        the others after the fact (its layout is an earlier vivibox's)."""
        found = self._allocated()
        if found and frozenset(found) not in LAYOUTS:
            return (
                f"{self.task_id}'s network is from an earlier vivibox, which gave a task one network "
                f"and not one per role; delete it and create it again: vivibox delete {self.task_id}"
            )
        return ""

    def recorded_network(self, role: str = "w") -> RoleNetwork | None:
        """The role's network as allocated, while its range is still in the pool; None before."""
        subnet = self._allocated().get(role)
        if subnet is None or not subnet.subnet_of(ipaddress.IPv4Network(self.network_pool)):
            return None
        return RoleNetwork(role, subnet, names.bridge(self.task_id, role))

    def networks(self) -> list[RoleNetwork]:
        """Every role network of the task that is allocated, in the roles' order."""
        return [n for role in names.ROLES if (n := self.recorded_network(role))]

    def made_roles(self) -> tuple[str, ...]:
        """The roles this task has a Docker network for: the writer's and the reviewer's, and the
        planner's when the pod has a control container."""
        return (*MADE_ROLES, "p") if self.has_control else MADE_ROLES

    def made_networks(self) -> list[RoleNetwork]:
        """The role networks that have a Docker network and a bridge, as recorded: what the host's
        rules are keyed by. The planner's only with a control container (made_roles)."""
        return [n for n in self.networks() if n.role in self.made_roles()]

    def network_name(self, role: str) -> str:
        """The Docker network of a role. The writer's keeps the name it had when it was the only
        one: the sidecar's configuration files and host.docker.internal depend on it."""
        return self.network if role == "w" else f"vivibox-{self.task_id}-{role}"

    def _network_there(self, role: str) -> bool:
        return self._run("docker", "network", "inspect", self.network_name(role), check=False).returncode == 0

    def _make(self, role: str, subnet: ipaddress.IPv4Network) -> str:
        """One role's network on its range, with vivibox's gateway, on vivibox's bridge: "" once
        it is made, else Docker's words."""
        made = self._run(
            "docker", "network", "create", "--subnet", str(subnet), "--gateway", str(subnet[1]),
            "-o", f"com.docker.network.bridge.name={names.bridge(self.task_id, role)}",
            self.network_name(role), check=False,
        )  # fmt: skip
        return "" if made.returncode == 0 else display.said(made.stderr or made.stdout)

    def ensure_networks(self) -> list[RoleNetwork]:
        """The networks of the roles that have one (the writer's first), made on the ranges
        recorded for the task, or on a free block of the pool, whose every role's range is then
        recorded. Each role's address is then its own, so its ports are nobody else's."""
        recorded = self.made_networks()
        if recorded:
            # Its block is the task's: a network of it that is gone (removed by hand while the
            # task was stopped) is made again on its range, and the rest are left as they are.
            for net in recorded:
                if not self._network_there(net.role) and (said := self._make(net.role, net.subnet)):
                    raise PodError(f"docker network create {self.network_name(net.role)}: {said}")
            return recorded
        for role in self.made_roles():
            # Made before its range was vivibox's to keep, or the pool moved: nothing is on it, as
            # a task without ranges recorded reaches here only at up(), before its sidecar is
            # made (up removes one whose range is not recorded first), never from a review.
            if self._network_there(role):
                self._run("docker", "network", "rm", self.network_name(role), check=False)
        # Tasks started at once can each find the same block free; Docker gives it to one, and the
        # others try the next. A block is taken whole: the writer's network made and another
        # role's refused, the writer's goes too.
        said = ""
        for _ in range(self.NETWORK_ATTEMPTS):
            ranges = split_block(self.free_subnet())
            made = []
            for role in self.made_roles():
                if said := self._make(role, ranges[role]):
                    for done in made:
                        self._run("docker", "network", "rm", self.network_name(done), check=False)
                    break
                made.append(role)
            else:
                self._record(ranges)
                return self.made_networks()
            if "overlap" not in said.lower():
                raise PodError(f"docker network create {self.network_name(role)}: {said}")
        raise PodError(f"docker network create {self.network_name(role)}: {said}")

    def ensure_network(self) -> RoleNetwork:
        """The writer's network, with every role's made or checked along with it."""
        return self.ensure_networks()[0]

    def check_bridge(self, net: RoleNetwork) -> None:
        """The network is on the bridge the host's rules are keyed by, as the kernel has it: one of
        its name made another way (by hand, by someone else's script) is on a bridge they do not
        know, and the pod would run without them."""
        if not (SYS_NET / net.bridge / "bridge").is_dir():
            name = self.network_name(net.role)
            raise PodError(
                f"the network {name} is not on the bridge {net.bridge} that this machine's "
                f"rules for the task are keyed by, so they would not hold; remove it "
                f"(docker network rm {name}) and start the task again"
            )
