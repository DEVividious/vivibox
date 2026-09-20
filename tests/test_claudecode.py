"""Claude Code as a harness: reading what one headless turn reports."""

import json
import subprocess

from vivibox.claudecode import CREDENTIALS, ClaudeCode, parse_result

# Trimmed from a real 'claude -p --output-format json' run.
DONE = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "stop_reason": "end_turn",
        "session_id": "fdcccc7a-1a4d-4f55-9234-5a32a86efd7c",
        "total_cost_usd": 0.0583115,
        "result": "OK",
        "usage": {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read_input_tokens": 10923,
            "cache_creation_input_tokens": 5274,
        },
        "modelUsage": {"claude-opus-5": {"costBasis": "list", "provider": "firstParty"}},
    }
)


def test_a_finished_turn_gives_back_what_the_task_records():
    turn = parse_result(DONE)
    assert turn.ok and not turn.error
    assert turn.session == "fdcccc7a-1a4d-4f55-9234-5a32a86efd7c"
    assert turn.cost == 0.058312 and turn.text == "OK"
    # Cache reads dwarf the rest: counting only input and output would report 6 for a turn that
    # moved 16 thousand tokens, and the limits would look far away when they are not.
    assert turn.tokens == 16203


def test_a_turn_that_did_not_finish_is_not_ok():
    """An interrupted or refused turn still prints a result object and exit code 0."""
    cut = json.loads(DONE)
    cut["terminal_reason"] = "max_turns"
    turn = parse_result(json.dumps(cut))
    assert not turn.ok and "max_turns" in turn.error

    failed = json.loads(DONE)
    failed["is_error"] = True
    assert not parse_result(json.dumps(failed)).ok


def test_output_that_is_not_a_result_is_reported_as_it_came():
    """Claude failing before it starts prints a message, not JSON. Returning an empty happy Turn
    there would record a turn that never happened and move the task on."""
    turn = parse_result("Invalid API key · Please run /login")
    assert not turn.ok and "Invalid API key" in turn.error
    assert parse_result("").error, "silence is a failure too, not a finished turn"


class FakePod:
    """Records what would run in the container."""

    agent = "vivibox-t1-agent"

    def __init__(self):
        self.ran: list[str] = []

    def exec(self, *cmd, check=True):
        self.ran.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, DONE, "")


def commands(pod):
    return "\n".join(pod.ran)


def test_a_subscription_turn_cannot_be_billed_to_a_key():
    """auth does not describe how you are logged in, it decides what reaches the container. A key
    left in the environment would otherwise bill a turn the task list shows as costing nothing."""
    pod = FakePod()
    ClaudeCode(pod, "claude-opus-5", metered=False).turn("go")
    assert "env -u ANTHROPIC_API_KEY claude -p" in commands(pod)
    assert "ANTHROPIC_API_KEY=$(cat" not in commands(pod)
    assert CREDENTIALS in commands(pod), "the login is installed where claude looks"


def test_a_metered_turn_gets_the_key_and_no_login():
    """The other way round: installing a login beside a key would leave which one paid unknowable."""
    pod = FakePod()
    ClaudeCode(pod, "claude-opus-5", metered=True).turn("go")
    assert "ANTHROPIC_API_KEY=$(cat" in commands(pod)
    assert CREDENTIALS not in commands(pod)
