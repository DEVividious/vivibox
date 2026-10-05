"""vivibox's own Serena in the writer's container of a real pod, on the configuration vivibox
writes for a task: it is told a folder that container has, it starts with the project active,
and a question of the project is answered; and what is Serena's own stays in the writer's
home, so that the clone is as git has it afterwards, whatever .serena the project commits. No
model and no turn: Serena is asked over its own protocol (MCP, JSON-RPC on its standard input
and output), and opencode's server is asked which MCP servers it started.

The question is a search for a pattern, which needs no language server. The image has none:
for the Python file of the project here Serena fetches one through the pod's network when it
activates the project, and the search is answered when that has ended, well or not.

Needs host/setup.sh to have run, Docker, and internet access (images).
Run with: uv run pytest -m docker tests/test_serena_live.py
"""

import contextlib
import json
import time
from pathlib import Path
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

# The variables vivibox gives the writer's container of a task with one agent, whose project
# names no JDK and passes no variable of yours, by name; the rest of its environment is the
# image's own.
GIVEN = {
    "DOCKER_HOST", "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "TESTCONTAINERS_HOST_OVERRIDE",
    "TESTCONTAINERS_RYUK_CONTAINER_IMAGE", "HUSKY", "MAVEN_OPTS", "OPENCODE_CONFIG", "CLAUDE_CONFIG_DIR",
}  # fmt: skip
# The folder Serena was told before, which no container of a task has.
NOWHERE = "/task/repo"
# Serena's own words for a tool asked of it while it has no project.
NO_PROJECT = "No active project"

# In the writer's container: starts Serena as the task's opencode.json there defines it (its
# command, with the variables of its entry), or on another folder when one is given, or as
# installed (Serena's own command of the entry alone, in the home it has by itself), and asks
# it over MCP on its standard input and output: who it is, which tools it offers, and each call
# asked for. Prints what it found, one JSON object, whatever happened; every wait is bounded.
ASK = r"""
import json, os, queue, subprocess, sys, threading

asked = json.loads(sys.argv[1])
entry = json.load(open(os.environ["OPENCODE_CONFIG"]))["mcp"]["serena"]
command, variables = list(entry["command"]), dict(entry.get("environment", {}))
if asked.get("as_installed"):
    command = command[command.index("serena"):]
    variables.pop("SERENA_HOME", None)
where = command.index("--project") + 1
command[where] = asked.get("project") or command[where]
log = "/tmp/serena-asked.log"
server = subprocess.Popen(
    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log, "w"), text=True,
    env={**os.environ, **variables},
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
# it was started with, where it works, and what its environment says of reporting its use and
# of its home.
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
        home = [v.split("=", 1)[1] for v in variables if v.startswith("SERENA_HOME=")]
        where = os.readlink(f"/proc/{pid}/cwd")
        found.append({"argv": argv[2:], "cwd": where, "reporting": reporting, "home": home})
    except (OSError, IndexError):
        continue
print(json.dumps(found))
"""


def ready() -> None:
    """The machine can run a pod, and the agent's image is built."""
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    image.build()


@contextlib.contextmanager
def a_started_task(folder: Path, files: dict[str, str] | None = None):
    """A task with one agent, made and started by vivibox itself (its pod, its keys and harness
    files, the writer's server), with Serena set on: the project is a few lines, which Serena's
    automatic mode leaves without it. files: what the project commits besides its one source
    file. A stand-in provider is named and never asked. Everything of the task is removed at
    the end."""
    machine = load_config()  # this machine's pool and address, before the test's own files
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
    for path, text in (files or {}).items():
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        (source / path).write_text(text)
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


@pytest.fixture(scope="module")
def writer(tmp_path_factory):
    """The writer's container of a task whose project has no .serena of its own."""
    ready()
    with a_started_task(tmp_path_factory.mktemp("own")) as started:
        yield started


def inspected(what: str, container: str):
    return json.loads(sh("docker", "inspect", "-f", f"{{{{json {what}}}}}", container).stdout)


def project_of(written: dict) -> str:
    command = written["mcp"]["serena"]["command"]
    return command[command.index("--project") + 1]


def ask(pod, calls: list, project: str = "", as_installed: bool = False) -> dict:
    """What Serena answers in the writer's container, started as the task's configuration there
    has it; project: on that folder in place of the configuration's; as_installed: without
    what vivibox starts it with besides its own command (its start script and its home)."""
    asked = json.dumps({"project": project, "calls": calls, "as_installed": as_installed})
    done = pod.exec("python3", "-c", ASK, asked, check=False)
    assert done.returncode == 0 and done.stdout.strip(), f"the question itself failed: {done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


SEARCH = ["search_for_pattern", {"substring_pattern": "def add"}]
# Where Serena keeps its project file for the task's project, in the writer's container.
OWN_PROJECT_FILE = f"{providers.SERENA_HOME}/project/project.yml"


def git_sees(pod) -> str:
    """What git in the writer's container says is not as committed in the clone, changed or
    new: what the verification asks before it builds. "" for a clone that is as git has it."""
    seen = bash(pod, "git status --porcelain --untracked-files=all")
    assert seen.returncode == 0, seen.stderr
    return seen.stdout


