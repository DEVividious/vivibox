"""A project's preparation: its commands run once in a task's clone, while the plan is made, and
the writer's first turn waits for them."""

from pathlib import Path

import pytest

from vivibox import prepare
from vivibox.config import Project
from vivibox.task import create_task

INSTALL = "bash mvnw -B install -DskipTests -f pom.xml"


class FakePod:
    def __init__(self, task):
        self.task, self.started, self.running = task, [], False

    def prepare_start(self, commands, log, exit_file):
        self.started.append((list(commands), log, exit_file))
        self.running = True

    def prepare_running(self):
        return self.running

    def finish(self, code):
        """What the commands leave behind when they end, in the task's handoff."""
        (self.task.meta / "handoff" / prepare.EXIT).write_text(f"{code}\n")
        self.running = False


@pytest.fixture
def task(tmp_path):
    return create_task(tmp_path, "shop", "Add health endpoint", "")


def project(*commands):
    return Project("shop", Path("/r"), ["true"], prepare=list(commands))


def kinds(task):
    return [e["type"] for e in task.events() if e["type"].startswith("prepare")]


def test_a_project_without_preparation_starts_nothing_and_waits_for_nothing(task):
    pod = FakePod(task)
    assert not prepare.begin(task, project(), pod)
    assert pod.started == [] and not prepare.waiting(task, project(), pod)


def test_the_commands_start_once_with_their_output_where_the_agent_and_you_read_it(task):
    pod = FakePod(task)
    assert prepare.begin(task, project(INSTALL), pod)
    assert pod.started == [([INSTALL], "/task/handoff/prepare.log", "/task/handoff/prepare.exit")]
    assert not prepare.begin(task, project(INSTALL), pod), "running already"
    assert prepare.waiting(task, project(INSTALL), pod)
    pod.finish(0)
    assert not prepare.waiting(task, project(INSTALL), pod)
    assert not prepare.begin(task, project(INSTALL), pod), "a stop and a start do not run it again"
    assert len(pod.started) == 1
    assert kinds(task) == ["prepare_started", "prepared"]
    assert task.events()[-1]["data"] == {"ok": True, "code": 0}


def test_a_failed_preparation_is_said_once_and_the_writer_goes_on(task):
    pod = FakePod(task)
    prepare.begin(task, project(INSTALL), pod)
    pod.finish(1)
    assert not prepare.waiting(task, project(INSTALL), pod)
    assert not prepare.waiting(task, project(INSTALL), pod)
    assert kinds(task) == ["prepare_started", "prepared"]
    assert task.events()[-1]["data"] == {"ok": False, "code": 1}
    assert prepare.status(task, pod) == "failed"
    assert not prepare.begin(task, project(INSTALL), pod), "not again by itself"


def test_a_preparation_the_stop_cut_short_starts_again(task):
    """A stop ends the agent's container and the commands in it before they wrote how they ended."""
    pod = FakePod(task)
    prepare.begin(task, project(INSTALL), pod)
    pod.running = False
    assert prepare.status(task, pod) == ""
    assert prepare.begin(task, project(INSTALL), pod)
    assert len(pod.started) == 2


def test_l_offers_the_preparations_output(task):
    from vivibox import logs

    pod = FakePod(task)
    prepare.begin(task, project(INSTALL), pod)
    (task.meta / "handoff" / prepare.LOG).write_text("[INFO] BUILD FAILURE\n")
    pod.finish(1)
    found, _ = logs.entries(task, task.read_state(), running=False)
    entry = next(e for e in found if e.label == "prepare.log")
    assert entry.said == "the project's preparation · failed, exit 1 · 1 lines"
    assert str(task.meta / "handoff" / prepare.LOG) in entry.command


@pytest.mark.real_start
def test_a_task_and_a_box_start_the_preparation_once_their_pod_is_up(env, monkeypatch, tmp_path):
    from vivibox import actions, box, image, opencode, secrets, toolchain
    from vivibox.cli import main
    from vivibox.pod import Pod, PodError

    project_file = env / "config" / "projects" / "demo.toml"
    project_file.write_text(project_file.read_text() + f'prepare = ["{INSTALL}"]\n')
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(image, "env", lambda ref: {})
    monkeypatch.setattr(secrets, "prepare", lambda task_id, keys: None)
    monkeypatch.setattr(opencode, "prepare", lambda task, model, verify, used: False)
    monkeypatch.setattr(opencode, "provider_of", lambda model: "p")
    order = []
    monkeypatch.setattr(Pod, "up", lambda self: order.append("up"))
    monkeypatch.setattr(toolchain, "ensure", lambda pod, java: order.append("java"))

    def began(task, project, pod):
        order.append(("prepare", project.prepare))
        raise PodError("enough")  # what follows needs opencode running

    monkeypatch.setattr(prepare, "begin", began)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    with pytest.raises(PodError, match="enough"):
        actions.start("demo-1")
    assert order == ["up", "java", ("prepare", [INSTALL])], "in the pod, with the project's JDK"
    order.clear()
    monkeypatch.setattr(box, "box_providers", lambda task: [])
    with pytest.raises(PodError, match="enough"):
        box.start_box("demo-1")
    assert order == ["up", "java", ("prepare", [INSTALL])]
