import subprocess
from pathlib import Path

import pytest
from conftest import make_repo

from vivibox import gate, repo
from vivibox.risky import Approvals
from vivibox.states import State
from vivibox.task import create_task

PLAN = """+++
mode = "code-only"
+++

# Goal

## Acceptance criteria

- [ ] endpoint returns 200
- [ ] error path is tested
"""


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def commit(path: Path, message: str) -> None:
    (path / f"f{abs(hash(message))}.txt").write_text(message)
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", message)


@pytest.fixture
def task(tmp_path):
    source = make_repo(tmp_path / "source")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    return t


class FakePod:
    def __init__(self, fail=(), output="", passed=()):
        self.fail = set(fail)
        self.commands = []
        self.output = output
        self.passed = list(passed)

    def passed_values(self):
        return self.passed

    def gate_up(self):
        self.up = True

    def gate_down(self):
        self.up = False

    def gate_exec(self, *cmd, check=True, timeout=None):
        assert self.up, "commands run in the gate container"
        self.commands.append(cmd[-1])
        rc = 1 if cmd[-1] in self.fail else 0
        return subprocess.CompletedProcess(cmd, rc, stdout=f"output of {cmd[-1]}\n{self.output}", stderr="")

    def gate_stream(self, *cmd, sink, workdir="", timeout=None):
        p = self.gate_exec(*cmd, check=False, timeout=timeout)
        for line in p.stdout.splitlines(keepends=True):
            sink(line)
        return p.returncode


def tick(task, *items):
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    text = path.read_text()
    for item in items:
        text = text.replace(f"- [ ] {item}", f"- [x] {item}")
    path.write_text(text)


def test_accept_plan_rejects_template_placeholder(tmp_path):
    t = create_task(tmp_path, "demo", "g", PLAN.replace("endpoint returns 200", gate.PLACEHOLDER))
    with pytest.raises(gate.GateError):
        gate.accept_plan(t)


def test_criteria_come_from_the_accepted_plan(task):
    gate.accept_plan(task)
    assert gate.missing_criteria(task) == ["endpoint returns 200", "error path is tested"]
    tick(task, "endpoint returns 200")
    assert gate.missing_criteria(task) == ["error path is tested"]
    # Rewording a criterion in the checklist does not satisfy it.
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    path.write_text(path.read_text() + "- [x] error path is tested somewhat\n")
    assert gate.missing_criteria(task) == ["error path is tested"]


def test_criteria_need_an_accepted_plan(task):
    with pytest.raises(gate.GateError, match="not been accepted"):
        gate.missing_criteria(task)


@pytest.mark.parametrize(
    "message,problem",
    [
        ("Add health endpoint", None),
        ("Add health endpoint\n\nLonger explanation", "single line"),
        ("Add health endpoint\n\nCo-Authored-By: Claude <noreply@anthropic.com>", "co-author"),
        ("Add endpoint (generated with Claude Code)", "co-author"),
        ("x" * 73, "longer than 72"),
    ],
)
def test_commit_rules(task, message, problem):
    commit(task.repo, message)
    problems = gate.commit_problems(task.repo, task.read_state().base_commit)
    if problem is None:
        assert problems == []
    else:
        assert any(problem in p for p in problems), problems


