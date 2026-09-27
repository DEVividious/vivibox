"""Docker Hub, kept once on this machine: a pull-through cache the pods' daemons pull through, so
the second task that needs mysql takes it from here and not from the internet again. Each pod keeps
its own daemon and its own images; only the download is shared.

One container on your Docker, vivibox-mirror, from the registry image, on the host's network and
listening only on the bridge's gateway: the address a pod knows the host by (host.docker.internal),
which your LAN does not reach. Its port is let through the pod's firewall like a host service.
A task can pull from it and nothing else: a registry that proxies takes no pushes, and deletes are
off. It holds public images and no credentials. Deleting a task never touches it; the registry
forgets what nobody pulled for two weeks. When it is down, a pod's daemon pulls from Docker Hub
itself, so the mirror can only make a pull faster, never stop one.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable

from .config import DEFAULT_HUB_MIRROR_PORT
from .pod import CA_BUNDLE, HOST_CA_BUNDLE, HOST_GATEWAY, PodError, Runner, run

NAME = "vivibox-mirror"
VOLUME = "vivibox-mirror"
IMAGE = "registry:3.1.2"
UPSTREAM = "https://registry-1.docker.io"
# How long a layer nobody pulls stays; the registry then deletes it by itself.
TTL = "336h"
DEFAULT_PORT = DEFAULT_HUB_MIRROR_PORT
# The address the mirror was made to listen on, so a change of the bridge or the port remakes it.
LABEL = "vivibox.mirror.listen"


def url(port: int) -> str:
    """Where a pod's daemon finds the mirror."""
    return f"http://{HOST_GATEWAY}:{port}"


def bridge_gateway(runner: Runner = run) -> str:
    """The host's address on Docker's default bridge: what host.docker.internal is in a pod."""
    p = runner(["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"])
    if p.returncode != 0 or not p.stdout.strip():
        raise PodError(f"cannot find the address of Docker's bridge: {p.stderr.strip()}")
    return p.stdout.strip()


def command(listen: str) -> list[str]:
    return [
        "docker", "run", "-d", "--name", NAME, "--restart", "unless-stopped", "--network", "host",
        "--label", f"{LABEL}={listen}",
        "-e", f"REGISTRY_HTTP_ADDR={listen}",
        "-e", f"REGISTRY_PROXY_REMOTEURL={UPSTREAM}",
        "-e", f"REGISTRY_PROXY_TTL={TTL}",
        "-e", "REGISTRY_STORAGE_DELETE_ENABLED=false",
        "-v", f"{VOLUME}:/var/lib/registry",
        # Docker Hub through a corporate proxy that inspects TLS: trusted as your host trusts it.
        *(("-v", f"{HOST_CA_BUNDLE}:{CA_BUNDLE}:ro") if HOST_CA_BUNDLE.exists() else ()),
        IMAGE,
    ]  # fmt: skip


def answers(listen: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{listen}/v2/", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def found(runner: Runner = run) -> tuple[str, str]:
    """The mirror's container as Docker has it: its state ("running", "exited") and the address it
    was made to listen on; ("", "") when there is none."""
    template = f'{{{{.State.Status}}}} {{{{index .Config.Labels "{LABEL}"}}}}'
    p = runner(["docker", "inspect", "-f", template, NAME])
    state, _, listen = p.stdout.strip().partition(" ") if p.returncode == 0 else ("", "", "")
    return state, listen


def ensure(
    port: int, runner: Runner = run, ready: Callable[[str], bool] = answers, timeout: float = 15
) -> str:
    """Starts the mirror unless it runs already; returns the address a pod's daemon pulls through."""
    listen = f"{bridge_gateway(runner)}:{port}"
    state, made_for = found(runner)
    if state and made_for != listen:
        runner(["docker", "rm", "-f", NAME])
        state = ""
    if not state:
        made = runner(command(listen))
        if made.returncode != 0:
            raise PodError(f"the Docker Hub mirror did not start: {made.stderr.strip()}")
    elif state != "running":
        runner(["docker", "start", NAME])
    deadline = time.monotonic() + timeout
    while not ready(listen):
        if time.monotonic() >= deadline:
            said = runner(["docker", "logs", "--tail", "5", NAME])
            raise PodError(f"the Docker Hub mirror did not answer on {listen}: {said.stderr.strip()[-400:]}")
        time.sleep(0.3)
    return url(port)


def size(runner: Runner = run) -> str:
    """What the mirror holds on disk, as du says it; "" when it is not there."""
    p = runner(["docker", "exec", NAME, "du", "-sh", "/var/lib/registry"])
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.strip() else ""


def remove(runner: Runner = run) -> None:
    """The container and what it holds; a pod pulls from Docker Hub itself again."""
    runner(["docker", "rm", "-f", NAME])
    runner(["docker", "volume", "rm", VOLUME])
