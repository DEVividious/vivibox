"""vivibox's own Serena in the writer's container: started on the project where that container
has the task's clone, with no report of its use to its makers, and with everything of its own
kept in the container's home, never in the clone."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_repo
from test_control import binds, variables
from test_control import docker as docker  # noqa: F401  (the fixture: a machine and a recording Docker)
from test_control import starting as starting  # noqa: F401  (the fixture: a start on stand-ins)
from test_pod import flag
from test_providers import files_in

from vivibox import actions, box, gate, opencode, podspec, providers, repo

MODEL = "deepseek/deepseek-v4-flash"
# Serena's command as it was while it named a folder no container of a task has.
AS_IT_WAS = [
    "serena", "start-mcp-server", "--context", "ide", "--project", "/task/repo",
    "--enable-web-dashboard", "false", "--open-web-dashboard", "false",
]  # fmt: skip
# What Serena's entry starts it with besides: no report of its use, and a home of its own.
QUIET = {"SERENA_USAGE_REPORTING": "false"}
ENVIRONMENT = {**QUIET, "SERENA_HOME": "/config/serena"}
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
@pytest.mark.parametrize(
    "flow", ["single_agent", "planner_executor", "supervisor_worker", "planner_writer_reviewer", "a box"]
)
def test_serena_is_started_on_the_clone_where_the_writers_container_has_it(starting, flow):  # noqa: F811
    """Serena was told the project is in /task/repo, a folder no container has: the writer's
    mounts the clone at the path it has on your machine and works there. It started with no
    project, and its tools answered that none is active. What every start writes for the
    writer (a task's in every flow, with one agent or with a planner, and a box's) names the
    folder the container mounts and works in."""
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
    monkeypatch.setattr(podspec, "agent_repo", lambda repo: f"/elsewhere{repo}")
    providers.set_serena_mode("on")
    task = started("single_agent")
    written = json.loads((task.meta / "harness" / "opencode.json").read_text())
    mounted, works_in = where_the_agent_has_the_clone(task)
    assert project_of(written) == mounted == works_in == f"/elsewhere{task.repo}"


def test_nothing_else_of_a_tasks_configuration_changed(env, tmp_path):
    """With Serena off the configuration is as it was, whole; with Serena on it differs from
    what it was in one argument, the project's folder, in the variables Serena is started
    with, and in the script that starts it."""
    small = files_in(tmp_path / "small", 3)
    big = files_in(tmp_path / "big", providers.SERENA_MIN_FILES)
    assert opencode.config(MODEL, repo=small) == WITHOUT_SERENA
    on = opencode.config(MODEL, repo=big)
    assert {key: value for key, value in on.items() if key != "mcp"} == WITHOUT_SERENA
    assert list(on) == [*list(WITHOUT_SERENA)[:-1], "mcp", "instructions"], "and in the order it had"
    serena = on["mcp"].pop("serena")
    assert on["mcp"] == {}, "no other server"
    assert serena == {"type": "local", "command": serena["command"], "environment": ENVIRONMENT}
    assert serena["command"][:3] == ["sh", "-c", providers.SERENA_START], "started by its start script"
    asked = serena["command"][3:]
    assert asked != AS_IT_WAS
    assert [arg if arg != str(big) else "/task/repo" for arg in asked] == AS_IT_WAS


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
    assert command[3:] == [arg for arg in AS_IT_WAS if arg not in ("--project", "/task/repo")]
    assert providers.mcp_secrets() == []


def test_serena_is_started_with_no_report_of_its_use(env, tmp_path):
    """Serena as installed asks its makers' server at every start, with its version, the
    system and how it was started, unless SERENA_USAGE_REPORTING is false in its environment.
    vivibox starts this server itself, and starts it with that: in the environment of its
    entry, which opencode hands to a local MCP server. The value is no secret, and nothing is
    mounted for it."""
    big = files_in(tmp_path / "big", providers.SERENA_MIN_FILES)
    written = opencode.config(MODEL, repo=big)
    assert QUIET.items() <= (written["mcp"]["serena"].get("environment") or {}).items()
    assert providers.mounted(written) == ["deepseek"], "the provider's key, and no secret of Serena's"
    providers.set_serena_mode("on")
    assert providers.task_mcp()["serena"].get("environment") == ENVIRONMENT, "with a project or without"
    assert providers.mcp_secrets() == []


# --- what is Serena's own stays out of the clone ------------------------------------------------

# A stand-in for Serena that does at its start what Serena 1.7.0 does with a project's folder
# (serena_config.get_project_serena_folder, ProjectConfig.load): the folder its configuration
# names when that folder is there; the project's own .serena when the named one is not there and
# that one is; and in the folder it took, its caches and a project.yml written whole. It says
# what it was started with and which folder it took.
STAND_IN = r"""#!{python}
import json, os, re, sys
from pathlib import Path

