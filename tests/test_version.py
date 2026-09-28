"""The version a bug report starts with names the commit that runs, also from a checkout that
was installed editable and pulled since (uv tool install -e: the metadata keeps the install's)."""

import subprocess

from vivibox import version


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def commit(repo, n):
    for i in range(n):
        git(repo, "commit", "-q", "--allow-empty", "-m", f"c{i}")
    return git(repo, "rev-parse", "--short=7", "HEAD").strip()


def test_a_checkout_is_named_by_its_git_as_the_build_would_be(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "a@b")
    git(repo, "config", "user.name", "a")
    sha = commit(repo, 2)
    assert version.from_checkout(repo) == f"0.0.1.dev2+{sha}"
    git(repo, "tag", "-a", "v0.2.0", "-m", "release")
    assert version.from_checkout(repo) == "0.2.0"
    sha = commit(repo, 3)
    assert version.from_checkout(repo) == f"0.2.1.dev3+{sha}"
    assert version.from_checkout(tmp_path / "not-git") == ""


def test_the_running_checkout_wins_over_the_installs_metadata(monkeypatch):
    monkeypatch.setattr(version, "installed", lambda name: "0.1.0")
    version.current.cache_clear()
    here = version.from_checkout(version.SOURCE)
    assert here, "the tests run from a checkout"
    assert version.current() == here
