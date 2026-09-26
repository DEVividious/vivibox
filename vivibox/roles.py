"""What a role runs on: the writer's and the planner's model and harness, the models each
provider offers, and the keys a task gets. Reached through actions, like everything the view and
the command line call.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from . import (
    claudecode,
    image,
    keys,
    manual,
    opencode,
    providers,
    version,
)
from .config import (
    Config,
    ConfigError,
    Role,
    load_config,
)
from .harness import HarnessError
from .pod import Pod
from .task import Task


def writer(config: Config, task: Task | None = None) -> tuple[str, str]:
    role = role_of(task, "writer", config)
    if role.harness != opencode.NAME:
        raise ConfigError(
            f"the writer runs on '{role.harness}', and only opencode can write yet. "
            "Put claude-code on the planner instead."
        )
    return role.harness, role.model


def role_of(task: Task | None, role_name: str, config: Config | None = None) -> Role:
    """A role as this task runs it: the configured one, on the model the task chose if it chose one.
    Every reader comes through here, so an override cannot apply in one place and not another."""
    role = (config or load_config()).roles[role_name]
    if not task:
        return role
    st = task.read_state()
    harness, model = st.harnesses.get(role_name, ""), st.models.get(role_name, "")
    if harness:
        return Role(harness, model)
    return replace(role, model=model) if model else role


# A choice for a role: the harness it runs in and the model, "" for a manual role.
Choice = tuple[str, str]
MODELS_CACHE_SECONDS = 24 * 3600


def models_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "vivibox" / "models.json"


# The providers most people come for, first; the rest follow by name.
POPULAR = ("anthropic", "openai", "google", "deepseek", "openrouter", "mistral", "xai", "groq")


def fetch_provider_catalog() -> list[tuple[str, str]]:
    """Every provider opencode knows, as (id, name): opencode fetches the list from models.dev
    when it first lists models, and keeps it where a throwaway container can print it."""
    cmd = ["docker", "run", "--rm", "--tmpfs", f"/config:uid={os.getuid()},gid={os.getgid()}",
           image.image_ref(), "sh", "-c",
           "opencode models >/dev/null 2>&1; cat /config/.cache/opencode/models.json"]  # fmt: skip
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return []
    found = [(pid, str(d.get("name") or pid)) for pid, d in data.items() if isinstance(d, dict)]
    return sorted(
        found,
        key=lambda f: (f[0] not in POPULAR, POPULAR.index(f[0]) if f[0] in POPULAR else 0, f[1].casefold()),
    )


def provider_catalog(refresh: bool = False) -> list[tuple[str, str]]:
    """The providers to pick from when adding one; kept for a day, and the last list kept when
    opencode cannot reach models.dev. Empty when it never could: then you type the name."""
    path = models_cache().with_name("providers.json")
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = {}
    if not refresh and cached.get("providers") and time.time() - cached.get("at", 0) < MODELS_CACHE_SECONDS:
        return [tuple(p) for p in cached["providers"]]
    try:
        found = fetch_provider_catalog()
    except (OSError, subprocess.SubprocessError):
        found = []
    if not found:
        return [tuple(p) for p in cached.get("providers", [])]
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "providers": found}) + "\n")
    return found


# Prints the ids the catalog marks deprecated: opencode's list still names them, and its server
# then refuses them ("Model not found", with the new name as a hint).
DEPRECATED = (
    'node -e \'const d=require("/config/.cache/opencode/models.json")[process.argv[1]];'
    "for (const [id, m] of Object.entries(d ? d.models : {}))"
    ' if (m.status === "deprecated") console.log(id)\''
)


def provider_models(provider: str) -> list[str]:
    """What opencode knows for a provider, asked in a throwaway container: a second or two. A
    model the catalog has retired is left out: opencode lists it and then refuses to run it."""
    env = ["-e", f"{provider.upper().replace('-', '_').replace('.', '_')}_API_KEY=placeholder"]
    script = f"opencode models {provider}; echo ---; {DEPRECATED} {provider} 2>/dev/null"
    cmd = ["docker", "run", "--rm", "--tmpfs", f"/config:uid={os.getuid()},gid={os.getgid()}", *env,
           image.image_ref(), "sh", "-c", script]  # fmt: skip
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    listed, _, retired = p.stdout.partition("\n---\n")
    gone = {f"{provider}/{line.strip()}" for line in retired.splitlines() if line.strip()}
    return [
        line.strip()
        for line in listed.splitlines()
        if line.strip().startswith(f"{provider}/") and line.strip() not in gone
    ]


def model_missing(config: Config, task: Task | None, available: dict[str, list[str]]) -> str:
    """Why a start would fail on a model: a role's model that its provider no longer offers,
    named with what it offers instead. "" when every role is fine, or its list could not be read."""
    for name in sorted(config.roles):
        role = role_of(task, name, config)
        if role.harness != opencode.NAME or not role.model:
            continue
        offered = available.get(opencode.provider_of(role.model)) or []
        if offered and role.model not in offered:
            return (
                f"the {name}'s model {role.model} is not one its provider offers now "
                f"({', '.join(m.split('/', 1)[1] for m in offered[:4])}); pick another under m, or k"
            )
    return ""


def available_models(refresh: bool = False) -> dict[str, list[str]]:
    """The models of every provider you have a key for, by provider. Kept for a day: a list that
    changes a few times a year is not worth a container each time you open a dialog."""
    path = models_cache()
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = {}
    # Your own providers list their models themselves; opencode knows nothing of them.
    on = lambda name: providers.enabled(providers.PROVIDER, name)  # noqa: E731
    found = {name: listed for name, listed in providers.models().items() if listed and on(name)}
    now_ = time.time()
    for provider in keys.list_keys():
        if provider in found or not on(provider) or not providers.is_provider_key(provider):
            continue
        entry = cached.get(provider) or {}
        # A list kept from before retired models were dropped may still name one: read again.
        fresh = (
            entry.get("models")
            and entry.get("retired_dropped")
            and now_ - entry.get("at", 0) < MODELS_CACHE_SECONDS
        )
        if not refresh and fresh:
            found[provider] = entry["models"]
            continue
        try:
            listed = provider_models(provider)
        except (OSError, subprocess.SubprocessError):
            listed = []
        found[provider] = listed or entry.get("models") or []
        if listed:
            cached[provider] = {"at": now_, "models": listed, "retired_dropped": True}
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cached, indent=2) + "\n")
    return found


def choices(role_name: str, config: Config, available: dict[str, list[str]] | None = None) -> list[Choice]:
    """What a role can run on: planning yourself (the planner), a model of each provider you have
    a key for through opencode, and Anthropic's through Claude Code. config.toml's own choice
    comes first, and is there even when the list could not be read."""
    found: list[Choice] = [configured_choice(config, role_name)]
    if role_name == "planner":
        found.append((manual.NAME, ""))
    for provider, models in (available or {}).items():
        found += [(opencode.NAME, m) for m in models]
        if provider == "anthropic" and role_name != "writer":  # only opencode can write yet
            found += [(claudecode.NAME, m.split("/", 1)[1]) for m in models]
    # The models your roles already name, for when the list could not be read.
    found += [(r.harness, r.model) for r in config.roles.values() if r.harness == opencode.NAME]
    if role_name == "writer":
        found = [c for c in found if c[0] == opencode.NAME]
    # A role with no model yet shows that it has none, and no model of its own twice.
    found = [c for c in found if c[1] or c[0] == manual.NAME or c == found[0]]
    return list(dict.fromkeys(found))


def needs_provider(config: Config) -> bool:
    """True on a first run: no provider set up and a role without a model. A task's lists would
    have nothing to pick, so the view sends you to k instead of opening them."""
    if keys.list_keys() or providers.load():
        return False
    return any(r.harness != manual.NAME and not r.model for r in config.roles.values())


def task_settings(task: Task, project, config: Config) -> dict:
    """What the task runs with, as one record: the roles as it runs them, the review, the limits,
    the verification and the preparation, the base, and which build of vivibox. A report about a
    task starts here; the timeline shows it, and m and e add what they change."""
    st = task.read_state()
    order = {"planner": 0, "writer": 1, "reviewer": 2}
    roles = {
        name: f"{r.harness} {r.model}".strip()
        for name in sorted(config.roles, key=lambda n: (order.get(n, 9), n))
        for r in [role_of(task, name, config)]
    }
    verify = "no build" if project.no_build else project.verify or "writer proposes"
    return {
        "roles": roles,
        "review": (st.review_mode or config.review_mode) if "reviewer" in config.roles else "",
        "max_reviews": config.max_reviews,
        "max_iterations": config.max_iterations,
        "verify": verify,
        "verify_timeout": project.verify_timeout or config.verify_timeout,
        "prepare": project.prepare,
        "java": project.java,
        "pass_env": project.pass_env,
        "base": st.base_commit[:7],
        "auto": st.auto_plan,
        "version": version.current(),
    }


def record_settings(task: Task, project, config: Config) -> None:
    task.event("settings", **task_settings(task, project, config))


def choose_role(task: Task, role: str, choice: Choice, config: Config) -> None:
    """m: this task's harness and model for a role, on the record. config.toml's own choice gives
    the role back to config.toml, following it when it changes; the harness is kept only when it
    differs, so a task on another model keeps following config.toml's harness."""
    harness, model = choice
    if choice == configured_choice(config, role):
        harness = model = ""
    elif harness == config.roles[role].harness:
        harness = ""
    task.set_role(role, harness, model)
    task.event("settings_changed", role=role, harness=harness, model=model)