project = Path(sys.argv[sys.argv.index("--project") + 1])
home = Path(os.environ.get("SERENA_HOME") or Path.home() / ".serena")
configuration = home / "serena_config.yml"
written = configuration.read_text() if configuration.exists() else ""
named = re.search(r'^project_serena_folder_location: "(.*)"$', written, re.M)
configured = Path(named.group(1)) if named else project / ".serena"
folder = configured if configured.is_dir() or not (project / ".serena").is_dir() else project / ".serena"
(folder / "cache").mkdir(parents=True, exist_ok=True)
(folder / "cache" / "symbols.pkl").write_text("x")
(folder / ".gitignore").write_text("/cache\n/project.local.yml\n")
(folder / "project.local.yml").write_text("")
(folder / "project.yml").write_text("# every key, with its comment\nproject_name: demo\n")
said = {{"argv": sys.argv[1:], "folder": str(folder), "configuration": written}}
Path(os.environ["STAND_IN_SAID"]).write_text(json.dumps(said))
"""
# A project file of Serena's as a repository commits one, in another shape than Serena writes.
TRACKED = "project_name: demo\nlanguages:\n- python\n"


def start_serena(entry: dict, tmp_path: Path, home: Path | None, cwd: Path) -> tuple[int, dict | None]:
    """The entry's command, run as opencode runs a local MCP server (the command, with the
    entry's variables over its own), a stand-in for Serena first on PATH. home: a folder of the
    test's in place of the container's home for Serena, or None for no SERENA_HOME at all.
    How the command ended, and what the stand-in said if it was started."""
    folder = tmp_path / "stand-in"
    folder.mkdir(exist_ok=True)
    (folder / "serena").write_text(STAND_IN.format(python=sys.executable))
    (folder / "serena").chmod(0o755)
    said = folder / "said.json"
    said.unlink(missing_ok=True)
    variables = {**os.environ, **entry.get("environment", {}), "STAND_IN_SAID": str(said)}
    variables["PATH"] = f"{folder}{os.pathsep}{variables['PATH']}"
    variables.pop("SERENA_HOME", None)
    if home is not None:
        variables["SERENA_HOME"] = str(home)
    done = subprocess.run(
        entry["command"], env=variables, cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert "Traceback" not in done.stderr and "SyntaxError" not in done.stderr, done.stderr
    return done.returncode, json.loads(said.read_text()) if said.exists() else None


def a_clone(tmp_path: Path, tracked: str = "") -> Path:
    """A task's clone as vivibox prepares one, of a repository that commits a project file of
    Serena's when tracked says one."""
    source = make_repo(tmp_path / "source")
    if tracked:
        (source / ".serena").mkdir()
        (source / ".serena" / "project.yml").write_text(tracked)
        for args in (("add", "-A"), ("commit", "-q", "-m", "Serena's project file")):
            subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    meta = tmp_path / "task" / ".task"
    meta.mkdir(parents=True)
    clone = tmp_path / "task" / "repo"
    repo.prepare(source, clone, "demo-1", meta)
    return clone


def git_would_take(clone: Path) -> str:
    done = subprocess.run(["git", "add", "-A", "-n"], cwd=clone, capture_output=True, text=True, check=True)
    return done.stdout


@pytest.mark.parametrize("tracked", [TRACKED, ""], ids=["a project file your repository tracks", "none"])
def test_a_start_of_serena_changes_nothing_git_tracks_and_leaves_nothing_git_would_take(
    env, tmp_path, tracked
):
    """Serena writes at its start: a project.yml of another shape than its own is written
    whole again, and its caches come beside it. In the clone that was a change to a file your
    repository tracks, which the clone's exclude does not cover: the verification refused the
    work for a file the agent never touched, and a box committed it. Serena's folder for the
    project is now in its own home, named in its configuration and made before it starts, so
    it takes that one whatever .serena the project has: the clone is as git has it, nothing
    there is new, and the tracked file is the bytes it was.

    Serena is a stand-in here that picks its folder the way Serena 1.7.0 does. That the
    Serena of the image takes the setting and the folder so is not shown by this test: a probe
    of the image showed it, and the live test (tests/test_serena_live.py) holds it."""
    providers.set_serena_mode("on")
    clone = a_clone(tmp_path, tracked)
    entry = providers.task_mcp(clone)["serena"]
    home = tmp_path / "home" / "serena"
    ended, said = start_serena(entry, tmp_path, home, clone)
    assert ended == 0 and said, "Serena was started"
    asked = [str(clone) if arg == "/task/repo" else arg for arg in AS_IT_WAS[1:]]
    assert said["argv"] == asked, "with Serena's own arguments, as they were"
    assert gate.uncommitted(clone) == []
    assert git_would_take(clone) == ""
    if tracked:
        assert (clone / ".serena" / "project.yml").read_text() == tracked
        assert sorted(p.name for p in (clone / ".serena").iterdir()) == ["project.yml"]
    else:
        assert not (clone / ".serena").exists(), "nothing of Serena's in a clone that had none"
    assert said["folder"] == str(home / "project") and (home / "project" / "project.yml").exists()
    assert said["configuration"] == f'projects: []\nproject_serena_folder_location: "{home}/project"\n', (
        "its configuration names the folder, and was there before it started"
    )
    again, said = start_serena(entry, tmp_path, home, clone)
    assert again == 0 and said["folder"] == str(home / "project"), "and at every start after"
    assert gate.uncommitted(clone) == [] and git_would_take(clone) == ""


