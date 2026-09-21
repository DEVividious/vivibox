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

    def gate_exec(self, *cmd, check=True):
        assert self.up, "commands run in the gate container"
        self.commands.append(cmd[-1])
        rc = 1 if cmd[-1] in self.fail else 0
        return subprocess.CompletedProcess(cmd, rc, stdout=f"output of {cmd[-1]}\n{self.output}", stderr="")


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
    gate.accept_plan(task, ["true"])
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
    gate.accept_plan(task, ["true"])
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
    gate.accept_plan(task, ["true"])
    result = gate.run_gate(task, FakePod(fail={"lint"}), ["lint", "test"], [])
    assert [c.command for c in result.commands] == ["lint"]
    assert not result.passed
    assert gate.next_state(result, 1, 3) is State.IMPLEMENT
    assert gate.next_state(result, 3, 3) is State.CHECKPOINT_BLOCKED
    gate.write_feedback(task, result)
    text = (task.meta / "handoff" / "verify-feedback.md").read_text()
    assert "Command failed: `lint`" in text and "Criterion not ticked: endpoint returns 200" in text


def test_risky_changes_go_to_approval_not_back_to_the_agent(task):
    gate.accept_plan(task, ["true"])
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "pom.xml").write_text("<project/>")
    git(task.repo, "add", "pom.xml")
    git(task.repo, "commit", "-q", "-m", "Add pom")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert result.passed and [c.path for c in result.risky] == ["pom.xml"]
    assert gate.next_state(result, 1, 3) is State.APPROVAL_RISKY


def test_gate_refuses_tampered_git_files(task):
    gate.accept_plan(task, ["true"])
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
    gate.accept_plan(task, ["true"])
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "Main.java").write_text("x‮y\n")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert not result.passed and result.hidden_characters == ["Main.java:1 U+202E"]
    gate.write_feedback(task, result)
    assert "Invisible character" in (task.meta / "handoff" / "verify-feedback.md").read_text()


def test_gate_before_plan_acceptance_is_a_baseline_check(task):
    result = gate.run_gate(task, FakePod(), ["./gradlew test"], [])
    assert [c.ok for c in result.commands] == [True]
    assert not result.passed and result.missing_criteria == ["(the plan is not accepted yet)"]


def test_uncommitted_changes_fail_the_gate(task):
    gate.accept_plan(task, ["true"])
    tick(task, "endpoint returns 200", "error path is tested")
    (task.repo / "Forgotten.java").write_text("class Forgotten {}\n")
    pod = FakePod()
    result = gate.run_gate(task, pod, ["true"], [])
    assert not result.passed and result.uncommitted == ["Forgotten.java"]
    assert not pod.up, "the gate container is removed after the run"
    assert "Not committed" in gate.feedback(result)


def test_a_plan_must_say_how_to_test_a_project_that_has_no_command(task):
    with pytest.raises(gate.GateError, match="builds and tests it"):
        gate.accept_plan(task)
    text = task.plan_path.read_text().replace("+++\n", '+++\nverify = ["npm test"]\n', 1)
    task.plan_path.write_text(text)
    assert gate.accept_plan(task).verify == ["npm test"]


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
    gate.accept_plan(t, ["true"])
    (t.meta / "handoff" / gate.CRITERIA_FILE).write_text(
        "# Acceptance criteria\n\n"
        "- [x] endpoint returns 200\n"
        "- [x] Every test added was seen failing on its own assertion, not on a missing module,\n"
        "      before the change that makes it pass, with the compared values recorded in\n"
        "      red.md\n"
    )
    assert gate.missing_criteria(t) == [], gate.missing_criteria(t)


def test_the_log_hides_the_values_of_passed_variables(task):
    gate.accept_plan(task, ["true"])
    pod = FakePod(output="GET https://repo/?token=s3cr3t-token 401\n", passed=["s3cr3t-token"])
    result = gate.run_gate(task, pod, ["mvn -B verify"], [])
    log = result.log.read_text()
    assert "s3cr3t-token" not in log and "token=*** 401" in log
