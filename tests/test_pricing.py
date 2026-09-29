"""List prices of the models an agent's CLI runs on, for what a subscription session would have
cost on an API key (ADR-0036)."""

import re
from dataclasses import fields
from datetime import date

import pytest

from vivibox import pricing
from vivibox.pricing import Tokens

MILLION = 1_000_000


def test_every_model_has_every_rate_and_the_table_its_date():
    assert date.fromisoformat(pricing.PRICED_ON)
    for model, rates in pricing.RATES.items():
        assert re.fullmatch(r"(claude|gpt)-[a-z0-9.-]+", model), model
        for rate in fields(rates):
            value = getattr(rates, rate.name)
            assert isinstance(value, (int, float)) and value > 0, (model, rate.name)
        # A cache read is cheaper than input, and a write dearer, on every model priced.
        assert rates.cache_read < rates.input <= rates.cache_write_5m <= rates.cache_write_1h


def test_each_kind_of_token_is_priced_at_its_own_rate():
    opus = pricing.RATES["claude-opus-5-5"]
    assert pricing.price("claude-opus-5-5", Tokens(input=MILLION)) == opus.input == 4
    assert pricing.price("claude-opus-5-5", Tokens(output=MILLION)) == 20
    assert pricing.price("claude-opus-5-5", Tokens(cache_read=MILLION)) == 0.20
    assert pricing.price("claude-opus-5-5", Tokens(cache_write_5m=MILLION)) == 5
    assert pricing.price("claude-opus-5-5", Tokens(cache_write_1h=MILLION)) == 8
    both = Tokens(input=2, output=130, cache_read=23060, cache_write_5m=14748)
    expected = (2 * 4 + 130 * 20 + 23060 * 0.20 + 14748 * 5) / MILLION
    assert pricing.price("claude-opus-5-5", both) == pytest.approx(expected)


def test_fast_mode_and_us_inference_cost_more():
    once = pricing.price("claude-opus-5-5", Tokens(output=MILLION))
    assert pricing.price("claude-opus-5-5", Tokens(output=MILLION), fast=True) == 2 * once
    assert pricing.price("claude-opus-5-5", Tokens(output=MILLION), us=True) == pytest.approx(1.1 * once)


def test_a_dated_model_name_is_the_model_and_an_unknown_one_has_no_price():
    assert pricing.price("claude-haiku-4-5-20251001", Tokens(input=MILLION)) == 1
    assert pricing.price("claude-opus-5", Tokens(input=MILLION)) == 5, "not Opus 5.5's rate"
    assert pricing.price("codex-auto-review", Tokens(input=MILLION)) is None
    assert pricing.price("", Tokens(input=MILLION)) is None


def test_tokens_add_up():
    assert Tokens(input=1, output=2) + Tokens(input=3, cache_read=4) == Tokens(
        input=4, output=2, cache_read=4
    )
    assert Tokens(input=1, output=2, cache_write_1h=3).total == 6
