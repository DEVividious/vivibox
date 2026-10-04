"""The reviewer's container: its harness works in a directory the host made, and reads the work
in a view of the commits the host made, never in the writer's clone."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import as_verified
from test_control import FLOWS, a_large_repository, binds, commit, git, made, variables
from test_control import docker as docker  # noqa: F401  (the fixture: a machine and a recording Docker)
from test_control import starting as starting  # noqa: F401  (the fixture: a start on stand-ins)
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
        "GIT_CONFIG_COUNT=1",  # git there never takes a committed folder for a bare repository
        "GIT_CONFIG_KEY_0=safe.bareRepository",
        "GIT_CONFIG_VALUE_0=explicit",
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
    with pytest.raises(PodError, match="the review could not start: the reviewer's working directory") as e:
        pod.review_start([], {})
    assert str(e.value).endswith(f"start the task again: vivibox start {task.id}"), "what to do"
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
    as_verified(task)
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


def test_the_supervisor_brings_a_round_up_with_the_readers_brief_and_the_projects_commands(docker, verified):  # noqa: F811
    """What the supervisor's own review_up writes for the reviewer: the reader's brief, for a
    container that has the handoff to read, naming the project's verification commands as a
    fact; never the writer's brief, which describes the writer's container."""
    from vivibox import brief, supervise

    task, pod, head = verified
    _, project = actions.load(task.id)
    actions.save_verify(project, ["make test"])
    _, project = actions.load(task.id)
    assert project.verify == ["make test"]
    sup = supervise.make_supervisor(task, project, pod, load_config())
    out = sup.ports.review_up()
    assert out == pod.review_dir / "out"
    told = (pod.review_dir / "harness" / "instructions.md").read_text()
    facts = (task.id, task.repo, repo.branch_name(task.id), ["make test"])
    assert told == brief.reader(*facts, writes_handoff=False) and "`make test`" in told
    assert "Docker, docker compose and Testcontainers work" not in told
    assert sup.reviewer.pod.agent == pod.review and sup.reviewer.port == opencode.REVIEW_PORT


# --- the view's own .git, git in a reader's container, links -------------------------------------

BRANCH = "vivibox/demo-1"


def a_clone(where: Path) -> tuple[Path, str]:
    """A repository on the task's branch with one commit, no task beside it; and that commit."""
    where.mkdir()
    git(where, "init", "-q", "-b", "main")
    base = commit(where, "calc.py", "x = 1\n")
    git(where, "checkout", "-q", "-b", BRANCH)
    return where, base


