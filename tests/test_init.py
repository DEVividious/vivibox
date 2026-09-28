import subprocess
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import init, prompts
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
    assert "gradlew runs: bash ./gradlew test --no-daemon --console=plain" in found.notes
    assert found.java == java


def test_maven_and_npm(tmp_path):
    maven = make_repo(tmp_path / "api")
    (maven / "mvnw").write_text("")
    (maven / "pom.xml").write_text(
        "<properties><maven.compiler.release>17</maven.compiler.release></properties>"
    )
    assert init.detect(maven).verify == [] and "mvnw runs: bash ./mvnw -B verify" in init.detect(maven).notes
    assert init.detect(maven).java == "", "Java 21 builds code written for 17"
    web = make_repo(tmp_path / "Web_App")
    (web / "package.json").write_text("{}")
    found = init.detect(web)
    assert found.verify == [] and found.name == "web-app"
    assert "package.json runs: npm ci && npm test" in found.notes


def test_the_build_files_suggest_what_to_prepare_a_new_tasks_clone_with(tmp_path):
    """A build without tests, so the writer starts on a built project and builds one module at a
    time: the wrapper's own words for Maven and Gradle, the install by its lockfile for Node,
    nothing where the files say nothing or the image has no tool for them."""
    maven = make_repo(tmp_path / "api")
    (maven / "mvnw").write_text("")
    (maven / "pom.xml").write_text("<project/>")
    assert init.prepare_suggestion(maven) == ["bash ./mvnw -B install -DskipTests"]
    assert init.detect(maven).prepare == ["bash ./mvnw -B install -DskipTests"]
    (maven / "mvnw").unlink()
    assert init.prepare_suggestion(maven) == ["mvn -B install -DskipTests"]
    gradle = make_repo(tmp_path / "app")
    (gradle / "gradlew").write_text("")
    assert init.prepare_suggestion(gradle) == ["bash ./gradlew assemble --no-daemon --console=plain"]
    (gradle / "mvnw").write_text("")
    assert init.prepare_suggestion(gradle)[0].startswith("bash ./gradlew"), "one build tool: the first found"
    web = make_repo(tmp_path / "web")
    (web / "package.json").write_text("{}")
    assert init.prepare_suggestion(web) == [], "npm ci without a lockfile fails every time"
    (web / "pnpm-lock.yaml").write_text("")
    assert init.prepare_suggestion(web) == ["pnpm install --frozen-lockfile"]
    other = make_repo(tmp_path / "svc")
    (other / "go.mod").write_text("module svc\n")
    assert init.prepare_suggestion(other) == ["go build ./..."], "the project's Go is installed for it"
    assert 'prepare = ["mvn -B install -DskipTests"]' in init.render(init.detect(maven)), (
        "in the project file"
    )


YARN = "yarn install --immutable && yarn test"
YARN_CLASSIC = "yarn install --frozen-lockfile && yarn test"
PNPM = "pnpm install --frozen-lockfile && pnpm test"
BUN = "bun install --frozen-lockfile && bun run test"


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
        ("{}", {"bun.lock": ""}, BUN),
        ("{}", {"bun.lockb": ""}, BUN),
        ('{"packageManager": "bun@1.3.2"}', {}, BUN),
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
    assert main(["init", str(fresh), "--git", "--yes", "--verify", "npm test", "--prepare", "npm ci"]) == 0
    project = load_project("clicker")
    assert project.repo == fresh and project.verify == ["npm test"]
    assert project.prepare == ["npm ci"], "--prepare sets what a new task's clone runs first"
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


