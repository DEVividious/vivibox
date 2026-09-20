"""Claude Code as a harness: Anthropic's own CLI, run headless in the agent container.

`claude -p --output-format json` runs one turn and prints a single object describing it; `--resume`
carries the conversation on. Unlike opencode there is no server to keep alive and no port: each turn
is one process.

A subscription turn reports `total_cost_usd` at API list prices, which is not money spent. Nothing
in the output says which credential was used, so the role's `auth` decides it instead: vivibox
either mounts the subscription login or passes a key, never both.
"""

from __future__ import annotations

import contextlib
import json
import shlex

from . import keys
from . import secrets as secrets_module
from .opencode import HarnessError, Turn
from .pod import Pod
from .secrets import MOUNT

NAME = "claude-code"
# CLAUDE_CONFIG_DIR moves everything claude keeps -- the login, but also its transcripts and prompt
# history -- to a per-task place. Sharing one directory between tasks would share the conversations,
# and a company task's transcript has no business in a personal task's container.
CONFIG_DIR = "/config/claude"
CREDENTIALS = f"{CONFIG_DIR}/.credentials.json"


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
        # is_error is the turn's own verdict; terminal_reason says how the run ended.
        failed = bool(result.get("is_error")) or result.get("terminal_reason") not in (None, "completed")
        error = (
            ""
            if not failed
            else json.dumps(
                {k: result.get(k) for k in ("is_error", "terminal_reason", "stop_reason", "api_error_status")}
            )
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

    def __init__(self, pod: Pod, model: str, metered: bool = False):
        self.pod = pod
        self.model = model
        # api-key or subscription, and this is what makes it so. A metered turn is given the key and
        # no login; a subscription turn is given the login and the key is unset, so a key that
        # happened to be in the environment cannot quietly bill a turn the task list calls free.
        self.metered = metered
        self._ready = False

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

    def turn(self, prompt: str, session: str = "", title: str = "") -> Turn:
        self.ensure_ready()
        args = ["claude", "-p", "--output-format", "json", "--model", self.model]
        args += ["--resume", session] if session else []
        quoted = " ".join(shlex.quote(a) for a in [*args, prompt])
        run = (
            f"ANTHROPIC_API_KEY=$(cat {MOUNT}/anthropic) {quoted}"
            if self.metered
            else (f"env -u ANTHROPIC_API_KEY {quoted}")
        )
        p = self.pod.exec("bash", "-c", run, check=False)
        turn = parse_result(p.stdout)
        if p.returncode != 0 and turn.ok:
            turn.ok, turn.error = False, (p.stderr or p.stdout).strip()[-2000:]
        self.keep_refreshed_login()
        return turn

    def keep_refreshed_login(self) -> None:
        if self.metered:
            return
        """Claude refreshes the access token in place. The refresh token has an expiry of its own,
        so a copy left behind in a task that has ended would go stale; the login goes back to where
        it came from instead, and the next task starts from a current one."""
        got = self.pod.exec("cat", CREDENTIALS, check=False)
        if got.returncode != 0 or not got.stdout.strip():
            return
        with contextlib.suppress(Exception):  # a login we cannot write back still ran this turn
            if got.stdout != keys.get_login():
                keys.set_login(got.stdout)

    def attach_command(self, session: str) -> list[str]:
        """For your tmux window: the same conversation, in the interface you would use by hand."""
        resume = f"claude --resume {shlex.quote(session)}"
        return ["docker", "exec", "-it", self.pod.agent, "bash", "-c", resume]

    def ensure_ready(self) -> None:
        """Puts the login where claude looks, once per pod. Failing here says what to do; failing on
        the turn says only that the model refused."""
        if self._ready:
            return
        if self.metered:  # a key needs no login file, and installing one would blur which is in use
            self._ready = True
            return
        stored = f"{MOUNT}/{secrets_module.CLAUDE_LOGIN}"
        install = (
            f"mkdir -p {CONFIG_DIR} && "
            f"if [ ! -s {CREDENTIALS} ] && [ -s {stored} ]; then "
            f"  install -m 600 {stored} {CREDENTIALS}; fi && "
            f"test -s {CREDENTIALS}"
        )
        if self.pod.exec("bash", "-c", install, check=False).returncode != 0:
            raise HarnessError(
                "no Claude login in this pod. Store yours with 'vivibox auth claude', or give the "
                'role an API key instead (auth = "api-key").'
            )
        self._ready = True
