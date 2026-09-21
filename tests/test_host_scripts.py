import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOST = Path(__file__).parent.parent / "host"
HELPER = HOST / "vivibox-netns"


def helper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(HELPER), *args], capture_output=True, text=True)


def test_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "run 'uv sync' to install shellcheck-py"
    scripts = [HOST / "setup.sh", HELPER]
    result = subprocess.run([shellcheck, "-x", *map(str, scripts)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "args",
    [(), ("apply",), ("drop", "vivibox-shop-1-dind"), ("clear", "vivibox-shop-1-dind", "extra")],
)
def test_helper_rejects_bad_usage(args):
    assert helper(*args).returncode == 2


@pytest.mark.parametrize(
    "name",
    ["other", "vivibox-shop-dind", "vivibox-shop-1", "vivibox-Shop-1-dind", "vivibox-shop-1-dind;id", "-f"],
)
def test_helper_rejects_non_sidecar_names(name):
    result = helper("show", name)
    assert result.returncode == 1 and "not an vivibox sidecar" in result.stderr


@pytest.mark.parametrize(
    "target",
    [
        "10.0.0.1",
        "10.0.0.1:0",
        "10.0.0.1:65536",
        "256.0.0.1:80",
        "10.0.0:80",
        "host:80",
        "10.0.0.1:80;id",
        "::1:80",
    ],
)
def test_helper_rejects_bad_targets(target):
    result = helper("apply", "vivibox-shop-1-dind", "10.0.0.2:5432", target)
    assert result.returncode == 1 and "not <ipv4>:<port>" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root path")
def test_helper_validates_before_requiring_root():
    result = helper("apply", "vivibox-shop-1-dind", "172.20.0.1:5432", "192.168.1.10:8080")
    assert result.returncode == 1 and "must run as root" in result.stderr


@pytest.mark.parametrize(
    "pool",
    ["198.51.100.0", "198.51.100.0/", "198.51.100.0/31", "198.51.100.0/7", "256.0.0.0/24", "x/24"],
)
def test_helper_rejects_a_bad_pool(pool):
    result = helper("apply", "vivibox-shop-1-dind", "--pool", pool)
    assert result.returncode == 1 and "not an ipv4 network" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root path")
def test_helper_takes_a_pool_before_the_targets():
    result = helper("apply", "vivibox-shop-1-dind", "--pool", "198.51.100.0/24", "172.20.0.1:5432")
    assert result.returncode == 1 and "must run as root" in result.stderr, "validation got that far"


def setup_fn(call: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Calls one of setup.sh's functions: sourced, the script defines them and stops."""
    script = f'source "{HOST / "setup.sh"}"; {call}'
    run_env = {k: v for k, v in os.environ.items() if k != "VIVIBOX_DOCKER_RANGES"}
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env={**run_env, **(env or {})}
    )


@pytest.mark.parametrize(
    ("a", "b", "shared"),
    [
        ("172.20.0.0/16", "172.20.5.0/24", True),
        ("172.20.5.0/24", "172.20.0.0/16", True),
        ("172.16.0.0/12", "172.25.0.0/16", True),
        ("172.20.0.0/16", "172.21.0.0/16", False),
        ("172.20.0.0/16", "172.200.0.0/16", False),
        ("10.0.0.0/8", "10.203.0.0/16", True),
        ("172.20.0.0/16", "172.20.9.9", True),
        ("198.51.100.0/24", "198.51.101.0/24", False),
    ],
)
def test_ranges_overlap_by_their_addresses_not_their_prefix(a, b, shared):
    assert (setup_fn(f"overlaps {a} {b}").returncode == 0) is shared


def test_docker_ranges_are_the_usual_ones_when_nothing_routes_them():
    result = setup_fn("pick_docker_ranges 192.168.1.0/24 198.51.100.0/24")
    assert result.stdout.split() == ["172.20.0.0/16", "172.25.0.0/16"]


def test_docker_ranges_step_around_a_vpn_and_docker_networks():
    routes = "172.20.0.0/16 172.16.0.0/14 172.24.0.0/13 10.0.0.0/8"
    result = setup_fn(f"pick_docker_ranges {routes}")
    assert result.stdout.split() == ["172.21.0.0/16", "172.22.0.0/16"], result.stderr


def test_docker_ranges_are_refused_when_none_is_free():
    result = setup_fn("pick_docker_ranges 172.16.0.0/12 10.0.0.0/8")
    assert result.returncode == 1 and "set VIVIBOX_DOCKER_RANGES" in result.stderr


def test_docker_ranges_can_be_chosen_and_are_still_checked():
    chosen = {"VIVIBOX_DOCKER_RANGES": "10.77.0.0/16 10.78.0.0/16"}
    assert setup_fn("pick_docker_ranges 172.20.0.0/16", chosen).stdout.split() == [
        "10.77.0.0/16",
        "10.78.0.0/16",
    ]
    taken = setup_fn("pick_docker_ranges 10.0.0.0/8", chosen)
    assert taken.returncode == 1 and "10.77.0.0/16 overlaps the route to 10.0.0.0/8" in taken.stderr


@pytest.mark.parametrize(
    ("release", "shiftfs", "runs"),
    [
        ("7.0.0-31-generic", "no", True),
        ("6.8.0-45-generic", "no", True),
        ("5.15.0-122-generic", "no", True),
        ("5.12.0", "no", True),
        ("5.11.0-27-generic", "no", False),
        ("5.11.0-27-generic", "yes", True),
        ("5.4.0-200-generic", "yes", False),
        ("4.18.0-553.el8_10.x86_64", "no", False),
    ],
)
def test_the_kernel_says_whether_vivibox_can_run(release, shiftfs, runs):
    result = setup_fn(f"kernel_verdict {release} {shiftfs}")
    assert (result.returncode == 0) is runs
    said = result.stdout if runs else result.stderr
    assert f"kernel {release}: vivibox {'can' if runs else 'cannot'} run" in said
