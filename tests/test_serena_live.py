"""vivibox's own Serena in the writer's container of a real pod, on the configuration vivibox
writes for a task: it is told a folder that container has, it starts with the project active,
and a question of the project is answered. No model and no turn: Serena is asked over its own
protocol (MCP, JSON-RPC on its standard input and output), and opencode's server is asked which
MCP servers it started.

The question is a search for a pattern, which needs no language server. The image has none:
for the Python file of the project here Serena fetches one through the pod's network when it
activates the project, and the search is answered when that has ended, well or not.

Needs host/setup.sh to have run, Docker, and internet access (images).
Run with: uv run pytest -m docker tests/test_serena_live.py
"""

import json
import time
from types import SimpleNamespace

import mock_provider
import pytest
from test_control_live import MOCK_MODEL, MOCK_PORT, bash, sh
from test_pod_live import TASKS_ROOT, check_id

from vivibox import actions, image, keys, opencode, providers, repo, secrets
from vivibox.config import load_config
from vivibox.pod import CACHES, ca_mounts
from vivibox.pod_egress import helper_missing

pytestmark = [pytest.mark.docker, pytest.mark.real_start]

# The folder Serena was told before, which no container of a task has.
NOWHERE = "/task/repo"
# Serena's own words for a tool asked of it while it has no project.
NO_PROJECT = "No active project"

# In the writer's container: starts Serena as the task's opencode.json there defines it (its
# command, with the variables of its entry), or on another folder when one is given, and asks
# it over MCP on its standard input and output: who it is, which tools it offers, and each call
# asked for. Prints what it found, one JSON object, whatever happened; every wait is bounded.
ASK = r"""
import json, os, queue, subprocess, sys, threading

asked = json.loads(sys.argv[1])
entry = json.load(open(os.environ["OPENCODE_CONFIG"]))["mcp"]["serena"]
command = list(entry["command"])
where = command.index("--project") + 1
command[where] = asked.get("project") or command[where]
log = "/tmp/serena-asked.log"
server = subprocess.Popen(
    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log, "w"), text=True,
    env={**os.environ, **entry.get("environment", {})},
)
lines = queue.Queue()


def read():
    for line in server.stdout:
        lines.put(line)
    lines.put(None)


threading.Thread(target=read, daemon=True).start()


def ask(number, method, params, within):
    message = {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
    server.stdin.write(json.dumps(message) + "\n")
    server.stdin.flush()
    while True:
        line = lines.get(timeout=within)
        if line is None:
            raise RuntimeError(f"Serena ended before it answered {method}")
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if answer.get("id") == number:
            return answer


found = {"project": command[where], "there": os.path.isdir(command[where]), "calls": []}
try:
    client = {"name": "check", "version": "0"}
    hello = {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": client}
    found["server"] = ask(1, "initialize", hello, 120)["result"]["serverInfo"]["name"]
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    server.stdin.flush()
    found["tools"] = sorted(tool["name"] for tool in ask(2, "tools/list", {}, 120)["result"]["tools"])
    for number, (tool, arguments) in enumerate(asked.get("calls", []), 3):
        answer = ask(number, "tools/call", {"name": tool, "arguments": arguments}, 600)
        result = answer.get("result") or {}
        said = "".join(part.get("text", "") for part in result.get("content", []))
        failed = "error" in answer or bool(result.get("isError"))
        found["calls"].append({"tool": tool, "failed": failed, "said": said or json.dumps(answer)})
except Exception as e:
    found["gave_up"] = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__}: no answer in time"
finally:
    try:
        server.stdin.close()
        server.wait(timeout=30)
    except Exception:
        server.kill()
    found["log"] = open(log).read()[-3000:]
    print(json.dumps(found))
"""

# In the writer's container: every Serena that runs there as an MCP server, with the arguments
# it was started with, where it works, and what its environment says of reporting its use.
RUNNING = r"""
import json, os

found = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().split("\0")[:-1]
        if os.path.basename(argv[1]) != "serena" or argv[2] != "start-mcp-server":
            continue
        variables = open(f"/proc/{pid}/environ", "rb").read().decode().split("\0")
        reporting = [v.split("=", 1)[1] for v in variables if v.startswith("SERENA_USAGE_REPORTING=")]
        found.append({"argv": argv[2:], "cwd": os.readlink(f"/proc/{pid}/cwd"), "reporting": reporting})
    except (OSError, IndexError):
        continue
print(json.dumps(found))
"""


