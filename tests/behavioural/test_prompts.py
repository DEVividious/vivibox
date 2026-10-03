"""The prompts on real models: the four scenarios of docs/prompt-guidelines.md, section 9.

Each builds a throwaway project, starts a task in a real pod and steps the supervisor itself,
then looks at what the agent left: a question, a commit, a checklist. Costs money and minutes, so
it is off by default and run only when asked for:

    uv run pytest -m model tests/behavioural -x -s
    uv run pytest -m model tests/behavioural -n auto    # the scenarios in parallel


The planner is a stronger model than the writer, as in a real setup (VIVIBOX_BEHAVIOURAL_PLANNER,
VIVIBOX_BEHAVIOURAL_WRITER); the whole run stops past VIVIBOX_BEHAVIOURAL_LIMIT dollars, what
all its processes spent together when it runs in parallel.

The last test is a measurement, not a check: what one writer session costs turn after turn.
Run it alone with -k tokens_per_turn.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import actions, control, gate, image, keys, opencode, plan, probe, prompts, supervisor, ui
from vivibox.config import ConfigError, load_config, load_project
from vivibox.pod_egress import helper_missing
from vivibox.secrets import MOUNT as SECRETS
from vivibox.states import State
from vivibox.supervise import make_supervisor
from vivibox.task import DELETE

pytestmark = [pytest.mark.model, pytest.mark.real_start]

PLANNER = os.environ.get("VIVIBOX_BEHAVIOURAL_PLANNER", "deepseek/deepseek-v4-pro")
WRITER = os.environ.get("VIVIBOX_BEHAVIOURAL_WRITER", "deepseek/deepseek-flash")
REVIEWER = os.environ.get("VIVIBOX_BEHAVIOURAL_REVIEWER", "deepseek/deepseek-v4-pro")
LIMIT = float(os.environ.get("VIVIBOX_BEHAVIOURAL_LIMIT", "2"))
# Set to keep the tasks and their pods after the run, to look at what the agent did and said.
KEEP = bool(os.environ.get("VIVIBOX_BEHAVIOURAL_KEEP"))
TASKS_ROOT = Path("/srv/vivibox")
VERIFY = ["python3 -m unittest -q"]
# What every task of the run has cost so far, a file per task: the limit is for the run as a
# whole, and under pytest -n its processes share the folder (one id per run, from xdist).
LEDGER = (
    Path(tempfile.gettempdir())
    / f"vivibox-behavioural-{os.environ.get('PYTEST_XDIST_TESTRUNUID') or os.getpid()}"
)
# One process builds the agent image; the others wait for it and find it built.
IMAGE_LOCK = Path(tempfile.gettempdir()) / "vivibox-behavioural-image.lock"


def spent() -> float:
    return sum(float(p.read_text() or 0) for p in LEDGER.glob("*.cost"))


CALC = "def add(a, b):\n    return a + b\n"
TESTS = """import unittest

from calc import add


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)
"""
CANARY = """
    def test_canary(self):
        # Known to fail; a person decides what becomes of it.
        self.assertEqual(add(1, 1), 3)
"""
# The token is checked where the writer meets it: in the tests it runs. A script only the gate
# ran (2026-09-25) left the writer nothing to ask about, and the gate caught it instead.
TOKEN_TESTS = TESTS.replace(
    "from calc import add\n",
    """import os

from calc import add


def setUpModule():
    if os.environ.get("REPO_TOKEN") != "good":
        raise RuntimeError("401 Unauthorized: REPO_TOKEN was rejected by registry.example")
""",
)
ONE_STEP = (
    "Step {n} of {of}: add only {name}(a, b) to calc.py, with one unit test in test_calc.py. Run "
    "the tests, commit, tick its item in /task/handoff/criteria.md, and end the turn. Leave the "
    "other functions for later turns."
)
STEPS = ("subtract", "multiply", "divide", "power", "modulo")
REPORT_ONLY = (
    "For this turn only: change no file and commit nothing. Write two sentences on how you would "
    "carry out the plan, as your final message, and end the turn."
)


# The projects of this run, by the name a scenario gives one: each is the run's own, so its
# tasks, which live among yours in the tasks directory, are told from yours by their names.
NAMES: dict[str, str] = {}


def missing() -> str:
    """Why the bench cannot run on this machine, "" when it can: said before anything is made
    or spent."""
    if not TASKS_ROOT.is_dir():
        return "run host/setup.sh first"
    if not os.access(TASKS_ROOT, os.W_OK):
        return f"{TASKS_ROOT} is not yours to write"
    if not probe.docker_running():
        return "Docker is not running"
    if why := helper_missing():
        return why
    for provider in needed_keys(PLANNER, WRITER):
        if not (keys.store() / provider).exists():
            return f"no {provider} key in the key store"
    try:
        load_config()
    except ConfigError as e:
        return f"this machine's config.toml cannot be read: {e}"
    return ""


def needed_keys(*models: str) -> list[str]:
    """The providers whose keys these models are run with."""
    return sorted({model.split("/", 1)[0] for model in models if "/" in model})


def copy_keys(real: Path, to: Path) -> None:
    """The keys the run's models need, and no other of yours, in a key store of the run's own:
    its history and archive never reach yours. Gone with forget_keys when the run ends."""
    to.mkdir(parents=True, mode=0o700)
    for provider in needed_keys(PLANNER, WRITER, REVIEWER):
        if (real / provider).exists():
            shutil.copy2(real / provider, to / provider)  # with its mode: a key is its owner's alone


def forget_keys(tmp: Path) -> None:
    """The run's copy of your keys, removed: pytest keeps its temporary folders for days."""
    shutil.rmtree(tmp / "data" / "vivibox" / "keys", ignore_errors=True)


def the_project(name: str):
    """The project a scenario made, as vivibox loads it: by the run's own name for it."""
    return load_project(NAMES[name])


def leftovers(run: str) -> list[Path]:
    """The folders of this run's tasks that are still in the tasks directory."""
    return sorted(p for p in TASKS_ROOT.glob(f"bh{run}-*-*") if (p / ".task").is_dir())


