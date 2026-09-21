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


class FakeDocker:
    def __init__(self, images, containers, refuse=()):
        self.images, self.containers, self.refuse = images, containers, set(refuse)
        self.removed = []

    def __call__(self, cmd):
        out, rc = "", 0
        if cmd[:3] == ["docker", "image", "ls"]:
            out = "\n".join(self.images) + "\n"
        elif cmd[:3] == ["docker", "ps", "-a"]:
            out = "\n".join(self.containers) + "\n"
        elif cmd[:3] == ["docker", "image", "rm"]:
            rc = 1 if cmd[3] in self.refuse else 0
            if not rc:
                self.removed.append(cmd[3])
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


def test_older_agent_images_go_and_the_current_and_used_ones_stay():
    docker = FakeDocker(
        images=["vivibox-agent:new", "vivibox-agent:old1", "vivibox-agent:used", "vivibox-agent:old2",
                "vivibox-agent:<none>"],
        containers=["docker:29.8.1-dind", "vivibox-agent:used"],
        refuse=["vivibox-agent:old2"],
    )  # fmt: skip
    removed = image.remove_old("vivibox-agent:new", runner=docker)
    assert docker.removed == ["vivibox-agent:old1"], "not the current one, nor one a pod runs on"
    assert removed == ["vivibox-agent:old1"], "one Docker refused is not reported as removed"


def test_nothing_is_removed_when_docker_cannot_say_what_is_in_use():
    class Down(FakeDocker):
        def __call__(self, cmd):
            if cmd[:3] == ["docker", "ps", "-a"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="daemon down")
            return super().__call__(cmd)

    docker = Down(images=["vivibox-agent:new", "vivibox-agent:old"], containers=[])
    assert image.remove_old("vivibox-agent:new", runner=docker) == [] and docker.removed == []
