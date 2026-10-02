"""The names vivibox gives what it makes on the host: a task network's bridge, which the host's
egress rules are keyed by (the kernel takes at most 15 characters for an interface's name)."""

import random
import string

import pytest

from vivibox import names


def test_a_short_task_keeps_its_id_in_the_bridges_name():
    assert names.bridge("shop-1", "w") == "vbx-shop-1-w"
    assert names.bridge("shop-1", "r") == "vbx-shop-1-r"


def test_a_long_task_id_is_shortened_by_a_hash_and_stays_apart():
    long_one = names.bridge("a-project-with-a-long-name-123", "w")
    assert len(long_one) <= 15
    assert long_one.startswith("vbx") and long_one.endswith("-w")
    assert long_one != names.bridge("a-project-with-a-long-name-124", "w")


@pytest.mark.parametrize("role", ["", "writer", "x", "W", "w;"])
def test_only_the_known_roles_have_a_bridge(role):
    with pytest.raises(ValueError):
        names.bridge("shop-1", role)


@pytest.mark.parametrize("task_id", ["", "Shop-1", "shop", "shop-1;id", "../shop-1", "shop-1\n"])
def test_only_a_task_id_has_a_bridge(task_id):
    with pytest.raises(ValueError):
        names.bridge(task_id, "w")


def random_id(rng: random.Random) -> str:
    alphabet = string.ascii_lowercase + string.digits + "-"
    project = rng.choice(string.ascii_lowercase) + "".join(
        rng.choice(alphabet) for _ in range(rng.randint(0, 30))
    )
    return f"{project}-{rng.randint(1, 99999)}"


def test_every_bridge_name_fits_is_deterministic_and_unique_per_task_and_role():
    """Property-style: thousands of ids, short and long, never collide, and never pass 15."""
    rng = random.Random(1234)
    ids = {random_id(rng) for _ in range(4000)} | {f"p-{n}" for n in range(1, 400)}
    seen: dict[str, tuple[str, str]] = {}
    for task_id in ids:
        for role in names.ROLES:
            name = names.bridge(task_id, role)
            assert len(name) <= 15, name
            assert name == names.bridge(task_id, role), "deterministic"
            assert set(name) <= set(string.ascii_lowercase + string.digits + "-"), name
            assert name not in seen, f"{name}: {(task_id, role)} and {seen.get(name)}"
            seen[name] = (task_id, role)


def test_every_bridge_name_matches_the_hosts_wildcard():
    """The host rejects whatever comes from a vbx bridge without a policy of its own."""
    assert names.bridge("shop-1", "w").startswith(names.BRIDGE_PREFIX)
    assert names.bridge("a-project-with-a-long-name-123", "p").startswith(names.BRIDGE_PREFIX)
