"""A box: a project's pod with no task and no agent, for you to work in by hand. Its work comes
back the way accepted work does."""

import subprocess

import pytest

from vivibox import actions, ui
from vivibox.cli import main
from vivibox.config import load_config, load_project
from vivibox.states import State
from vivibox.task import find_task


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    """Opening a box means a pod; tests get a stand-in that records the call."""
    started: list[str] = []
    monkeypatch.setattr(actions, "boxes_started", started, raising=False)
    monkeypatch.setattr(actions, "start_box", lambda task_id: started.append(task_id))
    # Closing commits inside the pod; here on the host, in the clone, which the pod would do.
    monkeypatch.setattr(actions, "commit_in_box", lambda task: commit_here(task))


def commit_here(task):
    run = lambda *a: subprocess.run(["git", *a], cwd=task.repo, capture_output=True, check=False)  # noqa: E731
    run("add", "-A")
    run("-c", "user.name=You", "-c", "user.email=y@x", "commit", "-qm", "Work in the box")


def seen(task):
    return ui.view(task, task.read_state(), False, 3)


def test_a_box_is_a_task_without_a_plan_or_an_agent(env):
    task = actions.open_box("demo")
    st = task.read_state()
    assert st.box and st.state is State.IMPLEMENT and not st.paused
    assert actions.boxes_started == [task.id]
    assert seen(task).status == "box open" and seen(task).group == "Working"
    assert not (task.meta / "plan.accepted.md").exists()
    assert (task.repo / "README.md").exists(), "a clone of the project, like a task's"
    assert seen(task).commands == (f"vivibox attach {task.id}", f"vivibox accept {task.id}")


def test_a_stopped_box_is_closed_and_a_started_one_open(env):
    task = actions.open_box("demo")
    task.set_paused(True)
    assert seen(task).status == "box closed" and seen(task).group == "Stopped"
    assert seen(task).commands == (f"vivibox start {task.id}",)


def test_closing_a_box_brings_its_work_to_review(env):
    task = actions.open_box("demo")
    (task.repo / "idea.md").write_text("my work\n")
    where = actions.close_box(task, load_project("demo"))
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert where and where.exists(), "the review copy, as for a task"
    assert seen(task).status == "review the work"
    subjects = subprocess.run(
        ["git", "log", "--format=%s", "-1"], cwd=task.repo, capture_output=True, text=True
    )
    assert subjects.stdout.strip() == "Work in the box", "what was not committed is, so nothing is lost"


def test_closing_a_box_with_a_risky_change_waits_for_your_approval(env):
    task = actions.open_box("demo")
    (task.repo / "pom.xml").write_text("<project/>\n")
    assert actions.close_box(task, load_project("demo")) is None
    assert task.read_state().state is State.APPROVAL_RISKY
    count, review = actions.approve_risky(task, load_project("demo"))
    assert count == 1 and review is not None and task.read_state().state is State.CHECKPOINT_FINAL


def test_a_closed_box_is_accepted_like_a_task(env, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    task = actions.open_box("demo")
    (task.repo / "idea.md").write_text("my work\n")
    actions.close_box(task, load_project("demo"))
    assert main(["accept", task.id]) == 0
    source = load_project("demo").repo
    assert (source / "idea.md").read_text() == "my work\n" and not task.root.exists()
    assert actions.history()[0]["title"] == "Box in demo"
    assert "Commit it when you like, e.g.: git -C" in capsys.readouterr().out
    assert '-m "Work in the box"' not in capsys.readouterr().out, "the message is yours to write"


def test_a_box_is_described_without_a_plan_or_criteria(env, capsys):
    task = actions.open_box("demo")
    assert main(["status", task.id]) == 0
    out = capsys.readouterr().out
    assert "criteria" not in out and "Plan " not in out
    (task.repo / "idea.md").write_text("x\n")
    actions.close_box(task, load_project("demo"))
    assert main(["status", task.id]) == 0
    out = capsys.readouterr().out
    assert f"vivibox accept {task.id}" in out and "reply" not in out, "nobody in a box to reply to"


def test_accepting_a_box_offers_no_commit_message_to_keep(env, monkeypatch):
    task = actions.open_box("demo")
    (task.repo / "idea.md").write_text("my work\n")
    actions.close_box(task, load_project("demo"))
    done = actions.finish(task, load_project("demo"))
    assert done.message == "", "'Work in the box' is not a message for your history"


def test_a_box_without_changes_closes_into_nothing_to_review(env):
    task = actions.open_box("demo")
    with pytest.raises(actions.gate.GateError, match="nothing changed"):
        actions.close_box(task, load_project("demo"))
    assert task.read_state().state is State.IMPLEMENT


def test_vivibox_box_opens_one_from_the_shell(env, capsys):
    assert main(["box", "demo"]) == 0
    out = capsys.readouterr().out
    task = find_task(load_config().tasks_dir, "demo-1")
    assert task.read_state().box and actions.boxes_started == ["demo-1"]
    assert f"vivibox attach {task.id}" in out and "Ctrl-q" in out


def test_vivibox_box_new_starts_a_project_and_a_box_in_it(env, tmp_path, capsys):
    where = tmp_path / "idea"
    assert main(["box", "--new", str(where)]) == 0
    assert (where / ".git").exists() and "idea" in actions.projects_named()
    task = find_task(load_config().tasks_dir, "idea-1")
    assert task.read_state().box and task.read_state().project == "idea"


def test_accept_on_an_open_box_closes_it(env, capsys):
    task = actions.open_box("demo")
    (task.repo / "idea.md").write_text("my work\n")
    assert main(["accept", task.id]) == 0
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert "review" in capsys.readouterr().out


def test_a_box_gets_every_provider_you_have_a_key_for(env, monkeypatch):
    from vivibox import keys, providers

    keys.set_key("deepseek", "k1")
    keys.set_key("anthropic", "k2")
    providers.set_enabled(providers.PROVIDER, "anthropic", False)
    task = actions.open_box("demo")
    assert actions.box_providers(task) == ["deepseek"], "what is on; the switched-off one stays out"
