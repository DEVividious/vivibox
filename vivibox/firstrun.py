"""The first run of vivibox: config.toml written from a few answers instead of copied by hand."""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable
from importlib.resources import files

from . import actions, keys
from .config import ConfigError, config_dir

# The planner the template starts with: you plan in a chat of your own (ADR-0014).
PLAN_YOURSELF = ("manual", "Opus on claude.ai")


def template() -> str:
    return files("vivibox").joinpath("templates/config.toml").read_text()


def with_role(text: str, role: str, harness: str, model: str) -> str:
    """The template with one role's harness and model replaced, its comments kept."""
    pattern = re.compile(rf'(^\[roles\.{role}\]\nharness = )"[^"]*"(\nmodel = )"[^"]*"(.*)$', re.MULTILINE)

    def fill(m: re.Match) -> str:
        # A model you pick is a real name; the label's comment about running nothing would be wrong.
        note = m.group(3) if harness == "manual" else ""
        return f'{m.group(1)}"{harness}"{m.group(2)}"{model}"{note}'

    replaced, n = pattern.subn(fill, text)
    if n != 1:
        raise ConfigError(f"the config template has no [roles.{role}] to fill in")
    return replaced


def choose(options: list[str], ask: Callable[[str], str]) -> str:
    """One of the options, by its number or its name; the first when you just press Enter."""
    for i, option in enumerate(options, 1):
        print(f"  {i:3}  {option}")
    while True:
        answer = ask("Number or name [1]: ").strip()
        if not answer:
            return options[0]
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer
        print(f"  not one of the {len(options)} above")


def ensure_config(
    ask: Callable[[str], str] = input,
    secret: Callable[[str], str] = getpass.getpass,
    list_models: Callable[[str], list[str]] = actions.provider_models,
    interactive: bool | None = None,
) -> None:
    """Writes config.toml from your answers when there is none yet."""
    path = config_dir() / "config.toml"
    if path.exists():
        return
    if not (sys.stdin.isatty() if interactive is None else interactive):
        raise ConfigError(f"no {path} yet; run vivibox in a terminal once to set it up")
    print(f"First run: vivibox writes {path} from three answers.\n")
    stored = list(keys.list_keys())
    hint = f" [{stored[0]}]" if stored else ""
    print("Which provider runs the model that writes the code? opencode's names: deepseek, anthropic,")
    provider = ask(f"openai, openrouter, google, ...{hint}: ").strip() or (stored[0] if stored else "")
    if not provider:
        raise ConfigError("no provider given; nothing written")
    if provider not in stored:
        keys.set_key(provider, secret(f"API key for {provider} (not shown): "))
        print(f"Stored {provider}: {keys.masked(keys.get_key(provider))} in {keys.store()}")
    models = list_models(provider)
    if not models:
        raise ConfigError(f"opencode knows no models for '{provider}'; check the provider's name")
    print(f"\nThe model that writes the code, from {provider}:")
    writer = choose(models, ask)
    print("\nPlanning: in a chat of your own (claude.ai, Gemini, a CLI), where vivibox gives you the")
    print("prompt and takes back the plan, or by a model on your key, without you.")
    own = ask("Plan in your own chat? [Y/n]: ").strip().lower() in ("", "y", "yes")
    text = with_role(template(), "writer", "opencode", writer)
    if own:
        text = with_role(text, "planner", *PLAN_YOURSELF)
    else:
        print(f"\nThe model that plans, from {provider} (worth a stronger one than the writer):")
        text = with_role(text, "planner", "opencode", choose(models, ask))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"\nWrote {path}. Models per task can be changed when you create one.\n")