@pytest.mark.parametrize(
    "written",
    [
        "# Verification proposal\n\nbash ./mvnw -B verify\n",
        "# Verification proposal\n\n```bash\n# from the root\n$ bash ./mvnw -B verify\n```\n",
        "## Command\n\nRun `bash ./mvnw -B verify` from the root.\n",
        "- `bash ./mvnw -B verify`\n",
    ],
)
def test_a_proposal_written_as_markdown_gives_its_command_not_its_heading(env, written):
    """The file is Markdown and a writer writes it so: a heading, a fenced block with its
    language, a sentence around the command. The command is what comes out."""
    from vivibox import actions, proposal

    task = actions.create("demo", "Refactor the parser")
    (task.meta / "handoff").mkdir(parents=True, exist_ok=True)
    (task.meta / "handoff" / proposal.PROPOSAL).write_text(written)
    assert proposal.proposed(task) == "bash ./mvnw -B verify"


def test_every_build_file_is_a_candidate_with_its_source(tmp_path):
    """What a new project is told: one command per build file that names it, as a note."""
    (tmp_path / "mvnw").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert init.candidates(tmp_path) == [
        ("bash ./mvnw -B verify", "mvnw"),
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
        ("bash ./mvnw --batch-mode verify -Pintegration", ".github/workflows/ci.yml"),
        ("npm ci && npm run lint", ".github/workflows/ci.yml"),
    ]
    found = init.candidates(tmp_path)
    assert found[0] == ("bash ./mvnw -B verify", "mvnw") and found[1:] == init.ci_commands(tmp_path)
    detected = init.detect(tmp_path)
    assert detected.verify == []
    assert any("ci.yml runs: bash ./mvnw --batch-mode verify -Pintegration" in n for n in detected.notes)


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
        ("bash ./gradlew test --no-daemon", ".gitlab-ci.yml"),
        ("npm ci && npm test", "Jenkinsfile"),
        ("pnpm install --frozen-lockfile && pnpm test", "bitbucket-pipelines.yml"),
    ]
    assert init.ci_commands(tmp_path / "nowhere") == []


def test_what_maven_adds_by_itself_is_said_in_the_notes(tmp_path):
    """On a work laptop the command built somewhere else without -f pom.xml, and the build cache left modules
    without their jars: both were in .mvn, where nobody looks while typing the command."""
    (tmp_path / "mvnw").write_text("")
    (tmp_path / ".mvn").mkdir()
    (tmp_path / ".mvn" / "maven.config").write_text("-f backend/pom.xml\n-pl app\n")
    (tmp_path / ".mvn" / "extensions.xml").write_text(
        "<extensions><extension><groupId>org.apache.maven.extensions</groupId>"
        "<artifactId>maven-build-cache-extension</artifactId></extension></extensions>"
    )
    notes = init.detect(tmp_path).notes
    assert ".mvn/maven.config adds to every mvn run: -f backend/pom.xml -pl app" in notes
    assert ".mvn/extensions.xml turns the Maven Build Cache on" in notes
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pom.xml").write_text("<project/>")
    assert not any(".mvn" in n for n in init.detect(plain).notes)


def test_a_pipelines_variables_are_filled_in_so_the_note_says_what_ci_runs(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "env:\n  MAVEN_EXTRA_ARGS: -Pintegration -DskipITs\n"
        "jobs:\n  build:\n    env:\n      PROFILE: 'fast'\n    steps:\n"
        "      - run: ./mvnw --batch-mode verify $MAVEN_EXTRA_ARGS\n"
        "      - run: ./mvnw -P${PROFILE} test ${{ env.MAVEN_EXTRA_ARGS }}\n"
        "      - run: ./mvnw verify -Dos=${{ matrix.os }}\n"
    )
    assert [c for c, _ in init.ci_commands(tmp_path)] == [
        "bash ./mvnw --batch-mode verify -Pintegration -DskipITs",
        "bash ./mvnw -Pfast test -Pintegration -DskipITs",
    ], "a matrix value is not known, so that step stays out"
    (tmp_path / ".gitlab-ci.yml").write_text(
        "variables:\n  GRADLE_OPTS: --offline\ntest:\n  script:\n    - ./gradlew test $GRADLE_OPTS $UNKNOWN\n"
    )
    assert ("bash ./gradlew test --offline $UNKNOWN", ".gitlab-ci.yml") in init.ci_commands(tmp_path)


