from pathlib import Path

from vivibox import ntfy, supervise
from vivibox.config import Config, Role
from vivibox.states import State
from vivibox.task import create_task

TEMPLATE = "+++\n+++\n\n## Acceptance criteria\n\n- [ ] x\n"


def config(**over) -> Config:
    roles = {"planner": Role("opencode", "m"), "writer": Role("opencode", "m")}
    return Config(Path("/t"), 3, roles, **over)


def test_no_topic_no_channel_and_the_token_comes_from_the_key_store():
    assert supervise.channel_for(config()) is None
    ch = supervise.channel_for(config(ntfy="t"), get_key=lambda n: "tk", stored=lambda: {})
    assert ch is not None and ch.url == "https://ntfy.sh/t" and ch.token == "" and ch.level == "decisions"
    ch = supervise.channel_for(
        config(ntfy="t", ntfy_server="https://ntfy.example", ntfy_events="all"),
        get_key=lambda n: "tk",
        stored=lambda: {"ntfy": "x"},
    )
    assert ch.url == "https://ntfy.example/t" and ch.token == "tk" and ch.level == "all"


def test_the_supervisors_messages_reach_the_topic_at_the_tasks_own_priority(tmp_path, monkeypatch):
    """The same message as the window and the desktop get; high priority once the task waits for
    you or has stopped, which the supervisor has recorded before it speaks."""
    task = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)
    desktop, sent = [], []
    monkeypatch.setattr(supervise.supervisor, "notify", lambda *a: desktop.append(a[1]))
    monkeypatch.setattr(supervise.actions, "buttons", lambda *a: [])
    channel = ntfy.Channel(
        "https://ntfy.sh/t", post=lambda u, h, b: sent.append((h, b.decode())), spawn=lambda r: r()
    )
    monkeypatch.setattr(supervise, "channel_for", lambda config: channel if config.ntfy else None)
    notify = supervise.notifier(task, None, lambda: config(ntfy="t"))
    notify(task.id, "agent turn failed (502); trying again in 30 s")
    task.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
    notify(task.id, "question from the agent: which port?", kind="plan")
    task.set_paused(True, problem="stopped on an error: boom")
    notify(task.id, "stopped on an error: boom")
    assert desktop == [
        "agent turn failed (502); trying again in 30 s",
        "question from the agent: which port?",
        "stopped on an error: boom",
    ], "the desktop keeps the whole message"
    assert [(h["Priority"], h.get("Tags", ""), body) for h, body in sent] == [
        ("default", "", "agent turn failed (502); trying again in 30 s"),
        ("high", "hourglass", "question from the agent"),
        ("high", "warning", "stopped on an error"),
    ]
    quiet = supervise.notifier(task, None, lambda: config())
    quiet(task.id, "plan ready for review")
    assert len(sent) == 3, "no channel, nothing sent"


def test_a_topic_set_while_the_task_runs_gets_the_next_message(tmp_path, monkeypatch):
    """The settings are read at every message, not once at the start: a topic set under k while
    a task runs gets the next decision, and a file that cannot be read keeps the last settings."""
    task = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)
    sent = []
    monkeypatch.setattr(supervise.supervisor, "notify", lambda *a: None)
    monkeypatch.setattr(supervise.actions, "buttons", lambda *a: [])
    channel = ntfy.Channel(
        "https://ntfy.sh/t", post=lambda u, h, b: sent.append(b.decode()), spawn=lambda r: r()
    )
    monkeypatch.setattr(supervise, "channel_for", lambda config: channel if config.ntfy else None)
    now = [config()]
    notify = supervise.notifier(task, None, lambda: now[0])
    notify(task.id, "plan ready for review")
    now[0] = config(ntfy="t")
    notify(task.id, "work ready for your review")
    assert sent == ["work ready for your review"]
    loads = [Exception("broken")]
    start = config(ntfy="t")

    def load_config():
        if loads:
            raise supervise.ConfigError(str(loads.pop()))
        return config()

    monkeypatch.setattr(supervise, "load_config", load_config)
    current = supervise.live_config(start)
    assert current() is start, "unreadable for a moment: the settings the supervisor started with"
    assert current().ntfy == "", "readable again: what the file says now"