def configured_choice(config: Config, role_name: str) -> Choice:
    role = config.roles[role_name]
    return role.harness, role.model if role.harness != manual.NAME else ""


def not_offered(choice: Choice, available: dict[str, list[str]] | None) -> bool:
    """A model config.toml or a task names that its provider's list no longer has: retired, or
    mistyped. Only when the list could be read; an empty one says nothing."""
    harness, model = choice
    if harness != opencode.NAME or "/" not in model or not available:
        return False
    listed = available.get(opencode.provider_of(model)) or []
    return bool(listed) and model not in listed


def choice_label(
    choice: Choice, config_choice: Choice | None = None, available: dict[str, list[str]] | None = None
) -> str:
    harness, model = choice
    if not model and harness != manual.NAME:
        return "no model yet: pick one below"
    text = "you, in your own chat" if harness == manual.NAME else model
    if harness == claudecode.NAME:
        text += " (Claude Code)"
    # config.toml's own choice. Named after the file it read "you, in your own chat · config.toml",
    # as if the chat were in the file.
    text += "  (default)" if choice == config_choice else ""
    # Still offered here because a file names it; a task on it would not start.
    return text + ("  (not offered now)" if not_offered(choice, available) else "")


def parse_choice(text: str) -> Choice:
    """A choice as the command line takes it: manual, provider/model for opencode, or a Claude
    model id (no provider) for Claude Code."""
    text = text.strip()
    if text == manual.NAME:
        return manual.NAME, ""
    return (opencode.NAME, text) if "/" in text else (claudecode.NAME, text)


