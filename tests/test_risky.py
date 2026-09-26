import os
import subprocess

import pytest

from vivibox.risky import DEFAULT_PATTERNS, NESTED_GIT, Approvals, matches, scan


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("pom.xml", "pom.xml", True),
        ("module/pom.xml", "pom.xml", True),
        ("src/pom.xml.bak", "pom.xml", False),
        (".mvn/extensions.xml", ".mvn/**", True),
        ("sub/.mvn/wrapper/maven-wrapper.properties", ".mvn/**", True),
        (".mvn", ".mvn/**", False),
        ("app/build.gradle.kts", "*.gradle.kts", True),
        ("gradle/wrapper/gradle-wrapper.jar", "gradle/wrapper/**", True),
        ("sub/gradle/wrapper/x", "gradle/wrapper/**", False),
        ("docs/readme.md", ".idea/**", False),
        ("scripts/run.sh", "scripts/*.sh", True),
    ],
)
def test_matches(path, pattern, expected):
    assert matches(path, pattern) is expected


def write(root, rel, text="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    write(root, "pom.xml", "<project/>")
    write(root, "src/Main.java", "class Main {}")
    write(root, ".idea/workspace.xml")
    write(root, "node_modules/pkg/package.json")
    write(root, "web/package.json", "{}")
    return root


def test_scan_finds_risky_files_only(repo):
    assert set(scan(repo)) == {"pom.xml", ".idea/workspace.xml", "web/package.json"}


def test_scan_reports_nested_git_repositories(repo):
    (repo / "vendor" / "lib" / ".git").mkdir(parents=True)
    write(repo, "sub/.git", "gitdir: ../elsewhere")
    found = scan(repo)
    assert found["vendor/lib/.git"] == NESTED_GIT and found["sub/.git"] == NESTED_GIT


def test_scan_catches_symlinked_risky_directory(repo):
    write(repo, "docs/fake-idea/workspace.xml")
    (repo / ".vscode").symlink_to("docs/fake-idea")
    assert ".vscode" in scan(repo)


def test_symlinked_file_changes_when_its_target_does(repo):
    write(repo, "config/real-pom.xml", "<a/>")
    (repo / "lib").mkdir()
    (repo / "lib" / "pom.xml").symlink_to("../config/real-pom.xml")
    before = scan(repo)["lib/pom.xml"]
    write(repo, "config/real-pom.xml", "<b/>")
    assert scan(repo)["lib/pom.xml"] != before


def test_extra_patterns_from_project(repo):
    write(repo, "scripts/dev.sh")
    assert "scripts/dev.sh" in scan(repo, ["scripts/*.sh"])


def test_approvals_track_changes_and_show_diffs(tmp_path, repo):
    approvals = Approvals(tmp_path / "meta", repo)
    approvals.approve()
    assert approvals.changes() == []

    write(repo, "pom.xml", "<project><build/></project>")
    write(repo, ".mvn/extensions.xml", "<extensions/>")
    (repo / ".idea" / "workspace.xml").unlink()
    kinds = {c.path: c.kind for c in approvals.changes()}
    assert kinds == {"pom.xml": "changed", ".mvn/extensions.xml": "added", ".idea/workspace.xml": "removed"}
    pom = next(c for c in approvals.changes() if c.path == "pom.xml")
    diff = approvals.diff(pom)
    assert "-<project/>" in diff and "+<project><build/></project>" in diff

    approvals.approve()
    assert approvals.changes() == []
    write(repo, "pom.xml", "<project><build/><x/></project>")
    assert [c.path for c in approvals.changes()] == ["pom.xml"], "a later change needs a new approval"


def test_binary_diff_is_summarised(tmp_path, repo):
    approvals = Approvals(tmp_path / "meta", repo)
    approvals.approve()
    (repo / "gradle" / "wrapper").mkdir(parents=True)
    (repo / "gradle" / "wrapper" / "gradle-wrapper.jar").write_bytes(os.urandom(64) + b"\xff\xfe")
    change = approvals.changes()[0]
    assert "binary" in approvals.diff(change)


def test_scan_of_a_real_clone_ignores_its_own_git_dir(tmp_path):
    root = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert scan(root) == {}


@pytest.mark.parametrize(
    "path",
    [
        "vitest.config.ts", "web/vite.config.js", "jest.config.mjs", ".mocharc.yml", "playwright.config.ts",
        "cypress.config.js", "karma.conf.js", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
        "tests/conftest.py", "Makefile", "src/test/resources/junit-platform.properties",
    ],
)  # fmt: skip
def test_test_configuration_is_risky_too(path):
    """What decides which tests run, and how, switches tests off without any of the words the gate
    looks for in added lines; a change to it is for you to see before the work counts."""
    assert any(matches(path, p) for p in DEFAULT_PATTERNS), path


def test_lockfiles_are_risky(repo):
    """A lockfile decides what `npm ci` installs on your host, and an agent that adds one where
    the project has none (a Yarn monorepo's root) changes how the project is verified."""
    for name in ("web/package-lock.json", "web/yarn.lock", "web/pnpm-lock.yaml", "npm-shrinkwrap.json"):
        write(repo, name, "")
    found = set(scan(repo))
    assert {"web/package-lock.json", "web/yarn.lock", "web/pnpm-lock.yaml", "npm-shrinkwrap.json"} <= found


def test_the_agents_instruction_files_are_risky(repo):
    """AGENTS.md is read by the agent as its instructions: an agent that edits it instructs the
    next turn, and the next task, so the change waits for you like a build file's."""
    write(repo, "AGENTS.md", "# Rules\n")
    write(repo, "web/CLAUDE.md", "# Rules\n")
    assert {"AGENTS.md", "web/CLAUDE.md"} <= set(scan(repo))