def test_a_narrowed_proposal_is_not_run_but_named_for_the_gate(env, tmp_path):
    """A writer that proposes `-Dtest=Mine test` would be verified by its own tests alone: the
    gate gets the selection to refuse, and nothing to run; a project's own command is its choice."""
    import dataclasses

    from vivibox import actions, proposal
    from vivibox.config import load_project
    from vivibox.states import State

    fresh = tmp_path / "clinic"
    actions.setup_project(fresh, "clinic", [], create=True)
    task = actions.create("clinic", "Add Pet tests")
    task.plan_path.write_text("+++\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] pets are tested\n")
    task.transition(State.CHECKPOINT_PLAN)
    actions.accept_plan(task, load_project("clinic"))
    project = load_project("clinic")
    (task.meta / "handoff" / proposal.PROPOSAL).write_text("./mvnw -Dtest=PetTests test\n")
    assert actions.narrowed_proposal(task, project) == "-Dtest=PetTests"
    assert actions.verify_commands(task, project) == [], "not run: the gate refuses it instead"
    assert actions.missing_command(task, project) == "", "a command was proposed, a wrong one"
    (task.meta / "handoff" / proposal.PROPOSAL).write_text("./mvnw -B verify\n")
    assert actions.narrowed_proposal(task, project) == ""
    assert actions.verify_commands(task, project) == ["./mvnw -B verify"]
    own = dataclasses.replace(project, verify=["./mvnw -Dtest=Smoke test"])
    assert actions.narrowed_proposal(task, own) == "", "the project's own choice"


