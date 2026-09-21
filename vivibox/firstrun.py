"""The first run of vivibox: config.toml from the commented template, with no model chosen yet.

The view opens at once. The writer's model is picked, or its provider added, the first time you
create a task, and that first choice is written back here as the default.
"""

from __future__ import annotations

import re
from importlib.resources import files

from .config import ConfigError, config_dir


def template() -> str:
    return files("vivibox").joinpath("templates/config.toml").read_text()


def with_role(text: str, role: str, harness: str, model: str) -> str:
    """The config with one role's harness and model replaced, its comments kept."""
    pattern = re.compile(rf'(^\[roles\.{role}\]\nharness = )"[^"]*"(\nmodel = )"[^"]*"(.*)$', re.MULTILINE)

    def fill(m: re.Match) -> str:
        # The template's note on the line holds for the placeholder, not for a model you chose.
        return f'{m.group(1)}"{harness}"{m.group(2)}"{model}"'

    replaced, n = pattern.subn(fill, text)
    if n != 1:
        raise ConfigError(f"config.toml has no [roles.{role}] with a harness and a model line to fill in")
    return replaced


def ensure_config() -> bool:
    """Writes config.toml from the template when there is none; True if it did."""
    path = config_dir() / "config.toml"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template())
    return True


def remember(role: str, harness: str, model: str) -> None:
    """A role's first model becomes its default in config.toml."""
    path = config_dir() / "config.toml"
    path.write_text(with_role(path.read_text(), role, harness, model))
