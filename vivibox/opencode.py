"""opencode as a harness: a headless server in the agent container, driven turn by turn.

`opencode serve` runs in the agent container. The supervisor sends each turn with
`opencode run --attach … --format json` and gets the session's events back when the turn ends; your
tmux window shows the same session through `opencode attach`. The server listens on the pod's
localhost and requires a per-task password.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import brief, providers, repo
from .harness import Harness, HarnessError, OnStep, Turn
from .pod import Pod
from .secrets import MOUNT
from .task import Task

NAME = "opencode"
PORT = 39417
URL = f"http://127.0.0.1:{PORT}"
# The reviewer's server, in its own container but the same network namespace as the writer's.
REVIEW_PORT = 39418
HARNESS_MOUNT = "/task/harness"
CONFIG = f"{HARNESS_MOUNT}/opencode.json"
INSTRUCTIONS = f"{HARNESS_MOUNT}/instructions.md"
# Where opencode writes its own log in the pod (its home is /config).
SERVER_LOG = "/config/.local/share/opencode/log/*.log"
WITH_PASSWORD = f"OPENCODE_SERVER_PASSWORD=$(cat {MOUNT}/server-password) exec"


def provider_of(model: str) -> str:
    provider, sep, _ = model.partition("/")
    if not sep or not provider:
        raise HarnessError(f"model '{model}' must be <provider>/<model>")
    return provider


def provider_entry(provider: str) -> dict:
    """A provider as a task's opencode.json names it: its definition when it is one of yours
    (providers.json), and the key mounted in the pod unless its endpoint takes none."""
    entry = providers.definition(provider)
    if not providers.keyless(provider):
        entry.setdefault("options", {})["apiKey"] = f"{{file:{MOUNT}/{provider}}}"
    return entry


def config(model: str, used: list[str] | tuple = (), repo: Path | None = None) -> dict:
    """used: every provider the task's roles run on; the writer's is always there."""
    names = list(dict.fromkeys([provider_of(model), *used]))
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "autoupdate": False,
        "share": "disabled",
        # The pod is the boundary; inside it the agent may edit and run anything. Nobody is there to
        # answer a permission prompt during a headless turn, and an unanswered prompt ends the turn.
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow", "external_directory": "allow"},
        "provider": {name: provider_entry(name) for name in names},
        # The MCP servers you brought over from opencode; their secrets are mounted like keys.
        **({"mcp": servers} if (servers := providers.task_mcp(repo)) else {}),
        "instructions": [INSTRUCTIONS],
    }


def prepare(task: Task, model: str, verify: list[str], used: list[str] | tuple = ()) -> bool:
    """Writes the harness files seen by the agent at /task/harness. True if they changed."""
    d = task.meta / "harness"
    d.mkdir(exist_ok=True)
    before = {p.name: p.read_text() for p in d.iterdir()}
    (d / "instructions.md").write_text(brief.common(task.id, task.repo, repo.branch_name(task.id), verify))
    (d / "opencode.json").write_text(json.dumps(config(model, used, task.repo), indent=2) + "\n")
    # Whether Serena is in, and why, for the task's panel; said again only when it changes.
    on, why = providers.serena_for(task.repo)
    said = [e for e in task.events() if e["type"] == "serena"]
    if not said or said[-1]["data"].get("why") != why:
        task.event("serena", on=on, why=why)
    return before != {p.name: p.read_text() for p in d.iterdir()}


# The running cost, tokens and step count of a turn, reported as each step of it finishes.
def parse_events(output: str) -> Turn:
    session, cost, tokens, texts, errors = "", 0.0, 0, [], []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session = event.get("sessionID") or session
        part = event.get("part") or {}
        if event.get("type") == "step_finish":
            cost += float(part.get("cost") or 0)
            tokens += int((part.get("tokens") or {}).get("total") or 0)
        elif event.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
        elif event.get("type") == "error":
            errors.append(json.dumps(event.get("error") or event)[:500])
    return Turn(session, not errors, round(cost, 6), tokens, "\n".join(texts), "\n".join(errors))


class OpenCode(Harness):
    name = NAME
    # A provider key is always billed per token.
    metered = True

    def __init__(self, pod: Pod, model: str = "", port: int = PORT):
        self.pod = pod
        # Sent with every turn. The server has one model in its config, the writer's, and a planner
        # on another model would otherwise plan on the writer's without anything saying so.
        self.model = model
        # Where this container's server listens: the reviewer's shares the pod's network namespace
        # with the writer's, so it needs a port of its own.
        self.port = port
        self.url = f"http://127.0.0.1:{port}"

    def _get(self, path: str) -> bool:
        probe = (
            f'curl -fsS -o /dev/null -u "opencode:$(cat {MOUNT}/server-password)" {_quote(self.url + path)}'
        )
        return self.pod.exec("bash", "-c", probe, check=False).returncode == 0

    def healthy(self) -> bool:
        return self._get("/session")

    def ensure_server(self, timeout: float = 30) -> None:
        if self.healthy():
            return
        serve = (
            f"{WITH_PASSWORD} opencode serve --port {self.port} --hostname 127.0.0.1 "
            ">/tmp/opencode-serve.log 2>&1"
        )
        self.pod.exec_detached("bash", "-c", serve)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            time.sleep(0.5)
        log = self.pod.exec("tail", "-20", "/tmp/opencode-serve.log", check=False).stdout
        raise HarnessError(f"opencode server did not start:\n{log}")

    def session_exists(self, session: str) -> bool:
        return self._get(f"/session/{session}")

    def start_session(self, title: str) -> str:
        """A new conversation, made before the turn that fills it. `opencode run` reports the one it
        makes only when the turn is over, so for the whole first turn -- the one worth watching --
        vivibox had no session to show you and said the agent was not working."""
        self.ensure_server()
        body = json.dumps({"title": title})
        post = (
            f'curl -fsS -u "opencode:$(cat {MOUNT}/server-password)" -X POST '
            f"-H 'content-type: application/json' -d {_quote(body)} {_quote(self.url + '/session')}"
        )
        p = self.pod.exec("bash", "-c", post, check=False)
        try:
            session = json.loads(p.stdout).get("id") or ""
        except (json.JSONDecodeError, AttributeError):
            session = ""
        if p.returncode != 0 or not session:
            raise HarnessError(f"opencode made no session: {(p.stderr or p.stdout).strip()[-500:]}")
        return session

    def restart_server(self) -> None:
        self.pod.exec("pkill", "-f", "opencode serve", check=False)
        self.ensure_server()

    def turn(self, prompt: str, session: str = "", title: str = "", on_step: OnStep | None = None) -> Turn:
        """One turn of the agent. on_step, when given, gets the running cost, tokens and step
        count at every step_finish as the events stream in, so the view can show them during
        the turn rather than when it ends."""
        self.ensure_server()
        args = ["opencode", "run", "--attach", self.url, "--format", "json", "--auto"]
        args += ["--model", self.model] if self.model else []
        args += ["--session", session] if session else ["--title", title or self.pod.task_id]
        quoted = " ".join(_quote(a) for a in [*args, prompt])
        command = ("bash", "-c", f"{WITH_PASSWORD} {quoted}")
        if on_step is not None and hasattr(self.pod, "stream"):
            output, returncode = self._stream(command, on_step)
            stderr = ""
        else:
            p = self.pod.exec(*command, check=False)
            output, returncode, stderr = p.stdout, p.returncode, p.stderr
        turn = parse_events(output)
        if returncode != 0 and turn.ok:
            turn.ok, turn.error = False, (stderr or output).strip()[-2000:]
        # "Unexpected server error. Check server logs for details." says nothing; the log does.
        if not turn.ok and "server logs" in turn.error and (said := self.server_error()):
            turn.error = f"{turn.error}; server log: {said}"
        return turn

    def server_error(self) -> str:
        """The last error opencode's server logged, for a turn that failed without saying why."""
        p = self.pod.exec(
            "bash", "-c", f"grep -h 'level=ERROR' {SERVER_LOG} 2>/dev/null | tail -1", check=False
        )
        m = re.search(r' error="((?:[^"\\]|\\.)*)"', p.stdout)
        return m.group(1).split("\\n")[0][:300] if m else ""

    def _stream(self, command: tuple[str, ...], on_step: OnStep) -> tuple[str, int]:
        p = self.pod.stream(*command)
        lines: list[str] = []
        cost, tokens, steps = 0.0, 0, 0
        for line in p.stdout:
            lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "step_finish":
                part = event.get("part") or {}
                cost = round(cost + float(part.get("cost") or 0), 6)
                tokens += int((part.get("tokens") or {}).get("total") or 0)
                steps += 1
                on_step(cost, tokens, steps)
        p.wait()
        return "".join(lines), p.returncode

    def attach_command(self, session: str) -> list[str]:
        """For your tmux window: the full opencode interface on the task's session."""
        attach = f"{WITH_PASSWORD} opencode attach {self.url} --session {_quote(session)}"
        return ["docker", "exec", "-it", self.pod.agent, "bash", "-c", attach]


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"