def models_offered(config: Config | None = None, harness: str = "") -> list[str]:
    """What a role can be put on without asking a provider: the models your own roles name. Asking
    opencode means a container per keypress, and nobody wants to scroll two hundred model ids."""
    seen: list[str] = []
    for role in (config or load_config()).roles.values():
        # A manual role's model is a label for a chat of yours, not something a harness can run.
        if role.harness != manual.NAME and role.model not in seen and harness in ("", role.harness):
            seen.append(role.model)
    return seen


def harness_for(role_name: str, pod: Pod, task: Task | None = None) -> object:
    """The tool a role talks through. Two roles on the same harness share nothing but the pod."""
    role = role_of(task, role_name)
    if role.harness == manual.NAME:
        return manual.Manual()
    if role.harness == claudecode.NAME:
        return claudecode.ClaudeCode(pod, role.model)
    if role_name == "reviewer":
        return opencode.OpenCode(pod, role.model, port=opencode.REVIEW_PORT)
    return opencode.OpenCode(pod, role.model, task=task if role_name == "writer" else None)


def provider_keys(config: Config, task: Task | None = None) -> list[str]:
    """Every metered provider a role needs, on the models this task runs them on: a task moved to
    another provider's model needs that provider's key, not the one config.toml's model uses."""
    found = []
    for name, role in ((n, role_of(task, n, config)) for n in config.roles):
        if not role.model and role.harness == opencode.NAME:
            raise ConfigError(f"the {name} has no model yet; pick one when you create a task, or press m")
        if role.harness == opencode.NAME and (p := opencode.provider_of(role.model)) not in found:
            if not providers.keyless(p):
                found.append(p)
        elif role.harness == claudecode.NAME and "anthropic" not in found:
            found.append("anthropic")
    return found


def machine_problem(config: Config, docker_ok: bool | None = None) -> str:
    """What would keep every task from starting, in a line for the header: Docker down, a role
    without a model, or a provider a role needs without a key. "" when nothing would."""
    if docker_ok is False:
        return "Docker is not running: no task can start"
    try:
        needed = provider_keys(config)
    except ConfigError as e:
        return str(e.args[0] if e.args else e)
    except HarnessError:
        return ""  # a model not of the <provider>/<model> shape is refused at the start, with its own words
    have = keys.list_keys()
    if missing := [p for p in needed if p not in have]:
        return f"no key for {', '.join(missing)}: press k"
    return ""