@pytest.fixture(scope="module")
def writer(tmp_path_factory):
    """A task with one agent, made and started by vivibox itself (its pod, its keys and harness
    files, the writer's server), with Serena set on: the project is a few lines, which Serena's
    automatic mode leaves without it. A stand-in provider is named and never asked."""
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    image.build()
    machine = load_config()  # this machine's pool and address, before the test's own files
    folder = tmp_path_factory.mktemp("own")
    name = f"srn{check_id('x').split('-')[0][1:]}"
    source = folder / "source"
    source.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "T"),
        ("config", "user.email", "t@e.c"),
    ):
        sh("git", *args, cwd=source)
    (source / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    sh("git", "add", ".", cwd=source)
    sh("git", "commit", "-q", "-m", "Base", cwd=source)
    cfg = folder / "config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{TASKS_ROOT}"\nagent_orchestration_mode = "single_agent"\n'
        f'[roles.planner]\nharness = "opencode"\nmodel = "{MOCK_MODEL}"\n'
        f'[roles.writer]\nharness = "opencode"\nmodel = "{MOCK_MODEL}"\n'
        f'[network]\npool = "{machine.network_pool}"\nhost_address = "{machine.host_address}"\n'
    )
    (cfg / "projects" / f"{name}.toml").write_text(f'repo = "{source}"\nverify = ["true"]\n')
    own = pytest.MonkeyPatch()
    own.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    own.setenv("XDG_DATA_HOME", str(folder / "data"))
    own.setattr(actions, "available_models", lambda refresh=False: {})
    try:
        assert not keys.list_keys(), "a key store of the test's own, empty"
        mock = dict(mock_provider.opencode_config(MOCK_PORT)["provider"][mock_provider.PROVIDER])
        providers._save(providers.path(), {mock_provider.PROVIDER: {**mock, providers.KEYLESS: True}})
        providers.set_serena_mode("on")
        task = actions.create(name, "Add divide")
        _, project = actions.load(task.id)
        try:
            actions.start(task.id, supervise=False)
            written = json.loads((task.meta / "harness" / "opencode.json").read_text())
            # The parts of the clone the container has mounts of its own for, as the start found them.
            protected = [mount.arg() for mount in repo.agent_mounts(task.repo, task.meta)]
            pod = actions.task_pod(task.id)
            yield SimpleNamespace(task=task, pod=pod, written=written, protected=protected)
        finally:
            actions.remove(task, project)
    finally:
        own.undo()


def inspected(what: str, container: str):
    return json.loads(sh("docker", "inspect", "-f", f"{{{{json {what}}}}}", container).stdout)


def project_of(written: dict) -> str:
    command = written["mcp"]["serena"]["command"]
    return command[command.index("--project") + 1]


def ask(pod, calls: list, project: str = "") -> dict:
    """What Serena answers in the writer's container, started as the task's configuration there
    has it; project: on that folder in place of the configuration's."""
    done = pod.exec("python3", "-c", ASK, json.dumps({"project": project, "calls": calls}), check=False)
    assert done.returncode == 0 and done.stdout.strip(), f"the question itself failed: {done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


SEARCH = ["search_for_pattern", {"substring_pattern": "def add"}]


def test_serena_is_told_the_folder_the_writers_container_mounts_and_works_in(writer):
    """What vivibox wrote for the writer names the clone's folder as that container has it: the
    one it mounts the clone at and works in, and it is there. The folder Serena was told
    before is not."""
    task, pod = writer.task, writer.pod
    project = project_of(writer.written)
    assert project == str(task.repo)
    assert inspected(".Config.WorkingDir", pod.agent) == project
    assert f"{task.repo}:{project}" in inspected(".HostConfig.Binds", pod.agent)
    read = bash(pod, 'pwd && cat "$OPENCODE_CONFIG"')
    there, _, configuration = read.stdout.partition("\n")
    assert read.returncode == 0 and there == project, "where a command of the writer's runs"
    assert json.loads(configuration) == writer.written, "the configuration the container reads is that one"
    assert pod.exec("test", "-f", f"{project}/calc.py", check=False).returncode == 0, "the project is there"
    assert pod.exec("test", "-e", NOWHERE, check=False).returncode != 0, (
        "and nothing where it was told before"
    )


