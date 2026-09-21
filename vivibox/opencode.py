"""opencode as a harness: a headless server in the agent container, driven turn by turn.

`opencode serve` runs in the agent container. The supervisor sends each turn with
`opencode run --attach … --format json` and gets the session's events back when the turn ends; your
tmux window shows the same session through `opencode attach`. The server listens on the pod's
localhost and requires a per-task password.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from importlib.resources import files

from . import providers, repo
from .pod import Pod
from .secrets import MOUNT
from .task import Task

NAME = "opencode"
PORT = 39417
URL = f"http://127.0.0.1:{PORT}"
HARNESS_MOUNT = "/task/harness"
CONFIG = f"{HARNESS_MOUNT}/opencode.json"
INSTRUCTIONS = f"{HARNESS_MOUNT}/instructions.md"
WITH_PASSWORD = f"OPENCODE_SERVER_PASSWORD=$(cat {MOUNT}/server-password) exec"


class HarnessError(Exception):
    pass


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


def config(model: str, used: list[str] | tuple = ()) -> dict:
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
        **({"mcp": servers} if (servers := providers.load_mcp()) else {}),
        "instructions": [INSTRUCTIONS],
    }


def prepare(task: Task, model: str, verify: list[str], used: list[str] | tuple = ()) -> bool:
    """Writes the harness files seen by the agent at /task/harness. True if they changed."""
    d = task.meta / "harness"
    d.mkdir(exist_ok=True)
    before = {p.name: p.read_text() for p in d.iterdir()}
    template = files("vivibox").joinpath("templates/instructions.md").read_text()
    (d / "instructions.md").write_text(
        template.format(
            task_id=task.id,
            repo=task.repo,
            branch=repo.branch_name(task.id),
            verify="\n".join(f"  - `{c}`" for c in verify),
        )
    )
    (d / "opencode.json").write_text(json.dumps(config(model, used), indent=2) + "\n")
    return before != {p.name: p.read_text() for p in d.iterdir()}


@dataclass
class Turn:
    session: str
    ok: bool
    cost: float
    tokens: int
    text: str
    error: str = ""


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


class OpenCode:
    name = NAME
    # A provider key is always billed per token.
    metered = True

    def __init__(self, pod: Pod, model: str = ""):
        self.pod = pod
        # Sent with every turn. The server has one model in its config, the writer's, and a planner
        # on another model would otherwise plan on the writer's without anything saying so.
        self.model = model

    def _get(self, path: str) -> bool:
        probe = f'curl -fsS -o /dev/null -u "opencode:$(cat {MOUNT}/server-password)" {_quote(URL + path)}'
        return self.pod.exec("bash", "-c", probe, check=False).returncode == 0

    def healthy(self) -> bool:
        return self._get("/session")

    def ensure_server(self, timeout: float = 30) -> None:
        if self.healthy():
            return
        serve = (
            f"{WITH_PASSWORD} opencode serve --port {PORT} --hostname 127.0.0.1 >/tmp/opencode-serve.log 2>&1"
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
            f"-H 'content-type: application/json' -d {_quote(body)} {_quote(URL + '/session')}"
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

    def turn(self, prompt: str, session: str = "", title: str = "") -> Turn:
        self.ensure_server()
        args = ["opencode", "run", "--attach", URL, "--format", "json", "--auto"]
        args += ["--model", self.model] if self.model else []
        args += ["--session", session] if session else ["--title", title or self.pod.task_id]
        quoted = " ".join(_quote(a) for a in [*args, prompt])
        p = self.pod.exec("bash", "-c", f"{WITH_PASSWORD} {quoted}", check=False)
        turn = parse_events(p.stdout)
        if p.returncode != 0 and turn.ok:
            turn.ok, turn.error = False, (p.stderr or p.stdout).strip()[-2000:]
        return turn

    def attach_command(self, session: str) -> list[str]:
        """For your tmux window: the full opencode interface on the task's session."""
        attach = f"{WITH_PASSWORD} opencode attach {URL} --session {_quote(session)}"
        return ["docker", "exec", "-it", self.pod.agent, "bash", "-c", attach]


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"
