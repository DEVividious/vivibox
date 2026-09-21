import pytest

from vivibox import keys
from vivibox.config import ConfigError, config_dir, load_config
from vivibox.firstrun import ensure_config, template, with_role

MODELS = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"]


@pytest.fixture
def fresh(env):
    """A machine where vivibox runs for the first time: no config.toml, no keys."""
    (config_dir() / "config.toml").unlink()
    return env


def answers(*given):
    it = iter(given)
    return lambda prompt: next(it)


def test_first_run_writes_a_config_that_loads(fresh, capsys):
    ensure_config(
        ask=answers("deepseek", "1", ""),
        secret=lambda prompt: "sk-test-1234567890",
        list_models=lambda provider: MODELS,
        interactive=True,
    )
    config = load_config()
    assert (config.roles["writer"].harness, config.roles["writer"].model) == ("opencode", MODELS[0])
    assert config.roles["planner"].harness == "manual"
    assert keys.get_key("deepseek") == "sk-test-1234567890"
    assert "sk-test" not in capsys.readouterr().out


def test_a_planner_on_the_key_is_chosen_from_the_same_list(fresh):
    ensure_config(
        ask=answers("deepseek", "deepseek/deepseek-v4-flash", "n", "2"),
        secret=lambda prompt: "sk-test",
        list_models=lambda provider: MODELS,
        interactive=True,
    )
    config = load_config()
    assert (config.roles["planner"].harness, config.roles["planner"].model) == ("opencode", MODELS[1])
    assert "vivibox runs nothing here" not in (config_dir() / "config.toml").read_text()


def test_a_stored_key_is_offered_and_not_asked_again(fresh):
    keys.set_key("deepseek", "sk-stored")
    asked = []
    ensure_config(
        ask=answers("", "", ""),
        secret=lambda prompt: asked.append(prompt) or "",
        list_models=lambda provider: MODELS,
        interactive=True,
    )
    assert not asked and keys.get_key("deepseek") == "sk-stored"
    assert load_config().roles["writer"].model == MODELS[0]


def test_a_wrong_answer_is_asked_again(fresh, capsys):
    ensure_config(
        ask=answers("deepseek", "7", "nope", "2", ""),
        secret=lambda prompt: "sk-test",
        list_models=lambda provider: MODELS,
        interactive=True,
    )
    assert load_config().roles["writer"].model == MODELS[1]
    assert capsys.readouterr().out.count("not one of the 2 above") == 2


def test_an_unknown_provider_writes_nothing(fresh):
    with pytest.raises(ConfigError, match="no models for 'deepsek'"):
        ensure_config(
            ask=answers("deepsek"),
            secret=lambda prompt: "sk-test",
            list_models=lambda provider: [],
            interactive=True,
        )
    assert not (config_dir() / "config.toml").exists()


def test_without_a_terminal_it_says_what_to_do(fresh):
    with pytest.raises(ConfigError, match="run vivibox in a terminal"):
        ensure_config(interactive=False)


def test_an_existing_config_is_left_alone(env):
    before = (config_dir() / "config.toml").read_text()
    ensure_config(ask=answers(), interactive=True)
    assert (config_dir() / "config.toml").read_text() == before


def test_the_template_is_a_config_as_it_stands():
    text = with_role(template(), "writer", "opencode", "deepseek/deepseek-v4-flash")
    assert 'model = "deepseek/deepseek-v4-flash"' in text and "[roles.planner]" in text