def test_the_supervisor_asks_the_pod_whether_the_projects_preparation_still_runs(tmp_path, monkeypatch):
    from vivibox.config import Project

    task = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())

    class Pod:
        running = True

        def prepare_running(self):
            return self.running

        def review_down(self):
            pass

        def review_side(self):
            return self

    project = Project("demo", tmp_path, ["true"], prepare=["mvn -B install"])
    sup = supervise.make_supervisor(task, project, Pod(), config())
    assert sup.prepared == ["mvn -B install"]
    assert sup.ports.preparing(), "the writer waits while it runs"


def test_what_you_change_under_e_reaches_a_running_task_without_a_restart(env, monkeypatch):
    """The supervisor read the project file once, at its start: a verify command or a
    preparation set under e waited for a stop and a start."""
    from vivibox import gate
    from vivibox.cli import main
    from vivibox.config import load_project
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(supervise.load_config().tasks_dir, "demo-1")
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())
    ran = []
    missing = []
    monkeypatch.setattr(
        gate,
        "run_gate",
        lambda t, pod, commands, *a, **k: ran.append(commands) or missing.append(k["no_command"]),
    )

    class Pod:
        def prepare_running(self):
            return True

        def review_down(self):
            pass

        def review_side(self):
            return self

    sup = supervise.make_supervisor(task, load_project("demo"), Pod(), config())
    assert not sup.ports.preparing(), "nothing to prepare yet"
    path = env / "config" / "projects" / "demo.toml"
    path.write_text(
        path.read_text().replace('verify = ["true"]', 'verify = ["npm test"]') + 'prepare = ["npm ci"]\n'
    )
    assert sup.ports.preparing(), "the preparation set since"
    sup.ports.run_gate(task)
    assert ran == [["npm test"]] and missing == [""]
    path.write_text(path.read_text().replace('verify = ["npm test"]', "verify = []"))
    sup.ports.run_gate(task)
    assert ran[-1] == [] and "proposed no command" in missing[-1], "the writer's, and it wrote none"
    path.write_text(path.read_text().replace("verify = []", 'verify = ["npm test"]'))
    task.set_paused(True)  # a step with nothing to do still reads the file
    assert not sup.step()
    assert sup.project_verify == ["npm test"] and sup.prepared == ["npm ci"]
    # Every change the running task read is on its record, once each, with what it changed to.
    changed = [e["data"] for e in task.events() if e["type"] == "settings_changed"]
    assert changed == [
        {"verify": ["npm test"], "prepare": ["npm ci"]},
        {"verify": []},
        {"verify": ["npm test"]},
    ], changed


def test_the_writer_is_told_the_projects_own_verification_limit(env, monkeypatch):
    """The limit the gate runs with, config.toml's or the project's own, is the one the writer
    is told for its build: the same number, from the same place."""
    from vivibox import actions
    from vivibox.config import load_project

    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())

    class Pod:
        def review_side(self):
            return None

        def review_down(self):
            pass

        def prepare_running(self):
            return False

    task = actions.create("demo", "Goal", cwd=env)
    assert supervise.make_supervisor(task, load_project("demo"), Pod(), config()).verify_timeout == 1800
    project = env / "config" / "projects" / "demo.toml"
    project.write_text(project.read_text() + "verify_timeout = 600\n")
    assert supervise.make_supervisor(task, load_project("demo"), Pod(), config()).verify_timeout == 600


def test_the_agents_window_opens_on_w_and_not_at_every_turn(env, monkeypatch):
    """The supervisor used to open a tmux window with `opencode attach` at every turn, for w to
    have something to show; each such window rendered opencode's interface for nobody, at a third
    of a core. Now w opens it, and leaving it closes it."""
    from vivibox.cli import main
    from vivibox.config import load_project
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(supervise.load_config().tasks_dir, "demo-1")
    task.set_session("writer", "ses_1")
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())
    opened = []
    monkeypatch.setattr(supervise.actions, "agent_view", lambda t, command: opened.append(command))

    class Pod:
        def prepare_running(self):
            return False

        def review_down(self):
            pass

        def review_side(self):
            return self

    sup = supervise.make_supervisor(task, load_project("demo"), Pod(), config())
    sup.ports.session_started(task.read_state())
    assert opened == [], "nothing opened for nobody"
    assert not hasattr(supervise, "agent_window")


