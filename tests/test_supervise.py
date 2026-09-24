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