def answered(found: dict) -> None:
    """Serena, started on the project, answered the search with the line of the project's file."""
    assert "gave_up" not in found, f"{found.get('gave_up')}: {found['log']}"
    (search,) = found["calls"]
    assert not search["failed"], f"{search['said']}: {found['log']}"
    matches = json.loads(search["said"])
    assert list(matches) == ["calc.py"] and "def add(a, b):" in matches["calc.py"][0], search["said"]


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
    answered(here)
    assert here["server"] == "Serena" and here["there"] and here["project"] == str(writer.task.repo)
    assert "search_for_pattern" in here["tools"] and "activate_project" not in here["tools"], here["tools"]
    nowhere = ask(pod, [SEARCH], project=NOWHERE)
    assert "gave_up" not in nowhere, f"{nowhere.get('gave_up')}: {nowhere['log']}"
    assert nowhere["server"] == "Serena" and not nowhere["there"], "it starts without the folder too"
    assert "activate_project" in nowhere["tools"], "and asks for a project"
    (refused,) = nowhere["calls"]
    assert refused["failed"] and NO_PROJECT in refused["said"], refused["said"]


def test_serena_leaves_nothing_in_a_clone_that_has_no_serena_folder(writer):
    """Serena started on the project and asked keeps its project file, its caches and its
    memories in its own home in the writer's container: the clone has no .serena, and git
    there sees nothing changed or new."""
    pod = writer.pod
    answered(ask(pod, [SEARCH]))
    assert pod.exec("test", "-s", OWN_PROJECT_FILE, check=False).returncode == 0, (
        "its project file, in its home"
    )
    assert not (writer.task.repo / ".serena").exists()
    assert pod.exec("test", "-e", f"{writer.task.repo}/.serena", check=False).returncode != 0
    assert git_sees(pod) == ""


def test_opencode_starts_serena_on_the_project_and_with_no_report_of_its_use(writer):
    """The writer's own server, started by the task's start, starts the MCP servers of its
    configuration when it is first asked for them: Serena is connected, runs on the project's
    folder with the arguments vivibox wrote, and has in its environment the variable that
    turns its usage reporting off and the one that names its home, which opencode took from
    the entry."""
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
        asked = command[command.index("start-mcp-server") :]
        assert serena["argv"] == asked, "started with Serena's arguments of the configuration"
        assert serena["cwd"] == project_of(writer.written)
        assert serena["reporting"] == ["false"], "no report of its use"
        assert serena["home"] == [providers.SERENA_HOME], "in its own home"
    assert writer.written["mcp"]["serena"]["environment"] == {
        "SERENA_USAGE_REPORTING": "false",
        "SERENA_HOME": providers.SERENA_HOME,
    }


def test_the_writers_container_has_the_mounts_and_the_secrets_it_had(writer):
    """Nothing came to the container with Serena's project or its variable: its mounts are the
    ones a task with one agent has (the Docker socket, the clone at its own path, its home,
    the shared caches, what a build installs, the host's authorities, the protected parts of
    the clone, the task's files with the handoff, your replies and the review folder, and its
    secrets), its secrets are the server's password alone (the provider here takes no key and
    Serena none), and its environment has the variables of the image and those vivibox gives
    a writer's container, by name, and no other: Serena's are its entry's, not the
    container's."""
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
    names = sorted(variable.split("=", 1)[0] for variable in inspected(".Config.Env", pod.agent))
    assert names == sorted(set(image.env(pod.image)) | GIVEN), "the image's and vivibox's, each once"
    assert not [name for name in names if "SERENA" in name.upper()]
    assert providers.mounted(writer.written) == [], "the configuration reads no secret"


# A project file of Serena's as a repository commits one, in another shape than Serena writes.
MINIMAL = "project_name: demo\nlanguages:\n- python\n"
# A key of the project file Serena 1.7.0 writes, on a line of its own there.
A_KEY = "ignored_memory_patterns"


def test_a_project_file_your_repository_tracks_is_left_as_it_is(tmp_path_factory):
    """A project that commits .serena/project.yml, in two shapes Serena writes whole again
    when it reads them: a few keys, and the file this Serena writes itself with one key taken
    out (what a project file of another version of Serena is). After Serena started on the
    project and answered the search, the clone is as git has it (nothing changed, nothing
    new, no file beside the tracked one) and the tracked file is the bytes that were committed.

    The control, last: the same Serena as installed, without the script and the home vivibox
    starts it with, takes the project's own .serena and leaves the tracked file changed. So
    the check can fail, and what keeps the file is how vivibox starts Serena."""
    ready()
    with a_started_task(tmp_path_factory.mktemp("tracked"), {".serena/project.yml": MINIMAL}) as started:
        task, pod = started.task, started.pod
        tracked = task.repo / ".serena" / "project.yml"
        assert tracked.read_bytes() == MINIMAL.encode() and git_sees(pod) == "", (
            "the clone as vivibox made it"
        )
        answered(ask(pod, [SEARCH]))
        assert git_sees(pod) == ""
        assert tracked.read_bytes() == MINIMAL.encode(), "the bytes that were committed"
        assert sorted(path.name for path in tracked.parent.iterdir()) == ["project.yml"], "nothing beside it"
        own = pod.exec("cat", OWN_PROJECT_FILE).stdout
        another = "".join(line for line in own.splitlines(keepends=True) if not line.startswith(A_KEY))
        assert own and another != own, "Serena's own file, in its home, and that file without a key"
        tracked.write_text(another)
        committed = bash(
            pod, "git commit -q -a -m 'Another project file of Serena' && git status --porcelain"
        )
        assert committed.returncode == 0 and committed.stdout == "", committed.stderr
        answered(ask(pod, [SEARCH]))
        assert git_sees(pod) == ""
        assert tracked.read_bytes() == another.encode(), "the bytes that were committed"
        installed = ask(pod, [], as_installed=True)
        assert "gave_up" not in installed, f"{installed.get('gave_up')}: {installed['log']}"
        assert " M .serena/project.yml" in git_sees(pod).splitlines(), "as installed, Serena writes it again"
        assert tracked.read_bytes() != another.encode()
