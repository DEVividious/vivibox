import pytest

from vivibox import actions, firstrun, keys
from vivibox.config import ConfigError, config_dir, load_config


@pytest.fixture
def fresh(env, monkeypatch):
    """A machine where vivibox runs for the first time: no config.toml. The template's tasks
    directory is /srv/vivibox, yours: tasks made here go to the test's own."""
    (config_dir() / "config.toml").unlink()
    template = firstrun.template().replace('"/srv/vivibox"', f'"{env / "tasks"}"')
    assert str(env / "tasks") in template
    monkeypatch.setattr(firstrun, "template", lambda: template)
    return env


def test_first_run_writes_the_template_and_asks_nothing(fresh):
    assert firstrun.ensure_config()
    config = load_config()
    assert config.roles["planner"].harness == "manual"
    assert (config.roles["writer"].harness, config.roles["writer"].model) == ("opencode", "")


def test_an_existing_config_is_left_alone(env):
    before = (config_dir() / "config.toml").read_text()
    assert not firstrun.ensure_config()
    assert (config_dir() / "config.toml").read_text() == before


def test_a_writer_without_a_model_shows_it_at_the_top_of_its_list(fresh):
    """The list offers what your providers have; a provider itself is added under k, nowhere else."""
    firstrun.ensure_config()
    offered = actions.choices("writer", load_config(), {"deepseek": ["deepseek/deepseek-v4-flash"]})
    assert offered == [("opencode", ""), ("opencode", "deepseek/deepseek-v4-flash")]
    assert actions.choice_label(offered[0]) == "no model yet: pick one below"


def test_a_task_needs_a_provider_before_the_first_dialog(fresh):
    firstrun.ensure_config()
    assert actions.needs_provider(load_config()), "no provider and the writer has no model"
    keys.set_key("deepseek", "sk-mine")
    assert not actions.needs_provider(load_config()), "with a provider the list has models to pick"


def test_a_task_is_refused_until_the_writer_has_a_model(fresh):
    firstrun.ensure_config()
    with pytest.raises(ConfigError, match="the writer has no model yet"):
        actions.create("demo", "Add health endpoint")


def test_the_first_model_picked_becomes_the_default(fresh):
    firstrun.ensure_config()
    task = actions.create(
        "demo", "Add health endpoint", roles={"writer": ("opencode", "deepseek/deepseek-v4-flash")}
    )
    assert load_config().roles["writer"].model == "deepseek/deepseek-v4-flash"
    assert "writer" not in task.read_state().models, "the default, not a choice for this task only"
    text = (config_dir() / "config.toml").read_text()
    assert "picked in the view" not in text and "[roles.planner]" in text


def test_a_config_without_the_role_lines_says_so(env):
    with pytest.raises(ConfigError, match=r"no \[roles.reviewer\]"):
        firstrun.with_role(firstrun.template(), "reviewer", "opencode", "x/y")