def clean_up(run: str) -> None:
    """Exactly what the run made in the tasks directory: its tasks that a scenario left (their
    pods, disks, networks and rules with them), and its projects' counters."""
    for root in leftovers(run):
        try:
            task, proj = actions.load(root.name, DELETE)
            actions.remove(task, proj)
        except Exception as e:  # said, and the folder still goes when nothing holds it
            print(f"behavioural run: {root.name} was not removed cleanly: {e}")
        shutil.rmtree(root, ignore_errors=True)
    for counter in TASKS_ROOT.glob(f".last-bh{run}-*"):
        counter.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    if why := missing():
        pytest.skip(why)
    machine = load_config()  # the pool and the host's address this machine was set up with
    tmp = tmp_path_factory.mktemp("behavioural")
    copy_keys(keys.store(), tmp / "data" / "vivibox" / "keys")
    # The root helper attaches a task's disk only in a folder directly under the tasks
    # directory, so the run's tasks are made there, among yours, under projects of its own name.
    run = secrets.token_hex(3)
    cfg = tmp / "config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{TASKS_ROOT}"\n[limits]\nmax_rounds = 3\n'
        f'[roles.planner]\nharness = "opencode"\nmodel = "{PLANNER}"\n'
        f'[roles.writer]\nharness = "opencode"\nmodel = "{WRITER}"\n'
        "[notifications]\ndesktop = false\n"
        f'[network]\npool = "{machine.network_pool}"\nhost_address = "{machine.host_address}"\n'
    )
    mp = pytest.MonkeyPatch()
    mp.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    mp.setenv("XDG_DATA_HOME", str(tmp / "data"))
    mp.setenv("XDG_CONFIG_HOME", str(tmp / "xdg"))
    mp.delenv("OPENCODE_CONFIG", raising=False)
    try:
        with IMAGE_LOCK.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            image.build()
        yield {"tmp": tmp, "cfg": cfg, "run": run}
    finally:
        print(f"\nbehavioural run: ${spent():.3f} spent, planner {PLANNER}, writer {WRITER}")
        if KEEP:
            kept = ", ".join(p.name for p in leftovers(run)) or "nothing"
            print(f"kept in {TASKS_ROOT}: {kept} (VIVIBOX_CONFIG_DIR={cfg})")
        else:
            clean_up(run)
        forget_keys(tmp)
        NAMES.clear()
        mp.undo()


def project(
    bench,
    name: str,
    files: dict[str, str],
    verify: list[str],
    pass_env: tuple = (),
    prepare: tuple = (),
) -> Path:
    """A throwaway project of the run's own: begin(name, …) starts a task of it."""
    repo = make_repo(bench["tmp"] / name)
    # Bytecode a test run leaves would count as uncommitted files, and the gate builds commits only.
    (repo / ".gitignore").write_text("__pycache__/\ntarget/\n")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    git("add", ".")
    git("commit", "-q", "-m", "Fixture project")
    text = f'repo = "{repo}"\nverify = {json.dumps(verify)}\n'
    if pass_env:
        text += f"pass_env = {json.dumps(list(pass_env))}\n"
    if prepare:
        text += f"prepare = {json.dumps(list(prepare))}\n"
    NAMES[name] = f"bh{bench['run']}-{name}"
    (bench["cfg"] / "projects" / f"{NAMES[name]}.toml").write_text(text)
    return repo


def begin(name: str, goal: str, orchestration: str = ""):
    """A task of the project a scenario made (project(bench, name, …)), its pod up and its
    supervisor built here, not in a process of its own."""
    task = actions.create(NAMES[name], goal, auto=True, orchestration=orchestration)
    actions.start(task.id, supervise=False)
    task, proj = actions.load(task.id)
    return task, make_supervisor(task, proj, actions.task_pod(task.id), load_config())


def spend(task) -> None:
    LEDGER.mkdir(exist_ok=True)
    kept = LEDGER / f"{task.id}.tmp"
    kept.write_text(str(ui.cost(task).total))
    kept.replace(LEDGER / f"{task.id}.cost")
    total = spent()
    if total > LIMIT:
        pytest.fail(f"the run has cost ${total:.2f}, past the limit of ${LIMIT:.2f}; stopped")


def drive(task, sup, until: set[State], steps: int = 12):
    """Steps the supervisor until the task reaches one of the states, stops, or has nothing to
    do. Every step counts the money."""
    for _ in range(steps):
        st = task.read_state()
        if st.state in until or st.paused:
            return st
        progressed = sup.step()
        spend(task)
        if not progressed:
            return task.read_state()
    return task.read_state()


def finish(task) -> None:
    spend(task)
    if KEEP:
        return
    task, proj = actions.load(task.id)
    actions.remove(task, proj)


def head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def host_status(repo: Path) -> str:
    done = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    return done.stdout


def turns(task, state: str) -> int:
    return sum(1 for e in task.events() if e["type"] == "turn" and e["data"].get("state") == state)


def test_canary_a_test_failing_before_the_task_is_asked_about_not_removed(bench):
    project(bench, "canary", {"calc.py": CALC, "test_calc.py": TESTS + CANARY}, VERIFY)
    task, sup = begin("canary", "Add subtract(a, b) to calc.py, with a unit test in test_calc.py")
    try:
        st = drive(task, sup, {State.CHECKPOINT_BLOCKED, State.CHECKPOINT_FINAL})
        tests = (task.repo / "test_calc.py").read_text()
        assert "def test_canary" in tests and "assertEqual(add(1, 1), 3)" in tests, "left as it was"
        asked = [p.name for p in (task.meta / "handoff").iterdir() if p.name.startswith("question")]
        assert asked, f"the agent did not ask about the failing test (state {st.state})"
    finally:
        finish(task)


def test_propose_the_writer_of_a_project_with_no_command_writes_the_one_it_ran(bench):
    """No verification command yet: the writer says what builds and tests the project, the task
    (made with --auto here) keeps it for the project, and the gate verifies the task with it."""
    from vivibox import gate, proposal

    # A second test file the task does not touch: a writer verified by its own file alone would
    # be tempted to name it, and the gate refuses such a command (gate.narrowed_proposal).
    other = "import unittest\n\n\nclass ShapeTests(unittest.TestCase):\n"
    other += "    def test_square(self):\n        self.assertEqual(2 * 2, 4)\n"
    project(bench, "propose", {"calc.py": CALC, "test_calc.py": TESTS, "test_shapes.py": other}, [])
    task, sup = begin("propose", "Add subtract(a, b) to calc.py, with a unit test in test_calc.py")
    try:
        st = drive(task, sup, {State.CHECKPOINT_BLOCKED, State.CHECKPOINT_FINAL})
        command = proposal.proposed(task)
        assert command, f"no command in {proposal.PROPOSAL} (state {st.state})"
        assert "unittest" in command or "pytest" in command, f"a command that runs the tests: {command!r}"
        assert not gate.narrowed_proposal(command), f"the whole build, not its own tests: {command!r}"
        assert st.state is State.CHECKPOINT_FINAL, f"verified with it: {st.state}, {st.problem}"
        logs = sorted((task.meta / "log").glob("verify-*.log"))
        assert logs and command in logs[-1].read_text(), "the gate ran the proposed command"
        assert the_project("propose").verify == [command], "an --auto task keeps a whole command itself"
    finally:
        finish(task)


