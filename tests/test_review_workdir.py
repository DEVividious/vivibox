"""The reviewer's container: its harness works in a directory the host made, and reads the work
in a view of the commits the host made, never in the writer's clone."""

import json
import subprocess
from pathlib import Path

import pytest
from test_control import FLOWS, a_large_repository, binds, commit, git, made, variables
from test_control import docker as docker  # noqa: F401  (the fixture: a machine and a recording Docker)
from test_pod import flag

from vivibox import actions, claudecode, control, opencode, providers, reading, repo, reviewing, secrets
from vivibox.config import load_config
from vivibox.pod import CACHES, SOCKET_DIR, Pod, PodError


@pytest.fixture
def verified(docker):  # noqa: F811
    """A task with a reviewer of its own whose pod is up, the writer's work committed and that
    commit verified; then one more commit the verification has not seen."""
    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    head = commit(task.repo, "calc.py", "def divide(a, b):\n    return a / b\n")
    task.set_verified_commit(head)
    commit(task.repo, "calc.py", "def divide(a, b):\n    return a // b  # not verified\n")
    return task, pod, head


def review_run(docker, task) -> list[str]:  # noqa: F811
    return made(docker, f"vivibox-{task.id}-review")


def test_the_reviewers_harness_works_in_a_directory_the_host_made_beside_a_view_of_the_commits(
    docker,  # noqa: F811
    verified,
):
    """The review container's working directory is a folder of the host's making, read-only: a
    git repository with one empty commit and nothing else. The work is a view of the commits,
    read-only, at the path the writer has its clone, and the writer's clone itself is not
    mounted; git answers for the view from anywhere. The rest is as it was: a home and a
    scratch folder of its own for the round, its network, its limits, no means of the build."""
    task, pod, head = verified
    reviewing.up(task, pod, load_config())
    cmd = review_run(docker, task)
    mounted = binds(cmd)
    work, view = f"{task.root}/disk/review/work", f"{task.root}/disk/review/src"
    assert cmd[cmd.index("-w") + 1] == "/work"
    assert f"{work}:/work:ro" in mounted, "the host's, read-only"
    assert f"{view}:{task.repo}:ro" in mounted, "the view, read-only, where the writer has its clone"
    assert [m for m in mounted if m.startswith(f"{task.repo}:")] == [], "the writer's clone is not mounted"
    assert [m for m in mounted if str(task.repo) in m] == [f"{view}:{task.repo}:ro"]
    assert not [m for m in mounted if m.startswith(f"{task.root}/disk/review:")], "nor the whole folder"
    targets = [m.split(":")[1] for m in mounted]
    assert not any(t.startswith("/work/") for t in targets), "nothing mounted in its working directory"
    assert set(variables(cmd)) == {
        "GIT_OPTIONAL_LOCKS=0",
        f"GIT_DIR={task.repo}/.git",
        f"GIT_WORK_TREE={task.repo}",
        "OPENCODE_DISABLE_LSP_DOWNLOAD=true",
        "OPENCODE_CONFIG=/task/harness/opencode.json",
    }
    size = pod.limits.tmpfs_size("review")
    scratch = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--tmpfs"]
    assert scratch == [f"/tmp:exec,mode=1777,size={size}", f"/config:exec,mode=1777,size={size}"]
    assert flag(cmd, "--network") == f"vivibox-{task.id}-r" and flag(cmd, "--ip") == "198.51.100.18"
    assert flag(cmd, "--add-host") == "host.docker.internal:198.51.100.17"
    assert flag(cmd, "--dns") == "203.0.113.1" and flag(cmd, "--dns-search") == "."
    assert not any("container:" in arg for arg in cmd)
    for given in (["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"], ["--read-only"]):
        i = cmd.index(given[0])
        assert cmd[i : i + len(given)] == given
    joined = " ".join(cmd)
    assert SOCKET_DIR not in joined and "vivibox-cache-" not in joined and "DOCKER_HOST" not in joined
    assert not any(path in joined for path in CACHES.values())
    assert flag(cmd, "--memory") == str(pod.limits.memory_of("review"))
    assert f"{task.meta}:/task:ro" in mounted
    assert f"{task.root}/disk/review/harness:/task/harness:ro" in mounted
    assert f"{task.root}/disk/review/out:/task/review" in mounted
    assert f"{secrets.runtime_dir(task.id + '-review')}:/run/vivibox-secrets:ro" in mounted
    assert sorted(p.name for p in pod.review_work.iterdir()) == [".git"], "nothing but git's own"
    assert not (pod.review_work / ".git" / "hooks").exists(), "no hook, not even a sample"
    assert git(pod.review_work, "rev-list", "--count", "HEAD") == "1"
    assert git(pod.review_work, "ls-tree", "-r", "HEAD") == "", "one empty commit"