@pytest.mark.parametrize(
    "command",
    [
        "yarn --cwd apps/web install --frozen-lockfile && yarn --cwd apps/web test",
        "npm --prefix apps/web ci && npm --prefix apps/web test",
        "cd apps/web && yarn install && yarn test",
        "pnpm -C apps/web install --frozen-lockfile && pnpm -C apps/web test",
    ],
)
def test_a_command_that_installs_in_a_folder_is_left_alone(tmp_path, command):
    """`--cwd`, `--prefix`, `-C` or a `cd` before the install: the command installs itself, and
    an `npm ci` put in front of it would fail on a root without a lockfile."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    assert init.with_dependencies(tmp_path, [command]) == [command]


def test_a_monorepo_offers_its_apps_commands_not_the_roots(tmp_path):
    """The root package.json of a monorepo has no test script and no lockfile; the apps under
    it, or the workspaces it names, have both. The candidates are theirs, from their folder."""
    (tmp_path / "package.json").write_text('{"private": true, "scripts": {"prepare": "cd apps/web && yarn"}}')
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    (web / "yarn.lock").write_text("")
    docs = tmp_path / "apps" / "docs"
    docs.mkdir()
    (docs / "package.json").write_text('{"scripts": {"build": "next build"}}')
    assert init.candidates(tmp_path) == [
        ("cd apps/web && yarn install --frozen-lockfile && yarn test", "apps/web/package.json"),
    ], "one per app with a test script; the root, which tests nothing, is not offered"
    (tmp_path / "package.json").write_text('{"workspaces": ["packages/*"]}')
    core = tmp_path / "packages" / "core"
    core.mkdir(parents=True)
    (core / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (core / "pnpm-lock.yaml").write_text("")
    assert init.candidates(tmp_path) == [
        ("cd packages/core && pnpm install --frozen-lockfile && pnpm test", "packages/core/package.json"),
    ], "the workspaces it names win over the usual folders"


def test_accepting_the_command_keeps_it_for_the_project_and_starts_the_verification(env, tmp_path):
    from vivibox import actions, gate, proposal, supervisor
    from vivibox.config import load_project
    from vivibox.states import State

    fresh = tmp_path / "clinic"
    actions.setup_project(fresh, "clinic", [], create=True)

    def at_checkpoint(goal, command):
        task = actions.create("clinic", goal)
        task.plan_path.write_text("+++\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] it works\n")
        task.transition(State.CHECKPOINT_PLAN)
        actions.accept_plan(task, load_project("clinic"))
        if command:
            (task.meta / "handoff" / proposal.PROPOSAL).write_text(f"{command}\n")
        task.transition(State.CHECKPOINT_COMMAND)
        return task

    task = at_checkpoint("Add Pet tests", "./mvnw -B verify")
    actions.accept_command(task, load_project("clinic"))
    assert load_project("clinic").verify == ["./mvnw -B verify"], "kept for the project, once"
    assert task.read_state().state is State.VERIFY, "the verification runs with it now"
    path = env / "config" / "projects" / "clinic.toml"
    path.write_text(path.read_text().replace('verify = ["./mvnw -B verify"]', "verify = []"))
    task = at_checkpoint("Add Vet tests", "")
    with pytest.raises(gate.GateError, match="no command came from the writer"):
        actions.accept_command(task, load_project("clinic"))
    assert task.read_state().state is State.CHECKPOINT_COMMAND
    actions.accept_command(task, load_project("clinic"), "./mvnw -B test")
    assert load_project("clinic").verify == ["./mvnw -B test"], "yours wins over the proposal"
    path.write_text(path.read_text().replace('verify = ["./mvnw -B test"]', "verify = []"))
    task = at_checkpoint("Add Owner tests", "./mvnw -Dtest=Owner test")
    assert actions.reply(task, "The whole build, please") is State.IMPLEMENT
    assert supervisor.next_prompt(task, prompts.IMPLEMENT_PROMPT) == prompts.COMMENT_PROMPT


def test_a_command_that_starts_in_a_folder_gets_its_install_there(tmp_path):
    """A monorepo's app has the lockfile, not the root: the command the writer proposed runs
    from the app's folder (`cd apps/web && …`) and, on a fresh clone, found nothing installed
    (`tsc: not found`, exit 127, two turns of the writer lost). The install goes in front of it,
    in the same folder, by that folder's lockfile."""
    (tmp_path / "package.json").write_text('{"private": true}')
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    (web / "yarn.lock").write_text("")
    command = "cd apps/web && cp .env.example .env && yarn build && yarn test --run"
    assert init.with_dependencies(tmp_path, [command]) == [
        "cd apps/web && yarn install --frozen-lockfile",
        command,
    ]
    installs = "cd apps/web && yarn install --frozen-lockfile && yarn test"
    assert init.with_dependencies(tmp_path, [installs]) == [installs], "installs itself"
    assert init.with_dependencies(tmp_path, ["cd apps/other && yarn test"]) == [
        "cd apps/other && yarn test"
    ], "no lockfile there: nothing to install by"


def python_project(path: Path, pyproject: str = "", **files: str) -> Path:
    make_repo(path)
    if pyproject:
        (path / "pyproject.toml").write_text(pyproject)
    for name, text in files.items():
        (path / name.replace("__", ".").replace("_txt", ".txt")).write_text(text)
    return path


PYPROJECT = '[project]\nname = "shop"\nversion = "0.1.0"\n'


