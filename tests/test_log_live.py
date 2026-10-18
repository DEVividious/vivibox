"""A role's Docker log on a real pod: what anything run in it writes to the container's own output
stays within the bound, and the role goes on.

Needs host/setup.sh to have run and Docker.
Run with: uv run pytest -m docker tests/test_log_live.py
"""

import json
import secrets
import shutil
import subprocess

import pytest
from test_pod_live import TASKS_ROOT, check_id, own_folder

from vivibox import image
from vivibox.config import load_config
from vivibox.pod import Pod
from vivibox.pod_egress import helper_missing
from vivibox.podspec import LOG_OPTS

pytestmark = pytest.mark.docker

MB = 2**20


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="module")
def pod():
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    task_id = check_id("logcheck")
    folder = own_folder(task_id)
    repo = folder / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    ref, _ = image.build()
    cfg = load_config()
    made = Pod(
        task_id, repo, ref, [], host_address=cfg.host_address, network_pool=cfg.network_pool,
        limits=cfg.limits, folder=folder,
    )  # fmt: skip
    try:
        made.up()
        yield made
    finally:
        made.remove()
        shutil.rmtree(folder, ignore_errors=True)


def log_files(pod: Pod) -> dict[str, int]:
    """The agent's log files on the host's disk and their sizes, read by a container of root's
    that mounts their folder read-only and has no network: Docker's folder is root's."""
    folder = docker("inspect", "-f", "{{.LogPath}}", pod.agent).strip().rsplit("/", 1)[0]
    listing = docker(
        "run", "--rm", "--network", "none", "--user", "0", "-v", f"{folder}:/logs:ro",
        "--entrypoint", "sh", pod.image,
        "-c", "cd /logs && for f in *-json.log*; do echo \"$f $(stat -c %s \"$f\")\"; done",
    )  # fmt: skip
    return {name: int(size) for name, size in (line.split() for line in listing.splitlines())}


def test_a_role_that_floods_its_log_keeps_one_megabyte_and_goes_on(pod):
    made_with = json.loads(docker("inspect", "-f", "{{json .HostConfig.LogConfig}}", pod.agent))
    assert made_with == {"Type": "json-file", "Config": LOG_OPTS}, "as the mirror's check reads it"
    budget = int(LOG_OPTS["max-size"].removesuffix("m")) * MB * int(LOG_OPTS["max-file"])

    # Eight times the budget, through the container's own output, as code the writer runs could.
    pod.exec("bash", "-c", "head -c 6M /dev/urandom | base64 -w 76 > /proc/1/fd/1")
    marker = f"vivibox-log-probe-{secrets.token_hex(4)}"
    pod.exec("bash", "-c", f"echo {marker} > /proc/1/fd/1")

    files = log_files(pod)
    assert len(files) == 1 and sum(files.values()) <= budget + 64 * 1024, files
    # docker logs still says what was printed last, and the role goes on.
    assert marker in docker("logs", "--tail", "1", pod.agent)
    assert docker("inspect", "-f", "{{.State.Status}}", pod.agent).strip() == "running"
    assert pod.exec("echo", "ok").stdout.strip() == "ok"

    # Stopped, killed and started again as before; the agent made again keeps the bound.
    pod.down()
    assert docker("inspect", "-f", "{{.State.Status}}", pod.agent).strip() == "exited"
    pod.up()
    pod.kill()
    pod.up()
    assert pod.exec("echo", "ok").stdout.strip() == "ok"
    again = json.loads(docker("inspect", "-f", "{{json .HostConfig.LogConfig}}", pod.agent))
    assert again == made_with
