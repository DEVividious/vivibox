import subprocess

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


def test_completion_searches_the_projects_tree_by_a_piece_of_the_path(env, tmp_path):
    repo = env / "repo"
    orders = repo / "src" / "main" / "java" / "com" / "acme" / "orders"
    orders.mkdir(parents=True)
    (orders / "OrderService.java").write_text("")
    (orders / "OrderRepository.java").write_text("")
    (repo / "docs").mkdir()
    (repo / "docs" / "orders.md").write_text("")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Add"], cwd=repo, check=True)
    (orders / "OrderDto.java").write_text("")  # not committed yet: still yours to point at
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "OrderGen.java").write_text("")  # ignored: not part of the project
    (repo / ".github").mkdir()
    (repo / ".github" / "order.yml").write_text("")
    service = "src/main/java/com/acme/orders/OrderService.java"
    assert context.complete("OrderSer", tmp_path, repo=repo) == [service]
    assert context.complete("orders/OrderSer", tmp_path, repo=repo) == [service], "a piece of the path"
    assert context.complete("order", tmp_path, repo=repo) == [
        "docs/orders.md",
        "src/main/java/com/acme/orders/",
        "src/main/java/com/acme/orders/OrderDto.java",
        "src/main/java/com/acme/orders/OrderRepository.java",
        service,
    ], "names that start with it first, the shortest path first, and no ignored files"
    assert context.complete("java/com", tmp_path, repo=repo)[0] == "src/main/java/com/"
    assert context.complete("./Order", tmp_path, repo=repo) == [], "an explicit path is where it says"
    assert context.complete(".github/or", tmp_path, repo=repo) == [".github/order.yml"]
    assert "src/main/java/com/acme/orders/" in context.complete("orders", tmp_path, repo=repo)


def test_completion_without_a_repository_only_completes_folders(tmp_path):
    (tmp_path / "plain" / "src").mkdir(parents=True)
    (tmp_path / "plain" / "src" / "Main.java").write_text("")
    assert context.complete("Main", tmp_path, repo=tmp_path / "plain") == []
    assert context.complete("Main", tmp_path, repo=tmp_path / "gone") == []


def test_task_numbers_skip_branches_in_your_repository(env):
    import subprocess

    from vivibox.config import load_project

    source = load_project("demo").repo
    subprocess.run(["git", "branch", "vivibox/demo-4"], cwd=source, check=True)
    assert actions.create("demo", "Next").id == "demo-5"


def in_repo(env, path: str, text: str = "x\n", commit: bool = True):
    import subprocess

    repo = env / "repo"
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    if commit:
        subprocess.run(["git", "add", path], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", f"Add {path}"], cwd=repo, check=True)
    return file


def test_a_file_of_the_project_points_at_the_agents_clone_and_is_not_copied(env, tmp_path):
    in_repo(env, "src/Order.java")
    task = actions.create("demo", "Round VAT in @src/Order.java per line", cwd=tmp_path)
    plan = task.plan_path.read_text()
    assert f"{task.repo}/src/Order.java" in plan and "/task/context" not in plan
    assert not (task.meta / "context").exists() or not list((task.meta / "context").iterdir())
    assert (task.repo / "src" / "Order.java").exists(), "the same file, in the clone the agent edits"


def test_an_absolute_path_into_the_project_points_at_the_clone_too(env, tmp_path):
    in_repo(env, "README.md", "readme\n")
    task = actions.create("demo", f"See @{env / 'repo' / 'README.md'}", cwd=tmp_path)
    assert f"{task.repo}/README.md" in task.plan_path.read_text()


def test_a_changed_file_of_the_project_is_noted_because_the_agent_sees_the_commit(env, tmp_path):
    in_repo(env, "src/Order.java")
    in_repo(env, "src/Order.java", "changed\n", commit=False)
    task = actions.create("demo", "Fix @src/Order.java", cwd=tmp_path)
    notes = [e["data"]["notes"] for e in task.events() if e["type"] == "context"]
    assert notes == [["src/Order.java has uncommitted changes; the agent sees the committed version"]]
    assert (task.repo / "src" / "Order.java").read_text() == "x\n"


def test_an_untracked_file_of_the_project_is_copied_since_the_clone_lacks_it(env, tmp_path):
    in_repo(env, "notes/idea.md", "idea\n", commit=False)
    task = actions.create("demo", "Do @notes/idea.md", cwd=tmp_path)
    assert "/task/context/idea.md" in task.plan_path.read_text()
    notes = [e["data"]["notes"] for e in task.events() if e["type"] == "context"]
    assert notes == [["notes/idea.md is not committed, so the agent gets a copy under /task/context"]]


def test_completion_offers_the_projects_files_as_well_as_where_you_are(env, tmp_path):
    (env / "repo" / "src").mkdir()
    (tmp_path / "tickets").mkdir()
    found = context.complete("", tmp_path, repo=env / "repo")
    assert found[:1] == ["~/"] and "src/" in found and "tickets/" in found
    assert context.complete("sr", tmp_path, repo=env / "repo") == ["src/"]


def test_a_relative_mention_is_the_projects_file_before_the_shells(env, tmp_path):
    """@README.md typed from another folder meant that folder's README; the description is about
    the project, so its file wins. ./ and ~/ stay explicit, from where you are."""
    in_repo(env, "README.md", "# the project\n")
    (tmp_path / "README.md").write_text("# somewhere else\n")
    task = actions.create("demo", "Update @README.md and @./README.md", cwd=tmp_path)
    plan = task.plan_path.read_text()
    assert f"{task.repo}/README.md" in plan, "the project's, by its path in the clone"
    assert (task.meta / "context" / "README.md").read_text() == "# somewhere else\n", "./ is explicit"


def test_a_project_named_vivibox_is_not_mistaken_for_the_key_store(env, tmp_path):
    """Any path with a folder called vivibox was refused, to keep ~/.local/share/vivibox and
    ~/.config/vivibox from the agent; a project of that name, this one, could attach nothing."""
    notes = tmp_path / "vivibox" / "docs" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("notes\n")
    task = actions.create("demo", f"See @{notes}", cwd=tmp_path)
    assert (task.meta / "context" / "notes.md").exists()
    for kept in (".local/share/vivibox/keys/deepseek", ".config/vivibox/config.toml"):
        secret = tmp_path / kept
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("x")
        with pytest.raises(context.ContextError, match="credential"):
            actions.create("demo", f"See @{secret}", cwd=tmp_path)
