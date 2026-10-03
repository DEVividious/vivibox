"""The shape of the code, as AGENTS.md states it: a module past 800 lines is split before a feature
is added to it. Checked here so the debt is seen when it is made, not after."""

import re
import subprocess
from pathlib import Path

LIMIT = 800
ROOT = Path(__file__).parent.parent

# The docker verbs that start or resume a container, as a word of an argument list ("docker",
# "run"; "docker", "container", "start"; ["docker"] + ["run"]) or of a string split into one
# ("docker run -d".split()).
VERB = r"(?:run|start|restart|unpause)"
BETWEEN = r"[\s,+*\[\]]*"
STARTER = re.compile(
    rf'["\']docker["\']{BETWEEN}(?:["\']container["\']{BETWEEN})?["\']{VERB}["\']'
    rf'|["\']docker\s+(?:container\s+)?{VERB}\b'
)
# Where one may be composed, how many, and why: the pod's own, and containers that are not of a pod.
STARTERS = {
    "pod.py": 1,  # docker start of the sidecar; the runs of its containers come from podspec
    "podspec.py": 4,  # the argv of the sidecar, the agent, the gate and the review, built only here
    "mirror.py": 2,  # the Docker Hub mirror, a container of your Docker, not of any pod
    "image.py": 1,  # the image's checks, a throwaway container on no network
    "roles.py": 2,  # a throwaway container that lists an agent CLI's models
    "cli_providers.py": 1,  # a throwaway container that lists an agent CLI's providers
}
# The builders of a pod container's argv (podspec, and Pod's methods over them): public, and a
# ready argv to anyone who calls one; so nobody outside the pod does.
BUILDER = re.compile(r"\b(?:sidecar|agent|gate|review)_command\b")
BUILDERS = {
    "podspec.py": 4,  # defined here
    "pod.py": 13,  # Pod's methods over them, and their only callers: up, gate_up, review_start
    "keys_plan.py": 2,  # the view's own review_command: the `review the command` decision, no pod in it
    "tui.py": 1,  # the same, called
}


def sites(pattern: re.Pattern) -> dict[str, int]:
    package = ROOT / "vivibox"
    return {
        str(path.relative_to(package)): n
        for path in sorted(package.rglob("*.py"))
        if (n := len(pattern.findall(path.read_text())))
    }


def test_only_the_pod_starts_a_container_of_a_pod():
    """Every operation that can start or resume a pod's container reconciles the host's rules first,
    fail-closed, through the pod (up, gate_up, review_start). A docker run, start, restart or
    unpause composed anywhere else would start one without them: the sites that compose one are
    listed here, each with its reason, so a new one is a failure until it is accounted for."""
    assert sites(STARTER) == STARTERS, "a new docker run or start: a pod container starts only through pod.py"


def test_only_the_pod_takes_a_pod_containers_argv():
    """The argv of a pod container is a ready docker run to whoever calls its builder: a caller
    outside the pod would run it past _rules_before with no docker literal of its own."""
    assert sites(BUILDER) == BUILDERS, "a pod container's argv is built and run in pod.py only"


def test_no_module_is_past_the_limit():
    package = Path(__file__).parent.parent / "vivibox"
    big = {
        str(path.relative_to(package)): lines
        for path in sorted(package.rglob("*.py"))
        if (lines := len(path.read_text().splitlines())) > LIMIT
    }
    assert not big, f"split before adding to it: {big}"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def test_a_change_under_the_package_has_its_line_in_the_changelog():
    """AGENTS.md: a branch that changes vivibox/ changes CHANGELOG.md in the same change, so the
    release notes are written as the work is done. Compared with main, committed and uncommitted
    alike, so it fails before the commit; on main itself, or where main is unknown (a shallow
    checkout in CI), there is nothing to compare with."""
    branch = git("branch", "--show-current").stdout.strip()
    if branch in ("", "main") or git("rev-parse", "--verify", "-q", "main").returncode:
        return
    changed = git("diff", "--name-only", "main", "--").stdout.split()
    if any(path.startswith("vivibox/") for path in changed):
        assert "CHANGELOG.md" in changed, "a change under vivibox/ needs its line in CHANGELOG.md"
