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


def test_the_shared_maven_repository_is_set_for_every_maven_a_wrapper_may_fetch():
    """MAVEN_ARGS is read by Maven 3.9 and later only. A project's mvnw fetching an older Maven
    built into $HOME/.m2 instead: the writer's home, which the gate, whose home is fresh, never
    sees, so its fresh clone lacked what the writer had built. MAVEN_OPTS reaches every version."""
    from importlib.resources import files

    text = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    assert 'MAVEN_OPTS="-Dmaven.repo.local=/cache/m2"' in text
    assert 'MAVEN_ARGS="-Dmaven.repo.local=/cache/m2' in text, "3.9's own way stays, with the file locks"
    checks = image.checks(1000, 1000)
    assert any("$MAVEN_OPTS" in check.command for check in checks), "the image check covers it"


def test_what_a_task_installs_is_kept_apart_from_what_maven_downloads():
    """Maven 3.9 splits its local repository: downloads under cached/, shared by every task, and
    what a build installs under installed/, where each task mounts a volume of its own. A fresh
    volume takes its owner from the image, so the image has the directory, the agent's."""
    from importlib.resources import files

    text = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    args = text[text.index('MAVEN_ARGS="') :].split('"')[1]
    assert "-Daether.enhancedLocalRepository.split=true" in args
    assert "/cache/m2/installed" in text
    writable = next(c for c in image.checks(1000, 1000) if c.name == "caches are writable")
    assert "m2/installed" in writable.command


def test_a_build_cache_hit_leaves_the_modules_files_in_place():
    """With lazyRestore the extension marks a module built and leaves its target/ empty until
    asked, and whatever reads the jar there fails: the next module, a packaging plugin."""
    from importlib.resources import files

    text = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    args = text[text.index('MAVEN_ARGS="') :].split('"')[1]
    assert "-Dmaven.build.cache.lazyRestore=false" in args


def test_python_gets_a_shared_uv_cache_and_writes_no_bytecode_into_the_clone():
    """The gate's clone is fresh every time: without a shared cache uv downloads every package
    again, and __pycache__ folders are untracked files that stop the build."""
    from importlib.resources import files

    from vivibox import pod

    text = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    assert "UV_CACHE_DIR=/cache/uv" in text and "UV_PYTHON_INSTALL_DIR=/cache/uv/python" in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text and "/cache/uv" in text.split("mkdir -p /config")[1]
    assert pod.CACHES["uv"] == "/cache/uv"
    writable = next(c for c in image.checks(1000, 1000) if c.name == "caches are writable")
    assert " uv " in writable.command


def test_pip_installs_only_into_a_virtual_environment():
    """mise's Python lives in the cache every task shares: a planner's `pip install -e .` put one
    task's clone on every other task's import path, and the gate's."""
    from importlib.resources import files

    text = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    assert "PIP_REQUIRE_VIRTUALENV=1" in text
    assert any("PIP_REQUIRE_VIRTUALENV" in check.command for check in image.checks(1000, 1000))


def test_go_and_rust_keep_their_downloads_and_builds_in_shared_caches():
    """The gate's home is fresh every time: without these every verification downloads the
    modules, the crates and Rust itself again."""
    from importlib.resources import files

    from vivibox import pod

    text = (files("vivibox") / "images" / "agent" / "Dockerfile").read_text()
    for setting in ("GOMODCACHE=/cache/go/mod", "GOCACHE=/cache/go/build", "GOFLAGS=-modcacherw",
                    "CARGO_HOME=/cache/cargo", "RUSTUP_HOME=/cache/rustup"):  # fmt: skip
        assert setting in text, setting
    made = text.split("mkdir -p /config")[1].split("&&")[0]
    assert all(d in made for d in ("/cache/go", "/cache/cargo", "/cache/rustup"))
    assert {"go", "cargo", "rustup"} <= set(pod.CACHES)
    writable = next(c for c in image.checks(1000, 1000) if c.name == "caches are writable")
    assert " go cargo rustup;" in writable.command
