"""A task's own Docker network: one /28 of the configured pool, which no other network on this
machine holds. Mixed into Pod, whose _run, network and network_pool it uses."""

from __future__ import annotations

import ipaddress

from .config import TASK_NETWORK_BITS


class PodError(Exception):
    pass


class PodNetwork:
    def taken_subnets(self) -> list[ipaddress.IPv4Network]:
        """Every subnet Docker has handed out, here or to anything else on this machine."""
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

    def ensure_network(self) -> ipaddress.IPv4Network:
        """The task's own network. Its address is then its own, so its ports are nobody else's."""
        found = self._run(
            "docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}}{{end}}",
            self.network, check=False,
        )  # fmt: skip
        if found.returncode == 0 and found.stdout.strip():
            return ipaddress.IPv4Network(found.stdout.strip())
        # Tasks started at once can each find the same range free; Docker gives it to one, and the
        # others try the next.
        for _ in range(self.NETWORK_ATTEMPTS):
            subnet = self.free_subnet()
            made = self._run(
                "docker", "network", "create", "--subnet", str(subnet), self.network, check=False
            )
            if made.returncode == 0:
                return subnet
            said = (made.stderr or made.stdout).strip()
            if "overlap" not in said.lower():
                raise PodError(f"docker network create {self.network}: {said}")
        raise PodError(f"docker network create {self.network}: {said}")