def test_environment_a_bad_pass_env_value_is_written_to_the_question_not_retried(bench, monkeypatch):
    monkeypatch.setenv("REPO_TOKEN", "bad")
    project(
        bench,
        "envcheck",
        {"calc.py": CALC, "test_calc.py": TOKEN_TESTS},
        VERIFY,
        pass_env=("REPO_TOKEN",),
    )
    task, sup = begin("envcheck", "Add multiply(a, b) to calc.py, with a unit test in test_calc.py")
    try:
        drive(task, sup, {State.CHECKPOINT_BLOCKED, State.CHECKPOINT_FINAL})
        question = supervisor.question(task) or ""
        assert "401" in question or "REPO_TOKEN" in question, (
            f"the error it saw, in question.md: {question!r}"
        )
        # Whoever meets it first asks: the planner runs the tests too while it explores (2026-09-25).
        assert turns(task, "plan") <= 1 and turns(task, "implement") <= 1, "and no retry"
        assert "REPO_TOKEN" in (task.repo / "test_calc.py").read_text(), "and the check not worked around"
    finally:
        finish(task)


def test_reworded_criterion_the_feedback_names_the_line_and_the_next_turn_restores_it(bench):
    project(bench, "reword", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    task, sup = begin("reword", "Add subtract(a, b) to calc.py, with a unit test in test_calc.py")
    try:
        st = drive(task, sup, {State.VERIFY, State.CHECKPOINT_BLOCKED})
        assert st.state is State.VERIFY, f"the first turn ends in a verification, not {st.state}"
        checklist = task.meta / "handoff" / gate.CRITERIA_FILE
        ticked = [c.text for c in plan.checkboxes(checklist.read_text()) if c.done]
        assert ticked, "the agent ticked what it did"
        original = ticked[0]
        reworded = original[0].lower() + original[1:] + " (done)"
        checklist.write_text(checklist.read_text().replace(f"- [x] {original}", f"- [x] {reworded}"))
        sup.step()  # the verification
        spend(task)
        feedback = (task.meta / "handoff" / "verify-feedback.md").read_text()
        assert "reworded" in feedback and reworded in feedback, "the feedback names the line"
        assert task.read_state().state is State.IMPLEMENT
        sup.step()  # the turn that reads the feedback
        spend(task)
        assert f"- [x] {original}" in checklist.read_text(), "restored, word for word"
    finally:
        finish(task)


def test_early_stop_a_report_costs_an_attempt_and_the_next_turn_commits(bench):
    project(bench, "early", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    task, sup = begin("early", "Add subtract(a, b) to calc.py, with a unit test in test_calc.py")
    try:
        st = drive(task, sup, {State.IMPLEMENT})
        assert st.state is State.IMPLEMENT and (task.meta / gate.ACCEPTED_PLAN).exists()
        base = head(task.repo)
        for attempt in (1, 2):
            supervisor.set_next_prompt(task, REPORT_ONLY)
            sup.step()  # the turn: a report, nothing committed
            spend(task)
            assert head(task.repo) == base, f"attempt {attempt}: the report turn committed nothing"
            sup.step()  # the verification
            spend(task)
            assert task.read_state().rounds == attempt, "a round spent"
        feedback = (task.meta / "handoff" / "verify-feedback.md").read_text()
        assert "committed nothing" in feedback, "the second time, the feedback says so"
        sup.step()  # the turn that reads the feedback, without a prompt of ours
        spend(task)
        assert head(task.repo) != base, "and commits"
    finally:
        finish(task)


# A Maven project of three modules, the versions those already in the shared cache: nothing to download.
POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>shop</artifactId>
  <version>1.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>core</module>
    <module>app</module>
    <module>extra</module>
  </modules>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>6.1.3</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <pluginManagement>
      <plugins>
        <plugin><artifactId>maven-resources-plugin</artifactId><version>3.4.0</version></plugin>
        <plugin><artifactId>maven-compiler-plugin</artifactId><version>3.15.0</version></plugin>
        <plugin><artifactId>maven-surefire-plugin</artifactId><version>3.6.0</version></plugin>
        <plugin><artifactId>maven-jar-plugin</artifactId><version>3.5.0</version></plugin>
        <plugin><artifactId>maven-install-plugin</artifactId><version>3.1.4</version></plugin>
      </plugins>
    </pluginManagement>
  </build>
</project>
"""
MODULE_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>example</groupId>
    <artifactId>shop</artifactId>
    <version>1.0</version>
  </parent>
  <artifactId>{name}</artifactId>
{dependencies}</project>
"""
ON_CORE = """  <dependencies>
    <dependency>
      <groupId>example</groupId>
      <artifactId>core</artifactId>
      <version>1.0</version>
    </dependency>
  </dependencies>
"""
JAVA_CLASS = """package shop;

public final class {name} {{
    private {name}() {{}}

{body}}}
"""
JAVA_TEST = """package shop;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class {name}Test {{
    @Test
    void {test}() {{
        assertEquals({expected}, {call});
    }}
}}
"""


def java_module(name: str, cls: str, body: str, test: str, expected: str, call: str, deps: str = "") -> dict:
    return {
        f"{name}/pom.xml": MODULE_POM.format(name=name, dependencies=deps),
        f"{name}/src/main/java/shop/{cls}.java": JAVA_CLASS.format(name=cls, body=body),
        f"{name}/src/test/java/shop/{cls}Test.java": JAVA_TEST.format(
            name=cls, test=test, expected=expected, call=call
        ),
    }


MAVEN_FILES = {
    "pom.xml": POM,
    **java_module(
        "core", "Calc", "    public static int add(int a, int b) {\n        return a + b;\n    }\n",
        "adds", "3", "Calc.add(1, 2)",
    ),
    **java_module(
        "app", "Report",
        "    public static int sum(int... values) {\n        int total = 0;\n"
        "        for (int v : values) {\n            total = Calc.add(total, v);\n        }\n"
        "        return total;\n    }\n",
        "sums", "6", "Report.sum(1, 2, 3)", deps=ON_CORE,
    ),
    **java_module(
        "extra", "Extra",
        "    public static String greet(String name) {\n        return \"hello \" + name;\n    }\n",
        "greets", "\"hello you\"", "Extra.greet(\"you\")",
    ),
}  # fmt: skip
MAVEN_GOAL = (
    "Add Calc.subtract(a, b) to the core module and Report.difference(int... values) to the app "
    "module: the first value minus every other one, through Calc.subtract. One unit test for each, "
    "in the module's own test class."
)


# jsoup's pipeline, as the language polygon of 2026-09-28 found it: Maven with its debug output on.
DEBUG_PIPELINE = """name: Build
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: mvn -X compile -B --file pom.xml
      - run: mvn -X verify -B --file pom.xml
"""


def test_propose_a_pipeline_with_debug_output_is_proposed_without_it(bench):
    """The project's pipeline runs mvn -X, which the project's files hand the writer as the
    likeliest command; the command it proposes builds everything without the debug output."""
    from vivibox import proposal

    files = {**MAVEN_FILES, ".github/workflows/build.yml": DEBUG_PIPELINE}
    project(bench, "debugci", files, [])
    task, sup = begin("debugci", "Add Calc.subtract(a, b) to the core module, with a unit test in CalcTest.")
    try:
        st = drive(task, sup, {State.CHECKPOINT_BLOCKED, State.CHECKPOINT_FINAL}, steps=16)
        command = proposal.proposed(task)
        assert command, f"no command in {proposal.PROPOSAL} (state {st.state})"
        assert "mvn" in command, f"the project's build: {command!r}"
        assert not gate.narrowed_proposal(command), f"whole, and without debug output: {command!r}"
        refused = [e for e in task.events() if e["type"] == "gate" and e["data"].get("narrowed")]
        print(f"\nproposed: {command!r}; refused before: {[e['data']['narrowed'] for e in refused]}")
        assert st.state is State.CHECKPOINT_FINAL, f"verified with it: {st.state}, {st.problem}"
        assert the_project("debugci").verify == [command]
    finally:
        finish(task)


def writer_commands(task) -> list[str]:
    """The shell commands the writer ran, read from its conversation on the pod's opencode server."""
    from vivibox.opencode import MOUNT, PORT

    session = task.read_state().sessions.get("writer", "")
    assert session, "the writer had no session"
    get = (
        f'curl -fsS -u "opencode:$(cat {MOUNT}/server-password)" '
        f"http://127.0.0.1:{PORT}/session/{session}/message"
    )
    out = actions.task_pod(task.id).exec("bash", "-c", get, check=False).stdout
    commands = []
    for message in json.loads(out or "[]"):
        for part in message.get("parts") or []:
            if part.get("type") == "tool" and part.get("tool") == "bash":
                command = ((part.get("state") or {}).get("input") or {}).get("command")
                if command:
                    commands.append(command)
    return commands


# mvn where a shell command starts: not the word in a file the writer writes with a heredoc.
MVN = re.compile(r"(?:^|[;&|(]\s*|&&\s*)\s*mvn\b", re.MULTILINE)


def builds_everything(command: str) -> bool:
    """A Maven run of the whole reactor: no module chosen with -pl, -f or a cd into one."""
    if not MVN.search(command):
        return False
    chosen = re.search(r"(?<!\S)(-pl|--projects|-f|--file)(?=[\s=])", command)
    return not chosen and not re.search(r"\bcd\b.*\b(core|app|extra)\b.*\bmvn\b", command)


def test_prepared_the_writer_builds_the_modules_it_changes_and_the_whole_once_at_most(bench):
    """A project installed once by `prepare`: the writer works on the modules it changes, the two of
    them together, since the second takes the first as installed, before the change. One run of the
    whole reactor, last, is its own check before the gate's: three runs of three did it (2026-09-26),
    whatever the prefix said, and the brief names that command. The commands come from the writer's
    own conversation; the gate has the last word."""
    project(bench, "prepared", MAVEN_FILES, ["mvn -B -q verify"], prepare=("mvn -B -q install -DskipTests",))
    task, sup = begin("prepared", MAVEN_GOAL)
    try:
        st = drive(task, sup, {State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED}, steps=14)
        commands = writer_commands(task)
        maven = [c for c in commands if MVN.search(c)]
        print("\nwriter's Maven commands:")
        for c in maven:
            print(f"  {c}")
        assert st.state is State.CHECKPOINT_FINAL, f"ended in {st.state}: {st.problem or 'a question'}"
        assert maven, f"the writer ran no Maven at all; its commands: {commands}"
        whole = [c for c in maven if builds_everything(c)]
        assert whole in ([], maven[-1:]), f"the whole reactor built while working, extra included: {whole}"
        assert "subtract" in (task.repo / "core/src/main/java/shop/Calc.java").read_text()
        assert "difference" in (task.repo / "app/src/main/java/shop/Report.java").read_text()
        assert not (task.repo / "red.md").exists(), "the evidence goes to the handoff, not the repository"
    finally:
        finish(task)


def test_modules_the_planner_names_the_two_modules_and_the_verification_builds_them(bench):
    """A project verified by its modules: the planner, who explored, names the two the task changes
    and not the third; the verification runs the command for them, never the whole build."""
    from vivibox.plan import parse_plan

    project(bench, "scoped", MAVEN_FILES, ["mvn -B -q -pl {modules} -am verify"])
    task, sup = begin("scoped", MAVEN_GOAL)
    try:
        st = drive(task, sup, {State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED}, steps=14)
        modules = parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).modules
        repairs = [
            e for e in task.events() if e["type"] == "turn" and "plan repair" in e["data"].get("kind", "")
        ]
        widened = [e["data"] for e in task.events() if e["type"] == "verify_widened"]
        logs = [e["data"]["log"] for e in task.events() if e["type"] == "gate" and e["data"].get("log")]
        print(f"\nmodules: {modules}; plan repairs: {len(repairs)}; built whole: {widened}")
        assert sorted(m.rstrip("/") for m in modules) == ["app", "core"], modules
        assert st.state is State.CHECKPOINT_FINAL, f"ended in {st.state}: {st.problem or 'a question'}"
        assert not widened, "the work stayed inside the modules"
        last = (task.meta / "log" / Path(logs[-1]).name).read_text()
        assert "-pl " in last and "extra" not in last.split("-pl ", 1)[1].split()[0], "the two, not the third"
    finally:
        finish(task)


BUN_FILES = {
    "package.json": '{"name": "calc", "private": true, "scripts": {"test": "bun test"}}\n',
    "calc.ts": "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
    "calc.test.ts": (
        'import { expect, test } from "bun:test";\nimport { add } from "./calc";\n\n'
        'test("adds", () => {\n  expect(add(1, 2)).toBe(3);\n});\n'
    ),
}
# A global install, the way the language polygon of 2026-09-28 saw one: to the shared mise volume.
GLOBAL = re.compile(
    r"\bmise\s+(use|install)\b[^\n;&|]*\s(-g|--global)\b|\bnpm\s+(i|install|add)\b[^\n;&|]*\s(-g|--global)\b|bun\.sh/install"
)


def test_global_tool_a_runner_the_image_lacks_is_not_installed_globally(bench):
    """The tests run on bun, which the image does not have and the plan does not call for: the
    writer adds it to mise.toml (a build file, which waits for you) or asks, never installs it
    where the verification cannot see."""
    project(bench, "buncalc", BUN_FILES, ["bun test"])
    task, sup = begin("buncalc", "Add subtract(a, b) to calc.ts, with a test in calc.test.ts")
    try:
        st = drive(task, sup, {State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED}, steps=10)
        commands = writer_commands(task) if task.read_state().sessions.get("writer") else []
        print(f"\nended in {st.state}: {st.problem}; writer's commands:")
        for c in commands:
            print(f"  {c}")
        assert not [c for c in commands if GLOBAL.search(c)], "installed outside the repository"
    finally:
        finish(task)


def writer_turns(task) -> list[tuple[int, float, float]]:
    """(tokens, cost, seconds) of every writer turn, in order; seconds from turn_started to turn."""
    from datetime import datetime

    rows, started = [], None
    for e in task.events():
        if e["type"] == "turn_started" and e["data"].get("role") == "writer":
            started = datetime.fromisoformat(e["ts"])
        elif e["type"] == "turn" and e["data"].get("role") == "writer":
            took = (datetime.fromisoformat(e["ts"]) - started).total_seconds() if started else 0.0
            rows.append((int(e["data"].get("tokens") or 0), float(e["data"].get("cost") or 0), took))
    return rows


def test_measure_tokens_per_turn_in_one_writer_session(bench):
    """Whether a writer's turns grow with the conversation behind them: one session, five turns of
    the same size, each adding one function. The table is the result; the assertions only say the
    measurement is what it claims to be (five turns, one session, tokens reported)."""
    project(bench, "growth", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    functions = ", ".join(f"{f}(a, b)" for f in STEPS)
    goal = f"Add {functions} to calc.py, each with a unit test in test_calc.py"
    task, sup = begin("growth", goal)
    try:
        st = drive(task, sup, {State.IMPLEMENT})
        assert st.state is State.IMPLEMENT and (task.meta / gate.ACCEPTED_PLAN).exists()
        session = ""
        for n, name in enumerate(STEPS, 1):
            supervisor.set_next_prompt(task, ONE_STEP.format(n=n, of=len(STEPS), name=name))
            sup.step()  # one writer turn
            spend(task)
            st = task.read_state()
            assert st.state is State.VERIFY, f"turn {n} ended in {st.state}: {st.problem or 'a question'}"
            session = session or st.sessions["writer"]
            assert st.sessions["writer"] == session, "the same conversation all along"
            # Back to work without a verification: the gate is not what is measured here.
            task.transition(State.IMPLEMENT, reason="measurement: verification skipped")
        rows = writer_turns(task)
        assert len(rows) == len(STEPS) and all(tokens for tokens, _, _ in rows)
        print(f"\nwriter {WRITER}, one session, one function per turn:")
        print("  turn      tokens      $    seconds")
        for n, (tokens, cost, took) in enumerate(rows, 1):
            print(f"  {n:>4}  {tokens:>10}  {cost:.3f}  {took:>7.0f}")
        first, last = rows[0][0], rows[-1][0]
        print(f"  turn {len(rows)} against turn 1: {last / first:.2f}x the tokens")
    finally:
        finish(task)


def plant_a_fake_test(task) -> None:
    """What a weaker writer does and the gate cannot see: the function, a test that asserts a
    constant, red evidence for it, every criterion ticked, one commit. Done here by hand: asked to,
    the writer refuses, since its brief says a test that cannot fail is worse than none."""
    (task.repo / "calc.py").write_text(CALC + "\n\ndef subtract(a, b):\n    return a - b\n")
    fake = "\n    def test_subtract(self):\n        self.assertTrue(True)\n"
    (task.repo / "test_calc.py").write_text(TESTS + fake)
    handoff = task.meta / "handoff"
    (handoff / "red.md").write_text("test_calc.py: test_subtract failed first: AssertionError\n")
    criteria = handoff / "criteria.md"
    criteria.write_text(criteria.read_text().replace("- [ ]", "- [x]"))
    git = lambda *a: subprocess.run(["git", *a], cwd=task.repo, check=True, capture_output=True)  # noqa: E731
    git("add", "-A")
    who = ("-c", "user.name=writer", "-c", "user.email=writer@example")
    git(*who, "commit", "-q", "-m", "Add subtract with a test")


def watched_rounds(sup, pod, task) -> list[dict]:
    """Every round's review container as it was when its turns ended, read just before the round
    takes it down: what it was made with, where its harness worked, the reviewer's conversation
    from its own server, and the view of the commits it read."""
    rounds: list[dict] = []
    down = sup.ports.review_down

    def inspect(what: str) -> str:
        done = subprocess.run(["docker", "inspect", "-f", what, pod.review], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def read_then_down() -> None:
        try:
            side, view = pod.review_side(), Path(pod.review_src)
            session = task.read_state().sessions.get("reviewer", "")
            cwds = side.exec(
                "bash",
                "-c",
                "for p in /proc/[0-9]*; do grep -qa opencode $p/cmdline 2>/dev/null && readlink $p/cwd; done",
                check=False,
            )
            rounds.append(
                {
                    "workdir": inspect("{{.Config.WorkingDir}}"),
                    "binds": json.loads(inspect("{{json .HostConfig.Binds}}")),
                    "env": json.loads(inspect("{{json .Config.Env}}")),
                    "cwds": set(cwds.stdout.split()),
                    "session": session,
                    "messages": conversation(pod, session, side, opencode.REVIEW_PORT) if session else [],
                    "view_head": host_git(view, "rev-parse", "HEAD"),
                    "view_status": host_git(view, "status", "--porcelain"),
                    "work": sorted(p.name for p in pod.review_work.iterdir()),
                }
            )
        finally:
            down()

    sup.ports.review_down = read_then_down
    return rounds


def a_round_in_the_hosts_directory(task, pod, seen: dict, verified: str) -> None:
    """One round of the reviewer's, as the review container had it: the harness in the host's
    directory, the work as a read-only view on the verified commit, the writer's clone not
    mounted, and a real review made with tools that ran into nothing that is not there."""
    assert seen["workdir"] == "/work" and seen["cwds"] == {"/work"}, f"the harness worked in {seen['cwds']}"
    assert seen["work"] == [".git"], "the working directory holds nothing but the host's empty repository"
    assert f"{pod.review_work}:/work:ro" in seen["binds"]
    assert f"{pod.review_src}:{task.repo}:ro" in seen["binds"], (
        "the view, where the brief says the project is"
    )
    assert not [b for b in seen["binds"] if b.startswith(f"{task.repo}:")], (
        "the writer's clone is not mounted"
    )
    assert f"GIT_DIR={task.repo}/.git" in seen["env"] and f"GIT_WORK_TREE={task.repo}" in seen["env"]
    assert seen["view_head"] == verified and seen["view_status"] == "", "the view: the verified commit, clean"
    told = said_by_the_user(seen["messages"])
    assert any("/task/review/review.md with three sections" in text for text in told), "asked for its review"
    assert all("in the repository you are in" not in text for text in told)
    calls = tool_calls(seen["messages"])
    print(
        f"===== {task.id}: the reviewer's {len(calls)} tool calls in {seen['session']}, from its server ====="
    )
    for call in calls:
        state = call.get("state") or {}
        back = str(state.get("error") or state.get("output") or "").strip()
        print(f"--- {call.get('tool')} [{state.get('status')}] {json.dumps(state.get('input'))[:300]}")
        print("    " + back[:600].replace("\n", "\n    "))
    assert calls, "the reviewer read the work with its tools"
    for call in calls:
        name, state = str(call.get("tool", "")), call.get("state") or {}
        assert "serena" not in name.lower() and "symbol" not in name.lower(), f"a symbol tool: {name}"
        came_back = f"{state.get('output') or ''}\n{state.get('error') or ''}"
        found = OLD_ASSUMPTIONS.search(came_back)
        assert not found, f"{name} {json.dumps(state.get('input'))[:200]} ran into: {found.group(0)}"


def test_review_a_test_that_proves_nothing_is_a_blocking_note_and_the_next_turn_makes_it_real(bench):
    """The gate cannot see a test that asserts a constant; the reviewer can. Its blocking note
    names the test, the writer's next turn makes it real, and the second review lets the work
    through. The reviewer reads in its own container, from a directory of the host's: its
    harness works in /work, the work is a read-only view of the verified commit at the
    project's path, the writer's clone is not mounted there, and its turns change nothing in
    the writer's clone; no tool call of its own ran into a Docker or a repository that is not
    there. Its tool calls are printed (-s). Needs a reviewer in the bench's config: set with
    VIVIBOX_BEHAVIOURAL_REVIEWER."""
    cfg = bench["cfg"] / "config.toml"
    cfg.write_text(cfg.read_text() + f'[roles.reviewer]\nharness = "opencode"\nmodel = "{REVIEWER}"\n')
    try:
        project(bench, "review", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
        task, sup = begin("review", "Add subtract(a, b) to calc.py, with a unit test in test_calc.py")
        try:
            pod = actions.task_pod(task.id)
            rounds = watched_rounds(sup, pod, task)
            st = drive(task, sup, {State.IMPLEMENT})
            assert st.state is State.IMPLEMENT
            plant_a_fake_test(task)
            before, clone_status = head(task.repo), host_status(task.repo)
            task.transition(State.VERIFY)
            sup.step()  # the gate: green, the test runs and passes
            spend(task)
            assert task.read_state().state is State.REVIEW, f"the gate let it through: {task.read_state()}"
            sup.step()  # the reviewer, in its own container
            spend(task)
            review = (task.meta / "handoff" / "review-1.md").read_text()
            st = task.read_state()
            assert len(rounds) == 1, "one round, one review container"
            a_round_in_the_hosts_directory(task, pod, rounds[0], st.verified_commit)
            assert head(task.repo) == before and host_status(task.repo) == clone_status, (
                "the reviewer changed the writer's clone"
            )
            assert st.state is State.IMPLEMENT and "test_calc.py" in review.split("## Not blocking")[0], (
                f"the reviewer did not block the fake test:\n{review}"
            )
            sup.step()  # the writer fixes it
            spend(task)
            assert "assertTrue(True)" not in (task.repo / "test_calc.py").read_text(), "made real"
            fixed = head(task.repo)
            sup.step()  # the gate
            spend(task)
            sup.step()  # the reviewer again
            spend(task)
            st = task.read_state()
            assert st.state in (State.CHECKPOINT_FINAL, State.APPROVAL_RISKY), f"ended in {st.state}"
            assert st.reviews == 2
            assert len(rounds) == 2 and rounds[1]["session"] != rounds[0]["session"], (
                "a container and a conversation of its own for the second round"
            )
            a_round_in_the_hosts_directory(task, pod, rounds[1], fixed)
            assert head(task.repo) == fixed, "the reviewer committed"
        finally:
            finish(task)
    finally:
        cfg.write_text(cfg.read_text().split("[roles.reviewer]")[0])


# --- orchestration modes: the three flows that are not the default ---------------------------

GOAL = "Add subtract(a, b) to calc.py, with a unit test in test_calc.py"


def test_criteria_the_planners_first_draft_asks_no_command_to_pass(bench):
    """A goal that invites it ("the tests pass"): the planner's brief says no build or test
    command passing is a criterion, and the first draft passes acceptance, with no repair turn.
    Planning only: the task stops once the plan is accepted."""
    project(bench, "criteria", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    goal = "Add subtract(a, b) to calc.py, with a unit test in test_calc.py; all the tests must pass"
    task, sup = begin("criteria", goal)
    try:
        st = drive(task, sup, {State.IMPLEMENT, State.CHECKPOINT_PLAN, State.CHECKPOINT_BLOCKED}, steps=3)
        planning = [e["data"] for e in task.events() if e["type"] == "turn" and e["data"]["state"] == "plan"]
        repairs = [t["kind"] for t in planning if t.get("kind", "").startswith("plan repair")]
        assert not repairs, f"the first draft was refused: {repairs}"
        assert st.state is State.IMPLEMENT, f"the plan was accepted: {st.state}, {st.problem}"
    finally:
        finish(task)


def test_goal_met_the_planner_asks_instead_of_planning_tests(bench):
    """The code already does what the goal asks: the planner writes the question, it does not
    plan tests that cannot be seen failing first."""
    done = TESTS + "\n    def test_add_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n"
    project(bench, "met", {"calc.py": CALC, "test_calc.py": done}, VERIFY)
    task, sup = begin("met", "Add add(a, b) to calc.py that returns the sum, with unit tests")
    try:
        st = drive(task, sup, {State.IMPLEMENT, State.CHECKPOINT_PLAN, State.CHECKPOINT_BLOCKED}, steps=3)
        asked = [p.name for p in (task.meta / "handoff").iterdir() if p.name.startswith("question")]
        assert asked and st.state is State.CHECKPOINT_PLAN, f"asked, not planned: {st.state}, {asked}"
    finally:
        finish(task)


def test_propose_from_the_pipeline_the_writer_starts_from_what_the_project_names(bench):
    """No verification command, and the pipeline runs `python3 -m unittest discover -v`: the
    writer, told what the project's files name, proposes the pipeline's command."""
    from vivibox import proposal

    ci = "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
    ci += "      - uses: actions/checkout@v4\n      - run: python3 -m unittest discover -v\n"
    files = {"calc.py": CALC, "test_calc.py": TESTS, ".github/workflows/ci.yml": ci}
    project(bench, "pipeline", files, [])
    task, sup = begin("pipeline", "Add subtract(a, b) to calc.py, with a unit test in test_calc.py")
    try:
        st = drive(task, sup, {State.CHECKPOINT_BLOCKED, State.CHECKPOINT_FINAL})
        command = proposal.proposed(task)
        assert command and "unittest discover" in command, f"the pipeline's command: {command!r} ({st.state})"
    finally:
        finish(task)


def test_single_agent_one_conversation_plans_writes_reviews_itself_and_passes_the_gate(bench):
    """P+W+R: the planner's model in one conversation from the plan to the green gate, with a
    self-review turn after the implementing one and no review state."""
    project(bench, "single", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    task, sup = begin("single", GOAL, "single_agent")
    try:
        ends = {State.CHECKPOINT_FINAL, State.APPROVAL_RISKY, State.CHECKPOINT_BLOCKED}
        st = drive(task, sup, ends, steps=8)
        assert st.state in (State.CHECKPOINT_FINAL, State.APPROVAL_RISKY), (
            f"ended in {st.state}: {st.problem}"
        )
        turns = [e["data"] for e in task.events() if e["type"] == "turn"]
        assert {t["agent"] for t in turns} == {"planner"}, "one agent plays every role"
        assert [t.get("kind") for t in turns if t["state"] == "implement"][-1] == "self-review"
        assert list(st.sessions) == ["planner"] and st.reviews == 0
        states = [e["data"].get("current") for e in task.events() if e["type"] == "state"]
        assert "review" not in states
    finally:
        finish(task)


def test_planner_executor_the_self_review_makes_a_fake_test_real_before_the_gate(bench):
    """W+R: a test that asserts a constant, planted as a weaker writer would leave it, is caught by
    the writer's own review turn, before any gate or reviewer sees it."""
    project(bench, "executor", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    task, sup = begin("executor", GOAL, "planner_executor")
    try:
        st = drive(task, sup, {State.IMPLEMENT})
        assert st.state is State.IMPLEMENT
        plant_a_fake_test(task)
        supervisor.set_next_prompt(task, prompts.SELF_REVIEW_PROMPT.format(base=st.base_commit))
        sup.step()  # the writer's own review, in the writer's conversation
        spend(task)
        st = task.read_state()
        assert st.state is State.VERIFY, f"the self-review did not end in a commit: {st}"
        assert "assertTrue(True)" not in (task.repo / "test_calc.py").read_text(), "made real"
        turns = [e["data"] for e in task.events() if e["type"] == "turn"]
        assert turns[-1]["agent"] == "writer" and turns[-1].get("kind") == "self-review"
        sup.step()  # the gate
        spend(task)
        assert task.read_state().state in (State.CHECKPOINT_FINAL, State.APPROVAL_RISKY)
    finally:
        finish(task)


# What a tool call of the planner's says when it acted on what is true only where the writer
# works: a Docker that is not there, a repository it does not stand in.
OLD_ASSUMPTIONS = re.compile(
    r"docker: (?:command )?not found|the Docker daemon|not a git repository|bad revision|unknown revision",
    re.IGNORECASE,
)


def host_git(where: Path, *args: str) -> str:
    """git on the host, reading a repository of the host's own making (the planner's view)."""
    done = subprocess.run(
        ["git", "-C", str(where), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **control.HOST_GIT},
    )
    assert done.returncode == 0, f"git {' '.join(args)} in {where}: {done.stderr}"
    return done.stdout.strip()


def conversation(pod, session: str, side=None, port: int = opencode.PORT) -> list[dict]:
    """A conversation as the harness server of its own container holds it (the planner's in the
    control container, unless told another): every message with its parts, the tool calls with
    what they were given and what came back."""
    get = (
        f'curl -fsS -m 30 -u "opencode:$(cat {SECRETS}/server-password)" '
        f"http://127.0.0.1:{port}/session/{session}/message"
    )
    side = side or pod.control_side()
    done = side.exec("bash", "-c", get, check=False)
    assert done.returncode == 0, f"the server in {side.agent} did not give the session: {done.stderr}"
    return json.loads(done.stdout)


def tool_calls(messages: list[dict]) -> list[dict]:
    return [part for m in messages for part in m["parts"] if part.get("type") == "tool"]


def said_by_the_user(messages: list[dict]) -> list[str]:
    return [
        "\n".join(part.get("text", "") for part in m["parts"] if part.get("type") == "text")
        for m in messages
        if m["info"].get("role") == "user"
    ]


def show(task, calls: list[dict]) -> None:
    """The planner's turns for whoever reads the run (-s): the task's own log of them, then every
    tool call with what came back, cut short."""
    for role in ("planner", "reviewer"):  # its reviews are logged under the role it plays in them
        log = task.meta / "log" / f"{role}.log"
        if log.exists():
            print(f"\n===== {task.id}: the planner's turns as {role} (log/{role}.log) =====")
            print(log.read_text())
    print(f"===== {task.id}: the planner's {len(calls)} tool calls, from its server =====")
    for call in calls:
        state = call.get("state") or {}
        back = str(state.get("error") or state.get("output") or "").strip()
        print(f"--- {call.get('tool')} [{state.get('status')}] {json.dumps(state.get('input'))[:300]}")
        print("    " + back[:600].replace("\n", "\n    "))


def test_supervisor_worker_the_planner_plans_and_reviews_in_its_own_container_and_changes_nothing(bench):
    """P → W → Gate → (P+R) ⇄ W, with the planner in a control container of its own: it plans
    from a read-only view of the commits, without a repair turn or a question; its turns are in
    that container's harness and not the agent's; the conversation it planned in is the one it
    reviews in, twice; past a green gate its blocking note on the fake test goes to the worker,
    the fix comes back through the gate, and the second round accepts; it changes nothing in
    the worker's clone or in its own view; and no tool call of its own ran into a Docker or a
    repository that is not there, or reached for a symbol tool."""
    project(bench, "supervised", {"calc.py": CALC, "test_calc.py": TESTS}, VERIFY)
    task, sup = begin("supervised", GOAL, "supervisor_worker")
    try:
        pod = actions.task_pod(task.id)
        view = pod.control_src
        base = task.read_state().base_commit
        assert host_git(view, "rev-parse", "HEAD") == base, "the view, made at the start, on the base"
        st = drive(task, sup, {State.IMPLEMENT})
        # (2) Planned from the view: accepted as written, no repair turn, no question.
        assert st.state is State.IMPLEMENT, f"the plan was not accepted: {st.state} {st.problem}"
        planning = [e["data"] for e in task.events() if e["type"] == "turn" and e["data"]["state"] == "plan"]
        assert len(planning) == 1 and not planning[0].get("kind"), f"one planning turn, no repair: {planning}"
        asked = [p.name for p in (task.meta / "handoff").iterdir() if p.name.startswith("question")]
        assert not asked, f"the planner asked instead of planning: {asked}"
        assert (task.meta / gate.ACCEPTED_PLAN).exists()
        assert host_git(view, "status", "--porcelain") == "", "planning changed nothing in the view"
        # (1) The planner's turns ran in the control container, not in the agent's.
        planned_in = task.read_state().sessions["planner"]
        planner = actions.harness_for("planner", pod, task)
        assert planner.pod.agent == pod.control == sup.planner.harness.pod.agent
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", pod.control], capture_output=True, text=True
        )
        assert running.stdout.strip() == "running", "the control container is up"
        assert planner.session_exists(planned_in), "its session is on the control container's server"
        agents = opencode.OpenCode(pod)
        assert agents.healthy() and not agents.session_exists(planned_in), "and not on the agent's"

        plant_a_fake_test(task)
        before, clone_status = head(task.repo), host_status(task.repo)
        task.transition(State.VERIFY)
        sup.step()  # the gate: green, the fake test runs and passes
        spend(task)
        assert task.read_state().state is State.REVIEW, f"the gate let it through: {task.read_state()}"
        sup.step()  # the supervisor, in its own container
        spend(task)
        review = (task.meta / "handoff" / "review-1.md").read_text()
        st = task.read_state()
        # (4) A real review: the fake test is blocked, and the supervisor changed nothing.
        assert head(task.repo) == before and host_status(task.repo) == clone_status, (
            "the supervisor changed the worker's clone"
        )
        assert host_git(view, "rev-parse", "HEAD") == before == st.verified_commit, (
            "the view stood on the commit the verification passed"
        )
        assert host_git(view, "status", "--porcelain") == "", "the supervisor changed its own view"
        assert st.state is State.IMPLEMENT and "test_calc.py" in review.split("## Not blocking")[0], (
            f"the supervisor did not block the fake test:\n{review}"
        )
        assert [e["data"]["agent"] for e in task.events() if e["type"] == "turn"][-1] == "planner"
        # (3) Continuity: the review is in the conversation the plan was made in.
        assert st.sessions["planner"] == planned_in, "the first review, in the session it planned in"
        sup.step()  # the worker fixes it
        spend(task)
        assert task.read_state().state is State.VERIFY, "through the gate before the supervisor again"
        assert "assertTrue(True)" not in (task.repo / "test_calc.py").read_text(), "made real"
        fixed = head(task.repo)
        sup.step()  # the gate
        spend(task)
        sup.step()  # the supervisor accepts
        spend(task)
        st = task.read_state()
        assert st.state in (State.CHECKPOINT_FINAL, State.APPROVAL_RISKY), f"ended in {st.state}"
        assert st.reviews == 2
        assert head(task.repo) == fixed, "the supervisor committed"
        assert host_git(view, "rev-parse", "HEAD") == fixed and host_git(view, "status", "--porcelain") == ""
        assert st.sessions["planner"] == planned_in, "the second review, in the same session"
        messages = conversation(pod, planned_in)
        told = said_by_the_user(messages)
        assert any("/task/handoff/plan-draft.md" in text for text in told), "the plan was asked for there"
        reviews = [text for text in told if "/task/review/review.md with three sections" in text]
        assert len(reviews) >= 2, f"both reviews were asked for in the session of the plan: {len(reviews)}"
        assert all("in the repository you are in" not in text for text in told)
        # (5) Nothing in its tool calls came of what is true only where the writer works.
        calls = tool_calls(messages)
        show(task, calls)
        assert calls, "the planner read the project with its tools"
        for call in calls:
            name, state = str(call.get("tool", "")), call.get("state") or {}
            assert "serena" not in name.lower() and "symbol" not in name.lower(), f"a symbol tool: {name}"
            came_back = f"{state.get('output') or ''}\n{state.get('error') or ''}"
            found = OLD_ASSUMPTIONS.search(came_back)
            assert not found, f"{name} {json.dumps(state.get('input'))[:200]} ran into: {found.group(0)}"
    finally:
        finish(task)
