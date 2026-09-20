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
