import pytest

from vivibox import actions, context
from vivibox.config import load_config
from vivibox.plan import parse_plan
from vivibox.task import find_task


def test_mentions_are_paths_not_email_addresses():
    text = "See @~/tickets/PAY-1.md, and @./notes/ (ask a@b.com or @john)\n@/tmp/x.png."
    assert context.mentions(text) == ["~/tickets/PAY-1.md", "./notes/", "/tmp/x.png"]


def test_people_mentioned_in_a_ticket_are_left_alone(env, tmp_path):
    task = actions.create("demo", "Fix it, as @john.doe asked in notes/x.md", cwd=tmp_path)
    assert "@john.doe" in task.plan_path.read_text()


def test_mentioned_files_are_copied_into_the_task(env, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "PAY-1.md").write_text("Details of the ticket\n")
    (notes / "shot.png").write_bytes(b"\x89PNG")
    task = actions.create("demo", "PAY-1: fix it\nSee @notes/PAY-1.md and @./notes", cwd=tmp_path)
    plan = task.plan_path.read_text()
    assert "/task/context/PAY-1.md" in plan and "/task/context/notes" in plan and "@notes" not in plan
    assert (task.meta / "context" / "PAY-1.md").read_text() == "Details of the ticket\n"
    assert (task.meta / "context" / "notes" / "shot.png").exists()


@pytest.mark.parametrize("name", [".env", "id_rsa", "server.pem", "settings.xml"])
def test_credentials_are_refused_and_nothing_is_created(env, tmp_path, name):
    (tmp_path / name).write_text("secret")
    with pytest.raises(context.ContextError, match="credential"):
        actions.create("demo", f"Fix it with @{tmp_path / name}", cwd=tmp_path)
    assert not any(load_config().tasks_dir.glob("demo-*"))


def test_missing_file(env, tmp_path):
    with pytest.raises(context.ContextError, match="no such file"):
        actions.create("demo", "Fix @./missing.md", cwd=tmp_path)


def test_bug_tasks_start_from_reproducing_it(env, tmp_path):
    task = actions.create("demo", "Cards expire a day early", kind="bug", cwd=tmp_path)
    text = task.plan_path.read_text()
    assert "## Reproduction" in text and "## Root cause" in text
    assert parse_plan(text).kind == "bug"
    assert find_task(load_config().tasks_dir, task.id).read_state().goal == "Cards expire a day early"


def test_completion_lists_matching_paths(tmp_path):
    (tmp_path / "src" / "main").mkdir(parents=True)
    (tmp_path / "src" / "Main.java").write_text("")
    (tmp_path / ".env").write_text("")
    assert context.complete("src/ma", tmp_path) == ["src/main/", "src/Main.java"]
    assert context.complete("", tmp_path) == ["~/", "src/"], "no hidden files until you type the dot"
    assert context.complete(".e", tmp_path) == [".env"]
    assert context.complete("nothing/here", tmp_path) == []


def test_task_numbers_skip_branches_in_your_repository(env):
    import subprocess

    from vivibox.config import load_project

    source = load_project("demo").repo
    subprocess.run(["git", "branch", "vivibox/demo-4"], cwd=source, check=True)
    assert actions.create("demo", "Next").id == "demo-5"
