import json
import subprocess

import pytest

from vivibox import opencode
from vivibox.task import create_task


def test_config_references_key_file_never_the_key():
    cfg = opencode.config("deepseek/deepseek-v4-flash")
    assert cfg["model"] == "deepseek/deepseek-v4-flash"
    assert cfg["provider"]["deepseek"]["options"]["apiKey"] == "{file:/run/vivibox-secrets/deepseek}"
    assert cfg["autoupdate"] is False and cfg["share"] == "disabled"
    assert cfg["instructions"] == [opencode.INSTRUCTIONS]


@pytest.mark.parametrize("model", ["deepseek", "/x", ""])
def test_model_needs_provider(model):
    with pytest.raises(opencode.HarnessError):
        opencode.provider_of(model)


def test_prepare_writes_config_and_instructions(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    assert opencode.prepare(task, "deepseek/deepseek-v4-flash", ["mvn -B verify"])
    assert not opencode.prepare(task, "deepseek/deepseek-v4-flash", ["mvn -B verify"]), "unchanged"
    assert opencode.prepare(task, "deepseek/deepseek-v4-pro", ["mvn -B verify"])
    d = task.meta / "harness"
    assert json.loads((d / "opencode.json").read_text())["model"] == "deepseek/deepseek-v4-pro"
    text = (d / "instructions.md").read_text()
    assert "vivibox/demo-1" in text and "`mvn -B verify`" in text and "Co-Authored-By" in text


def test_parse_events_sums_cost_and_keeps_session():
    lines = [
        {"type": "step_start", "sessionID": "ses_1", "part": {}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"cost": 0.001, "tokens": {"total": 7000}}},
        {"type": "text", "sessionID": "ses_1", "part": {"text": "DONE"}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"cost": 0.0005, "tokens": {"total": 300}}},
    ]
    turn = opencode.parse_events("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert (turn.session, turn.ok, turn.cost, turn.tokens, turn.text) == ("ses_1", True, 0.0015, 7300, "DONE")


def test_parse_events_reports_errors():
    turn = opencode.parse_events(
        json.dumps({"type": "error", "sessionID": "s", "error": {"name": "APIError"}})
    )
    assert not turn.ok and "APIError" in turn.error


def test_attach_command_reads_password_from_file():
    class P:
        agent = "vivibox-demo-1-agent"

    cmd = opencode.OpenCode(P()).attach_command("ses_1")
    assert cmd[:4] == ["docker", "exec", "-it", "vivibox-demo-1-agent"]
    assert "$(cat /run/vivibox-secrets/server-password)" in cmd[-1] and "ses_1" in cmd[-1]


def test_a_turn_names_its_model():
    """The server's config holds one model, the writer's. A planner on another model planned on
    the writer's, with nothing saying so, until the model went with every turn."""
    ran = []

    class P:
        task_id, agent = "t1", "vivibox-t1-agent"

        def exec(self, *cmd, check=True):
            ran.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")

    opencode.OpenCode(P(), "deepseek/deepseek-v4-pro").turn("plan it")
    assert "'--model' 'deepseek/deepseek-v4-pro'" in ran[-1]
