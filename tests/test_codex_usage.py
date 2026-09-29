"""A task planned in Codex: its session from CODEX_THREAD_ID, what it used from the per-response
records in its rollout, and how much of the plan's windows it had used (ADR-0036)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vivibox import actions, cli_session, cli_usage, pricing, ui
from vivibox.panel import detail
from vivibox.pricing import Tokens

FIXTURE = Path(__file__).parent / "fixtures" / "codex-session.jsonl"
THREAD = "01a0ecc1-59da-7a50-b474-0ea95779b7bd"


def rollout(home: Path) -> Path:
    path = home / ".codex" / "sessions" / "2026" / "09" / "29" / f"rollout-2026-09-29T12-41-29-{THREAD}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(FIXTURE.read_text())
    return path


def test_codex_s_session_is_its_thread_and_the_rollout_named_after_it(tmp_path):
    path = rollout(tmp_path)
    found = cli_session.detect({"HOME": str(tmp_path), "CODEX_THREAD_ID": THREAD}, Path("/w"))
    assert found == {"tool": "codex", "id": THREAD, "path": str(path)}
    assert cli_session.describe(found) == f"Codex session {THREAD}"


def test_codex_s_own_home_is_followed_and_a_rollout_not_there_yet_is_found_later(tmp_path):
    home = tmp_path / "elsewhere"
    found = cli_session.detect(
        {"HOME": str(tmp_path), "CODEX_HOME": str(home), "CODEX_THREAD_ID": THREAD}, Path("/w")
    )
    assert found == {"tool": "codex", "id": THREAD, "path": ""}
    made = home / "sessions" / "2026" / "09" / "29" / f"rollout-x-{THREAD}.jsonl"
    made.parent.mkdir(parents=True)
    made.write_text("")
    assert cli_usage.transcript(found, {"CODEX_HOME": str(home)}) == made


def test_claude_code_s_session_comes_first_when_both_are_set(tmp_path):
    env = {"HOME": str(tmp_path), "CODEX_THREAD_ID": THREAD, "CLAUDE_CODE_SESSION_ID": "abc-1"}
    assert cli_session.detect(env, Path("/w"))["tool"] == "claude-code"


def test_each_response_is_counted_once_with_the_turn_s_model():
    replies = cli_usage.codex_replies(FIXTURE)
    assert len(replies) == 5, "six records, one written twice"
    assert {r.model for r in replies} == {"gpt-6-sol"}
    total = sum((r.tokens for r in replies), Tokens())
    # The cached part is inside the input count, and priced apart.
    assert total == Tokens(input=120457 - 110208, output=1338, cache_read=110208)
    assert replies[0].at == datetime(2026, 9, 29, 10, 46, 40, 516000, tzinfo=UTC)


def test_the_windows_used_are_the_last_said_before_the_end():
    limits = cli_usage.codex_limits(FIXTURE, datetime(2026, 9, 29, 11, 0, tzinfo=UTC))
    assert limits == {"5h": 0.0, "week": 72.0}
    assert cli_usage.codex_limits(FIXTURE, datetime(2026, 9, 29, 10, 0, tzinfo=UTC)) == {}


@pytest.fixture
def planned(env, monkeypatch):
    home = env / "home"
    path = rollout(home)
    monkeypatch.setenv("HOME", str(home))
    task = actions.create("demo", "Add health endpoint", plan_in_cli=True)
    task.set_cli_session({"tool": "codex", "id": THREAD, "path": str(path)})
    return task


def test_planning_in_codex_is_priced_and_keeps_the_windows(planned):
    cli_usage.count(planned, "planning", until=datetime(2026, 9, 29, 11, 0, tzinfo=UTC))
    [event] = [e["data"] for e in planned.events() if e["type"] == "cli_usage"]
    assert event["model"] == "gpt-6-sol" and event["replies"] == 5
    assert event["cost"] == pytest.approx(
        pricing.price("gpt-6-sol", Tokens(input=10249, output=1338, cache_read=110208)), abs=1e-6
    )
    assert event["limits"] == {"5h": 0.0, "week": 72.0}
    spent = ui.cost(planned)
    assert spent.subscription["planning"] == pytest.approx(event["cost"])
    assert spent.subscription_limits == {"5h": 0.0, "week": 72.0}
    shown = detail(planned, planned.read_state(), 3, running=False)
    assert "Codex's limits used: 0% of the 5-hour window, 72% of the week" in shown
