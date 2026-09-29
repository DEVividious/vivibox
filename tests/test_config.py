from pathlib import Path

import pytest

from vivibox.config import ConfigError, HostService, load_config, load_project

ROLES = (
    '[roles.planner]\nharness = "opencode"\nmodel = "m"\n[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.parent


def test_loads_config_with_defaults(tmp_path):
    base = write(
        tmp_path / "config.toml",
        'tasks_dir = "/srv/vivibox"\n' + ROLES,
    )
    config = load_config(base)
    assert config.tasks_dir == Path("/srv/vivibox")
    assert (
        config.max_rounds == 3 and config.orchestration == "planner_writer_reviewer" and config.notice == ""
    )
    assert config.roles["writer"].harness == "opencode"
    assert config.desktop_notifications is True
    assert config.whole_build_before_review is False, "the pipeline builds it whole after a push"


def test_desktop_notifications_can_be_turned_off(tmp_path):
    text = 'tasks_dir = "/t"\n[notifications]\ndesktop = false\n' + ROLES
    assert load_config(write(tmp_path / "config.toml", text)).desktop_notifications is False


@pytest.mark.parametrize(
    "text",
    [
        'tasks_dir = "relative"\n' + ROLES,
        'tasks_dir = "/t"\n[roles.planner]\nharness = "opencode"\nmodel = "m"\n'
        '[roles.writer]\nharness = "other"\nmodel = "m"\n',
        'tasks_dir = "/t"\n[roles.reviewer]\nharness = "opencode"\nmodel = "m"\n',
        'tasks_dir = "/t"\n[limits]\nmax_rounds = 0\n' + ROLES,
        'tasks_dir = "/t"\nagent_orchestration_mode = "pair"\n' + ROLES,
        "tasks_dir = ",
        'tasks_dir = "/t"\n[notifications]\ndesktop = "no"\n' + ROLES,
    ],
)
def test_rejects_invalid_config(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path / "config.toml", text))


def test_missing_config_says_how_to_set_it_up(tmp_path):
    with pytest.raises(ConfigError, match="run vivibox to set it up"):
        load_config(tmp_path)


def test_loads_project(tmp_path):
    base = write(
        tmp_path / "projects" / "shop.toml",
        'repo = "/r"\nverify = ["mvn -B verify"]\nhost_services = ["host.docker.internal:5432"]\n',
    )
    project = load_project("shop", base.parent)
    assert project.verify == ["mvn -B verify"]
    assert project.host_services == [HostService("host.docker.internal", 5432)]


def test_a_project_passes_variables_from_your_shell(tmp_path):
    base = write(
        tmp_path / "projects" / "shop.toml",
        'repo = "/r"\nverify = ["x"]\npass_env = ["REPO_TOKEN", "ACCOUNT_ID"]\n',
    )
    assert load_project("shop", base.parent).pass_env == ["REPO_TOKEN", "ACCOUNT_ID"]
    base = write(tmp_path / "projects" / "old.toml", 'repo = "/r"\nverify = ["x"]\n')
    assert load_project("old", base.parent).pass_env == []


def test_a_project_may_be_prepared_before_the_writer_starts(tmp_path):
    base = write(
        tmp_path / "projects" / "shop.toml",
        'repo = "/r"\nverify = ["x"]\nprepare = ["bash mvnw -B install -DskipTests"]\n',
    )
    assert load_project("shop", base.parent).prepare == ["bash mvnw -B install -DskipTests"]
    base = write(tmp_path / "projects" / "old.toml", 'repo = "/r"\nverify = ["x"]\n')
    assert load_project("old", base.parent).prepare == []


@pytest.mark.parametrize(
    "text",
    [
        'repo = "/r"\nverify = ["x"]\nhost_services = ["nohost"]\n',
        'repo = "/r"\nverify = ["x"]\nhost_services = ["h:99999"]\n',
        'repo = "/r"\nverify = ["x"]\npass_env = ["TOKEN=abc"]\n',
        'repo = "/r"\nverify = ["x"]\npass_env = "TOKEN"\n',
        'repo = "/r"\nverify = ["x"]\npass_env = ["DOCKER_HOST"]\n',
        'repo = "/r"\nverify = ["x"]\nprepare = "mvn install"\n',
        'repo = "/r"\nverify = ["x"]\nprepare = [" "]\n',
    ],
)
def test_rejects_invalid_project(tmp_path, text):
    base = write(tmp_path / "projects" / "shop.toml", text)
    with pytest.raises(ConfigError):
        load_project("shop", base.parent)


@pytest.mark.parametrize("name", ["../etc", "Shop", "-x", ""])
def test_rejects_unsafe_project_name(tmp_path, name):
    with pytest.raises(ConfigError):
        load_project(name, tmp_path)