def test_gate_passes_when_everything_is_done(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Add health endpoint")
    pod = FakePod()
    result = gate.run_gate(task, pod, ["mvn -B verify"], [])
    assert result.passed and not result.risky
    # The toolchain a project declared has to be installed before anything tries to use it: a shim
    # exists only for an installed tool, so a Go project would fail with "go: command not found".
    assert pod.commands == ["mise install --yes", "mvn -B verify"]
    assert "output of mvn -B verify" in result.log.read_text()
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_FINAL
    assert task.events()[-1]["type"] == "gate"


def test_gate_stops_at_first_failing_command_and_writes_feedback(task):
    gate.accept_plan(task)
    result = gate.run_gate(task, FakePod(fail={"lint"}), ["lint", "test"], [])
    assert [c.command for c in result.commands] == ["lint"]
    assert not result.passed
    assert gate.next_state(result, 1, 3) is State.IMPLEMENT
    assert gate.next_state(result, 3, 3) is State.CHECKPOINT_BLOCKED
    gate.write_feedback(task, result)
    text = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "Command failed: `lint`" in text and "Criterion not ticked: endpoint returns 200" in text


def test_risky_changes_go_to_approval_not_back_to_the_agent(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "pom.xml").write_text("<project/>")
    git(task.repo, "add", "pom.xml")
    git(task.repo, "commit", "-q", "-m", "Add pom")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert result.passed and [c.path for c in result.risky] == ["pom.xml"]
    assert gate.next_state(result, 1, 3) is State.APPROVAL_RISKY


def test_gate_refuses_tampered_git_files(task):
    gate.accept_plan(task)
    subprocess.run(["git", "config", "core.fsmonitor", "x"], cwd=task.repo, check=True)
    with pytest.raises(repo.RepoError):
        gate.run_gate(task, FakePod(), ["true"], [])


@pytest.mark.parametrize(
    "text,codepoint",
    [
        ("int access = 0; // ‮ }⁦ if (admin) ⁩ ⁦", "U+202E"),
        ("hello​world", "U+200B"),
        ("note \U000e0049\U000e0047\U000e004e", "U+E0049"),
    ],
)
def test_hidden_characters_in_added_lines(task, text, codepoint):
    (task.repo / "Main.java").write_text(f"class Main {{}}\n{text}\n")
    found = gate.hidden_characters(task.repo, task.read_state().base_commit)
    assert f"Main.java:2 {codepoint}" in found


def test_hidden_characters_in_committed_and_modified_files(task):
    (task.repo / "README.md").write_text("demo\nzero‍width\n")
    commit(task.repo, "Update readme")
    (task.repo / "README.md").write_text("demo\nzero‍width\nplain\n")
    base = task.read_state().base_commit
    assert gate.hidden_characters(task.repo, base) == ["README.md:2 U+200D"]


def test_existing_hidden_characters_are_not_reported(tmp_path):
    source = make_repo(tmp_path / "source")
    (source / "i18n.txt").write_text("family \U0001f468‍\U0001f469\n")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add translations")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    (t.repo / "i18n.txt").write_text("family \U0001f468‍\U0001f469\nplain line\n")
    assert gate.hidden_characters(t.repo, t.read_state().base_commit) == []


def test_hidden_characters_fail_the_gate(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "Main.java").write_text("x‮y\n")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert not result.passed and result.hidden_characters == ["Main.java:1 U+202E"]
    gate.write_feedback(task, result)
    assert "Invisible character" in (task.meta / "handoff" / "verify-feedback.md").read_text()


def test_gate_before_plan_acceptance_is_a_baseline_check(task):
    result = gate.run_gate(task, FakePod(), ["make check"], [])
    assert [c.ok for c in result.commands] == [True]
    assert not result.passed and result.missing_criteria == ["(the plan is not accepted yet)"]


def test_uncommitted_changes_fail_the_gate(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "Forgotten.java").write_text("class Forgotten {}\n")
    pod = FakePod()
    result = gate.run_gate(task, pod, ["true"], [])
    assert not result.passed and result.uncommitted == ["Forgotten.java"]
    assert not getattr(pod, "up", False), "no gate container is left behind"
    assert "Not committed" in gate.feedback(result)


def test_a_plan_is_accepted_without_a_word_on_how_the_project_is_built(task):
    """The writer proposes that, once it has built the project, for you to accept on its own."""
    assert gate.accept_plan(task).verify == []


def test_a_task_its_writer_proposed_no_command_for_waits_for_you_without_a_build(task):
    """The project has no command and the writer wrote none: nothing to verify with. That is no
    fault of the code, so no attempt is spent, and the log says why nothing ran."""
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Add the endpoint")
    pod = FakePod()
    result = gate.run_gate(task, pod, [], [], no_command="the writer proposed no command")
    assert pod.commands == [] and result.environment == "the writer proposed no command"
    assert "the writer proposed no command" in result.log.read_text()
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_BLOCKED


def test_a_criterion_the_agent_wrapped_still_counts_as_ticked(tmp_path):
    """The checklist is the agent's file and it reformats it, so a long criterion comes back
    wrapped. The plan side joins wrapped lines; reading only the first line here compared a whole
    criterion against its opening clause and called it missing, while the agent had ticked every
    one of them and the tests passed."""
    long_one = (
        "Every test added was seen failing on its own assertion, not on a missing module, "
        "before the change that makes it pass, with the compared values recorded in red.md"
    )
    plan = PLAN.replace("- [ ] error path is tested", f"- [ ] {long_one}")
    t = create_task(tmp_path / "tasks", "demo", "goal", plan)
    gate.accept_plan(t)
    (t.meta / "handoff" / gate.CRITERIA_FILE).write_text(
        "# Acceptance criteria\n\n"
        "- [x] endpoint returns 200\n"
        "- [x] Every test added was seen failing on its own assertion, not on a missing module,\n"
        "      before the change that makes it pass, with the compared values recorded in\n"
        "      red.md\n"
    )
    assert gate.missing_criteria(t) == [], gate.missing_criteria(t)


def test_the_log_hides_the_values_of_passed_variables(task):
    gate.accept_plan(task)
    pod = FakePod(output="GET https://repo/?token=s3cr3t-token 401\n", passed=["s3cr3t-token"])
    result = gate.run_gate(task, pod, ["mvn -B verify"], [])
    log = result.log.read_text()
    assert "s3cr3t-token" not in log and "token=*** 401" in log


@pytest.mark.parametrize(
    "path,text",
    [
        ("src/test/java/ShopIT.java", '@Disabled("needs Docker")'),
        ("src/test/java/ShopTest.java", "    @Ignore"),
        ("pom.xml", "<skipITs>true</skipITs>"),
        (".mvn/maven.config", "-DskipITs"),
        ("app.test.js", "it.skip('pays out', () => {"),
        ("app.test.js", "describe.only('shop', () => {"),
        ("test_shop.py", "@pytest.mark.skip(reason='flaky')"),
        ("shop_test.go", '\tt.Skip("later")'),
    ],
)
def test_a_switched_off_test_fails_the_gate(task, path, text):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / path).parent.mkdir(parents=True, exist_ok=True)
    (task.repo / path).write_text(f"{text}\n")
    commit(task.repo, "Add a test")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert not result.passed and result.switched_off == [f"{path}:1: {text.strip()}"]
    gate.write_feedback(task, result)
    feedback = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "Test switched off" in feedback and "question.md" in feedback, "ask instead"


