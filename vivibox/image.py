"""Build and check the agent image."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CONTEXT = Path(__file__).parent / "images" / "agent"
REPOSITORY = "vivibox-agent"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def context_digest(uid: int, gid: int, context: Path = CONTEXT) -> str:
    """Changes whenever the build context or the target user changes, so the tag says when to rebuild."""
    h = hashlib.sha256(f"{uid}:{gid}\n".encode())
    for path in sorted(p for p in context.rglob("*") if p.is_file()):
        h.update(path.relative_to(context).as_posix().encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def image_ref(uid: int | None = None, gid: int | None = None) -> str:
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return f"{REPOSITORY}:{context_digest(uid, gid)[:12]}"


def build_command(ref: str, uid: int, gid: int, pull: bool) -> list[str]:
    cmd = ["docker", "build", "--build-arg", f"UID={uid}", "--build-arg", f"GID={gid}", "-t", ref]
    if pull:
        cmd.append("--pull")
    return [*cmd, str(CONTEXT)]


def env(ref: str, runner: Runner = run) -> dict[str, str]:
    """The image's environment, e.g. its PATH to extend for a project's own JDK."""
    p = runner(["docker", "image", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", ref])
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)


def exists(ref: str, runner: Runner = run) -> bool:
    return runner(["docker", "image", "inspect", ref]).returncode == 0


def build(pull: bool = False, force: bool = False) -> tuple[str, bool]:
    """Returns the image reference and whether it was built now. Build output goes to the terminal."""
    uid, gid = os.getuid(), os.getgid()
    ref = image_ref(uid, gid)
    if exists(ref) and not force:
        return ref, False
    subprocess.run(build_command(ref, uid, gid, pull), check=True)
    remove_old(ref)
    return ref, True


def remove_old(current: str, runner: Runner = run) -> list[str]:
    """Removes the agent images older versions of vivibox built, about 2 GB each. One a container
    still uses stays: a task's pod made before an update runs on it until the pod is made again.
    Returns what was removed."""
    listed = runner(["docker", "image", "ls", REPOSITORY, "--format", "{{.Repository}}:{{.Tag}}"])
    used = runner(["docker", "ps", "-a", "--format", "{{.Image}}"])
    if listed.returncode != 0 or used.returncode != 0:
        return []
    in_use = set(used.stdout.split())
    removed = []
    for ref in listed.stdout.split():
        if ref == current or ref in in_use or ref.endswith(":<none>"):
            continue
        if runner(["docker", "image", "rm", ref]).returncode == 0:
            removed.append(ref)
    return removed


@dataclass(frozen=True)
class Check:
    name: str
    command: str
    expect: str


def checks(uid: int, gid: int) -> list[Check]:
    return [
        Check("runs as your UID/GID", "echo $(id -u):$(id -g)", f"{uid}:{gid}"),
        Check(
            "HOME is /config and writable",
            "echo $HOME && touch $HOME/.w && rm $HOME/.w && echo ok",
            "/config\nok",
        ),
        Check(
            "Java user.home follows HOME",
            "java -XshowSettings:properties -version 2>&1 | grep user.home",
            "user.home = /config",
        ),
        Check("Java 21", "java -version 2>&1 | head -1", '"21.'),
        Check(
            "Maven uses the shared cache",
            "mvn -v | head -1 && echo $MAVEN_ARGS",
            "-Dmaven.repo.local=/cache/m2",
        ),
        Check(
            "an older Maven from a project's wrapper uses it too",
            "echo $MAVEN_OPTS",
            "-Dmaven.repo.local=/cache/m2",
        ),
        Check(
            "caches are writable",
            "cd /cache && for d in m2 m2/installed gradle npm mise corepack build-cache uv go cargo rustup;"
            " do touch $d/.w || exit 1; done; echo ok",
            "ok",
        ),
        Check("Gradle uses the shared cache", "echo $GRADLE_USER_HOME", "/cache/gradle"),
        # mise installs Python on first use, so this image has none to ask yet: the setting it is.
        Check("pip keeps out of the shared Python", "echo pip=$PIP_REQUIRE_VIRTUALENV", "pip=1"),
        Check("Node and npm", "node -v && npm -v", "v24."),
        Check("corepack", "corepack --version", "."),
        Check("git", "git --version", "git version"),
        Check("docker client and compose", "docker --version && docker compose version", "Docker Compose"),
        Check("no docker daemon in the image", "command -v dockerd || echo none", "none"),
        Check("opencode", "opencode --version", "."),
        Check("claude code", "claude --version", "(Claude Code)"),
        Check("mise", "mise --version", "linux"),
        Check("ripgrep", "rg --version | head -1", "ripgrep"),
        Check("serena, the MCP server vivibox offers", "serena --help | head -1", "Usage"),
        Check(
            "no secrets in the image environment",
            "env | cut -d= -f1 | grep -iE 'token|secret|key|password|auth' || echo none",
            "none",
        ),
    ]


def run_checks(ref: str, runner: Runner = run) -> list[tuple[Check, bool, str]]:
    """Runs each check in a fresh container with the same restrictions a task pod uses."""
    results = []
    for c in checks(os.getuid(), os.getgid()):
        p = runner(
            [
                "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", ref, "bash", "-c", c.command,
            ]
        )  # fmt: skip
        out = (p.stdout + p.stderr).strip()
        results.append((c, p.returncode == 0 and c.expect in out, out))
    return results
