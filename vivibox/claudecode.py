"""Claude Code as a harness: Anthropic's own CLI, run headless in the agent container.

`claude -p --output-format json` runs one turn and prints a single object describing it; `--resume`
carries the conversation on. Unlike opencode there is no server to keep alive and no port: each turn
is one process.

It runs on an Anthropic API key and nothing else. A subscription login is for your own use of
Claude, and an orchestrator that copies it into containers is not that; plan with your subscription
through the manual planner instead (ADR-0014).
"""

from __future__ import annotations

import json
import shlex

from .opencode import HARNESS_MOUNT, INSTRUCTIONS, Turn
from .pod import Pod
from .secrets import MOUNT

NAME = "claude-code"
# CLAUDE_CONFIG_DIR moves everything claude keeps -- the login, but also its transcripts and prompt
# history -- to a per-task place. Sharing one directory between tasks would share the conversations,
# and a company task's transcript has no business in a personal task's container.
CONFIG_DIR = "/config/claude"


def parse_result(output: str) -> Turn:
    """The one JSON object a headless turn prints. Anything else means it never got that far."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = result.get("usage") or {}
        tokens = sum(
            int(usage.get(k) or 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        # is_error is the turn's own verdict; terminal_reason says how the run ended. A denial is
        # neither: the turn was stopped from using a tool and reported success having skipped the
        # work, which is how the first real run came back finished with an empty handoff directory.
        denied = result.get("permission_denials") or []
        failed = (
            bool(result.get("is_error"))
            or result.get("terminal_reason") not in (None, "completed")
            or bool(denied)
        )
        named = ("is_error", "terminal_reason", "stop_reason", "api_error_status")
        error = (
            ""
            if not failed
            else json.dumps({**{k: result.get(k) for k in named}, "permission_denials": denied[:5]})
        )
        return Turn(
            result.get("session_id") or "",
            not failed,
            round(float(result.get("total_cost_usd") or 0), 6),
            tokens,
            str(result.get("result") or ""),
            error,
        )
    return Turn("", False, 0.0, 0, "", output.strip()[-2000:] or "claude printed nothing")


class ClaudeCode:
    name = NAME
    metered = True

    def __init__(self, pod: Pod, model: str):
        self.pod = pod
        self.model = model

    def session_exists(self, session: str) -> bool:
        """Whether a conversation can still be carried on. Claude keeps them as files under HOME,
        and the container's HOME is a volume, so one survives a pod that was taken down."""
        where = f"{CONFIG_DIR}/projects/*/{shlex.quote(session)}.jsonl"
        return (
            self.pod.exec(
                "bash", "-c", f"compgen -G {shlex.quote(where)} > /dev/null", check=False
            ).returncode
            == 0
        )

    def turn(self, prompt: str, session: str = "", title: str = "", on_step=None) -> Turn:
        # on_step: claude -p reports its cost when the turn ends; nothing to report as it runs.
        args = ["claude", "-p", "--output-format", "json", "--model", self.model]
        # The pod is the boundary, as it is for opencode: inside it the agent may edit and run
        # anything, and nobody is there to answer a prompt during a headless turn. Without this the
        # turn reports success having quietly skipped every tool it was not allowed to use.
        args += ["--permission-mode", "bypassPermissions"]
        # The plan and the answers live outside the repository, and claude will not touch a
        # directory it was not given.
        args += ["--resume", session] if session else []
        # --add-dir takes a list, so it swallows anything after it: with the prompt as the last
        # argument claude read it as another directory and refused the turn for having no input.
        # stdin keeps the two apart and takes a prompt of any length or shape.
        args += ["--add-dir", "/task/handoff", HARNESS_MOUNT]
        # The same briefing opencode gets from its config, which claude has no way to read. The
        # file is read in the container, so the shell there substitutes it.
        brief = f'--append-system-prompt "$(cat {INSTRUCTIONS})"'
        rest = " ".join(shlex.quote(a) for a in args[1:])
        quoted = f"{args[0]} {brief} {rest}"
        # Read in the container, so the key is in no argument list and no process table on the host.
        run = f"ANTHROPIC_API_KEY=$(cat {MOUNT}/anthropic) {quoted}"
        p = self.pod.exec("bash", "-c", f"printf %s {shlex.quote(prompt)} | {run}", check=False)
        turn = parse_result(p.stdout)
        if p.returncode != 0:
            # Whatever the parser made of an empty stdout, the shell's own words say more.
            turn.ok = False
            turn.error = (p.stderr.strip() or turn.error or p.stdout.strip())[-2000:]
        return turn

    def attach_command(self, session: str) -> list[str]:
        """For your tmux window: the same conversation, in the interface you would use by hand."""
        # The same key as the turns: without it claude would offer to log in, into the task's volume.
        resume = f"ANTHROPIC_API_KEY=$(cat {MOUNT}/anthropic) claude --resume {shlex.quote(session)}"
        return ["docker", "exec", "-it", self.pod.agent, "bash", "-c", resume]