@pytest.mark.parametrize(
    "path,text",
    [
        ("README.md", "Run with -DskipITs to leave out the slow tests."),
        ("pom.xml", "<skipITs>false</skipITs>"),
        (".mvn/maven.config", "-DskipITs=false"),
        ("src/Main.java", "boolean skipped = item.skip(1);"),
    ],
)
def test_what_does_not_switch_a_test_off_passes(task, path, text):
    (task.repo / path).parent.mkdir(parents=True, exist_ok=True)
    (task.repo / path).write_text(f"{text}\n")
    assert gate.switched_off_tests(task.repo, task.read_state().base_commit) == []


def implementing(task):
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(s)


def test_the_feedback_quotes_what_the_build_said(task):
    gate.accept_plan(task)
    noise = "\n".join(f"[INFO] Downloading artifact {i}" for i in range(200))
    said = "[ERROR] ShopIT.pays_out:42 expected 81.2 but was 0\n[INFO] BUILD FAILURE"
    result = gate.run_gate(
        task, FakePod(fail={"mvn -B verify"}, output=f"{noise}\n{said}"), ["mvn -B verify"], []
    )
    text = gate.feedback(result)
    assert "expected 81.2 but was 0" in text and "BUILD FAILURE" in text, "the lines that say why"
    assert "Downloading artifact" not in text, "not the whole log"
    assert "/task/handoff/verify.log" in text


def test_a_reworded_criterion_is_named_in_the_feedback(task):
    gate.accept_plan(task)
    path = task.meta / "handoff" / gate.CRITERIA_FILE
    path.write_text(path.read_text().replace("- [ ] endpoint returns 200", "- [x] endpoint returns HTTP 200"))
    tick(task, "error path is tested")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert result.missing_criteria == ["endpoint returns 200"]
    assert result.reworded == {"endpoint returns 200": "endpoint returns HTTP 200"}
    text = gate.feedback(result)
    assert "reworded" in text and "restore this exact line" in text and "endpoint returns 200" in text
    assert "Criterion not ticked: endpoint returns 200" not in text, "one reason, not two"


def test_the_feedback_lists_at_most_twenty_uncommitted_files(task):
    result = gate.GateResult(Path("/dev/null"), uncommitted=[f"f{i}.txt" for i in range(35)])
    text = gate.feedback(result)
    assert "f19.txt" in text and "f20.txt" not in text and "15 more" in text


def test_nothing_new_committed_reuses_the_last_build(task):
    gate.accept_plan(task)
    implementing(task)
    pod = FakePod(fail={"npm test"}, output="[ERROR] expected 1 but was 2")
    first = gate.run_gate(task, pod, ["npm ci", "npm test"], [])
    assert not first.passed and pod.commands.count("npm test") == 1
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)  # the agent's turn committed nothing
    again = gate.run_gate(task, pod, ["npm ci", "npm test"], [])
    assert pod.commands.count("npm test") == 1, "the same commit builds the same way; not built again"
    assert [(c.command, c.ok) for c in again.commands] == [("npm ci", True), ("npm test", False)]
    assert again.log == first.log
    text = gate.feedback(again)
    assert "committed nothing" in text and "expected 1 but was 2" in text, "told, with the old result"
    assert task.events()[-1]["data"]["reused"] == first.log.name


def test_a_new_commit_or_a_changed_command_builds_again(task):
    gate.accept_plan(task)
    implementing(task)
    pod = FakePod(fail={"npm test"})
    gate.run_gate(task, pod, ["npm test"], [])
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    gate.run_gate(task, pod, ["npm run check"], [])
    assert pod.commands.count("npm run check") == 1, "a changed command is a different build"
    task.transition(State.IMPLEMENT)
    commit(task.repo, "Fix the test")
    task.transition(State.VERIFY)
    gate.run_gate(task, pod, ["npm run check"], [])
    assert pod.commands.count("npm run check") == 2