def test_the_view_is_made_on_the_host_before_the_container_and_no_git_runs_in_it(docker, verified):  # noqa: F811
    """The host fetches the task's commits into the view and puts it on the commit the
    verification passed, whatever the branch points at since; then the container starts. No
    `git clone`, no git at all, is run in the review container."""
    task, pod, head = verified
    base = task.read_state().base_commit
    seen = []
    real = Pod.review_start

    def start(self, mounts, env):
        seen.append(git(Path(self.review_src), "rev-parse", "HEAD"))
        real(self, mounts, env)

    Pod.review_start = start
    try:
        before = len(docker.calls)
        reviewing.up(task, pod, load_config())
    finally:
        Pod.review_start = real
    assert seen == [head], "the view stood on the verified commit before the container started"
    view = Path(pod.review_src)
    assert (
        "return a / b" in (view / "calc.py").read_text()
        and "not verified" not in (view / "calc.py").read_text()
    )
    assert "calc.py" in git(view, "diff", "--stat", f"{base}..HEAD")
    assert "remote." not in git(view, "config", "--local", "--list"), "no remote: nothing names the clone"
    after = docker.calls[before:]
    assert [c for c in after if c[:2] == ["docker", "exec"] and "git" in c] == [], "no git in the container"
    assert not [c for c in after if "clone" in c]
    assert len([c for c in after if c[:3] == ["docker", "run", "-d"]]) == 1


def test_a_view_that_cannot_be_made_stops_the_review_and_starts_no_container(docker, verified, tmp_path):  # noqa: F811
    """Fail-closed: a clone that is not as recorded, or a verified commit that is not on the
    task's branch, makes no view; the review stops with the reason and no container is made,
    least of all one working in the writer's tree."""
    task, pod, head = verified
    (task.repo / ".git" / "commondir").write_text(f"{tmp_path}\n")
    before = len(docker.calls)
    with pytest.raises(repo.RepoError, match="the review could not start: .*commondir"):
        reviewing.up(task, pod, load_config())
    (task.repo / ".git" / "commondir").unlink()
    git(task.repo, "checkout", "-q", "--orphan", "elsewhere")
    stray = commit(task.repo, "stray.py", "y = 1\n")
    task.set_verified_commit(stray)
    with pytest.raises(repo.RepoError, match="the review could not start: .*is not on the task's branch"):
        reviewing.up(task, pod, load_config())
    assert not [c for c in docker.calls[before:] if c[:2] == ["docker", "run"]], "no container"


def test_a_review_container_is_not_started_on_folders_the_host_did_not_make(docker, verified):  # noqa: F811
    """The pod itself refuses: without the host's working directory, or without the view, there
    is no review container, whoever asks for one."""
    task, pod, head = verified
    pod.review_fresh()
    with pytest.raises(PodError, match="the review could not start: the reviewer's working directory"):
        pod.review_start([], {})
    reading.workdir(pod.review_work, "the reviewer's")
    with pytest.raises(PodError, match="the review could not start: the reviewer's view of the work"):
        pod.review_start([], {})
    assert not docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review")
    reading.view(task.repo, repo.branch_name(task.id), Path(pod.review_src), head, "the reviewer's")
    pod.review_start([], {})
    assert docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review")


