"""A project of many modules verifies a task with the modules its plan names ({modules} in its
command), and with the whole build, the same command without them, when the work reaches past them."""

import subprocess
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import gate, proposal, repo
from vivibox.config import Project, load_project
from vivibox.plan import PlanError, parse_plan
from vivibox.task import create_task

SCOPED = "mvn -B -pl {modules} -am verify"
WHOLE = ["mvn -B verify"]
PLAN = "+++\n{header}\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] subtract works\n"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_a_project_verified_by_module_has_modules_in_its_one_command(tmp_path):
    write(tmp_path / "projects" / "shop.toml", f'repo = "/r"\nverify = ["{SCOPED}"]\n')
    assert load_project("shop", tmp_path).by_module
    write(tmp_path / "projects" / "old.toml", 'repo = "/r"\nverify = ["mvn -B verify"]\n')
    assert not load_project("old", tmp_path).by_module


@pytest.mark.parametrize(
    "command, whole",
    [
        ("mvn -B -pl {modules} -am verify", "mvn -B verify"),
        ("bash mvnw -B --projects={modules} --also-make test", "bash mvnw -B test"),
        ("mvn -B -pl {modules} verify -DskipITs", "mvn -B verify -DskipITs"),
        ("make test", "make test"),
    ],
)
def test_the_whole_build_is_the_command_without_its_modules(command, whole):
    """One command: the whole build is what it runs with nothing chosen."""
    assert proposal.whole(command) == whole


def test_a_plan_names_the_modules_it_changes():
    plan = parse_plan(PLAN.format(header='modules = ["core", "services/app"]'))
    assert plan.modules == ["core", "services/app"]
    assert parse_plan(PLAN.format(header="")).modules == []


@pytest.mark.parametrize("modules", ['["../core"]', '["/core"]', '["core app"]', '[""]', '"core"', '["a,b"]'])
def test_a_module_is_a_directory_in_the_repository(modules):
    with pytest.raises(PlanError, match="modules"):
        parse_plan(PLAN.format(header=f"modules = {modules}"))


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def change(task, path: str) -> None:
    write(task.repo / path, "changed\n")
    git(task.repo, "add", ".")
    git(task.repo, "commit", "-q", "-m", f"Change {path}")


@pytest.fixture
def task(tmp_path):
    source = make_repo(tmp_path / "source")
    for module in ("core", "app", "extra"):
        write(source / module / "pom.xml", "<project/>\n")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Modules")
    t = create_task(tmp_path / "tasks", "shop", "goal", PLAN.format(header='modules = ["core", "app"]'))
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    gate.accept_plan(t)
    return t


SHOP = Project("shop", Path("/r"), [SCOPED])


def test_work_inside_the_planned_modules_is_verified_with_them(task):
    change(task, "core/src/Calc.java")
    change(task, "app/src/Report.java")
    assert proposal.verify_commands(task, SHOP) == ["mvn -B -pl core,app -am verify"]
    assert proposal.outside_modules(task, SHOP) == []


def test_work_outside_the_planned_modules_is_verified_with_the_whole_build(task):
    """A module the plan did not name would be built by no one: the verification would pass on
    code it never compiled. The root's own build file reaches every module."""
    change(task, "core/src/Calc.java")
    change(task, "pom.xml")
    assert proposal.verify_commands(task, SHOP) == WHOLE
    assert proposal.outside_modules(task, SHOP) == ["pom.xml"]


def test_a_project_without_the_command_is_verified_whole_and_a_plan_without_modules_by_its_changes(task):
    change(task, "core/src/Calc.java")
    assert proposal.verify_commands(task, Project("shop", Path("/r"), WHOLE)) == WHOLE, "not by module"
    (task.meta / gate.ACCEPTED_PLAN).write_text(PLAN.format(header=""))
    assert proposal.verify_commands(task, SHOP) == ["mvn -B -pl core -am verify"]


def test_a_module_the_work_reached_is_added_not_the_whole_build(task):
    """Task 2 changes A and B, and its plan named A: the verification builds both (ADR-0033,
    change of 2026-09-29), where it used to build the whole project."""
    change(task, "core/src/Calc.java")
    change(task, "extra/src/Tax.java")
    assert proposal.verify_commands(task, SHOP) == ["mvn -B -pl core,app,extra -am verify"]
    assert proposal.outside_modules(task, SHOP) == []
    assert proposal.scope(task, SHOP) == (["core", "app", "extra"], ["extra"])


def test_a_file_belongs_to_the_nearest_folder_with_a_build_file(task):
    write(task.repo / "extra" / "tax" / "pom.xml", "<project/>\n")
    change(task, "extra/tax/src/Rate.java")
    assert proposal.module_of(task.repo, "extra/tax/src/Rate.java") == "extra/tax"
    assert proposal.module_of(task.repo, "core/src/Calc.java") == "core"
    assert proposal.module_of(task.repo, "pom.xml") is None, "the root is every module's"
    assert proposal.module_of(task.repo, "gone/src/Old.java") is None
    for build_file in ("build.gradle", "build.gradle.kts", "package.json"):
        write(task.repo / "web" / build_file, "{}\n")
        assert proposal.module_of(task.repo, "web/src/app.ts") == "web", build_file
        (task.repo / "web" / build_file).unlink()


def test_a_file_in_no_module_builds_the_whole_project(task):
    change(task, "core/src/Calc.java")
    change(task, ".github/workflows/ci.yml")
    assert proposal.verify_commands(task, SHOP) == WHOLE
    assert proposal.outside_modules(task, SHOP) == [".github/workflows/ci.yml"]


def test_a_task_in_a_scoped_project_gets_the_modules_line_in_its_plan(env):
    from vivibox import actions

    demo = env / "config" / "projects" / "demo.toml"
    demo.write_text(demo.read_text().replace('verify = ["true"]', f'verify = ["{SCOPED}"]'))
    task = actions.create("demo", "Add subtract")
    header = task.plan_path.read_text().split("+++")[1]
    assert "modules = []" in header and SCOPED in header, "the planner fills it in, told what it is for"
    plain = env / "config" / "projects" / "demo.toml"
    plain.write_text(plain.read_text().replace(f'verify = ["{SCOPED}"]', 'verify = ["true"]'))
    assert "modules" not in actions.create("demo", "Add multiply").plan_path.read_text()


def test_the_modules_under_a_module_in_scope_are_built_with_it(task):
    """moshi: moshi-adapters/japicmp, the API check of moshi-adapters, is a project of its own, and
    a verification of moshi-adapters alone never ran it. A module nested under one in scope is in
    scope too (ADR-0033, change of 2026-09-29)."""
    write(task.repo / "pom.xml", "<project><modules><module>core</module><module>app</module>"
          "<module>extra</module></modules></project>")  # fmt: skip
    write(task.repo / "core" / "pom.xml", "<project><modules><module>api-check</module></modules></project>")
    write(task.repo / "core" / "api-check" / "pom.xml", "<project/>")
    git(task.repo, "add", ".")
    git(task.repo, "commit", "-q", "-m", "Modules in modules")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=task.repo, capture_output=True, text=True).stdout
    task.set_base_commit(base.strip())
    change(task, "core/src/Calc.java")
    assert proposal.scope(task, SHOP) == (["core", "app", "core/api-check"], [])
    assert proposal.nested(task, SHOP) == ["core/api-check"]
    assert proposal.verify_commands(task, SHOP) == ["mvn -B -pl core,app,core/api-check -am verify"]