def test_verifying_again_after_you_fixed_something_builds_again(task):
    gate.accept_plan(task)
    implementing(task)
    pod = FakePod(fail={"npm test"})
    gate.run_gate(task, pod, ["npm test"], [])
    task.transition(State.CHECKPOINT_BLOCKED)
    task.transition(State.VERIFY, reason="verify again")
    gate.run_gate(task, pod, ["npm test"], [])
    assert pod.commands.count("npm test") == 2, "you pressed g because something outside the code changed"


@pytest.mark.parametrize("leave", ["uncommitted", "switched off"])
def test_what_makes_a_build_meaningless_is_reported_before_it_is_built(task, leave):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    if leave == "uncommitted":
        (task.repo / "Forgotten.java").write_text("class Forgotten {}\n")
    else:
        (task.repo / "ShopIT.java").write_text('@Disabled("later")\n')
        commit(task.repo, "Add a test")
    pod = FakePod()
    result = gate.run_gate(task, pod, ["mvn -B verify"], [])
    assert not result.passed and "mvn -B verify" not in pod.commands, "the build would prove nothing"
    text = gate.feedback(result)
    assert "build was not run" in text and result.log.exists()
    assert task.events()[-1]["data"]["build_skipped"]


def test_a_log_excerpt_keeps_what_went_wrong():
    log = "\n".join(
        [f"[INFO] Downloading artifact {i}" for i in range(500)]
        + ["[ERROR] ShopIT.pays_out:42 permission denied while trying to connect to the docker API",
           "[ERROR] Tests run: 3, Failures: 0, Errors: 1, Skipped: 0", "[INFO] BUILD FAILURE"]
    )  # fmt: skip
    excerpt = gate.log_excerpt(log)
    assert "permission denied" in excerpt and "BUILD FAILURE" in excerpt and "Downloading" not in excerpt
    assert gate.log_excerpt("a\nb\nc", limit=2) == "b\nc", "no trouble lines: the end of the log"
    many = "\n".join(f"[ERROR] {i}" for i in range(50))
    assert "20 more such lines" in gate.log_excerpt(many, limit=30)


@pytest.mark.parametrize(
    "said",
    [
        "Could not find a valid Docker environment. Please check configuration.",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
        "npm error code ENOTFOUND\nnpm error network request to https://registry.npmjs.org/x failed",
        "[ERROR] Failed to execute goal on project shop: Could not resolve dependencies:"
        " Could not transfer artifact x from/to nexus (https://nexus/): status code: 401",
        "Error: connect ECONNREFUSED 10.0.0.5:5432",
        "curl: (6) Could not resolve host: artifacts.example.com",
        "The security token included in the request is expired",
        "No space left on device",
        "testcontainers cannot pull confluentinc/cp-kafka:7.6.1: Docker reports tls: failed to verify"
        " certificate: x509: certificate signed by unknown authority",
        "testcontainers cannot pull confluentinc/cp-kafka:7.6.1: Docker reports short read: expected"
        " 115204269 bytes but got 0: unexpected EOF",
    ],
)
def test_a_failure_of_the_environment_is_told_from_one_of_the_code(task, said):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Add health endpoint")
    result = gate.run_gate(task, FakePod(fail={"mvn -B verify"}, output=said), ["mvn -B verify"], [])
    assert result.environment, "outside the code"
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_BLOCKED, "no attempt of the agent's is spent"
    assert "Verification could not run" in gate.feedback(result)


def test_a_failing_test_is_a_failure_of_the_code(task):
    gate.accept_plan(task)
    said = (
        "[ERROR] ShopIT.pays_out:42 expected 81.2 but was 0\n"
        "AssertionError: connection was refused by the stub\n[INFO] BUILD FAILURE"
    )
    result = gate.run_gate(task, FakePod(fail={"mvn -B verify"}, output=said), ["mvn -B verify"], [])
    assert result.environment == "" and gate.next_state(result, 1, 3) is State.IMPLEMENT


class SlowPod(FakePod):
    """A command that never ends: the runner gives up at the time limit."""

    def gate_stream(self, *cmd, sink, workdir="", timeout=None):
        if cmd[-1] == "npm test" and timeout:
            assert self.up
            self.commands.append(cmd[-1])
            sink("[INFO] waiting for Docker…\n")
            raise subprocess.TimeoutExpired(cmd, timeout)
        return super().gate_stream(*cmd, sink=sink, workdir=workdir, timeout=timeout)


def test_a_command_that_does_not_finish_in_time_is_a_failure_of_the_environment(task):
    gate.accept_plan(task)
    result = gate.run_gate(task, SlowPod(), ["npm test"], [], timeout=5)
    assert [c.ok for c in result.commands] == [False] and "5 s" in result.environment
    log = result.log.read_text()
    assert "waiting for Docker" in log and "[timeout after 5 s]" in log, (
        "what it said so far, then why it stopped"
    )


