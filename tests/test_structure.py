"""The shape of the code, as AGENTS.md states it: a module past 800 lines is split before a feature
is added to it. Checked here so the debt is seen when it is made, not after."""

import ast
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
    # the argv of the sidecar, the agent, the gate, the review and the control, built only here
    "podspec.py": 5,
    "mirror.py": 2,  # the Docker Hub mirror, a container of your Docker, not of any pod
    "image.py": 1,  # the image's checks, a throwaway container on no network
    "roles.py": 2,  # a throwaway container that lists an agent CLI's models
    "cli_providers.py": 1,  # a throwaway container that lists an agent CLI's providers
}
# The builders of a pod container's argv (podspec, and Pod's methods over them): public, and a
# ready argv to anyone who calls one; so nobody outside the pod does.
BUILDER = re.compile(r"\b(?:sidecar|agent|gate|review|control)_command\b")
BUILDERS = {
    "podspec.py": 5,  # defined here
    "pod.py": 16,  # Pod's methods over them, and their only callers: up, gate_up, review_start
    "keys_plan.py": 2,  # the view's own review_command: the `review the command` decision, no pod in it
    "tui.py": 1,  # the same, called
}


# Where an argument list of git's own is written (a list or a tuple that begins with "git"),
# how many, and why. git on the host runs only through repo.run_git and repo._bounded, which
# give it repo.git_environment: your shell's GIT_DIR, GIT_INDEX_FILE or GIT_CONFIG_COUNT
# reaches none of them. git in a container (pod.exec, pod.gate_exec) is no such list: its
# environment is the container's.
GIT_ARGV = {
    "repo.py": 2,  # run_git, and git handing its command to _bounded: both in git_environment
    # the diff under f, run by keys_work.diff_pager in git_environment
    # (tests/test_git_environment.py runs it with every variable exported)
    "panel.py": 1,
    "pod.py": 1,  # compared with a docker exec's command to bound it; runs nothing
}


def git_argv_sites() -> dict[str, int]:
    package = ROOT / "vivibox"
    found = {}
    for path in sorted(package.rglob("*.py")):
        lists = (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.List | ast.Tuple))
        firsts = (n.elts[0] for n in lists if n.elts)
        if count := sum(1 for first in firsts if isinstance(first, ast.Constant) and first.value == "git"):
            found[str(path.relative_to(package))] = count
    return found


def test_git_on_the_host_runs_only_through_the_helper_that_cleans_its_environment():
    """A module that writes git's argument list itself would run it in your shell's environment,
    with whatever GIT_* variables it carries. The sites that write one are listed here, each
    with its reason, so a new one is a failure until it goes through repo.run_git, repo.git or
    repo.in_clone, or is accounted for."""
    assert git_argv_sites() == GIT_ARGV, "git on the host runs through repo.run_git, repo.git or in_clone"


def keyword_sites(name: str, unless) -> dict[str, int]:
    """Where a call in the package has the keyword name, by module, but for a value unless
    accepts."""
    package = ROOT / "vivibox"
    found = {}
    for path in sorted(package.rglob("*.py")):
        calls = (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Call))
        if count := sum(1 for c in calls for k in c.keywords if k.arg == name and not unless(k.value)):
            found[str(path.relative_to(package))] = count
    return found


def constant(value):
    """Whether a node is that constant."""
    return lambda node: isinstance(node, ast.Constant) and node.value is value


def test_one_git_on_the_host_is_handed_how_you_reach_your_remotes():
    """repo.TO_YOUR_REMOTES (your ssh command, your askpass, your certificates) pass to a git
    that is asked for with to_remotes: the fetch of a new clone's submodules in repo.prepare,
    which asks by whether the commit it starts from is your own (repo.yours), and no other.
    The rest of repo.py only hands the choice down, and only by name: the functions take it
    as a keyword alone, so no call says it out of this count's sight, and none says it
    outright."""
    assert keyword_sites("to_remotes", unless=constant(False)) == {"repo.py": 3}, (
        "prepare, by repo.yours; git to run_git; run_git to git_environment"
    )
    assert keyword_sites("to_remotes", unless=lambda node: not constant(True)(node)) == {}


def test_nothing_is_run_through_a_shell():
    """shell=True hands a string to sh: a git in it is no argument list for the guard above, and
    would run in your shell's environment."""
    assert keyword_sites("shell", unless=constant(False)) == {}


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
