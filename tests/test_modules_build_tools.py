"""Verification by modules for Gradle and npm/pnpm workspaces (ADR-0033, change of 2026-09-29):
init finds their modules, and {modules:FORMAT} writes each module the way the build tool names it
(%s the folder, %p Gradle's project path); the whole build is the same command for every module."""

import json
from pathlib import Path

import pytest

from vivibox import init, proposal
from vivibox.config import Project, by_modules


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_gradle_modules_come_from_the_settings(tmp_path):
    write(
        tmp_path / "settings.gradle", "rootProject.name = 'shop'\ninclude 'core', 'web:api'\ninclude ':app'\n"
    )
    assert init.modules(tmp_path) == ["core", "web/api", "app"]
    kts = tmp_path / "kts"
    write(kts / "settings.gradle.kts", 'include("core", ":web:api")\n')
    assert init.modules(kts) == ["core", "web/api"]


def test_npm_and_pnpm_workspaces_are_modules(tmp_path):
    npm = tmp_path / "npm"
    write(npm / "package.json", json.dumps({"workspaces": ["packages/*", "tools/cli"]}))
    for name in ("packages/a", "packages/b", "tools/cli"):
        write(npm / name / "package.json", "{}")
    (npm / "packages" / "notes").mkdir()
    assert init.modules(npm) == ["packages/a", "packages/b", "tools/cli"], "folders with a package.json"
    pnpm = tmp_path / "pnpm"
    write(pnpm / "package.json", "{}")
    write(pnpm / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n")
    write(pnpm / "apps" / "web" / "package.json", "{}")
    assert init.modules(pnpm) == ["apps/web"]


@pytest.mark.parametrize(
    "command, modules, runs, whole",
    [
        (
            "./gradlew {modules:%p:check}",
            ["core", "web/api"],
            "./gradlew :core:check :web:api:check",
            "./gradlew check",
        ),
        (
            "npm test {modules:--workspace=%s}",
            ["packages/a", "packages/b"],
            "npm test --workspace=packages/a --workspace=packages/b",
            "npm test --workspaces",
        ),
        ("pnpm {modules:--filter=%s} test", ["apps/web"], "pnpm --filter=apps/web test", "pnpm -r test"),
        (
            "mvn -B -pl {modules} -am verify",
            ["core", "app"],
            "mvn -B -pl core,app -am verify",
            "mvn -B verify",
        ),
    ],
)
def test_each_build_tool_names_its_modules_its_own_way(command, modules, runs, whole):
    assert by_modules(command)
    assert proposal.for_modules([command], modules) == [runs]
    assert proposal.whole(command) == whole
    assert Project("p", Path("/r"), [command]).by_module


@pytest.mark.parametrize(
    "command, scoped",
    [
        ("./gradlew test --no-daemon", "./gradlew {modules:%p:test} --no-daemon"),
        ("./gradlew build check", "./gradlew {modules:%p:build} {modules:%p:check}"),
        ("npm test", "npm test {modules:--workspace=%s}"),
        ("npm run check", "npm run check {modules:--workspace=%s}"),
        ("pnpm test", "pnpm {modules:--filter=%s} test"),
    ],
)
def test_a_gradle_or_npm_command_becomes_the_one_by_modules(command, scoped):
    assert init.scoped(command) == scoped


def test_init_verifies_a_gradle_build_by_modules_and_leaves_npm_to_the_writer(tmp_path):
    gradle = tmp_path / "gradle"
    write(gradle / "settings.gradle", "include 'a', 'b', 'c'\n")
    write(gradle / "gradlew", "")
    found = init.detect(gradle)
    assert found.modules == ["a", "b", "c"]
    assert found.verify == ["bash ./gradlew {modules:%p:check}"]
    npm = tmp_path / "npm"
    write(npm / "package.json", json.dumps({"workspaces": ["p/*"]}))
    for name in ("a", "b", "c"):
        write(npm / "p" / name / "package.json", "{}")
    found = init.detect(npm)
    assert found.modules == ["p/a", "p/b", "p/c"] and found.verify == [], (
        "its test script is the writer's to find"
    )