def test_the_reviewers_configuration_is_a_readers_no_language_server_and_no_serena(docker, verified):  # noqa: F811
    """Beside the reviewer's key and its review, nothing works on the project by itself: its
    opencode has its language servers off and downloads none, and vivibox's own Serena is left
    out of its configuration, while the MCP servers you brought stay, with their secrets."""
    task, pod, head = verified
    a_large_repository(task)
    yours = {
        "type": "local",
        "command": ["tools"],
        "environment": {"TOKEN": "{file:/run/vivibox-secrets/mcp.tools.token}"},
    }
    providers._save(providers.mcp_path(), {"tools": yours})
    from vivibox import keys

    keys.set_key("mcp.tools.token", "secret")
    opencode.prepare(task, "cheap/writes", ["true"], ["cheap"])
    reviewing.up(task, pod, load_config())
    agents = json.loads((task.meta / "harness" / "opencode.json").read_text())
    reviewers = json.loads((pod.review_dir / "harness" / "opencode.json").read_text())
    assert sorted(agents["mcp"]) == ["serena", "tools"], "the agent's: yours and Serena"
    assert sorted(reviewers["mcp"]) == ["tools"] and "serena" not in json.dumps(reviewers)
    assert reviewers["lsp"] is False and "snapshot" not in reviewers
    held = sorted(p.name for p in secrets.runtime_dir(f"{task.id}-review").iterdir())
    assert held == ["cheap", "mcp.tools.token", "server-password"]
    assert reviewing.env(task)["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "true"


@pytest.mark.parametrize(("mode", "has_control"), FLOWS)
def test_no_container_but_the_reviewers_mounts_anything_of_the_reviewers(docker, mode, has_control):  # noqa: F811
    """The reviewer's secrets, its view of the commits, its working directory, its harness
    files and the folder its review is written to are in the review container's argv alone, in
    every flow; and that container mounts nothing of the planner's and nothing of the writer's:
    not the clone, not the agent's home or secrets, not the Docker socket, not a build cache."""
    task = actions.create("demo", "Add divide", orchestration=mode)
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    pod._copying_clone = lambda *cmd, **kw: None  # the gate's clone: no git here
    pod.gate_up()
    reviewing.up(task, pod, load_config())
    runs = {run[4].rsplit("-", 1)[1]: run for run in docker.find("docker", "run", "-d", "--name")}
    assert set(runs) == {"dind", "agent", "gate", "review"} | ({"control"} if has_control else set())
    for name, run in runs.items():
        if name == "review":
            continue
        for reviewers in (f"{task.root}/disk/review", f"{task.id}-review"):
            assert reviewers not in " ".join(run), f"{name} has {reviewers}"
    sources = [m.split(":")[0] for m in binds(runs["review"])]
    writers = (
        str(task.repo), f"{task.root}/disk/config", f"{task.root}/disk/installed", f"{task.root}/disk/docker",
        f"{task.root}/disk/gate", str(secrets.runtime_dir(task.id)), str(task.meta / "harness"),
        str(task.meta / "handoff"), f"vivibox-{task.id}-socket",
    )  # fmt: skip
    planners = (
        f"{task.root}/disk/config-control", f"{task.root}/disk/control-src", f"{task.root}/disk/control-work",
        str(secrets.runtime_dir(control.secrets_id(task.id))), str(task.meta / "harness-control"),
    )  # fmt: skip
    for source in sources:
        assert source not in writers, f"the review container mounts the writer's {source}"
        assert source not in planners, f"the review container mounts the planner's {source}"
        assert not source.startswith("vivibox-cache-"), source
    assert sorted(s for s in sources if s.startswith(f"{task.root}/disk/")) == [
        f"{task.root}/disk/review/{name}" for name in ("harness", "out", "src", "work")
    ]


def test_a_reviewer_on_claude_code_would_be_given_no_directory(docker):  # noqa: F811
    """No reviewer runs on Claude Code (config.toml refuses it, and a task put on it is stopped
    before its container); were its harness ever made, it would be given no directory besides
    its own, as the planner's in its control container: Claude Code loads skills from each."""
    from vivibox.config import config_dir

    cfg = config_dir() / "config.toml"
    cfg.write_text(cfg.read_text() + '[roles.reviewer]\nharness = "opencode"\nmodel = "cheap/reads"\n')
    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    actions.choose_role(task, "reviewer", ("claude-code", "claude-opus"), load_config())
    harness = actions.harness_for("reviewer", pod.review_side(), task)
    assert isinstance(harness, claudecode.ClaudeCode) and harness.add_dirs == ()
    assert harness.pod.agent == pod.review


def test_git_in_the_reviewers_view_runs_nothing_the_writer_planted(docker, verified, tmp_path):  # noqa: F811
    """What a writer leaves in its clone's .git for the next git there (a commondir to a config
    that names a command for git diff) is not in the view: its .git is the host's making. With
    the check of the clone switched off (planted in the moment after it), the view has the
    commits, and git diff and git log there run no command of the writer's."""
    from test_control import planted

    task, pod, head = verified
    base = task.read_state().base_commit
    ran = planted(task, tmp_path)
    subprocess.run(["git", "-C", str(task.repo), "diff", f"{base}..HEAD"], capture_output=True)
    assert ran.exists(), "the plant is live: git in the writer's clone runs the writer's command"
    ran.unlink()
    real = repo.guard
    repo.guard = lambda clone: None
    try:
        reviewing.up(task, pod, load_config())
    finally:
        repo.guard = real
    view = Path(pod.review_src)
    assert git(view, "rev-parse", "HEAD") == head
    git(view, "diff", f"{base}..HEAD")
    git(view, "log", "--oneline")
    assert not (view / ".git" / "commondir").exists()
    assert not ran.exists(), "no command of the writer's ran, on the host or for the reviewer"