def program(path: Path, mark: Path, then: str = "exit 0") -> Path:
    """A program that leaves a mark when it runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {mark}\n{then}\n")
    path.chmod(0o755)
    return path


def test_a_view_and_a_working_directory_hold_no_hook_and_run_none_of_yours(tmp_path, monkeypatch):
    """Both are made without git's templates and under no configuration of yours: a template
    directory of your own (GIT_TEMPLATE_DIR) with active hooks, a core.hooksPath and a signing
    program in your global configuration put no hook into either folder and run nothing while
    the host makes them."""
    clone, _ = a_clone(tmp_path / "repo")
    ran = tmp_path / "ran"
    templates = tmp_path / "templates"
    for hook in ("reference-transaction", "post-checkout", "post-commit"):
        program(templates / "hooks" / hook, ran)
    (templates / "info").mkdir()
    (templates / "info" / "exclude").write_text("calc.py\n")
    (templates / "description").write_text("yours\n")
    for hook in ("reference-transaction", "pre-commit", "post-commit", "post-checkout"):
        program(tmp_path / "yours" / hook, ran)
    signer = program(tmp_path / "sign.sh", ran, "exit 1")
    yours = tmp_path / "gitconfig"
    yours.write_text(
        f"[core]\n\thooksPath = {tmp_path / 'yours'}\n[commit]\n\tgpgsign = true\n"
        f"[gpg]\n\tprogram = {signer}\n[init]\n\ttemplateDir = {templates}\n"
    )
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(templates))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(yours))
    work, view = tmp_path / "work", tmp_path / "view"
    work.mkdir()
    view.mkdir()
    reading.workdir(work, "the reviewer's")
    reading.view(clone, BRANCH, view, whose="the reviewer's")
    assert not (view / ".git" / "hooks").exists(), "the view's .git is the host's: no hook, not even a sample"
    assert not (work / ".git" / "hooks").exists()
    for folder in (view, work):
        for from_a_template in ("description", "info"):
            assert not (folder / ".git" / from_a_template).exists(), f"{folder.name}: {from_a_template}"
    assert not ran.exists(), "a hook or a program of yours ran while the host made them"
    assert (view / "calc.py").read_text() == "x = 1\n"
    assert subprocess.run(["git", "-C", str(work), "rev-list", "--count", "HEAD"], capture_output=True).stdout


def test_the_hosts_git_fetches_nothing_lazily_from_the_clone(tmp_path, monkeypatch):
    """A clone that names a promisor remote and a command to reach it, with an object missing:
    git serving the host's fetch would go to that remote for it, by the command. The host's git
    never fetches lazily, whatever your environment says (GIT_NO_LAZY_FETCH=0 turns it back on
    for a git that has it off by default): no view is made, and the command does not run."""
    clone, _ = a_clone(tmp_path / "repo")
    commit(clone, "calc.py", "x = 2\n")
    blob = git(clone, "rev-parse", "HEAD:calc.py")
    (clone / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    ran = tmp_path / "ssh-ran"
    ssh = program(tmp_path / "ssh.sh", ran, "exit 1")
    for key, value in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "origin"),
        ("remote.origin.url", "ssh://example.invalid/x"),
        ("remote.origin.promisor", "true"),
        ("remote.origin.partialclonefilter", "blob:none"),
        ("core.sshCommand", str(ssh)),
    ):
        git(clone, "config", key, value)
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    view = tmp_path / "view"
    view.mkdir()
    with pytest.raises(repo.RepoError):
        reading.view(clone, BRANCH, view, whose="the reviewer's")
    assert not ran.exists(), "the host's git went to the writer's remote, by the writer's command"
    assert reading.HOST_GIT["GIT_NO_LAZY_FETCH"] == "1"


def a_folder_like_a_bare_repository(clone: Path, ran: Path) -> None:
    """What a writer can commit: a folder with all a git directory has, and a config that names a
    command git runs for a status there."""
    nested = clone / "nested"
    for folder in ("objects/info", "refs/heads"):
        (nested / folder).mkdir(parents=True)
        (nested / folder / "keep").write_text("")
    (nested / "HEAD").write_text("ref: refs/heads/main\n")
    program(nested / "fsmonitor", ran)
    (nested / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n\tfsmonitor = ./fsmonitor\n"
    )
    git(clone, "add", "-A")
    git(clone, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "Add nested")


def test_git_in_a_readers_container_takes_no_committed_folder_for_a_repository(tmp_path):
    """git there answers for the view by GIT_DIR; a command whose environment has lost it, run
    inside a folder the writer committed to look like a bare repository, would take that folder
    for one and run what its config names. The readers' environment says git never uses a
    bare repository it was not pointed at (safe.bareRepository=explicit), so that git refuses."""
    import os

    clone, _ = a_clone(tmp_path / "repo")
    ran = tmp_path / "ran"
    a_folder_like_a_bare_repository(clone, ran)
    view = tmp_path / "view"
    view.mkdir()
    reading.view(clone, BRANCH, view, whose="the reviewer's")
    given = reading.git_env(view)
    assert given["GIT_DIR"] == f"{view}/.git" and given["GIT_WORK_TREE"] == str(view)
    plain = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | reading.HOST_GIT

    def status(env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "status"], cwd=view / "nested", env=env, capture_output=True, text=True)

    assert status(plain).returncode == 0 and ran.exists(), "the method: such a folder is taken for one"
    ran.unlink()
    without_git_dir = {k: v for k, v in given.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    refused = status(plain | without_git_dir)
    assert refused.returncode != 0 and "cannot use bare repository" in refused.stderr, refused.stderr
    assert not ran.exists(), "git ran what the committed folder's config names"
    whole = status(plain | given)
    assert whole.returncode == 0 and not ran.exists(), "with GIT_DIR: the view, as before"
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=view / "nested",
        env=plain | given,
        capture_output=True,
        text=True,
    )
    assert top.stdout.strip() == str(view)


def test_a_link_the_writer_committed_is_a_plain_file_in_the_view(tmp_path):
    """A committed symbolic link would be followed, in the reader's container, to that container's
    own files (its secrets), which a review could then quote to the writer. The host checks the
    view out with links as plain files holding the path they name; git there finds the view
    clean. A view made before, with a link checked out as one, is made over."""
    clone, base = a_clone(tmp_path / "repo")
    secret = tmp_path / "secret"
    secret.write_text("TOP-SECRET\n")
    (clone / "link").symlink_to(secret)
    (clone / "inner").symlink_to("calc.py")
    git(clone, "add", "-A")
    git(clone, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "Add links")
    assert (clone / "link").read_text() == "TOP-SECRET\n", "the method: in the clone the link is followed"
    view = tmp_path / "view"
    view.mkdir()
    reading.view(clone, BRANCH, view, whose="the reviewer's")
    for name, names in (("link", str(secret)), ("inner", "calc.py")):
        assert not (view / name).is_symlink() and (view / name).is_file(), name
        assert (view / name).read_text() == names, "the path it names, not what is there"
    assert git(view, "status", "--porcelain") == "", "and the view is clean to git"
    assert "link" in git(view, "diff", "--stat", f"{base}..HEAD")
    older = tmp_path / "older"
    older.mkdir()
    git(older, "init", "-q", "-b", BRANCH)
    git(older, "fetch", "-q", str(clone), f"refs/heads/{BRANCH}")
    git(older, "reset", "-q", "--hard", "FETCH_HEAD")
    assert (older / "link").is_symlink(), "a view made before: the link, as a link"
    reading.view(clone, BRANCH, older, whose="the planner's")
    assert not (older / "link").is_symlink() and (older / "link").read_text() == str(secret)
    assert git(older, "status", "--porcelain") == "" and not (older / ".git" / "hooks").exists()


def no_links(folder: Path) -> list[str]:
    """Every symbolic link under folder, its .git aside."""
    found = []
    for path in folder.rglob("*"):
        if ".git" not in path.relative_to(folder).parts and path.is_symlink():
            found.append(str(path.relative_to(folder)))
    return sorted(found)


def an_older_view(clone: Path, where: Path) -> Path:
    """A view as it was made before links were kept as plain files: git's own checkout."""
    where.mkdir()
    git(where, "init", "-q", "-b", BRANCH)
    git(where, "fetch", "-q", str(clone), f"refs/heads/{BRANCH}")
    git(where, "reset", "-q", "--hard", "FETCH_HEAD")
    return where


