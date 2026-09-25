import subprocess
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import init
from vivibox.cli import main
from vivibox.config import load_project


def gradle_project(path: Path, gradle: str, source: str) -> Path:
    make_repo(path)
    (path / "gradlew").write_text("#!/bin/sh\n")
    (path / "gradle" / "wrapper").mkdir(parents=True)
    (path / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{gradle}-bin.zip\n"
    )
    (path / "build.gradle").write_text(f"sourceCompatibility = '{source}'\n")
    return path


@pytest.mark.parametrize(
    "gradle,source,java",
    [("7.3.3", "11", "17"), ("8.10", "17", ""), ("6.9", "1.8", "11"), ("8.10", "21", "")],
)
def test_gradle_projects_get_a_jdk_their_gradle_runs_on(tmp_path, gradle, source, java):
    found = init.detect(gradle_project(tmp_path / "shop", gradle, source))
    assert found.verify == [], "a note, never the verification"
    assert "gradlew runs: bash gradlew test --no-daemon --console=plain" in found.notes
    assert found.java == java


def test_maven_and_npm(tmp_path):
    maven = make_repo(tmp_path / "api")
    (maven / "mvnw").write_text("")
    (maven / "pom.xml").write_text(
        "<properties><maven.compiler.release>17</maven.compiler.release></properties>"
    )
    assert init.detect(maven).verify == [] and "mvnw runs: bash mvnw -B verify" in init.detect(maven).notes
    assert init.detect(maven).java == "", "Java 21 builds code written for 17"
    web = make_repo(tmp_path / "Web_App")
    (web / "package.json").write_text("{}")
    found = init.detect(web)
    assert found.verify == [] and found.name == "web-app"
    assert "package.json runs: npm ci && npm test" in found.notes


YARN = "yarn install --immutable && yarn test"
YARN_CLASSIC = "yarn install --frozen-lockfile && yarn test"
PNPM = "pnpm install --frozen-lockfile && pnpm test"


@pytest.mark.parametrize(
    "package,files,verify",
    [
        ('{"packageManager": "yarn@3.6.4"}', {"package-lock.json": ""}, YARN),
        ('{"packageManager": "yarn@1.22.22"}', {}, YARN_CLASSIC),
        ('{"packageManager": "pnpm@9.1.0+sha256.abc"}', {"yarn.lock": ""}, PNPM),
        ('{"packageManager": "npm@10.8.0"}', {"yarn.lock": ""}, "npm ci && npm test"),
        ("{}", {"yarn.lock": "", ".yarnrc.yml": "nodeLinker: node-modules\n"}, YARN),
        ("{}", {"yarn.lock": ""}, YARN_CLASSIC),
        ("{}", {"pnpm-lock.yaml": ""}, PNPM),
        ("not json", {"pnpm-lock.yaml": ""}, PNPM),
    ],
)
def test_node_projects_are_tested_with_their_own_package_manager(tmp_path, package, files, verify):
    web = make_repo(tmp_path / "web")
    (web / "package.json").write_text(package)
    for name, text in files.items():
        (web / name).write_text(text)
    assert init.candidates(web)[0] == (verify, "package.json")


def test_init_writes_the_project_once(env, tmp_path, capsys):
    repo = gradle_project(tmp_path / "shop", "7.3.3", "11")
    assert main(["init", str(repo / "gradle"), "--yes"]) == 0
    project = load_project("shop")
    assert project.repo == repo and project.java == "17"
    assert main(["init", str(repo), "--yes", "--name", "shop-again"]) == 1
    assert "already project shop" in capsys.readouterr().err


def test_init_asks_first_and_writes_nothing_without_a_terminal(env, tmp_path):
    repo = gradle_project(tmp_path / "shop", "8.10", "21")
    assert main(["init", str(repo)]) == 1
    assert not (env / "config" / "projects" / "shop.toml").exists()


def test_init_outside_a_repository(env, tmp_path, capsys):
    (tmp_path / "plain").mkdir()
    assert main(["init", str(tmp_path / "plain"), "--yes", "--verify", "true"]) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_init_can_start_a_repository_from_scratch(env, tmp_path):
    from vivibox.config import load_project

    fresh = tmp_path / "clicker"
    assert main(["init", str(fresh), "--git", "--yes", "--verify", "npm test"]) == 0
    project = load_project("clicker")
    assert project.repo == fresh and project.verify == ["npm test"]
    assert (fresh / ".git").is_dir() and (fresh / "README.md").exists()
    log = subprocess.run(["git", "log", "--oneline"], cwd=fresh, capture_output=True, text=True).stdout
    assert "Initial commit" in log


