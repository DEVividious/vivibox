import json
import subprocess

import pytest

from vivibox import harness, opencode
from vivibox.task import create_task


def test_config_references_key_file_never_the_key():
    cfg = opencode.config("deepseek/deepseek-v4-flash")
    assert cfg["model"] == "deepseek/deepseek-v4-flash"
    assert cfg["provider"]["deepseek"]["options"]["apiKey"] == "{file:/run/vivibox-secrets/deepseek}"
    assert cfg["autoupdate"] is False and cfg["share"] == "disabled"
    assert cfg["instructions"] == [opencode.INSTRUCTIONS]


@pytest.mark.parametrize("model", ["deepseek", "/x", ""])
def test_model_needs_provider(model):
    with pytest.raises(harness.HarnessError):
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


def test_a_session_is_made_before_the_turn():
    ran = []

    class P:
        task_id, agent = "t1", "vivibox-t1-agent"

        def exec(self, *cmd, check=True):
            ran.append(cmd[-1])
            out = '{"id":"ses_new","title":"t1: goal"}' if "-X POST" in cmd[-1] else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

    assert opencode.OpenCode(P()).start_session("t1: goal") == "ses_new"
    post = next(c for c in ran if "-X POST" in c)
    assert '"title": "t1: goal"' in post and post.endswith("/session'")


def test_a_turn_reports_each_step_as_the_events_stream_in():
    """opencode reports cost and tokens at every step_finish; read as they come, the running total
    reaches the view during the turn, not minutes later when the turn ends."""
    lines = [
        '{"type":"step_start","sessionID":"ses_1"}\n',
        '{"type":"step_finish","sessionID":"ses_1","part":{"cost":0.01,"tokens":{"total":100}}}\n',
        "not json\n",
        '{"type":"step_finish","sessionID":"ses_1","part":{"cost":0.02,"tokens":{"total":250}}}\n',
        '{"type":"text","sessionID":"ses_1","part":{"text":"done"}}\n',
    ]

    class Streaming:
        returncode = 0

        def __init__(self):
            self.stdout = iter(lines)

        def wait(self):
            return 0

    class P:
        task_id, agent = "t1", "vivibox-t1-agent"

        def exec(self, *cmd, check=True):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def stream(self, *cmd):
            return Streaming()

    steps = []
    turn = opencode.OpenCode(P()).turn(
        "go", on_step=lambda cost, tokens, steps_: steps.append((cost, tokens, steps_))
    )
    assert steps == [(0.01, 100, 1), (0.03, 350, 2)], "running totals, one call per step"
    assert (turn.session, turn.ok, turn.cost, turn.tokens, turn.text) == ("ses_1", True, 0.03, 350, "done")


def test_a_turn_that_fails_without_saying_why_brings_the_servers_log_line():
    """opencode answers "Unexpected server error. Check server logs for details."; the reason, a
    model the catalog retired for instance, is in the log inside the pod."""
    log = (
        "timestamp=2026-09-24T08:02:58.344Z level=ERROR run=ffa59bbd message=failed ref=err_aa18b41f "
        'error="ProviderModelNotFoundError: Model not found: deepseek/deepseek-v4-flash. Did you mean: '
        'deepseek-flash?" cause="ProviderModelNotFoundError: ...\\n    at <anonymous>"\n'
    )
    failed = (
        '{"type":"error","sessionID":"s","error":{"name":"UnknownError",'
        '"data":{"message":"Unexpected server error. Check server logs for details."}}}\n'
    )

    class P:
        task_id, agent = "t1", "vivibox-t1-agent"

        def exec(self, *cmd, check=True):
            if "level=ERROR" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0, log, "")
            if "'--format'" in cmd[-1]:  # the turn itself; the others probe the server
                return subprocess.CompletedProcess(cmd, 1, failed, "")
            return subprocess.CompletedProcess(cmd, 0, "", "")  # the server is up

    turn = opencode.OpenCode(P()).turn("go")
    assert not turn.ok
    assert turn.error.endswith(
        "; server log: ProviderModelNotFoundError: Model not found: deepseek/deepseek-v4-flash. "
        "Did you mean: deepseek-flash?"
    )


def test_the_reviewers_server_has_a_port_of_its_own(monkeypatch):
    """Its container shares the pod's network namespace with the writer's, whose server has the
    usual port; a second server on it would not start."""
    from vivibox import roles
    from vivibox.config import Role

    class P:
        agent = "vivibox-demo-1-review"

    reviewer = opencode.OpenCode(P(), "other/strong", port=opencode.REVIEW_PORT)
    assert f":{opencode.REVIEW_PORT}" in reviewer.attach_command("s")[-1]
    assert opencode.REVIEW_PORT != opencode.PORT
    monkeypatch.setattr(roles, "role_of", lambda task, name, config=None: Role("opencode", "other/strong"))
    assert roles.harness_for("reviewer", P(), None).port == opencode.REVIEW_PORT
    assert roles.harness_for("writer", P(), None).port == opencode.PORT


def test_the_readiness_probe_gives_up_on_a_request_the_server_never_answers():
    """A request made while `opencode serve` is coming up can be accepted and never answered;
    without a time limit the probe hung for good, a turn with it, and the start lock of a task
    being made (three times in one afternoon with three or four pods starting at once). A bounded
    probe fails, and the wait loop asks again."""
    ran = []

    class P:
        task_id, agent = "t1", "vivibox-t1-agent"

        def exec(self, *cmd, check=True):
            ran.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")

    assert opencode.OpenCode(P()).healthy()
    assert f"--max-time {opencode.PROBE_SECONDS}" in ran[-1] and "/session" in ran[-1]