def test_python_projects_are_tested_through_uv_without_leaving_files_behind(tmp_path):
    """uv runs the tests in an environment from its lockfile, or a throwaway one from the project
    or its requirements: nothing it writes shows as an uncommitted file in the clone."""
    locked = python_project(
        tmp_path / "a", PYPROJECT + '[dependency-groups]\ndev = ["pytest>=8"]\n', uv__lock=""
    )
    assert "uv.lock runs: uv run --frozen pytest" in init.detect(locked).notes
    assert init.prepare_suggestion(locked) == ["uv sync --frozen"]
    no_pytest = python_project(tmp_path / "b", PYPROJECT, uv__lock="")
    assert "uv.lock runs: uv run --frozen --with pytest pytest" in init.detect(no_pytest).notes
    unlocked = python_project(tmp_path / "c", PYPROJECT)
    notes = init.detect(unlocked).notes
    assert "pyproject.toml runs: uv run --no-project --with-editable . --with pytest pytest" in notes
    assert init.prepare_suggestion(unlocked) == [], "a throwaway environment needs nothing prepared"
    plain = python_project(tmp_path / "d", requirements_txt="flask\n")
    assert (
        "requirements.txt runs: uv run --no-project --with-requirements requirements.txt --with pytest pytest"
        in init.detect(plain).notes
    )
    (plain / "requirements-dev.txt").write_text("pytest\n")
    assert (
        "requirements.txt runs: uv run --no-project --with-requirements requirements.txt"
        " --with-requirements requirements-dev.txt --with pytest pytest" in init.detect(plain).notes
    )


def test_python_tests_run_with_the_group_or_the_extra_that_has_pytest(tmp_path):
    """Pallets keep pytest in a `tests` dependency group, humanize in a `tests` extra: a plain
    `uv run pytest` installs neither and finds no pytest."""
    grouped = python_project(
        tmp_path / "a",
        PYPROJECT + '[dependency-groups]\ndev = ["ruff"]\ntests = ["freezegun", "pytest"]\n',
        uv__lock="",
    )
    assert "uv.lock runs: uv run --frozen --group tests pytest" in init.detect(grouped).notes
    extra = python_project(
        tmp_path / "b", PYPROJECT + '[project.optional-dependencies]\ntests = ["pytest>=9", "pytest-cov"]\n'
    )
    assert (
        "pyproject.toml runs: uv run --no-project --with-editable '.[tests]' pytest"
        in init.detect(extra).notes
    )
    both = python_project(
        tmp_path / "c", PYPROJECT + '[project.optional-dependencies]\ntest = ["pytest"]\n', uv__lock=""
    )
    assert "uv.lock runs: uv run --frozen --extra test pytest" in init.detect(both).notes
    broken = python_project(tmp_path / "d", "[project\n", uv__lock="")
    assert "uv.lock runs: uv run --frozen --with pytest pytest" in init.detect(broken).notes


