"""What an agent's CLI session spent on a task, read from the counters in its transcript at the
task's events (ADR-0036): planning up to the plan's import, each review round from its request to
its import, the rest of the conversation up to the end, and nothing counted twice."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vivibox import actions, cli_usage, gate, pricing, reviewing, ui
from vivibox.config import load_project
from vivibox.pricing import Tokens
from vivibox.states import State

FIXTURE = Path(__file__).parent / "fixtures" / "claude-session.jsonl"
SESSION = "fe9b8924-be65-48c7-aa7e-15b966473c2d"


def reply(at: str, id_: str, model: str = "claude-opus-5-5", **usage) -> str:
    usage = {"input_tokens": 0, "output_tokens": 0, **usage}
    return json.dumps(
        {"type": "assistant", "timestamp": at, "message": {"id": id_, "model": model, "usage": usage}}
    )


def at(minute: int) -> str:
    return (datetime(2026, 9, 29, 10, 0, tzinfo=UTC) + timedelta(minutes=minute)).isoformat()


@pytest.fixture
def planned(env):
    """A task planned in Claude Code, its transcript at the path the task keeps."""
    transcript = env / "home" / ".claude" / "projects" / "-w" / f"{SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    task = actions.create("demo", "Add health endpoint", plan_in_cli=True)
    task.set_cli_session({"tool": "claude-code", "id": SESSION, "path": str(transcript)})
    return task, transcript


def usage_events(task):
    return [e["data"] for e in task.events() if e["type"] == "cli_usage"]


def test_a_reply_written_over_several_lines_is_counted_once():
    replies = cli_usage.claude_replies(FIXTURE)
    assert len(replies) == 6, "11 lines, 6 replies"
    assert {r.model for r in replies} == {"claude-opus-5-5"}
    assert sum(r.tokens.output for r in replies) == 258 + 476 + 234 + 283 + 111 + 1096
    # Its time is its first line's.
    assert replies[0].at == datetime(2026, 9, 28, 22, 34, 49, 637000, tzinfo=UTC)


def test_the_cache_writes_are_split_by_how_long_they_last(tmp_path):
    path = tmp_path / "s.jsonl"
    split = {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 90}
    path.write_text(
        "\n".join(
            [
                reply(
                    at(1),
                    "a",
                    cache_creation_input_tokens=100,
                    cache_creation=split,
                    cache_read_input_tokens=7,
                ),
                # An older CLI names no split: the 5-minute cache, the default.
                reply(at(2), "b", cache_creation_input_tokens=30),
                reply(at(3), "c", model="claude-opus-5-5", output_tokens=5, speed="fast", inference_geo="us"),
                "not json",
                json.dumps({"type": "user", "timestamp": at(4)}),
            ]
        )
    )
    a, b, c = cli_usage.claude_replies(path)
    assert a.tokens == Tokens(cache_read=7, cache_write_5m=10, cache_write_1h=90)
    assert b.tokens == Tokens(cache_write_5m=30)
    assert c.fast and c.us and not a.fast and not a.us


def test_a_subagent_s_replies_are_the_session_s_too(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(reply(at(1), "a", output_tokens=1))
    (tmp_path / "s" / "subagents").mkdir(parents=True)
    (tmp_path / "s" / "subagents" / "agent-1.jsonl").write_text(
        reply(at(2), "b", "claude-haiku-4-5", output_tokens=2)
    )
    assert [r.model for r in cli_usage.claude_replies(path)] == ["claude-opus-5-5", "claude-haiku-4-5"]


def test_planning_is_counted_at_the_import_and_priced_by_model(planned, monkeypatch):
    task, transcript = planned
    transcript.write_text(
        "\n".join(
            [
                reply(at(1), "a", output_tokens=1000, input_tokens=10),
                reply(at(2), "b", "claude-haiku-4-5", output_tokens=100),
                reply(at(2), "b", "claude-haiku-4-5", output_tokens=100),
                reply(at(3), "c", "some-new-model", output_tokens=5),
            ]
        )
    )
    cli_usage.count(task, "planning", until=datetime.fromisoformat(at(10)))
    found = {e["model"]: e for e in usage_events(task)}
    assert set(found) == {"claude-opus-5-5", "claude-haiku-4-5", "some-new-model"}
    opus = found["claude-opus-5-5"]
    assert opus["stage"] == "planning" and opus["tokens"]["output"] == 1000 and opus["replies"] == 1
    assert opus["cost"] == pytest.approx(pricing.price("claude-opus-5-5", Tokens(input=10, output=1000)))
    assert found["claude-haiku-4-5"]["tokens"]["output"] == 100
    assert found["some-new-model"]["cost"] is None
    spent = ui.cost(task)
    assert spent.subscription_unknown and spent.total == 0.0
    assert spent.subscription["planning"] == pytest.approx(opus["cost"] + found["claude-haiku-4-5"]["cost"])


def test_a_review_round_is_its_own_window_and_the_talk_before_it_conversation(planned):
    task, transcript = planned
    lines = [reply(at(1), "plan", output_tokens=100)]
    transcript.write_text("\n".join(lines))
    cli_usage.count(task, "planning", until=datetime.fromisoformat(at(5)))
    lines += [reply(at(6), "talk", output_tokens=20), reply(at(12), "review", output_tokens=300)]
    transcript.write_text("\n".join(lines))
    cli_usage.count_review(task, asked=datetime.fromisoformat(at(10)), until=datetime.fromisoformat(at(15)))
    lines += [reply(at(20), "summary", output_tokens=7)]
    transcript.write_text("\n".join(lines))
    cli_usage.count(task, "conversation", until=datetime.fromisoformat(at(25)))
    stages = [(e["stage"], e["tokens"]["output"]) for e in usage_events(task)]
    assert stages == [("planning", 100), ("conversation", 20), ("review", 300), ("conversation", 7)]
    # Counted again: nothing new since, nothing twice.
    cli_usage.count(task, "conversation", until=datetime.fromisoformat(at(30)))
    assert len(usage_events(task)) == 4


def test_the_next_task_of_the_same_session_starts_where_the_last_one_ended(planned, env):
    task, transcript = planned
    transcript.write_text(reply(at(1), "first", output_tokens=100))
    cli_usage.count(task, "planning", until=datetime.fromisoformat(at(5)))
    transcript.write_text(transcript.read_text() + "\n" + reply(at(8), "second", output_tokens=50))
    other = actions.create("demo", "Add metrics", plan_in_cli=True)
    other.set_cli_session(task.read_state().cli_session)
    cli_usage.count(other, "planning", until=datetime.fromisoformat(at(10)))
    assert [e["tokens"]["output"] for e in usage_events(other)] == [50]


def test_a_transcript_gone_or_torn_leaves_the_sum_unknown_and_the_task_alone(planned):
    task, transcript = planned
    transcript.unlink()
    cli_usage.count(task, "planning")
    [event] = usage_events(task)
    assert event["cost"] is None and event["problem"]
    assert ui.cost(task).subscription_unknown
    transcript.write_bytes(b"\xff\xfe{broken")
    cli_usage.count(task, "conversation")  # nothing readable, and no exception


def test_a_task_not_from_the_cli_reads_nothing(env):
    task = actions.create("demo", "Add health endpoint")
    cli_usage.count(task, "planning")
    assert usage_events(task) == []


PLAN = (
    '+++\nmode = "code-only"\nsummary = "Add a health endpoint"\nverify = ["npm test"]\n+++\n\n'
    "# Goal\n\nHealth.\n\n## Acceptance criteria\n\n- [ ] GET /health returns 200\n"
)
REVIEW = "# Review\n\n## Blocking\n\n## Not blocking\n\n## Checked\n\n- app.py against the plan\n"


@pytest.fixture
def counted(monkeypatch):
    stages = []
    monkeypatch.setattr(cli_usage, "count", lambda task, stage, **kw: stages.append(stage))
    monkeypatch.setattr(cli_usage, "count_review", lambda task, **kw: stages.append("review"))
    return stages


def test_bringing_in_the_plan_counts_the_planning(planned, counted):
    task, _ = planned
    task.transition(State.CHECKPOINT_PLAN)
    task.set_awaiting_plan(True)
    actions.import_plan(task, PLAN)
    assert counted == ["planning"]


def test_bringing_in_a_review_counts_its_round(planned, counted):
    task, _ = planned
    st = task.read_state()
    st.state, st.awaiting_review = State.REVIEW, True
    task._write_state(st)
    reviewing.import_answer(task, REVIEW)
    assert counted == ["review"]


def test_deleting_a_task_counts_the_rest_before_its_history_line(planned):
    task, transcript = planned
    a_minute_ago = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    transcript.write_text(reply(a_minute_ago, "a", output_tokens=1000))
    actions.remove(task, load_project("demo"))
    [entry] = actions.history()
    assert entry["subscription"]["conversation"] == pytest.approx(
        pricing.price("claude-opus-5-5", Tokens(output=1000)), abs=1e-4
    )


def test_accepting_the_work_counts_the_rest(planned, counted):
    task, _ = planned
    (task.repo / "one.txt").write_text("x\n")
    git = lambda *a: subprocess.run(["git", *a], cwd=task.repo, check=True, capture_output=True)  # noqa: E731
    git("add", "one.txt")
    git("-c", "user.name=A", "-c", "user.email=a@b", "commit", "-qm", "Add one")
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))
    gate.accept_plan(task)
    for state in (State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    actions.finish(*actions.load(task.id))
    assert counted == ["conversation"], "once: the removal after it is the accepted task's"
