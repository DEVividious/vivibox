import stat

from vivibox import secrets


def test_prepare_writes_private_files_and_keeps_password(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: f"key-for-{p}")
    assert (d / "deepseek").read_text() == "key-for-deepseek"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "deepseek").stat().st_mode) == 0o600
    password = (d / "server-password").read_text()
    secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: "rotated")
    assert (d / "server-password").read_text() == password
    assert (d / "deepseek").read_text() == "rotated"
    secrets.remove("demo-1")
    assert not d.exists()


def test_only_the_needed_keys_are_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: "k")
    assert sorted(p.name for p in d.iterdir()) == ["deepseek", "server-password"]


def test_prepare_leaves_exactly_the_keys_asked_for_and_keeps_the_password(tmp_path, monkeypatch):
    """A task's directory from before keys went by container, or from a role on another provider
    since, holds keys nobody in the container needs: the next prepare takes them away."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = secrets.prepare("demo-1", ["deepseek", "anthropic", "mcp.tools.token"], get_key=lambda p: "k")
    password = (d / "server-password").read_text()
    outside = tmp_path / "yours"
    outside.write_text("not the task's")
    (d / "planted").symlink_to(outside)
    secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: "k")
    assert sorted(p.name for p in d.iterdir()) == ["deepseek", "server-password"]
    assert (d / "server-password").read_text() == password, "the running server's password stays"
    assert outside.read_text() == "not the task's", "a link is removed, not what it points at"


# --- which container holds which key --------------------------------------------------------------


import pytest  # noqa: E402

from vivibox.orchestration import MODES  # noqa: E402

KEYED = ("deepseek", "anthropic", "openai")


def use(env, mode="planner_writer_reviewer", **roles):
    """config.toml with these roles, as (harness, model), and a key for every provider."""
    from vivibox import keys

    lines = [f'tasks_dir = "{env / "tasks"}"', f'agent_orchestration_mode = "{mode}"']
    for name, (harness, model) in roles.items():
        lines += [f"[roles.{name}]", f'harness = "{harness}"', f'model = "{model}"']
    (env / "config" / "config.toml").write_text("\n".join(lines) + "\n")
    for provider in KEYED:
        keys.set_key(provider, f"key-{provider}")


def start(env, monkeypatch, task_id="demo-1"):
    """A real start up to the pod: the agent's keys and harness files are written by then."""
    from vivibox import actions, image
    from vivibox.pod import Pod, PodError

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(image, "env", lambda ref: {})
    monkeypatch.setattr(actions, "available_models", lambda refresh=False: {})

    def up(self):
        raise PodError("enough")

    monkeypatch.setattr(Pod, "up", up)
    with pytest.raises(PodError, match="enough"):
        actions.start(task_id)


def review(env, monkeypatch, task_id="demo-1"):
    """The reviewer's container brought up as a review round does, on a pod that runs nothing."""
    import subprocess

    from vivibox import actions, reviewing
    from vivibox import pod as pods
    from vivibox.config import load_config

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env / "run"))
    task, _ = actions.load(task_id)
    pod = pods.Pod(
        task.id, task.repo, "img", runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
        review_dir=task.root / "review",
    )  # fmt: skip
    reviewing.up(task, pod, load_config())


def held(task_id: str) -> list[str]:
    """The keys and secrets a container's directory holds, its server's password aside."""
    d = secrets.runtime_dir(task_id)
    return sorted(p.name for p in d.iterdir() if p.name != "server-password") if d.exists() else []


def agent_providers(task_id: str) -> list[str]:
    """The providers the agent's opencode.json reads a key for: a missing file stops opencode."""
    import json

    from vivibox import actions

    task, _ = actions.load(task_id)
    return sorted(json.loads((task.meta / "harness" / "opencode.json").read_text())["provider"])


@pytest.mark.real_start
def test_the_writer_has_no_key_of_the_reviewer_and_the_reviewer_none_of_the_writer(env, monkeypatch):
    from vivibox import actions

    use(
        env,
        planner=("opencode", "deepseek/plan"),
        writer=("opencode", "deepseek/write"),
        reviewer=("opencode", "anthropic/read"),
    )
    actions.create("demo", "Goal")
    start(env, monkeypatch)
    review(env, monkeypatch)
    assert "anthropic" not in held("demo-1"), "the reviewer's key in the writer's container"
    assert "anthropic" not in agent_providers("demo-1"), "opencode would look for its key"
    assert "deepseek" not in held("demo-1-review"), "the writer's key in the reviewer's container"
    assert held("demo-1") == ["deepseek"] and held("demo-1-review") == ["anthropic"]


