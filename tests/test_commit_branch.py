"""Where accepted work is committed: the branch the task started on, or a new one named after it."""

import subprocess

import pytest
from conftest import make_repo

from vivibox import actions, gate, review
from vivibox.cli import main
from vivibox.config import load_config, load_project
from vivibox.states import State
from vivibox.task import find_task


def git(source, *args):
    return subprocess.run(["git", *args], cwd=source, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize(
    ("kind", "title", "name"),
    [
        ("feature", "Add a login form", "feature/add-a-login-form"),
        ("bug", "Fix: the total is off by one!", "bugfix/fix-the-total-is-off-by-one"),
        ("other", "Upgrade Spring Boot to 3.4", "upgrade-spring-boot-to-3-4"),
        ("feature", "Zażółć gęślą jaźń", "feature/zazoc-gesla-jazn"),
        ("feature", "!!!", "feature/demo-3"),
    ],
)
def test_the_new_branch_is_the_kind_and_the_title_in_kebab_case(tmp_path, kind, title, name):
    source = make_repo(tmp_path / "repo")
    assert review.new_branch(source, kind, title, "demo-3") == name


def test_a_long_title_is_cut_at_a_word(tmp_path):
    source = make_repo(tmp_path / "repo")
    name = review.new_branch(source, "other", "word " * 30, "demo-3")
    assert len(name) <= 50 and not name.endswith("-") and name.startswith("word-word")


def test_a_taken_name_gets_a_number(tmp_path):
    source = make_repo(tmp_path / "repo")
    git(source, "branch", "feature/add-login")
    assert review.new_branch(source, "feature", "Add login", "demo-1") == "feature/add-login-1"
    git(source, "branch", "feature/add-login-1")
    assert review.new_branch(source, "feature", "Add login", "demo-1") == "feature/add-login-2"


def test_the_task_remembers_the_branch_it_started_on(env):
    source = load_project("demo").repo
    git(source, "switch", "-q", "-c", "develop")
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    assert task.read_state().base_branch == "develop"
    assert main(["new", "demo", "Other goal", "--draft", "--base", "main"]) == 0
    task = find_task(load_config().tasks_dir, "demo-2")
    assert task.read_state().base_branch == "main", "a branch picked for the task, not the checkout's"


def accepted(env, goal="Add a login form"):
    """A task whose work was accepted into the checkout, not committed yet."""
    assert main(["new", "demo", goal, "--draft"]) == 0
    tasks = load_config().tasks_dir
    task = find_task(tasks, max(tasks.glob("demo-*"), key=lambda p: int(p.name[5:])).name)
    (task.repo / "one.txt").write_text("x\n")
    git(task.repo, "add", "one.txt")
    git(task.repo, "-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one")
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    task, project = actions.load(task.id)
    return actions.finish(task, project)


def test_accepting_offers_the_start_branch_and_a_new_one(env):
    done = accepted(env)
    assert done.start_branch == "main"
    assert done.new_branch == "feature/add-a-login-form"


def test_a_start_branch_deleted_since_is_not_offered(env):
    source = load_project("demo").repo
    git(source, "switch", "-q", "-c", "spike")
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    git(source, "switch", "-q", "main")
    git(source, "branch", "-D", "spike")
    task = find_task(load_config().tasks_dir, "demo-1")
    (task.repo / "one.txt").write_text("x\n")
    git(task.repo, "add", "one.txt")
    git(task.repo, "-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one")
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    done = actions.finish(*actions.load(task.id))
    assert done.start_branch == "main", "the checkout's branch, not one that is gone"


def test_a_commit_on_a_new_branch_leaves_the_checkout_on_it(env):
    source = load_project("demo").repo
    done = accepted(env)
    review.commit_work(source, "Add a login form", branch=done.new_branch, create=True)
    assert git(source, "branch", "--show-current").strip() == "feature/add-a-login-form"
    assert git(source, "log", "-1", "--format=%s").strip() == "Add a login form"
    assert git(source, "log", "-1", "--format=%s", "main").strip() == "Initial commit", "main untouched"


def test_a_commit_on_the_start_branch_goes_back_to_it(env):
    source = load_project("demo").repo
    git(source, "branch", "elsewhere")
    done = accepted(env)
    git(source, "switch", "-q", "elsewhere")  # the staged work comes along
    review.commit_work(source, "Add a login form", branch=done.start_branch)
    assert git(source, "branch", "--show-current").strip() == "main"
    assert git(source, "log", "-1", "--format=%s", "main").strip() == "Add a login form"
    assert git(source, "log", "-1", "--format=%s", "elsewhere").strip() == "Initial commit"