def test_a_new_project_may_have_no_verify_yet(tmp_path):
    """The plan you accept sets it; a project can exist before the code does."""
    base = write(tmp_path / "projects" / "fresh.toml", 'repo = "/r"\nverify = []\n')
    assert load_project("fresh", base.parent).verify == []


CLAUDE = '[roles.planner]\nharness = "claude-code"\nmodel = "claude-opus-5"\n'


def test_a_role_that_still_asks_for_a_subscription_is_refused(tmp_path):
    """ADR-0014 took subscription logins out. Ignoring the field would run the planner on a key
    while the config says it runs on your plan, so the config is refused with where to go instead."""
    text = (
        'tasks_dir = "/t"\n' + CLAUDE + 'auth = "subscription"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )
    with pytest.raises(ConfigError, match='harness = "manual"'):
        load_config(write(tmp_path / "config.toml", text))


def test_planning_and_writing_are_both_required(tmp_path):
    """No falling back to the writer's model: you would think you had configured a planner and be
    planning on whatever types, with nothing to tell you."""
    only_writer = 'tasks_dir = "/t"\n[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    with pytest.raises(ConfigError, match="planner"):
        load_config(write(tmp_path / "config.toml", only_writer))


def test_a_verification_has_a_time_limit(tmp_path):
    base = write(tmp_path / "config.toml", 'tasks_dir = "/srv/vivibox"\n' + ROLES)
    assert load_config(base).verify_timeout == 1800, "half an hour unless you say otherwise"
    base = write(
        tmp_path / "config.toml", 'tasks_dir = "/srv/vivibox"\n[limits]\nverify_timeout = 600\n' + ROLES
    )
    assert load_config(base).verify_timeout == 600
    base = write(
        tmp_path / "projects" / "shop.toml",
        'repo = "/r"\nverify = ["mvn -B verify"]\nverify_timeout = 3600\n',
    )
    assert load_project("shop", base.parent).verify_timeout == 3600


@pytest.mark.parametrize("text", ["[limits]\nverify_timeout = 0\n", '[limits]\nverify_timeout = "10m"\n'])
def test_rejects_a_time_limit_that_is_not_seconds(tmp_path, text):
    base = write(tmp_path / "config.toml", 'tasks_dir = "/srv/vivibox"\n' + text + ROLES)
    with pytest.raises(ConfigError):
        load_config(base)


def test_a_project_may_say_it_has_no_build(tmp_path):
    base = write(tmp_path / "projects" / "docs.toml", 'repo = "/r"\nverify = false\n')
    project = load_project("docs", base.parent)
    assert project.verify == [] and project.no_build
    write(tmp_path / "projects" / "docs.toml", 'repo = "/r"\nverify = []\n')
    assert not load_project("docs", base.parent).no_build, "unset is unset, not no build"
    write(tmp_path / "projects" / "docs.toml", 'repo = "/r"\nverify = true\n')
    with pytest.raises(ConfigError, match="verify"):
        load_project("docs", base.parent)


def test_an_ntfy_topic_is_a_name_on_a_server(tmp_path):
    text = 'tasks_dir = "/t"\n[notifications]\nntfy = "a-name-nobody-guesses"\n' + ROLES
    config = load_config(write(tmp_path / "config.toml", text))
    assert (config.ntfy, config.ntfy_server, config.ntfy_events) == (
        "a-name-nobody-guesses",
        "https://ntfy.sh",
        "decisions",
    )
    assert load_config(write(tmp_path / "config.toml", 'tasks_dir = "/t"\n' + ROLES)).ntfy == "", "off"
    text = (
        'tasks_dir = "/t"\n[notifications]\nntfy = "t"\nntfy_server = "https://ntfy.example/"\n'
        'ntfy_events = "all"\n' + ROLES
    )
    config = load_config(write(tmp_path / "config.toml", text))
    assert config.ntfy_server == "https://ntfy.example" and config.ntfy_events == "all"
    for bad, said in (
        ('ntfy = "https://ntfy.sh/t"', "a topic's name"),
        ('ntfy = "t"\nntfy_server = "ntfy.sh"', "an address"),
        ('ntfy = "t"\nntfy_events = "some"', "decisions, all"),
    ):
        with pytest.raises(ConfigError, match=said):
            load_config(
                write(tmp_path / "config.toml", f'tasks_dir = "/t"\n[notifications]\n{bad}\n' + ROLES)
            )


def test_cost_limits_are_dollars_per_task_and_none_by_default(tmp_path):
    config = load_config(write(tmp_path / "config.toml", 'tasks_dir = "/t"\n' + ROLES))
    assert (config.cost_warning, config.cost_limit) == (0.0, 0.0), "none"
    text = 'tasks_dir = "/t"\n[limits]\ncost_warning = 1\ncost_limit = 2.5\n' + ROLES
    config = load_config(write(tmp_path / "config.toml", text))
    assert (config.cost_warning, config.cost_limit) == (1.0, 2.5)
    for bad in ('cost_limit = "2"', "cost_limit = -1", "cost_warning = true"):
        with pytest.raises(ConfigError, match="dollars"):
            load_config(write(tmp_path / "config.toml", f'tasks_dir = "/t"\n[limits]\n{bad}\n' + ROLES))


def test_a_reviewer_is_a_role_like_the_others_and_the_old_review_keys_are_one_notice(tmp_path):
    """The reviewer is optional: the mode decides whether someone reviews. `mode` under the
    reviewer and the two old limits are read no more; a file that still has them is told once,
    and the old attempt limit carries over as the round limit, so a task fixes as often as it did."""
    reviewer = '[roles.reviewer]\nharness = "opencode"\nmodel = "anthropic/claude-sonnet-5"\n'
    text = 'tasks_dir = "/t"\n' + ROLES + reviewer
    config = load_config(write(tmp_path / "config.toml", text))
    assert config.roles["reviewer"].model == "anthropic/claude-sonnet-5" and config.notice == ""
    text = (
        'tasks_dir = "/t"\n[limits]\nmax_iterations = 5\nmax_reviews = 1\n'
        + ROLES
        + reviewer.replace('"\n', '"\nmode = "supervised"\n', 1)
    )
    config = load_config(write(tmp_path / "config.toml", text))
    assert config.max_rounds == 5, "the old limit carries over"
    assert "limits.max_iterations, limits.max_reviews, roles.reviewer.mode" in config.notice
    assert "limits.max_rounds (5)" in config.notice and "planner_writer_reviewer" in config.notice
    text = 'tasks_dir = "/t"\n[limits]\nmax_iterations = 5\nmax_rounds = 2\n' + ROLES
    assert load_config(write(tmp_path / "config.toml", text)).max_rounds == 2, "the new key wins"
    text = 'tasks_dir = "/t"\n[roles.reviewer]\nharness = "manual"\nmodel = ""\n' + ROLES
    with pytest.raises(ConfigError, match="opencode"):
        load_config(write(tmp_path / "config.toml", text))


def test_the_orchestration_modes_are_the_four_of_the_decision(tmp_path):
    from vivibox.config import ORCHESTRATION_LEGEND, ORCHESTRATION_MODES

    modes = ["single_agent", "planner_executor", "planner_writer_reviewer", "supervisor_worker"]
    assert list(ORCHESTRATION_MODES) == modes
    for name, mode in ORCHESTRATION_MODES.items():
        assert mode.label and mode.subtitle and "Gate" in mode.flow and mode.summary and mode.tradeoff, name
        assert {role for role, _, _ in mode.agents} <= {"planner", "writer", "reviewer"}, name
    # Where the verification runs: before the reviewer, never after it.
    assert ORCHESTRATION_MODES["planner_writer_reviewer"].flow == "P → W → Gate → R ⇄ W"
    assert ORCHESTRATION_MODES["supervisor_worker"].flow == "P → W → Gate → (P+R) ⇄ W"
    assert "⇄" in ORCHESTRATION_LEGEND and "Rounds" in ORCHESTRATION_LEGEND
    text = 'tasks_dir = "/t"\nagent_orchestration_mode = "supervisor_worker"\n[limits]\nmax_rounds = 4\n'
    text += ROLES
    config = load_config(write(tmp_path / "config.toml", text))
    assert (config.orchestration, config.max_rounds) == ("supervisor_worker", 4)


def test_the_docker_hub_mirror_is_off_until_you_turn_it_on(tmp_path):
    base = write(tmp_path / "config.toml", 'tasks_dir = "/t"\n' + ROLES)
    assert load_config(base).hub_mirror is False
    base = write(tmp_path / "config.toml", 'tasks_dir = "/t"\n[network]\nhub_mirror = true\n' + ROLES)
    assert load_config(base).hub_mirror is True and load_config(base).hub_mirror_port == 5055
    text = 'tasks_dir = "/t"\n[network]\nhub_mirror = true\nhub_mirror_port = 6000\n' + ROLES
    assert load_config(write(tmp_path / "config.toml", text)).hub_mirror_port == 6000


@pytest.mark.parametrize("line", ['hub_mirror = "yes"', "hub_mirror_port = 0", "hub_mirror_port = 70000"])
def test_rejects_a_mirror_setting_that_is_not_one(tmp_path, line):
    base = write(tmp_path / "config.toml", f'tasks_dir = "/t"\n[network]\n{line}\n' + ROLES)
    with pytest.raises(ConfigError, match="network.hub_mirror"):
        load_config(base)