def test_go_and_rust_projects_get_their_toolchain_their_tests_and_a_build(tmp_path):
    """The image has no Go or Rust: the project file names the version its files ask for, and
    the pod installs it with mise, for the agent and for the gate; nothing is written into the
    repository."""
    go = make_repo(tmp_path / "svc")
    (go / "go.mod").write_text("module example.com/svc\n\ngo 1.24.2\n\ntoolchain go1.25.3\n")
    found = init.detect(go)
    assert found.tools == ["go@1.25.3"], "the toolchain line wins over the language version"
    assert "go.mod runs: go test ./..." in found.notes and found.prepare == ["go build ./..."]
    assert 'tools = ["go@1.25.3"]' in init.render(found)
    assert "go@1.25.3: not in the image; the pod installs it with mise" in found.notes
    (go / "go.mod").write_text("module example.com/svc\n\ngo 1.24\n")
    assert init.detect(go).tools == ["go@1.24"]
    rust = make_repo(tmp_path / "crate")
    (rust / "Cargo.toml").write_text('[package]\nname = "crate"\n')
    found = init.detect(rust)
    assert found.tools == ["rust@stable"] and found.prepare == ["cargo test --no-run"]
    assert "Cargo.toml runs: cargo test" in found.notes
    (rust / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.89.0"\n')
    assert init.detect(rust).tools == ["rust@1.89.0"]
    (rust / "rust-toolchain.toml").unlink()
    (rust / "rust-toolchain").write_text("1.85\n")
    assert init.detect(rust).tools == ["rust@1.85"]
    assert init.detect(make_repo(tmp_path / "plain")).tools == []


def test_a_bun_project_gets_bun_from_mise_and_installs_by_its_lockfile(tmp_path):
    """The image has no bun: an agent that installed it itself had it in its own home, and the
    gate, with a fresh one, did not."""
    web = make_repo(tmp_path / "web")
    (web / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    (web / "bun.lock").write_text("")
    found = init.detect(web)
    assert found.tools == ["bun@latest"] and found.prepare == ["bun install --frozen-lockfile"]
    (web / "package.json").write_text('{"packageManager": "bun@1.3.2", "scripts": {"test": "vitest"}}')
    assert init.detect(web).tools == ["bun@1.3.2"]
    assert init.with_dependencies(web, ["bun run test"]) == ["bun install --frozen-lockfile", "bun run test"]
    assert init.with_dependencies(web, ["bun install && bun run test"]) == ["bun install && bun run test"]


def test_go_before_toolchains_gets_a_go_that_builds_it(tmp_path):
    """Before Go 1.21 the go line was a floor that nothing enforced: cobra says go 1.15, and its
    tests use os.WriteFile from 1.16. Any Go since builds such a module."""
    go = make_repo(tmp_path / "cli")
    (go / "go.mod").write_text("module example.com/cli\n\ngo 1.15\n")
    assert init.detect(go).tools == ["go@latest"]
    (go / "go.mod").write_text("module example.com/cli\n\ngo 1.21.0\n")
    assert init.detect(go).tools == ["go@1.21.0"]


def test_a_large_maven_reactor_is_verified_by_the_modules_a_plan_names(tmp_path):
    """Its whole build outlasts a verification (keycloak: hundreds of modules and a frontend), so
    init writes the command with {modules} into the verification itself; a reactor of a few
    modules builds whole in minutes and its writer proposes the command, as ever."""
    big = make_repo(tmp_path / "big")
    (big / "mvnw").write_text("")
    modules = "".join(f"<module>m{i}</module>" for i in range(init.MANY_MODULES))
    (big / "pom.xml").write_text(f"<project><modules>{modules}</modules></project>")
    found = init.detect(big)
    assert found.verify == ["bash ./mvnw -B -pl {modules} -am verify"]
    assert any("{modules}" in note for note in found.notes), "and says why"
    small = make_repo(tmp_path / "small")
    (small / "pom.xml").write_text(
        "<project><modules><module>core</module><module>app</module></modules></project>"
    )
    assert init.detect(small).verify == []


def test_an_executable_wrapper_is_run_as_itself(tmp_path):
    """bash is only for a wrapper committed without its executable bit: run through bash, an
    executable one does not always work as the project expects. The bit is read from git, as the
    gate's fresh clone gets it."""
    repo = make_repo(tmp_path / "shop")
    (repo / "mvnw").write_text("#!/bin/sh\n")
    (repo / "gradlew").write_text("#!/bin/sh\n")
    subprocess.run(["git", "add", "mvnw", "gradlew"], cwd=repo, check=True)
    subprocess.run(["git", "update-index", "--chmod=+x", "mvnw", "gradlew"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Add the wrappers"], cwd=repo, check=True)
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  build:\n    steps:\n      - run: ./mvnw verify\n")
    assert init.candidates(repo)[:2] == [
        ("./gradlew test --no-daemon --console=plain", "gradlew"),
        ("./mvnw -B verify", "mvnw"),
    ]
    assert ("./mvnw verify", ".github/workflows/ci.yml") in init.ci_commands(repo)
    assert init.prepare_suggestion(repo) == ["./gradlew assemble --no-daemon --console=plain"]
    (repo / "pom.xml").write_text(
        "<project><modules>" + "<module>m</module>" * init.MANY_MODULES + "</modules></project>"
    )
    assert init.by_module(repo) == ["./mvnw -B -pl {modules} -am verify"]
