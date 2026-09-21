"""Claude Code as a harness: reading what one headless turn reports."""

import json
import subprocess

from vivibox.claudecode import ClaudeCode, parse_result

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


def test_a_turn_is_given_its_briefing_and_the_handoff_directory():
    """Without these claude reports a finished turn having written nothing: the plan and the
    answers live outside the repository, and it will not touch a directory it was not given."""
    pod = FakePod()
    ClaudeCode(pod, "claude-opus-5").turn("go")
    ran = commands(pod)
    assert "--add-dir /task/handoff" in ran
    turn_command = next(c for c in pod.ran if " claude " in c)
    assert turn_command.startswith("printf %s "), "the prompt goes on stdin, clear of --add-dir"
    assert "--permission-mode bypassPermissions" in ran
    assert '--append-system-prompt "$(cat /task/harness/instructions.md)"' in ran


def test_a_turn_runs_on_the_key_and_never_on_a_login():
    """ADR-0014: vivibox does not carry subscription logins into containers. The key is read in the
    container, so it appears in no argument list on the host, and the window you watch through
    gets it too; without it claude offers to log in, into the task's volume."""
    pod = FakePod()
    harness = ClaudeCode(pod, "claude-opus-5")
    harness.turn("go")
    assert "ANTHROPIC_API_KEY=$(cat /run/vivibox-secrets/anthropic) claude " in commands(pod)
    assert ".credentials.json" not in commands(pod)
    assert "ANTHROPIC_API_KEY=$(cat" in harness.attach_command("s1")[-1]


def test_a_turn_stopped_from_using_a_tool_is_not_a_finished_turn():
    """The first real run came back ok with an empty handoff directory: claude had been refused
    every write and said nothing about it. A turn that reports success having done nothing moves
    the task on from work that never happened."""
    blocked = json.loads(DONE)
    blocked["permission_denials"] = [{"tool_name": "Write", "tool_input": {"file_path": "/task/handoff/x"}}]
    turn = parse_result(json.dumps(blocked))
    assert not turn.ok
    assert "permission_denials" in turn.error and "Write" in turn.error
