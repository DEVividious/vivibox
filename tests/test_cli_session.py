"""The agent's CLI session a task planned there comes from (ADR-0036): recorded by vivibox new
from the environment the CLI gives its commands, with nothing asked of the agent."""

import json
from pathlib import Path

from vivibox import about, actions, cli_session, ui
from vivibox.cli import main

SESSION = "59ee8ff9-585d-4966-8d47-97b391b3d94f"


def claude(home: Path, **extra) -> dict[str, str]:
    return {"HOME": str(home), "CLAUDE_CODE_SESSION_ID": SESSION, **extra}


def test_the_transcript_is_under_the_folder_the_cli_names_after_the_working_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    found = cli_session.detect(claude(tmp_path), Path("/home/me/projects/my_app.v2"))
    assert found == {
        "tool": "claude-code",
        "id": SESSION,
        # Not there yet is no reason to leave it out: the CLI may write it a moment later.
        "path": str(tmp_path / ".claude/projects/-home-me-projects-my-app-v2" / f"{SESSION}.jsonl"),
    }


def test_a_transcript_already_there_is_found_whatever_folder_the_command_runs_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    started = tmp_path / ".claude/projects/-home-me-work"
    started.mkdir(parents=True)
    (started / f"{SESSION}.jsonl").write_text("")
    found = cli_session.detect(claude(tmp_path), Path("/home/me/work/sub/dir"))
    assert found["path"] == str(started / f"{SESSION}.jsonl")


def test_the_cli_s_own_config_folder_is_followed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    found = cli_session.detect(claude(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "cc")), Path("/w"))
    assert found["path"] == str(tmp_path / "cc/projects/-w" / f"{SESSION}.jsonl")


def test_no_session_in_the_environment_records_nothing(tmp_path):
    assert cli_session.detect({"HOME": str(tmp_path)}, Path("/w")) == {}
    # Not a session id: never a pattern to look for files with, nor part of a path.
    assert cli_session.detect(claude(tmp_path, CLAUDE_CODE_SESSION_ID="../*"), Path("/w")) == {}


def test_a_task_planned_in_the_cli_keeps_its_session(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    made = actions.create("demo", "Add health endpoint", plan_in_cli=True, cwd=env / "repo")
    st = made.read_state()
    assert st.cli_session["tool"] == "claude-code" and st.cli_session["id"] == SESSION
    assert [e["data"] for e in made.events() if e["type"] == "cli_session"] == [
        {"tool": "claude-code", "id": SESSION}
    ]
    # A task you plan in the view is not the CLI's, even when the view was started from one.
    other = actions.create("demo", "Add health endpoint", cwd=env / "repo")
    assert other.read_state().cli_session == {}


def test_a_task_planned_in_the_cli_without_its_session_works_as_before(env):
    made = actions.create("demo", "Add health endpoint", plan_in_cli=True)
    assert made.read_state().plan_in_cli and made.read_state().cli_session == {}


def test_a_task_written_before_the_field_reads_as_none(env):
    made = actions.create("demo", "Add health endpoint")
    state = made.meta / "state.json"
    data = json.loads(state.read_text())
    del data["cli_session"]
    state.write_text(json.dumps(data))
    assert made.read_state().cli_session == {}


def test_status_names_the_session_to_go_back_to(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env / "home"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    made = actions.create("demo", "Add health endpoint", plan_in_cli=True)
    shown = ui.task_detail(made, lambda t: "0/0", 2, 0, ui.Style(False), running=False)
    assert f"Claude Code session {SESSION}" in shown
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    other = actions.create("demo", "Add health endpoint", plan_in_cli=True)
    assert "planned in" not in ui.task_detail(other, lambda t: "0/0", 2, 0, ui.Style(False), running=False)


def test_info_says_which_session_a_task_made_here_would_record(env, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(env / "home"))
    assert main(["info", "--json", str(env / "repo")]) == 0
    assert json.loads(capsys.readouterr().out)["cli_session"] is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    found = about.gather(env / "repo")
    assert found["cli_session"]["id"] == SESSION
    assert f"Claude Code session {SESSION}" in about.describe(found)