def test_the_project_is_active_and_a_search_of_it_is_answered(writer):
    """Serena started as the configuration has it works on the project: its tools are the
    ones of a Serena with its project set (none to pick a project with), and a search for a
    pattern finds the line in the project's file. The same question of a Serena started on
    the folder it was told before shows what that was: it starts all the same and offers its
    tools, with one to pick a project, and the search is refused for want of an active one.
    So that it starts says nothing; what it answers does."""
    pod = writer.pod
    here = ask(pod, [SEARCH])
    assert "gave_up" not in here, f"{here.get('gave_up')}: {here['log']}"
    assert here["server"] == "Serena" and here["there"] and here["project"] == str(writer.task.repo)
    assert "search_for_pattern" in here["tools"] and "activate_project" not in here["tools"], here["tools"]
    (search,) = here["calls"]
    assert not search["failed"], f"{search['said']}: {here['log']}"
    matches = json.loads(search["said"])
    assert list(matches) == ["calc.py"] and "def add(a, b):" in matches["calc.py"][0], search["said"]
    nowhere = ask(pod, [SEARCH], project=NOWHERE)
    assert "gave_up" not in nowhere, f"{nowhere.get('gave_up')}: {nowhere['log']}"
    assert nowhere["server"] == "Serena" and not nowhere["there"], "it starts without the folder too"
    assert "activate_project" in nowhere["tools"], "and asks for a project"
    (refused,) = nowhere["calls"]
    assert refused["failed"] and NO_PROJECT in refused["said"], refused["said"]


def test_opencode_starts_serena_on_the_project_and_with_no_report_of_its_use(writer):
    """The writer's own server, started by the task's start, starts the MCP servers of its
    configuration when it is first asked for them: Serena is connected, runs on the project's
    folder with the arguments vivibox wrote, and has in its environment the variable that
    turns its usage reporting off, which opencode took from the entry."""
    pod = writer.pod
    servers = (
        f'curl -fsS --max-time 120 -u "opencode:$(cat {secrets.MOUNT}/{secrets.PASSWORD})" {opencode.URL}/mcp'
    )
    for _ in range(30):
        status = bash(pod, servers)
        if status.returncode == 0 and json.loads(status.stdout) == {"serena": {"status": "connected"}}:
            break
        time.sleep(2)
    assert status.returncode == 0, f"the writer's server did not answer: {status.stderr[-500:]}"
    assert json.loads(status.stdout) == {"serena": {"status": "connected"}}
    running = json.loads(pod.exec("python3", "-c", RUNNING).stdout)
    assert running, "the Serena opencode started is running"
    command = writer.written["mcp"]["serena"]["command"]
    for serena in running:
        assert serena["argv"] == command[1:], "started with the arguments of the configuration"
        assert serena["cwd"] == project_of(writer.written)
        assert serena["reporting"] == ["false"], "no report of its use"
    assert writer.written["mcp"]["serena"]["environment"] == {"SERENA_USAGE_REPORTING": "false"}


def test_the_writers_container_has_the_mounts_and_the_secrets_it_had(writer):
    """Nothing came to the container with Serena's project or its variable: its mounts are the
    ones a task with one agent has (the Docker socket, the clone at its own path, its home,
    the shared caches, what a build installs, the host's authorities, the protected parts of
    the clone, the task's files with the handoff, your replies and the review folder, and its
    secrets), its secrets are the server's password alone (the provider here takes no key and
    Serena none), and no variable of Serena's is in the container's own environment."""
    task, pod = writer.task, writer.pod
    disk, meta = task.root / "disk", task.meta
    expected = [
        f"vivibox-{task.id}-socket:/run/vivibox-docker",
        f"{task.repo}:{task.repo}",
        f"{disk}/config:/config",
        *(f"vivibox-cache-{name}:{path}" for name, path in CACHES.items()),
        f"{disk}/installed:/cache/m2/installed",
        *(mount.arg() for mount in ca_mounts()),
        *writer.protected,
        f"{meta}:/task:ro",
        f"{meta}/handoff:/task/handoff",
        f"{meta}/comments.md:/task/handoff/comments.md:ro",
        f"{meta}/review:/task/review",
        f"{secrets.runtime_dir(task.id)}:{secrets.MOUNT}:ro",
    ]
    assert sorted(inspected(".HostConfig.Binds", pod.agent)) == sorted(expected)
    assert bash(pod, f"ls -A {secrets.MOUNT}").stdout.split() == [secrets.PASSWORD]
    assert sorted(p.name for p in secrets.runtime_dir(task.id).iterdir()) == [secrets.PASSWORD]
    variables = inspected(".Config.Env", pod.agent)
    assert not [v for v in variables if "SERENA" in v.upper()], (
        "the variable is the entry's, not the container's"
    )
    assert providers.mounted(writer.written) == [], "the configuration reads no secret"
