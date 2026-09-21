import json
import subprocess
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
