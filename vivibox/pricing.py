"""List prices of the models an agent's CLI runs on, per million tokens, for what a session on a
subscription would have cost on an API key (ADR-0036). Taken from the providers' pricing pages
on PRICED_ON; a model not here is counted in tokens with no price, never guessed.

Anthropic: platform.claude.com/docs/en/about-claude/pricing. OpenAI:
developers.openai.com/api/docs/pricing, short-context rates (the page names no threshold for its
long-context ones, so a very long session is priced low), and no 1-hour cache."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

PRICED_ON = "2026-09-29"
MILLION = 1_000_000

# Fast mode and US-only inference, on the rates of the models that have them.
US_INFERENCE = 1.1


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    fast: float = 1.0


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))

    @property
    def total(self) -> int:
        return sum(getattr(self, f.name) for f in fields(self))


def _claude(input_: float, output: float, cache_read: float, fast: float = 1.0) -> Rates:
    return Rates(input_, output, cache_read, input_ * 1.25, input_ * 2, fast)


def _openai(input_: float, output: float, cache_read: float, cache_write: float) -> Rates:
    return Rates(input_, output, cache_read, cache_write, cache_write)


RATES: dict[str, Rates] = {
    "claude-fable-5-1": _claude(10, 50, 0.25),
    "claude-fable-5": _claude(10, 50, 1),
    "claude-opus-5-5": _claude(4, 20, 0.20, fast=2),
    "claude-opus-5": _claude(5, 25, 0.50, fast=2),
    "claude-opus-4-8": _claude(5, 25, 0.50, fast=2),
    "claude-opus-4-7": _claude(5, 25, 0.50),
    "claude-opus-4-6": _claude(5, 25, 0.50),
    "claude-opus-4-5": _claude(5, 25, 0.50),
    "claude-sonnet-5-5": _claude(2, 10, 0.20),
    "claude-sonnet-5": _claude(2, 10, 0.20),
    "claude-sonnet-4-6": _claude(3, 15, 0.30),
    "claude-sonnet-4-5": _claude(3, 15, 0.30),
    "claude-haiku-4-5": _claude(1, 5, 0.10),
    "gpt-6-astra": _openai(10, 50, 1, 12.50),
    "gpt-6-sol": _openai(2, 10, 0.20, 2.50),
    "gpt-6-luna": _openai(0.10, 0.50, 0.01, 0.125),
    "gpt-5.6-terra": _openai(2, 12, 0.20, 2.50),
}

# claude-haiku-4-5-20251001 is claude-haiku-4-5.
DATED = re.compile(r"-\d{8}$")


def rates(model: str) -> Rates | None:
    return RATES.get(DATED.sub("", model))


def price(model: str, tokens: Tokens, fast: bool = False, us: bool = False) -> float | None:
    """What these tokens cost on an API key, in dollars; None for a model with no price here."""
    found = rates(model)
    if found is None:
        return None
    dollars = sum(getattr(tokens, f.name) * getattr(found, f.name) for f in fields(tokens)) / MILLION
    return dollars * (found.fast if fast else 1) * (US_INFERENCE if us else 1)