def test_serena_is_not_started_where_its_folder_cannot_be_made(env, tmp_path):
    """Without its folder Serena takes the project's own .serena and writes there. So where
    the folder cannot be made (no home named for it, a home that is no folder), Serena is not
    started at all, and the clone stays as it is: the agent works without it."""
    providers.set_serena_mode("on")
    clone = a_clone(tmp_path, TRACKED)
    entry = providers.task_mcp(clone)["serena"]
    ended, said = start_serena(entry, tmp_path, None, clone)
    assert ended != 0 and said is None, "no home named: not started"
    (tmp_path / "a file").write_text("")
    ended, said = start_serena(entry, tmp_path, tmp_path / "a file" / "serena", clone)
    assert ended != 0 and said is None, "a home that cannot be made: not started"
    assert gate.uncommitted(clone) == [] and git_would_take(clone) == ""


def test_the_clones_path_is_an_argument_of_serenas_start_and_no_part_of_its_script(env, tmp_path):
    """The script that starts Serena is the same for every task and names no path: the home
    comes from the entry's environment, the project's folder as an argument. A clone whose
    path holds what a shell would run or split reaches Serena as it is, and nothing is run."""
    providers.set_serena_mode("on")
    clone = tmp_path / "a dir; $(touch ran) `touch ran` 'q' \"q\"" / "repo"
    clone.mkdir(parents=True)
    entry = providers.task_mcp(clone)["serena"]
    assert entry["command"][:2] == ["sh", "-c"], "a script starts it"
    script = entry["command"][2]
    assert script == providers.SERENA_START and str(tmp_path) not in script and "/config" not in script
    ended, said = start_serena(entry, tmp_path, tmp_path / "home", tmp_path)
    assert ended == 0 and said["argv"][said["argv"].index("--project") + 1] == str(clone)
    assert not list(tmp_path.rglob("ran")), "nothing of the path was run"


@pytest.mark.real_start
def test_serenas_home_is_in_the_writers_home_on_the_tasks_disk_and_outside_the_clone(starting):  # noqa: F811
    """Where Serena keeps what is its own is a folder of the agent's container's home, which
    is a folder of the task's disk mounted for writing, and neither in the clone nor over it."""
    providers.set_serena_mode("on")
    task = started("single_agent")
    written = json.loads((task.meta / "harness" / "opencode.json").read_text())
    home = written["mcp"]["serena"].get("environment", {}).get("SERENA_HOME")
    assert home, "a home of its own, named in its entry"
    pod = actions.task_pod(task.id)
    homes = [
        bind.split(":")[1:]
        for bind in binds(pod.agent_command())
        if bind.split(":")[0] == f"{pod.disk}/config"
    ]
    assert homes == [["/config"]], "the container's home: of the task's disk, for writing"
    assert Path(home).is_relative_to("/config") and home != "/config"
    clone = Path(project_of(written))
    assert not Path(home).is_relative_to(clone) and not clone.is_relative_to(home)
    assert home != "/config/.serena", "not the home of a Serena you start there yourself"


@pytest.mark.real_start
@pytest.mark.parametrize("flow", ["single_agent", "supervisor_worker", "a box"])
def test_serenas_variables_are_its_entrys_and_none_is_the_containers(starting, flow):  # noqa: F811
    """What Serena is started with is in its entry of the configuration, which opencode hands
    to that one process: the agent's container is given none of it, so nothing else that runs
    there (a build, a Serena you start yourself) gets Serena's home or its setting. A guard:
    it held before, and a variable moved to the container's flags fails it."""
    providers.set_serena_mode("on")
    task = started(flow)
    written = json.loads((task.meta / "harness" / "opencode.json").read_text())
    entrys = set(written["mcp"]["serena"]["environment"])
    assert entrys == {"SERENA_USAGE_REPORTING", "SERENA_HOME"}
    given = {variable.split("=", 1)[0] for variable in variables(actions.task_pod(task.id).agent_command())}
    assert given and not given & entrys and not [name for name in given if "SERENA" in name.upper()]