def test_work_past_the_planned_modules_is_verified_whole_and_the_timeline_says_why(env, monkeypatch):
    import subprocess

    from vivibox import gate
    from vivibox.cli import main
    from vivibox.config import load_project
    from vivibox.task import find_task

    path = env / "config" / "projects" / "demo.toml"
    path.write_text(
        path.read_text().replace('verify = ["true"]', 'verify = ["mvn -pl {modules} -am verify"]')
    )
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(supervise.load_config().tasks_dir, "demo-1")
    (task.meta / gate.ACCEPTED_PLAN).write_text('+++\nmodules = ["core"]\n+++\n')
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())
    ran = []
    monkeypatch.setattr(gate, "run_gate", lambda t, pod, commands, *a, **k: ran.append(commands))

    class Pod:
        def review_down(self):
            pass

        def review_side(self):
            return self

    sup = supervise.make_supervisor(task, load_project("demo"), Pod(), config())
    git = lambda *a: subprocess.run(["git", *a], cwd=task.repo, check=True, capture_output=True)  # noqa: E731
    (task.repo / "core").mkdir()
    (task.repo / "core" / "Calc.java").write_text("class Calc {}\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add Calc")
    sup.ports.run_gate(task)
    assert ran[-1] == ["mvn -pl core -am verify"]
    assert not [e for e in task.events() if e["type"] == "verify_widened"]
    assert [e["data"] for e in task.events() if e["type"] == "verify_scope"] == [
        {"modules": ["core"], "added": []}
    ]
    (task.repo / "pom.xml").write_text("<project/>\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add the root build")
    sup.ports.run_gate(task)
    assert ran[-1] == ["mvn verify"], "the same command, without the modules"
    assert [e["data"]["outside"] for e in task.events() if e["type"] == "verify_widened"] == [["pom.xml"]]


def test_the_whole_build_before_review_runs_only_when_asked_and_only_by_module(env, monkeypatch):
    import subprocess

    from vivibox import gate
    from vivibox.cli import main
    from vivibox.config import load_project
    from vivibox.task import find_task

    path = env / "config" / "projects" / "demo.toml"
    path.write_text(
        path.read_text().replace('verify = ["true"]', 'verify = ["mvn -pl {modules} -am verify"]')
    )
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(supervise.load_config().tasks_dir, "demo-1")
    (task.meta / gate.ACCEPTED_PLAN).write_text('+++\nmodules = ["core"]\n+++\n')
    monkeypatch.setattr(supervise.actions, "harness_for", lambda role, side, task: object())
    ran = []
    monkeypatch.setattr(gate, "run_gate", lambda t, pod, commands, *a, **k: ran.append(commands) or "result")

    class Pod:
        def review_down(self):
            pass

        def review_side(self):
            return self

    git = lambda *a: subprocess.run(["git", *a], cwd=task.repo, check=True, capture_output=True)  # noqa: E731
    (task.repo / "core").mkdir()
    (task.repo / "core" / "Calc.java").write_text("class Calc {}\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add Calc")
    sup = supervise.make_supervisor(task, load_project("demo"), Pod(), config())
    assert sup.ports.run_whole_gate(task) is None, "off unless you turn it on"
    sup = supervise.make_supervisor(task, load_project("demo"), Pod(), config(whole_build_before_review=True))
    assert sup.ports.run_whole_gate(task) == "result" and ran == [["mvn verify"]]
    assert [e for e in task.events() if e["type"] == "whole_build"]
    (task.repo / "pom.xml").write_text("<project/>\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add the root build")
    assert sup.ports.run_whole_gate(task) is None, "its verification was whole already"