def test_a_view_is_exactly_the_commit_whatever_was_in_its_folder(tmp_path):
    """After a refresh the view holds the commit's files and nothing else: nothing untracked,
    nothing ignored. A view made before (links checked out as links) whose branch has since
    deleted a link and a file keeps neither, and keeps no link at all; a link that pointed out
    of the view goes without what it pointed at being touched."""
    clone, _ = a_clone(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("not the view's\n")
    (clone / "link-in").symlink_to("calc.py")
    (clone / "link-out").symlink_to(outside)
    (clone / "old.py").write_text("old = 1\n")
    (clone / ".gitignore").write_text("*.log\n")
    git(clone, "add", "-A")
    git(clone, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "A")
    older = an_older_view(clone, tmp_path / "older")
    assert (older / "link-in").is_symlink() and (older / "link-out" / "kept.txt").exists(), "the method"
    current = tmp_path / "current"
    current.mkdir()
    reading.view(clone, BRANCH, current, whose="the planner's")
    for name in ("link-in", "link-out", "old.py"):
        git(clone, "rm", "-q", name)
    b = git(clone, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "B")
    head = git(clone, "rev-parse", "HEAD")
    for view in (older, current):
        # What only a process with the folder could have left: an ignored file, a stray folder
        # with a link out of the view in it.
        (view / "build.log").write_text("ignored\n")
        (view / "stray").mkdir()
        (view / "stray" / "out").symlink_to(outside)
        (view / "stray" / "file.txt").write_text("x\n")
        reading.view(clone, BRANCH, view, whose="the planner's")
        assert git(view, "rev-parse", "HEAD") == head, b
        for gone in ("link-in", "link-out", "old.py", "build.log", "stray"):
            assert not (view / gone).exists() and not (view / gone).is_symlink(), f"{view.name}: {gone}"
        assert git(view, "status", "--porcelain", "--ignored") == "", view.name
        assert no_links(view) == [], view.name
        assert sorted(p.name for p in view.iterdir()) == [".git", ".gitignore", "calc.py"]
    assert (outside / "kept.txt").read_text() == "not the view's\n", "nothing outside the view was touched"


def test_both_readers_views_have_links_as_plain_files_and_git_kept_to_the_view(docker, verified):  # noqa: F811
    """The planner's copy and the reviewer's are made by the same code, and both containers get
    the same environment for git."""
    task, pod, head = verified
    (task.repo / "link").symlink_to("/run/vivibox-secrets/server-password")
    git(task.repo, "add", "-A")
    git(task.repo, "-c", "user.name=W", "-c", "user.email=w@example.com", "commit", "-q", "-m", "Add a link")
    task.set_verified_commit(git(task.repo, "rev-parse", "HEAD"))
    control.refresh(task, pod)
    reviewing.up(task, pod, load_config())
    for view in (pod.control_src, Path(pod.review_src)):
        assert not (view / "link").is_symlink(), view
        assert (view / "link").read_text() == "/run/vivibox-secrets/server-password"
        assert not (view / ".git" / "hooks").exists()
    kept = {"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=safe.bareRepository", "GIT_CONFIG_VALUE_0=explicit"}
    assert kept <= set(variables(review_run(docker, task)))
    assert kept <= set(variables(made(docker, f"vivibox-{task.id}-control")))


# --- a round is its own ---------------------------------------------------------------------------


def test_a_review_of_a_task_with_no_verified_commit_is_refused(docker):  # noqa: F811
    """A review is of the commit the verification passed. A task that records none (not one the
    supervisor would review) gets no view of its branch as it happens to be, and no container."""
    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    commit(task.repo, "calc.py", "x = 1\n")
    with pytest.raises(repo.RepoError, match="the review could not start: .*no commit that passed") as e:
        reviewing.up(task, pod, load_config())
    assert str(e.value).endswith(f"start the task again: vivibox start {task.id}"), "what to do"
    assert not (Path(pod.review_src) / ".git").exists(), "no view of the branch as it is"
    assert not docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review")


def test_every_round_has_a_review_directory_and_a_view_of_its_own(docker, verified):  # noqa: F811
    """Nothing of a round is there for the next: the review directory is emptied and the view,
    the working directory, the harness files and the folder the review is written to are made
    again. A review the reviewer did not write in a round is not read from the round before."""
    task, pod, head = verified
    out = reviewing.up(task, pod, load_config())
    (out / "review.md").write_text("## Blocking\n\n## Not blocking\n\n## Checked\n\n- round one\n")
    view = Path(pod.review_src)
    (view / "left-by-round-one.txt").write_text("x\n")
    (pod.review_work / "opencode.json").write_text("{}\n")
    (pod.review_dir / "harness" / "extra.md").write_text("x\n")
    first = git(view, "rev-parse", "HEAD")
    later = commit(task.repo, "calc.py", "def divide(a, b):\n    return a / b if b else None\n")
    task.set_verified_commit(later)
    again = reviewing.up(task, pod, load_config())
    assert again == out and not (out / "review.md").exists(), "the last round's review is gone"
    assert not (view / "left-by-round-one.txt").exists()
    assert sorted(p.name for p in pod.review_work.iterdir()) == [".git"]
    assert sorted(p.name for p in (pod.review_dir / "harness").iterdir()) == [
        "instructions.md",
        "opencode.json",
    ]
    assert git(view, "rev-parse", "HEAD") == later != first, "the view, on this round's verified commit"
    assert len(docker.find("docker", "rm", "-f", f"vivibox-{task.id}-review")) >= 2, "the container too"


def test_a_reviewer_put_on_claude_code_stops_before_any_container(docker):  # noqa: F811
    """config.toml refuses a reviewer on another harness than opencode; a task put on Claude Code
    with m stops when its round is brought up, on the model's name, before the review directory
    is touched and before any container."""
    from vivibox.config import config_dir
    from vivibox.harness import HarnessError

    cfg = config_dir() / "config.toml"
    cfg.write_text(cfg.read_text() + '[roles.reviewer]\nharness = "opencode"\nmodel = "cheap/reads"\n')
    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    pod.up(timeout=1)
    task.set_verified_commit(commit(task.repo, "calc.py", "x = 1\n"))
    actions.choose_role(task, "reviewer", ("claude-code", "claude-opus"), load_config())
    with pytest.raises(HarnessError, match="must be <provider>/<model>"):
        reviewing.up(task, pod, load_config())
    assert not docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review")
    assert not pod.review_dir.exists(), "nothing made for the round"


@pytest.mark.real_start
def test_starting_a_task_refused_its_review_for_no_verified_commit_leads_to_a_review(
    docker,  # noqa: F811
    starting,  # noqa: F811
    monkeypatch,
):
    """The refusal names a way out, and it is one: a task in its review with no verified commit,
    stopped on that refusal, is started again as the message says; the supervisor then sends it
    back to the verification, which records the commit it passed, and the review after it
    starts on that commit and is read. (`vivibox verify` would not do: it records nothing.)"""
    from pathlib import Path as P

    from conftest import networks_up
    from test_control import CLEAN

    from vivibox import gate, supervise
    from vivibox.harness import Turn
    from vivibox.states import State

    task = actions.create("demo", "Add divide", orchestration="planner_writer_reviewer")
    pod = actions.task_pod(task.id)
    networks_up(pod)
    head = commit(task.repo, "calc.py", "def divide(a, b):\n    return a / b\n")
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.REVIEW):
        task.transition(state)
    assert task.read_state().verified_commit == ""
    with pytest.raises(repo.RepoError) as refused:
        reviewing.up(task, pod, load_config())
    said = str(refused.value)
    assert said.endswith(f"start the task again: vivibox start {task.id}")
    task.set_paused(True, problem=f"stopped on an error: {said}")  # as the supervisor leaves it
    assert not docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review")

    actions.start(task.id, supervise=False)  # what the message says
    st = task.read_state()
    assert not st.paused and st.state is State.REVIEW

    def turn(self, prompt, session="", title="", on_step=None):
        assert self.pod.agent == pod.review, "the reviewer's turn, in its container"
        (pod.review_dir / "out" / "review.md").write_text(CLEAN)
        return Turn("ses_review", True, 0.01, 10, "done")

    monkeypatch.setattr(opencode.OpenCode, "turn", turn)
    monkeypatch.setattr(opencode.OpenCode, "start_session", lambda self, title: "")
    _, project = actions.load(task.id)
    sup = supervise.make_supervisor(task, project, pod, load_config())
    verified = []
    sup.ports.run_gate = lambda t: verified.append(head) or gate.GateResult(P("/dev/null"), commit=head)
    sup.ports.prepare_review = lambda: None
    sup.ports.head = lambda: head
    sup.step()  # the review: nothing verified to read, back to the verification
    st = task.read_state()
    assert st.state is State.VERIFY and verified == [], "sent back, with no reviewer started"
    assert not docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review")
    sup.step()  # the verification: passes, and records what it passed
    st = task.read_state()
    assert st.state is State.REVIEW and st.verified_commit == head and verified == [head]
    sup.step()  # the review, of that commit
    assert docker.find("docker", "run", "-d", "--name", f"vivibox-{task.id}-review"), "a review started"
    assert git(P(pod.review_src), "rev-parse", "HEAD") == head
    st = task.read_state()
    assert st.reviews == 1 and (task.meta / "handoff" / "review-1.md").read_text() == CLEAN
    assert st.state in (State.CHECKPOINT_FINAL, State.APPROVAL_RISKY) and not st.paused