@pytest.mark.real_start
def test_a_planner_on_a_third_provider_has_its_key_with_the_writer_and_not_with_the_reviewer(
    env, monkeypatch
):
    from vivibox import actions

    use(
        env,
        planner=("opencode", "openai/plan"),
        writer=("opencode", "deepseek/write"),
        reviewer=("opencode", "anthropic/read"),
    )
    actions.create("demo", "Goal")
    start(env, monkeypatch)
    review(env, monkeypatch)
    assert held("demo-1") == ["deepseek", "openai"]
    assert held("demo-1-review") == ["anthropic"]


@pytest.mark.real_start
def test_a_manual_planner_puts_no_key_in_the_agents_container(env, monkeypatch):
    from vivibox import actions

    use(
        env,
        planner=("manual", ""),
        writer=("opencode", "deepseek/write"),
        reviewer=("opencode", "anthropic/read"),
    )
    actions.create("demo", "Goal")
    start(env, monkeypatch)
    assert held("demo-1") == ["deepseek"]


@pytest.mark.real_start
def test_a_planner_on_claude_code_has_the_anthropic_key_where_it_runs(env, monkeypatch):
    from vivibox import actions

    use(
        env,
        planner=("claude-code", "opus"),
        writer=("opencode", "deepseek/write"),
        reviewer=("opencode", "openai/read"),
    )
    actions.create("demo", "Goal")
    start(env, monkeypatch)
    assert held("demo-1") == ["anthropic", "deepseek"]
    assert agent_providers("demo-1") == ["deepseek"], "claude reads its key itself, opencode none of it"


@pytest.mark.real_start
def test_a_task_from_before_loses_the_reviewers_key_at_its_next_start(env, monkeypatch):
    from vivibox import actions

    use(
        env,
        planner=("opencode", "deepseek/plan"),
        writer=("opencode", "deepseek/write"),
        reviewer=("opencode", "anthropic/read"),
    )
    actions.create("demo", "Goal")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env / "run"))
    # What a start before keys went by container left: every role's key, and the server's password.
    d = secrets.prepare("demo-1", ["deepseek", "anthropic"])
    password = (d / "server-password").read_text()
    start(env, monkeypatch)
    assert held("demo-1") == ["deepseek"]
    assert (d / "server-password").read_text() == password


@pytest.mark.real_start
def test_a_writer_moved_to_another_provider_leaves_no_old_key_at_the_next_start(env, monkeypatch):
    from vivibox import actions
    from vivibox.config import load_config

    use(
        env,
        planner=("opencode", "openai/plan"),
        writer=("opencode", "deepseek/write"),
        reviewer=("opencode", "anthropic/read"),
    )
    task = actions.create("demo", "Goal")
    start(env, monkeypatch)
    assert held("demo-1") == ["deepseek", "openai"]
    actions.choose_role(task, "writer", ("opencode", "anthropic/write"), load_config())
    start(env, monkeypatch)
    assert held("demo-1") == ["anthropic", "openai"]


@pytest.mark.real_start
def test_a_provider_the_writer_and_the_reviewer_share_is_in_both_containers(env, monkeypatch):
    """Keys are a provider's, not a role's: the limit until the gateway keeps them out of the pods."""
    from vivibox import actions

    use(
        env,
        planner=("opencode", "deepseek/plan"),
        writer=("opencode", "anthropic/write"),
        reviewer=("opencode", "anthropic/read"),
    )
    actions.create("demo", "Goal")
    start(env, monkeypatch)
    review(env, monkeypatch)
    assert held("demo-1") == ["anthropic", "deepseek"]
    assert held("demo-1-review") == ["anthropic"]


# Where each role's model runs, by mode: the agent's container has the planner's and the writer's
# keys in every mode; only a mode with a reviewer of its own brings up the reviewer's container,
# on the reviewer's model, or the writer's where config.toml has no reviewer.
TABLE = [
    (mode, reviewer, ["deepseek", "openai"], (["anthropic"] if reviewer else ["deepseek"]))
    for mode in MODES
    for reviewer in (True, False)
]


@pytest.mark.real_start
@pytest.mark.parametrize("mode,has_reviewer,agent,reviewers", TABLE)
def test_every_mode_puts_each_roles_key_where_the_role_runs(
    env, monkeypatch, mode, has_reviewer, agent, reviewers
):
    from vivibox import actions

    roles = {"planner": ("opencode", "openai/plan"), "writer": ("opencode", "deepseek/write")}
    if has_reviewer:
        roles["reviewer"] = ("opencode", "anthropic/read")
    use(env, mode, **roles)
    actions.create("demo", "Goal")
    start(env, monkeypatch)
    assert held("demo-1") == agent
    assert agent_providers("demo-1") == agent
    if MODES[mode].separate_reviewer:
        review(env, monkeypatch)
        assert held("demo-1-review") == reviewers
    else:
        assert not secrets.runtime_dir("demo-1-review").exists(), "no reviewer's container in this mode"
