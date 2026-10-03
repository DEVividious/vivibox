"""The names vivibox gives what it makes on the host. A task's role network gets a bridge of its own
name, so the host's egress rules (host/vivibox-host) can be keyed by the interface a packet comes
in on, before the network exists and whatever Docker says about it."""

from __future__ import annotations

import hashlib
import re

# The roles a task has a network for, in the order their ranges are cut from the task's block:
# the writer's, the reviewer's, the planner's (its network is made once the planner has a
# container of its own). What each is called in a message a person reads.
ROLES = ("w", "r", "p")
ROLE_NAMES = {"w": "writer", "r": "reviewer", "p": "planner"}
# Every bridge of a task network starts so; the host rejects what comes from one without a policy.
BRIDGE_PREFIX = "vbx"
# The kernel's limit on an interface's name (IFNAMSIZ less its terminating zero).
IFNAME_MAX = 15
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*-[0-9]+$")
HASH_LENGTH = 10


def bridge(task_id: str, role: str) -> str:
    """The bridge of a task's role network, at most 15 characters: vbx-<task id>-<role> while it
    fits, else vbx<the id's hash>-<role>. The two shapes never meet (the first has a dash after
    vbx, a hash never does), so a name is the task's and the role's alone. host/vivibox-host
    works the same name out to refuse a bridge of another task."""
    if role not in ROLES:
        raise ValueError(f"not a role with a network: {role!r}")
    if not TASK_ID.fullmatch(task_id):
        raise ValueError(f"not a task id: {task_id!r}")
    readable = f"{BRIDGE_PREFIX}-{task_id}-{role}"
    if len(readable) <= IFNAME_MAX:
        return readable
    digest = hashlib.sha256(task_id.encode()).hexdigest()[:HASH_LENGTH]
    return f"{BRIDGE_PREFIX}{digest}-{role}"