class WatchedPod(FakePod):
    """Reads the log the way w does: while the command runs, not once it is over."""

    def __init__(self, task):
        super().__init__()
        self.task = task
        self.seen_at_gate_up = None
        self.seen_mid_command = ""

    def log(self):
        logs = list((self.task.meta / "log").glob("verify-*.log"))
        return logs[0].read_text() if logs else None

    def gate_up(self):
        self.seen_at_gate_up = self.log()
        super().gate_up()

    def gate_stream(self, *cmd, sink, workdir="", timeout=None):
        assert self.up
        self.commands.append(cmd[-1])
        sink("[INFO] Compiling 12 files\n")
        self.seen_mid_command = self.log()
        sink("[INFO] BUILD SUCCESS\n")
        return 0


def test_the_log_is_written_while_the_command_runs(task):
    """What there is to look at during a verification is its log, so a line the build printed
    is in the log before the build is over, not once the command has returned."""
    gate.accept_plan(task)
    pod = WatchedPod(task)
    result = gate.run_gate(task, pod, ["mvn -B verify"], [])
    assert pod.seen_mid_command.endswith("$ mvn -B verify\n[INFO] Compiling 12 files\n"), pod.seen_mid_command
    text = result.log.read_text()
    assert (
        "# started " in text
        and "\n$ mvn -B verify\n[INFO] Compiling 12 files\n[INFO] BUILD SUCCESS\n[exit 0 after " in text
    )
    assert text.endswith("# summary\n# mvn -B verify: ok after 0 s\n"), text


def test_the_log_is_there_before_the_gate_container_comes_up(task):
    """Preparing the fresh clone is the first thing that takes time; a log that only appears
    after it would leave nothing to look at for as long."""
    gate.accept_plan(task)
    pod = WatchedPod(task)
    gate.run_gate(task, pod, ["true"], [])
    assert pod.seen_at_gate_up is not None and pod.seen_at_gate_up.startswith("# fresh clone of commit ")


def test_a_failure_of_the_environment_is_not_reused(task):
    """You replied that Docker works again and the agent, rightly, committed nothing: the build
    must run, not repeat the failure."""
    gate.accept_plan(task)
    implementing(task)
    pod = FakePod(fail={"npm test"}, output="Cannot connect to the Docker daemon")
    gate.run_gate(task, pod, ["npm test"], [])
    task.transition(State.CHECKPOINT_BLOCKED)
    task.transition(State.IMPLEMENT)
    task.transition(State.VERIFY)
    gate.run_gate(task, pod, ["npm test"], [])
    assert pod.commands.count("npm test") == 2


def test_a_test_file_changed_without_red_evidence_fails_the_gate(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "test").mkdir()
    (task.repo / "test" / "math.test.js").write_text('test("adds", () => expect(1).toBe(1));\n')
    (task.repo / "src.js").write_text("x\n")
    commit(task.repo, "Add a test")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert result.no_red_evidence == ["test/math.test.js"] and not result.passed
    text = gate.feedback(result)
    assert "red.md" in text and "test/math.test.js" in text
    (task.meta / "handoff" / "red.md").write_text("## math.test.js > adds\nexpected 1, got undefined\n")
    assert gate.run_gate(task, FakePod(), ["true"], []).passed, "named by its file name is enough"


def test_red_evidence_is_asked_only_for_test_files(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "src.js").write_text("x\n")
    commit(task.repo, "Add code")
    assert gate.run_gate(task, FakePod(), ["true"], []).passed


@pytest.mark.parametrize(
    "path,is_test",
    [
        ("test/math.test.js", True),
        ("src/__tests__/app.spec.tsx", True),
        ("tests/test_shop.py", True),
        ("shop_test.go", True),
        ("src/test/java/ShopIT.java", True),
        ("src/test/kotlin/ShopSpec.kt", True),
        ("src/main/java/Shop.java", False),
        ("docs/testing.md", False),
        ("contest/entry.js", False),
        ("test/fixtures/bootstrap.json", False),  # data a test reads, not a test
        ("tests/data/players.csv", False),
        ("test/__snapshots__/app.snap", False),
        ("spec/models/shop_spec.rb", True),
    ],
)
def test_what_counts_as_a_test_file(path, is_test):
    assert gate.is_test_file(path) is is_test