def test_the_hosts_git_for_a_view_works_in_the_view_whatever_repository_your_shell_names(
    tmp_path, monkeypatch
):
    """git takes GIT_DIR and GIT_WORK_TREE from the environment before the folder it is run in
    (a shell inside a git hook has them set). The host's git for a view and for a working
    directory is told its own folder, so its reset and its clean, which delete, reach nothing
    of another repository's: not its commits, not its untracked files."""
    clone, _ = a_clone(tmp_path / "repo")
    yours, _ = a_clone(tmp_path / "yours")
    (yours / "notes.txt").write_text("untracked, and yours\n")
    (yours / "calc.py").write_text("x = 'changed, not committed'\n")
    before = git(yours, "rev-parse", "HEAD"), git(yours, "status", "--porcelain")
    head = commit(clone, "calc.py", "x = 2\n")
    monkeypatch.setenv("GIT_DIR", str(yours / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(yours))
    work, view = tmp_path / "work", tmp_path / "view"
    work.mkdir()
    view.mkdir()
    reading.workdir(work, "the reviewer's")
    reading.view(clone, BRANCH, view, head, "the reviewer's")
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert (yours / "notes.txt").read_text() == "untracked, and yours\n", "the clean reached your files"
    assert (git(yours, "rev-parse", "HEAD"), git(yours, "status", "--porcelain")) == before
    assert "changed, not committed" in (yours / "calc.py").read_text(), "the reset reached your work"
    assert git(view, "rev-parse", "HEAD") == head and (view / "calc.py").read_text() == "x = 2\n"
    assert (
        git(work, "rev-list", "--count", "HEAD") == "1" and git(yours, "rev-list", "--count", "HEAD") == "1"
    )
