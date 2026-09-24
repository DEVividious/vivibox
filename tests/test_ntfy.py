from vivibox import ntfy
from vivibox.states import State
from vivibox.task import TaskState


def channel(sent, level=ntfy.DECISIONS, token="", fail=False, said=None):
    def post(url, headers, body):
        if fail:
            raise OSError("connection refused")
        sent.append((url, headers, body.decode()))

    report = (said if said is not None else []).append
    return ntfy.Channel("https://ntfy.sh/t", level, token, post=post, report=report, spawn=lambda run: run())


def test_a_message_carries_the_kind_of_event_never_what_was_said():
    """A topic on ntfy.sh is read by anyone who knows its name: the question itself stays in the
    supervisor window and the view."""
    assert ntfy.terse("question from the agent: which port does the API use?") == "question from the agent"
    assert ntfy.terse("stopped on an error, see 'vivibox status demo-1': boom") == (
        "stopped on an error, see 'vivibox status demo-1'"
    )
    assert ntfy.terse("plan ready for review") == "plan ready for review"
    assert ntfy.terse("work ready for your review in /srv/vivibox/demo-1/demo; 2 tests removed") == (
        "work ready for your review"
    ), "a path names the project, and says nothing on a phone"
    assert (
        ntfy.terse("agent turn failed (502); trying again in 30 s")
        == "agent turn failed (502); trying again in 30 s"
    )


def test_a_decision_is_high_priority_and_a_retry_is_not():
    sent = []
    ch = channel(sent)
    ch.decision("demo-1", "plan ready for review", waiting=True)
    ch.decision("demo-1", "agent turn failed, task paused: 502", waiting=False, trouble=True)
    ch.decision("demo-1", "agent turn failed (502); trying again in 30 s", waiting=False)
    assert [s[2] for s in sent] == [
        "plan ready for review",
        "agent turn failed, task paused",
        "agent turn failed (502); trying again in 30 s",
    ]
    assert [(s[1]["Priority"], s[1].get("Tags", "")) for s in sent] == [
        ("high", "hourglass"),
        ("high", "warning"),
        ("default", ""),
    ]
    assert all(s[0] == "https://ntfy.sh/t" and s[1]["Title"] == "vivibox demo-1" for s in sent)
    assert not any("Authorization" in s[1] for s in sent), "no token, no header"


def test_a_token_goes_as_a_bearer_header():
    sent = []
    channel(sent, token="tk_secret").decision("demo-1", "plan ready for review", waiting=True)
    assert sent[0][1]["Authorization"] == "Bearer tk_secret"


def test_a_failure_to_send_is_said_and_stops_nothing():
    said = []
    channel([], fail=True, said=said).decision("demo-1", "plan ready for review", waiting=True)
    assert said == ["ntfy: not sent (connection refused)"]


def state(st: State, iteration: int = 1) -> TaskState:
    return TaskState(
        id="demo-1", project="demo", goal="g", state=st, iteration=iteration,
        paused=False, created="", updated="",
    )  # fmt: skip


def test_the_start_is_said_at_every_level_and_the_stages_at_level_all_when_they_change():
    """The word at the start is the first thing a topic gets after being set up, so it says the
    channel works; on a task that had turns before, it is started again."""
    sent = []
    stages = ntfy.Stages(channel(sent, level=ntfy.ALL))
    for st in (State.PLAN, State.PLAN, State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        stages.seen(state(st))
    stages.seen(state(State.IMPLEMENT, iteration=2))
    assert [s[2] for s in sent] == [
        "started: planning",
        "implementing",
        "verifying",
        "implementing, attempt 2",
    ]
    assert all(s[1]["Priority"] == "default" for s in sent), "a stage is news, not a decision"
    decisions = []
    stages = ntfy.Stages(channel(decisions), again=True)
    for st in (State.IMPLEMENT, State.VERIFY, State.IMPLEMENT):
        stages.seen(state(st, iteration=2))
    assert decisions[0][2] == "started again: implementing, attempt 2"
    assert len(decisions) == 1, "at the default level the stages after the start do not go"
    waiting = []
    ntfy.Stages(channel(waiting), again=True).seen(state(State.CHECKPOINT_PLAN))
    assert waiting[0][2] == "started again", "taken up at a checkpoint: nothing is being done yet"