def test_removed_tests_are_counted_for_you(tmp_path):
    from conftest import make_repo

    source = make_repo(tmp_path / "source")
    (source / "test").mkdir()
    (source / "test" / "math.test.js").write_text(
        'import { test } from "vitest";\ntest("adds", () => {});\ntest("subtracts", () => {});\n'
    )
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add tests")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    gate.accept_plan(t)
    tick(t, "endpoint returns 200", "error path is tested")
    (t.repo / "test" / "math.test.js").write_text('import { test } from "vitest";\ntest("adds", () => {});\n')
    git(t.repo, "commit", "-qam", "Drop a test")
    (t.meta / "handoff" / "red.md").write_text(
        "math.test.js: the subtracts test went with the feature; no new test\n"
    )
    result = gate.run_gate(t, FakePod(), ["true"], [])
    assert result.removed_tests == ['test/math.test.js: test("subtracts", () => {});']
    assert result.passed, "for you to see at review, not a failure"
    assert t.events()[-1]["data"]["removed_tests"] == 1
    assert "subtracts" not in gate.feedback(result), "not for the agent, which would put it back"


def test_a_certificate_failure_deep_in_a_maven_log_is_of_the_environment(task):
    """Maven ends a failed build with its reactor summary, dozens of lines below the test that
    could not pull an image: the words that say why are nowhere near the end."""
    gate.accept_plan(task)
    x509 = (
        "Caused by: com.github.dockerjava.api.exception.DockerClientException: Could not pull image:"
        " tls: failed to verify certificate: x509: certificate signed by unknown authority"
    )
    summary = "\n".join(f"[INFO] module-{i} ........... SUCCESS [  0.1 s]" for i in range(80))
    said = f"[ERROR] ReportsIT.starts:12\n{x509}\n[INFO] Reactor Summary:\n{summary}\n[INFO] BUILD FAILURE"
    result = gate.run_gate(task, FakePod(fail={"mvn -B verify"}, output=said), ["mvn -B verify"], [])
    assert "x509" in result.environment, "outside the code, wherever in the output it says so"
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_BLOCKED


def test_a_refused_connection_far_from_the_end_is_still_of_the_code(task):
    """A test that checks a refusal on purpose prints the same words; only as the build's last
    words do they mean the environment."""
    gate.accept_plan(task)
    lines = "\n".join(f"[INFO] line {i}" for i in range(80))
    said = f"Error: connect ECONNREFUSED 127.0.0.1:1\n{lines}\n[INFO] BUILD FAILURE"
    result = gate.run_gate(task, FakePod(fail={"npm test"}, output=said), ["npm test"], [])
    assert result.environment == ""


def test_your_reply_builds_the_same_commit_again(task):
    """The verification failed on something the gate took for the code, the agent asked, and you
    replied that it is fixed. The agent, rightly, committed nothing: the build must run, not
    repeat the old failure to the agent, which would only ask again."""
    gate.accept_plan(task)
    implementing(task)
    pod = FakePod(fail={"npm test"}, output="[ERROR] expected 1 but was 2")
    gate.run_gate(task, pod, ["npm test"], [])
    task.transition(State.CHECKPOINT_BLOCKED, reason="question from the agent")
    task.transition(State.IMPLEMENT, reason="your reply")
    task.transition(State.VERIFY)
    again = gate.run_gate(task, pod, ["npm test"], [])
    assert pod.commands.count("npm test") == 2 and not again.unchanged


def test_a_plan_may_say_there_is_nothing_to_build(task):
    from vivibox.plan import parse_plan

    criteria = "\n\n## Acceptance criteria\n\n- [ ] x\n"
    assert parse_plan(f"+++\nverify = false\n+++{criteria}").no_build
    gate.check_plan(parse_plan(f"+++\nverify = false\n+++{criteria}"))


def test_a_project_with_no_build_is_verified_without_the_pod(task):
    """The criteria, the commits and the rest are checked as ever; there is just nothing to run,
    so no clone and no container for it."""
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Write the handbook")
    pod = FakePod()
    result = gate.run_gate(task, pod, [], [], no_build=True)
    assert result.passed and pod.commands == [] and not hasattr(pod, "up")
    assert result.build_files == [], "a project that says it has no build is not told about one"
    assert "no build" in result.log.read_text()
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_FINAL


def test_a_build_file_the_task_adds_is_pointed_out_when_the_project_runs_nothing(task):
    """A new product's first plan finds an empty repository and says verify = false; the writer
    then makes the build. The gate runs none of it, and says so, for you to pick the command."""
    task.plan_path.write_text(task.plan_path.read_text().replace("+++\n", "+++\nverify = false\n", 1))
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    commit(task.repo, "Scaffold the app")
    result = gate.run_gate(task, FakePod(), [], [])
    assert result.passed, "a hint, not a failure"
    assert result.build_files == [("npm ci && npm test", "package.json")]
    assert "package.json" in result.log.read_text() and "npm ci && npm test" in result.log.read_text()
    assert task.events()[-1]["data"]["build_files"] == [["npm ci && npm test", "package.json"]]
    assert gate.run_gate(task, FakePod(), [], [], no_build=True).build_files == []


def ran(pod: FakePod) -> list[str]:
    """The verification's commands, without the toolchain's own mise call."""
    return [c for c in pod.commands if not c.startswith("mise ")]


