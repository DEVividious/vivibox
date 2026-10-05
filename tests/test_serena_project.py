"""vivibox's own Serena in the writer's container: started on the project where that container
has the task's clone."""

import json

import pytest
from test_control import binds
from test_control import docker as docker  # noqa: F401  (the fixture: a machine and a recording Docker)
from test_control import starting as starting  # noqa: F401  (the fixture: a start on stand-ins)
from test_pod import flag
from test_providers import files_in

from vivibox import actions, box, opencode, podspec, providers

MODEL = "deepseek/deepseek-v4-flash"
# Serena's command as it was while it named a folder no container of a task has.
AS_IT_WAS = [
    "serena", "start-mcp-server", "--context", "ide", "--project", "/task/repo",
    "--enable-web-dashboard", "false", "--open-web-dashboard", "false",
]  # fmt: skip
# A task's configuration without an MCP server, whole.
WITHOUT_SERENA = {
    "$schema": "https://opencode.ai/config.json",
    "model": MODEL,
    "autoupdate": False,
    "share": "disabled",
    "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow", "external_directory": "allow"},
    "provider": {"deepseek": {"options": {"apiKey": "{file:/run/vivibox-secrets/deepseek}"}}},
    "instructions": ["/task/harness/instructions.md"],
}


def project_of(configuration: dict) -> str:
    """Where Serena is told the project is, in a task's opencode.json."""
    command = configuration["mcp"]["serena"]["command"]
    return flag(command, "--project")


def started(flow: str):
    """A task of the flow, or a box, started by vivibox on stand-ins for its pod and servers."""
    if flow == "a box":
        return box.open_box("demo")
    task = actions.create("demo", "Add divide", orchestration=flow)
    actions.start(task.id, supervise=False)
    return task


def where_the_agent_has_the_clone(task) -> tuple[str, str]:
    """Where the agent's container mounts the task's clone, and where it works."""
    command = actions.task_pod(task.id).agent_command()
    clone = [bind.split(":")[1] for bind in binds(command) if bind.split(":")[0] == str(task.repo)]
    assert len(clone) == 1, "the clone, mounted once"
    return clone[0], flag(command, "-w")


@pytest.mark.real_start
@pytest.mark.parametrize("flow", ["single_agent", "supervisor_worker", "planner_writer_reviewer", "a box"])
def test_serena_is_started_on_the_clone_where_the_writers_container_has_it(starting, flow):  # noqa: F811
    """Serena was told the project is in /task/repo, a folder no container has: the writer's
    mounts the clone at the path it has on your machine and works there. It started with no
    project, and its tools answered that none is active. What every start writes for the
    writer (a task's with one agent, a task's with a planner, a box's) names the folder the
    container mounts and works in."""
    providers.set_serena_mode("on")
    task = started(flow)
    written = json.loads((task.meta / "harness" / "opencode.json").read_text())
    mounted, works_in = where_the_agent_has_the_clone(task)
    assert project_of(written) == mounted == works_in == str(task.repo)


@pytest.mark.real_start
def test_the_mount_and_serenas_project_are_one_value(starting, monkeypatch):  # noqa: F811
    """One place says where the clone is in the agent's container (podspec.agent_repo), and the
    mount, the working directory and Serena's project are all made from it: said elsewhere,
    they move together, and none of them keeps a path of its own."""
    monkeypatch.setattr(podspec, "agent_repo", lambda repo: f"/elsewhere{repo}", raising=False)
    providers.set_serena_mode("on")
    task = started("single_agent")
    written = json.loads((task.meta / "harness" / "opencode.json").read_text())
    mounted, works_in = where_the_agent_has_the_clone(task)
    assert project_of(written) == mounted == works_in == f"/elsewhere{task.repo}"


def test_nothing_else_of_a_tasks_configuration_changed(env, tmp_path):
    """With Serena off the configuration is as it was, whole; with Serena on it differs from
    what it was in one argument, the project's folder."""
    small = files_in(tmp_path / "small", 3)
    big = files_in(tmp_path / "big", providers.SERENA_MIN_FILES)
    assert opencode.config(MODEL, repo=small) == WITHOUT_SERENA
    on = opencode.config(MODEL, repo=big)
    assert {key: value for key, value in on.items() if key != "mcp"} == WITHOUT_SERENA
    assert list(on) == [*list(WITHOUT_SERENA)[:-1], "mcp", "instructions"], "and in the order it had"
    serena = on["mcp"].pop("serena")
    assert on["mcp"] == {}, "no other server"
    assert serena == {"type": "local", "command": serena["command"]}
    assert serena["command"] != AS_IT_WAS
    assert [arg if arg != str(big) else "/task/repo" for arg in serena["command"]] == AS_IT_WAS


def test_a_readers_configuration_has_no_serena_whatever_its_mode(env, tmp_path):
    """The planner's and the reviewer's configurations are a reader's: Serena is in neither,
    set on or not, and nothing of the writer's change reaches them."""
    big = files_in(tmp_path / "big", providers.SERENA_MIN_FILES)
    providers.set_serena_mode("on")
    reads = opencode.config(MODEL, (), big, reader=True)
    assert reads == {**WITHOUT_SERENA, "lsp": False}
    assert "serena" not in providers.task_mcp(big, builtin=False)


def test_serena_asked_for_without_a_clone_names_no_project(env):
    """What is asked for no task's clone (the secrets the MCP servers read) names no folder:
    nothing is told to Serena that no container has."""
    providers.set_serena_mode("on")
    command = providers.task_mcp()["serena"]["command"]
    assert "--project" not in command and "/task/repo" not in command
    assert command == [arg for arg in AS_IT_WAS if arg not in ("--project", "/task/repo")]
    assert providers.mcp_secrets() == []