def test_a_project_with_no_command_is_verified_with_what_its_writer_proposed(env, tmp_path):
    """A plan that names a command sets nothing: the writer, who builds the project while it
    works, proposes one; the task is verified with it, and the project keeps nothing yet."""
    from vivibox import actions, gate, proposal
    from vivibox.config import load_project
    from vivibox.states import State

    fresh = tmp_path / "clicker"
    actions.setup_project(fresh, "clicker", [], create=True)
    task = actions.create("clicker", "A click counter page")
    plan = '+++\nverify = ["npm test"]\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] it counts clicks\n'
    task.plan_path.write_text(plan)
    task.transition(State.CHECKPOINT_PLAN)
    actions.accept_plan(task, load_project("clicker"))
    project = load_project("clicker")
    assert project.verify == [], "the plan settles nothing for the project"
    assert actions.verify_commands(task, project) == []
    assert "proposed no command" in actions.missing_command(task, project)
    (task.meta / "handoff" / proposal.PROPOSAL).write_text("```\n$ npm ci && npm test\n```\n")
    assert actions.verify_commands(task, project) == ["npm ci && npm test"]
    assert actions.missing_command(task, project) == ""
    assert load_project("clicker").verify == [], "kept only once you accept it"
    assert (task.meta / gate.ACCEPTED_PLAN).exists()


def test_every_build_file_is_a_candidate_with_its_source(tmp_path):
    """What a new project is told: one command per build file that names it, as a note."""
    (tmp_path / "mvnw").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert init.candidates(tmp_path) == [
        ("bash mvnw -B verify", "mvnw"),
        ("npm ci && npm test", "package.json"),
    ]
    found = init.detect(tmp_path)
    assert found.verify == [] and found.source == "mvnw"
    assert init.candidates(tmp_path / "nowhere") == []


def test_a_plan_without_a_build_holds_for_that_task_only(env, tmp_path):
    from vivibox import actions
    from vivibox.config import config_dir
    from vivibox.states import State

    fresh = tmp_path / "notes"
    actions.setup_project(fresh, "notes", [], create=True)
    task = actions.create("notes", "Write the handbook")
    plan = "+++\nverify = false\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] it is written\n"
    task.plan_path.write_text(plan)
    task.transition(State.CHECKPOINT_PLAN)
    actions.accept_plan(task, load_project("notes"))
    project = load_project("notes")
    assert not project.no_build and project.verify == [], "the next plan decides for the project"
    assert actions.verify_commands(task, project) == [], "this task runs no build"
    assert actions.missing_command(task, project) == "", "and nobody is asked for a command"
    assert "verify = false" not in (config_dir() / "projects" / "notes.toml").read_text()


def test_the_project_file_takes_a_command_or_no_build_from_the_picker(env, tmp_path):
    from vivibox import actions
    from vivibox.config import config_dir

    fresh = tmp_path / "notes"
    actions.setup_project(fresh, "notes", [], create=True)
    actions.save_verify(load_project("notes"), [], no_build=True)
    assert load_project("notes").no_build
    actions.save_verify(load_project("notes"), ["npm test"])
    project = load_project("notes")
    assert project.verify == ["npm test"] and not project.no_build
    assert (config_dir() / "projects" / "notes.toml").read_text().count("verify") == 1


def test_the_ci_definition_says_how_the_project_is_built(tmp_path):
    """The one place a project says how it builds, written by people who know it. Offered after
    the build files' own commands, so the detected default stays what the build tool runs, and
    said in the notes so a person sees it at init."""
    (tmp_path / "mvnw").write_text("")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: ./mvnw --batch-mode verify -Pintegration\n"
        "      - name: Lint\n        run: |\n          npm ci\n          npm run lint\n"
        "      - run: ./mvnw deploy\n"
        "      - run: echo ${{ matrix.os }}\n"
    )
    assert init.ci_commands(tmp_path) == [
        ("bash mvnw --batch-mode verify -Pintegration", ".github/workflows/ci.yml"),
        ("npm ci && npm run lint", ".github/workflows/ci.yml"),
    ]
    found = init.candidates(tmp_path)
    assert found[0] == ("bash mvnw -B verify", "mvnw") and found[1:] == init.ci_commands(tmp_path)
    detected = init.detect(tmp_path)
    assert detected.verify == []
    assert any("ci.yml runs: bash mvnw --batch-mode verify -Pintegration" in n for n in detected.notes)


def test_gitlab_jenkins_and_bitbucket_definitions_are_read_too(tmp_path):
    (tmp_path / ".gitlab-ci.yml").write_text(
        "test:\n  script:\n    - ./gradlew test --no-daemon\n    - echo done\n"
    )
    (tmp_path / "Jenkinsfile").write_text(
        "pipeline {\n  stages {\n    stage('Test') {\n      steps {\n        sh 'npm ci && npm test'\n"
        '        sh "./gradlew publish"\n      }\n    }\n  }\n}\n'
    )
    (tmp_path / "bitbucket-pipelines.yml").write_text(
        "pipelines:\n  default:\n    - step:\n        script:\n"
        "          - pnpm install --frozen-lockfile\n          - pnpm test\n"
    )
    assert init.ci_commands(tmp_path) == [
        ("bash gradlew test --no-daemon", ".gitlab-ci.yml"),
        ("npm ci && npm test", "Jenkinsfile"),
        ("pnpm install --frozen-lockfile && pnpm test", "bitbucket-pipelines.yml"),
    ]
    assert init.ci_commands(tmp_path / "nowhere") == []