def test_a_node_projects_dependencies_are_installed_on_the_fresh_clone_first(task):
    """A plan says verify = ["npm test"]; on a fresh clone there is no node_modules, and every
    tool it needs is "not found". The gate installs first, the way the lockfile says, as a
    command of its own in the log; a command that installs already is left alone."""
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "package.json").write_text('{"scripts": {"test": "tsc --noEmit && vitest run"}}')
    (task.repo / "package-lock.json").write_text("{}")
    commit(task.repo, "Scaffold the app")
    pod = FakePod()
    result = gate.run_gate(task, pod, ["npm test"], [])
    assert ran(pod) == ["npm ci", "npm test"]
    assert [c.command for c in result.commands] == ["npm ci", "npm test"] and result.passed
    assert "$ npm ci" in result.log.read_text()
    installs = ["npm ci && npm test"], ["npm install", "npm test"], ["yarn install --immutable", "yarn test"]
    for own in installs:
        pod = FakePod()
        gate.run_gate(task, pod, own, [])
        assert ran(pod) == own, "installs itself: nothing added"
    (task.repo / "package-lock.json").unlink()
    (task.repo / "yarn.lock").write_text("")
    (task.repo / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
    commit(task.repo, "Switch to yarn")
    pod = FakePod()
    gate.run_gate(task, pod, ["yarn test"], [])
    assert ran(pod) == ["yarn install --immutable", "yarn test"], "by the lockfile"


def test_a_project_without_package_json_gets_no_install(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Add code")
    pod = FakePod()
    gate.run_gate(task, pod, ["mvn -B verify"], [])
    assert ran(pod) == ["mvn -B verify"]


def test_the_feedback_is_markdown_that_keeps_its_list_out_of_the_code_block():
    """The command's output is a code block inside the first list item; its lines have to be
    indented like the item, or the parser ends the list there and the next fence swallows every
    line that follows, unwrapped and cut off in the panel."""
    from markdown_it import MarkdownIt

    result = gate.GateResult(Path("/dev/null"))
    said = "sh: 1: tsc: not found\n> tsc && vitest run"
    result.commands = [gate.CommandResult("npm test", False, 0.1, said)]
    result.no_red_evidence = ["test/a.test.ts", "test/b.test.ts"]
    tokens = MarkdownIt().parse(gate.feedback(result))
    fences = [t.content for t in tokens if t.type == "fence"]
    assert len(fences) == 1 and "tsc: not found" in fences[0] and "No red evidence" not in fences[0]
    assert sum(t.type == "list_item_open" for t in tokens) == 3, "the command and the two files"


def test_the_log_ends_with_a_summary_that_names_the_first_trouble_line(task):
    """A pager opened at the end lands on the summary: each command's outcome and time, and for
    the one that failed the line of its first trouble, a jump away."""
    gate.accept_plan(task)
    pod = FakePod(fail={"npm test"}, output="ok\nFAIL src/a.test.js\n  Error: boom\n")
    result = gate.run_gate(task, pod, ["npm run lint", "npm test", "npm run e2e"], [])
    text = result.log.read_text()
    lines = text.splitlines()
    # The lint printed the same lines and passed: the trouble named is the failed command's own.
    at = lines.index("FAIL src/a.test.js", lines.index("$ npm test")) + 1
    assert text.endswith(
        "# summary\n# npm run lint: ok after 0 s\n# npm test: failed after 0 s, first trouble at line "
        f"{at}\n# npm run e2e: not run\n"
    )
    assert gate.first_trouble(["$ x", "fine", "[exit 0 after 1 s]"], "x") == 0


def test_an_image_testcontainers_cannot_get_deep_in_a_maven_log_is_of_the_environment(task):
    """Testcontainers could not pull the image a test needs (a registry the network does not reach,
    a rate limit): nothing the agent commits changes that, and in a many-module build the line is
    far from the end. Seen on a work laptop with quay.io/minio/minio."""
    gate.accept_plan(task)
    pulled = (
        "Caused by: org.testcontainers.containers.ContainerFetchException: Can't get Docker image:"
        " RemoteDockerImage(imageName=quay.io/minio/minio:latest, imagePullPolicy=DefaultPullPolicy())"
    )
    summary = "\n".join(f"[INFO] module-{i} ........... SUCCESS [  0.1 s]" for i in range(80))
    said = f"[ERROR] StorageIT.puts:20\n{pulled}\n[INFO] Reactor Summary:\n{summary}\n[INFO] BUILD FAILURE"
    result = gate.run_gate(task, FakePod(fail={"mvn -B verify"}, output=said), ["mvn -B verify"], [])
    assert "quay.io/minio/minio" in result.environment
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_BLOCKED, "no attempt of the agent's spent"
    rate = "toomanyrequests: You have reached your pull rate limit."
    result = gate.run_gate(
        task, FakePod(fail={"mvn -B verify"}, output=f"{rate}\n{summary}"), ["mvn -B verify"], []
    )
    assert "pull rate limit" in result.environment


@pytest.mark.parametrize(
    "command,missing",
    [
        ("./mvnw -B verify -f pom.xml", "mvnw"),
        ("bash gradlew test --no-daemon", "gradlew"),
        ("npm ci && sh scripts/check.sh", "scripts/check.sh"),
        ("cd app && ./mvnw verify", "app/mvnw"),
    ],
)
def test_a_command_that_runs_a_file_the_repository_does_not_have_waits_for_you(task, command, missing):
    """The project moved from Maven to Gradle, or the wrapper was never committed: the build would
    fail with "not found", and an attempt would go on something the code cannot fix. Said as it is,
    for you, with no container made for it."""
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Add the endpoint")
    pod = FakePod()
    result = gate.run_gate(task, pod, [command], [])
    assert pod.commands == [], "nothing built"
    assert missing in result.environment and "does not have" in result.environment
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_BLOCKED
    assert missing in result.log.read_text()


def test_a_file_the_command_runs_is_looked_for_where_the_command_runs_it(task):
    (task.repo / "app").mkdir()
    (task.repo / "app" / "mvnw").write_text("#!/bin/sh\n")
    (task.repo / "gradlew").write_text("#!/bin/sh\n")
    commit(task.repo, "Wrappers")
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    for command in ("cd app && ./mvnw verify", "bash gradlew test", "npm test", "mvn -B verify"):
        assert gate.missing_program(task.repo, [command]) == "", command
    (task.repo / "stray").write_text("uncommitted\n")
    assert "stray" in gate.missing_program(task.repo, ["sh stray"]), "only what is committed counts"


@pytest.mark.parametrize(
    "command,selection",
    [
        ("./mvnw -Dtest=PetTests test", "-Dtest=PetTests"),
        ("./mvnw -B -Dit.test=OwnerIT verify", "-Dit.test=OwnerIT"),
        ("bash gradlew test --tests com.acme.PetTest", "--tests com.acme.PetTest"),
        ("uv run pytest -k test_pet", "-k test_pet"),
        ("uv run pytest tests/test_pet.py", "tests/test_pet.py"),
        ("yarn vitest run src/utils/__tests__/format.test.ts", "src/utils/__tests__/format.test.ts"),
        ("npx jest -t 'formats a date'", "-t 'formats"),
        ("go test -run TestPet ./...", "-run TestPet"),
        ("python -m unittest test_calc", "unittest test_calc"),
        ("python -m unittest -v tests.test_calc", "unittest -v tests.test_calc"),
        ("python -m unittest", ""),
        ("python -m unittest discover -s tests", ""),
        ("./mvnw -B verify", ""),
        ("bash gradlew test --no-daemon --console=plain", ""),
        ("cd apps/react-vite && yarn install --frozen-lockfile && yarn vitest run", ""),
        ("uv run pytest -q", ""),
    ],
)
def test_a_command_narrowed_to_some_tests_is_told_from_a_whole_build(command, selection):
    """The writer proposes the command it is verified with; one that picks its own tests would
    pass whatever it broke elsewhere. The gate names the selection, or "" for a whole build."""
    assert gate.narrowed_proposal(command) == selection


def test_a_proposal_narrowed_to_the_writers_tests_is_refused_before_the_build(task):
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    commit(task.repo, "Add the endpoint")
    pod = FakePod()
    result = gate.run_gate(task, pod, ["./mvnw -Dtest=PetTests test"], [], narrowed="-Dtest=PetTests")
    assert ran(pod) == [], "a build with the wrong command proves nothing, and costs minutes"
    assert not result.passed and result.narrowed == "-Dtest=PetTests"
    assert "narrowed" in result.log.read_text() and "-Dtest=PetTests" in result.log.read_text()
    assert task.events()[-1]["data"]["narrowed"] == "-Dtest=PetTests"
    text = gate.feedback(result)
    assert "-Dtest=PetTests" in text and "whole project" in text and "verify-proposal.md" in text
    assert gate.next_state(result, 1, 3) is State.IMPLEMENT, "the writer's to fix: an attempt spent"


def test_a_package_json_without_a_lockfile_gets_no_install(task):
    """A monorepo's root package.json has no lockfile: `npm ci` there fails every time, before the
    command that would have installed the app's dependencies itself."""
    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "package.json").write_text(
        '{"private": true, "scripts": {"prepare": "cd apps/web && yarn"}}'
    )
    commit(task.repo, "Monorepo root")
    own = "cd apps/web && yarn install --frozen-lockfile && yarn vitest run"
    for command in (own, "yarn --cwd apps/web vitest run"):
        pod = FakePod()
        gate.run_gate(task, pod, [command], [])
        assert ran(pod) == [command], "nothing to install by: the command is on its own"
