import os
import subprocess

import pytest

from vivibox import image


def test_digest_depends_on_user_and_context(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    a = image.context_digest(1000, 1000, tmp_path)
    assert image.context_digest(1001, 1000, tmp_path) != a
    (tmp_path / "Dockerfile").write_text("FROM busybox\n")
    assert image.context_digest(1000, 1000, tmp_path) != a


def test_build_command_passes_user():
    cmd = image.build_command("vivibox-agent:abc", 1234, 5678, pull=True)
    assert cmd[:2] == ["docker", "build"]
    assert "UID=1234" in cmd and "GID=5678" in cmd and "--pull" in cmd
    assert cmd[-1] == str(image.CONTEXT)


def test_checks_run_restricted_and_compare_output():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="none\n", stderr="")

    results = image.run_checks("ref", runner)
    assert all(c[:3] == ["docker", "run", "--rm"] for c in calls)
    assert all("--cap-drop" in c and "no-new-privileges" in c and "none" in c for c in calls)
    passed = {check.name for check, ok, _ in results if ok}
    assert "no docker daemon in the image" in passed
    assert "runs as your UID/GID" not in passed


def test_failing_command_fails_check():
    def runner(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="none", stderr="")

    assert not any(ok for _, ok, _ in image.run_checks("ref", runner))


@pytest.mark.docker
def test_built_image_passes_checks():
    ref, _ = image.build()
    failed = [(c.name, out) for c, ok, out in image.run_checks(ref) if not ok]
    assert not failed, failed
    assert os.getuid() != 0
