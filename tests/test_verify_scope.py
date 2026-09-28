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


def test_a_plan_without_modules_or_a_project_without_the_command_is_verified_whole(task):
    change(task, "core/src/Calc.java")
    assert proposal.verify_commands(task, Project("shop", Path("/r"), WHOLE)) == WHOLE, "not by module"
    (task.meta / gate.ACCEPTED_PLAN).write_text(PLAN.format(header=""))
    assert proposal.verify_commands(task, SHOP) == WHOLE


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
