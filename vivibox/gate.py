"""Verification gate: what the orchestrator checks itself, whatever the agent reports.

1. The project's verify commands pass, run on a fresh clone of the committed work in a container of
   its own: no build outputs, files or processes the agent left behind, so nothing it did outside
   its commits counts. Uncommitted changes fail the gate; you only ever get the commits.
2. Every acceptance criterion of the plan you accepted is ticked in the agent's handoff/criteria.md.
   The criteria come from the accepted plan, so the agent cannot reword them.
3. New commits follow the commit rules: one short line, no co-author or AI signature.
   Every test file added or changed is named in handoff/red.md: the writer's evidence that it saw
   the test fail first. The gate checks the name is there, not the evidence; you read that.
   Added lines switch no test off (@Disabled, skipITs, it.skip and the like): one that does not run
   checks nothing. They contain no invisible characters either: zero-width, bidirectional controls
   ("Trojan Source") or Unicode tag characters, which hide text from you in a diff but not from a model.
4. Risky files unchanged since your last approval; changes need approval, not another iteration.

A command that fails on something outside the code (no Docker, no network, a credential, a full
disk, or its time limit) is a failure of the environment: the task waits for you and no attempt of
the agent's is spent, because nothing the agent could commit would change it.

Uncommitted files and switched-off tests are checked before anything is built: a build of a tree
that is not what was committed, or whose tests do not run, would prove nothing, so it is not run.
And a turn that committed nothing gets the last build's result again instead of a new build.
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import repo, toolchain
from .plan import CRITERIA_HEADING, HEADING, Plan, checkboxes, parse_plan
from .pod import Pod
from .risky import Approvals, Change
from .states import State
from .task import Task

ACCEPTED_PLAN = "plan.accepted.md"
CRITERIA_FILE = "criteria.md"
PLACEHOLDER = "Replace with an observable outcome you can check"
MAX_SUBJECT = 72
HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
AI_MARKERS = re.compile(r"co-authored-by|generated (with|by)|\bai[- ]generated\b|🤖", re.IGNORECASE)


class GateError(Exception):
    pass


def check_plan(plan: Plan, project_verify: list[str] | tuple = ()) -> None:
    """What keeps a plan from being accepted, as a GateError; the planner is told the same."""
    if any(c.text == PLACEHOLDER for c in plan.criteria):
        raise GateError("the plan still carries the template's placeholder criterion; replace it")
    if not plan.criteria:
        # Name the heading: the criteria are usually written, just not where this looks for them.
        raise GateError("no '- [ ]' criteria under an 'Acceptance criteria' heading in the plan")
    if not project_verify and not plan.verify:
        raise GateError(
            "this project has no command that builds and tests it yet; the plan must set one, "
            'for example verify = ["npm test"] in its header'
        )


def accept_plan(task: Task, project_verify: list[str] | tuple = ()) -> Plan:
    """Freezes the plan you accepted and gives the agent a checklist of its criteria to tick.
    A project with no verify command of its own (a new one) gets it from the plan."""
    text = task.plan_path.read_text()
    plan = parse_plan(text)
    check_plan(plan, project_verify)
    (task.meta / ACCEPTED_PLAN).write_text(text)
    checklist = "".join(f"- [ ] {c.text}\n" for c in plan.criteria)
    (task.meta / "handoff" / CRITERIA_FILE).write_text(
        "# Acceptance criteria\n\nTick an item only when it is met and verified.\n\n" + checklist
    )
    return plan


def _with_criteria(text: str, items: list[str]) -> str:
    """The plan with items added at the end of its criteria section, where a reader expects them."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if (m := HEADING.match(line)) and m.group(1).casefold() == CRITERIA_HEADING.casefold()),
        None,
    )  # fmt: skip
    added = [f"- [ ] {item}" for item in items]
    if start is None:
        return "\n".join([*lines, "", f"## {CRITERIA_HEADING}", "", *added]) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if HEADING.match(lines[i])), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join([*lines[:end], *added, *lines[end:]]) + "\n"


def add_criteria(task: Task, items: list[str]) -> list[str]:
    """Criteria you add after accepting the plan, usually for something found at review. They join
    the frozen plan the gate checks, the plan you read and the agent's checklist, unticked; the
    gate then holds the work to them exactly as to the ones you accepted. Returns what was added."""
    accepted = task.meta / ACCEPTED_PLAN
    if not accepted.exists():
        raise GateError("the plan is not accepted yet; add criteria to the plan itself")
    have = {c.text for c in parse_plan(accepted.read_text()).criteria}
    new: list[str] = []
    for item in items:
        # One line, as the gate compares them, and without a checkbox you may have typed yourself.
        text = " ".join(re.sub(r"^\s*[-*]\s*\[[ xX]\]\s*", "", item).split())
        if not text:
            continue
        if text == PLACEHOLDER or text in have or text in new:
            raise GateError(f"already a criterion: {text}")
        new.append(text)
    if not new:
        return []
    accepted.write_text(_with_criteria(accepted.read_text(), new))
    if task.plan_path.exists():
        task.plan_path.write_text(_with_criteria(task.plan_path.read_text(), new))
    checklist = task.meta / "handoff" / CRITERIA_FILE
    current = checklist.read_text() if checklist.exists() else ""
    checklist.write_text(current.rstrip("\n") + "\n" + "".join(f"- [ ] {t}\n" for t in new))
    # The record that the checklist grew after you accepted it, and by what.
    task.event("criteria_added", criteria=new)
    return new


def missing_criteria(task: Task) -> list[str]:
    accepted = task.meta / ACCEPTED_PLAN
    if not accepted.exists():
        raise GateError("the plan has not been accepted yet")
    required = [c.text for c in parse_plan(accepted.read_text()).criteria]
    reported = task.meta / "handoff" / CRITERIA_FILE
    ticked = set()
    if reported.exists():
        ticked = {c.text for c in checkboxes(reported.read_text()) if c.done}
    return [c for c in required if c not in ticked]


def reworded_criteria(task: Task, missing: list[str]) -> dict[str, str]:
    """Missing criteria that the agent's checklist carries in other words. An agent that reworded
    an item and ticked it would otherwise be told only that it is not ticked, and tick the same
    reworded line again."""
    reported = task.meta / "handoff" / CRITERIA_FILE
    if not reported.exists():
        return {}
    lines = [c.text for c in checkboxes(reported.read_text())]
    found = {}
    for required in missing:
        close = difflib.get_close_matches(required, lines, n=1, cutoff=0.8)
        if close and close[0] != required:
            found[required] = close[0]
    return found


# What a build says when the trouble is outside the code: no Docker, no network or registry, a
# credential, a full disk. Matched against the end of a failed command's output only.
ENVIRONMENT = re.compile(
    r"Could not find a valid Docker environment|Cannot connect to the Docker daemon"
    r"|docker: command not found|TESTCONTAINERS.*(?:not found|refused)"
    r"|\b(ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH)\b"
    r"|Could not resolve host|Could not transfer artifact|Could not resolve dependencies"
    r"|status code: (401|403|407)|\b(401 Unauthorized|403 Forbidden|407 Proxy)\b"
    r"|token.{0,40}(expired|invalid)|(expired|invalid).{0,40}token|credentials? (not found|expired|invalid)"
    r"|SSL certificate problem|unable to get local issuer certificate"
    r"|No space left on device|Cannot allocate memory|Out of memory|\bOOM\b|Killed process"
    r"|Temporary failure in name resolution|network is unreachable",
    re.IGNORECASE,
)
ENVIRONMENT_TAIL = 60


def environment_problem(output: str) -> str:
    """The line at the end of a failed command's output that says the trouble is outside the code,
    or "". Only the end: a test that checks a connection refusal prints the same words on purpose,
    but not as its last words."""
    tail = [line.strip() for line in output.splitlines()[-ENVIRONMENT_TAIL:] if line.strip()]
    return next((line[:200] for line in tail if ENVIRONMENT.search(line)), "")


# Lines of a build log that say what went wrong, in the usual tools' words.
TROUBLE = re.compile(
    r"\[ERROR\]|BUILD FAILURE|FAILED|FAILURE|Tests run:.*(Failures: [1-9]|Errors: [1-9])"
    r"|Exception\b|\berror\b|Error:|permission denied|not found|\bfail(ed|s)?\b",
    re.IGNORECASE,
)


def log_excerpt(text: str, limit: int = 30) -> str:
    """What a failed build said, short enough to read: its trouble lines, or its end when none of
    them looks like trouble. The whole log is a file away."""
    lines = [line.rstrip() for line in text.splitlines()]
    trouble = list(dict.fromkeys(line for line in lines if TROUBLE.search(line)))
    if not trouble:
        return "\n".join(lines[-limit:]).strip()
    shown = trouble[:limit]
    if len(trouble) > limit:
        shown.append(f"… {len(trouble) - limit} more such lines in the full log")
    return "\n".join(shown)


def commit_problems(repo_dir: Path, base: str) -> list[str]:
    log = repo.git("log", "--format=%h%x1f%B%x1e", f"{base}..HEAD", cwd=repo_dir).stdout
    problems = []
    for entry in filter(None, (e.strip("\n") for e in log.split("\x1e"))):
        sha, _, message = entry.partition("\x1f")
        lines = message.strip().splitlines()
        subject = lines[0] if lines else ""
        if len(lines) != 1:
            problems.append(f"{sha}: the message must be a single line")
        if len(subject) > MAX_SUBJECT:
            problems.append(f"{sha}: the message is longer than {MAX_SUBJECT} characters")
        if AI_MARKERS.search(message):
            problems.append(f"{sha}: no co-author or AI signature")
    return problems


def _hidden_in(path: str, number: int, line: str) -> list[str]:
    return [f"{path}:{number} U+{ord(ch):04X}" for ch in dict.fromkeys(HIDDEN.findall(line))]


def added_lines(repo_dir: Path, base: str):
    """(path, line number, text) of each line added since base, committed or not."""
    yield from _diff_lines(repo_dir, base, "+")


def removed_lines(repo_dir: Path, base: str):
    """(path, line number in the old file, text) of each line removed since base."""
    yield from _diff_lines(repo_dir, base, "-")


def _diff_lines(repo_dir: Path, base: str, sign: str):
    diff = repo.git("-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff", "--unified=0", base,
                    cwd=repo_dir).stdout  # fmt: skip
    path, number = "", 0
    group = 2 if sign == "+" else 1
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            number = int(re.match(r"@@ -(\d+)\S* \+(\d+)", line).group(group))
        elif line.startswith(sign) and path and not line.startswith(sign * 3):
            yield path, number, line[1:]
            number += 1


def hidden_characters(repo_dir: Path, base: str) -> list[str]:
    """Invisible characters in lines added since base, committed or not, and in new untracked files."""
    found = []
    for path, number, text in added_lines(repo_dir, base):
        found += _hidden_in(path, number, text)
    untracked = repo.git("ls-files", "-z", "--others", "--exclude-standard", cwd=repo_dir).stdout
    for rel in filter(None, untracked.split("\0")):
        try:
            text = (repo_dir / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            found += _hidden_in(rel, number, line)
    return found


# Ways to switch a test off. An agent whose tests would not run once disabled them and reported
# the task done; a test that does not run checks nothing, so the gate does not count it as passing.
SWITCHED_OFF = re.compile(
    r"@Disabled\b|@Ignore\b"  # JUnit
    r"|<(skipTests|skipITs|maven\.test\.skip)>\s*true"  # Maven, in a pom
    r"|-D(skipTests|skipITs|maven\.test\.skip)\b(?!=false)"  # Maven, on a command line or in .mvn/
    r"|\b(it|test|describe|context)\.(skip|only)\s*\(|\bx(it|test|describe)\s*\("  # Jest, Mocha, Vitest
    r"|@pytest\.mark\.skip\b|@unittest\.skip\b|\bpytest\.skip\s*\("  # Python
    r"|\bt\.Skip(Now|f)?\s*\("  # Go
)
PROSE = (".md", ".txt", ".rst", ".adoc")


def switched_off_tests(repo_dir: Path, base: str) -> list[str]:
    """Added lines that switch a test off, as path:line: text."""
    return [
        f"{path}:{number}: {text.strip()[:120]}"
        for path, number, text in added_lines(repo_dir, base)
        if not path.endswith(PROSE) and SWITCHED_OFF.search(text)
    ]


# Where tests live, by the usual conventions of the languages the image serves.
TEST_FILE = re.compile(
    r"(^|/)(test|tests|__tests__|spec)/"  # a test directory anywhere on the path
    r"|(^|/)test_[^/]*\.py$|_test\.(py|go)$"  # Python, Go
    r"|\.(test|spec)\.[^/]+$"  # Jest, Vitest, Mocha
    r"|(Test|Tests|IT|Spec)\.(java|kt|kts|scala|groovy)$"  # JVM
)
# A test being defined, in those languages' words: what a removed line of a test file loses.
TEST_DEFINITION = re.compile(
    r"^\s*(@Test\b|@ParameterizedTest\b|def test_\w+|func Test\w+"
    r"|(it|test|describe|context)\s*\(|@pytest\.mark\b)"
)


def is_test_file(path: str) -> bool:
    return not path.endswith(PROSE) and bool(TEST_FILE.search(path))


def changed_test_files(repo_dir: Path, base: str) -> list[str]:
    """Test files added or changed in the commits since base."""
    names = repo.git("diff", "--name-only", "--diff-filter=AM", base, "HEAD", cwd=repo_dir).stdout
    return [path for path in names.splitlines() if path and is_test_file(path)]


def red_evidence_missing(task: Task, base: str) -> list[str]:
    """Changed test files that red.md does not name, by path or by file name. The gate holds the
    writer to naming each file; whether the evidence is real is yours to read."""
    red = task.meta / "handoff" / "red.md"
    text = red.read_text() if red.exists() else ""
    return [
        path
        for path in changed_test_files(task.repo, base)
        if path not in text and Path(path).name not in text
    ]


def removed_tests(repo_dir: Path, base: str) -> list[str]:
    """Test definitions removed from test files since base, for you to see at review."""
    return [
        f"{path}: {text.strip()[:120]}"
        for path, _, text in removed_lines(repo_dir, base)
        if is_test_file(path) and TEST_DEFINITION.search(text)
    ]


@dataclass
class CommandResult:
    command: str
    ok: bool
    seconds: float
    # When it failed: the lines of its output that say why, for the agent and for you.
    said: str = ""


@dataclass
class GateResult:
    log: Path
    commands: list[CommandResult] = field(default_factory=list)
    missing_criteria: list[str] = field(default_factory=list)
    commit_problems: list[str] = field(default_factory=list)
    hidden_characters: list[str] = field(default_factory=list)
    switched_off: list[str] = field(default_factory=list)
    uncommitted: list[str] = field(default_factory=list)
    risky: list[Change] = field(default_factory=list)
    # Required criterion -> the line in criteria.md that looks like it reworded.
    reworded: dict[str, str] = field(default_factory=dict)
    # Why the build was not run, when it was not: what made it meaningless.
    build_skipped: str = ""
    # The commit whose build this result repeats, when the turn committed nothing new.
    unchanged: str = ""
    # What outside the code kept a command from succeeding, in the log's own words; "" when the
    # failure is the code's to fix.
    environment: str = ""
    # Test files added or changed that red.md does not name.
    no_red_evidence: list[str] = field(default_factory=list)
    # Test definitions removed since the base commit, as path: line. For you, at review: a
    # refactoring removes tests rightly, and an agent told about it would put them back.
    removed_tests: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Checks the agent can fix. Risky changes are for you to approve, not a failure."""
        return (
            all(c.ok for c in self.commands)
            and not self.missing_criteria
            and not self.commit_problems
            and not self.hidden_characters
            and not self.switched_off
            and not self.uncommitted
            and not self.no_red_evidence
        )

    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "failed_commands": [c.command for c in self.commands if not c.ok],
            "missing_criteria": len(self.missing_criteria),
            "commit_problems": len(self.commit_problems),
            "hidden_characters": len(self.hidden_characters),
            "switched_off_tests": len(self.switched_off),
            "uncommitted": len(self.uncommitted),
            "risky_changes": [c.path for c in self.risky],
            "log": self.log.name,
            "build_skipped": self.build_skipped,
            "environment": self.environment,
            "no_red_evidence": len(self.no_red_evidence),
            "removed_tests": len(self.removed_tests),
            "removed": self.removed_tests[:MAX_LISTED],
        }


def uncommitted(repo_dir: Path) -> list[str]:
    status = repo.git("status", "--porcelain", "--untracked-files=all", cwd=repo_dir).stdout
    return [line[3:] for line in status.splitlines()]


def masked(text: str, values: list[str]) -> str:
    """The log without the values of passed variables: the agent reads it, and you might paste it."""
    for value in values:
        text = text.replace(value, "***")
    return text


def _after_a_turn(task: Task) -> bool:
    """Whether this verification follows a turn of the agent, as against your 'verify again'
    after fixing something outside the code, which must build whatever the commit."""
    for event in reversed(task.events()):
        if event["type"] == "state":
            return event["data"].get("previous") == str(State.IMPLEMENT)
    return False


def _last_build(task: Task) -> dict | None:
    for event in reversed(task.events()):
        if event["type"] == "gate" and event["data"].get("commands") is not None:
            return event["data"]
    return None


def _said_in(log_text: str, command: str) -> str:
    """The output one command left in a verification log."""
    m = re.search(rf"^\$ {re.escape(command)}\n(.*?)^\[exit -?\d+\]$", log_text, re.MULTILINE | re.DOTALL)
    return log_excerpt(m.group(1)) if m else ""


def _reuse(task: Task, result: GateResult, head: str, commands: list[str]) -> bool:
    """The last build's result again, when the agent committed nothing since and the commands are
    the same: the same commit builds the same way, and a build is minutes."""
    last = _last_build(task)
    if not _after_a_turn(task) or not last or last.get("commit") != head or last["commands"] != commands:
        return False
    if last.get("environment"):
        return False  # you fixed something outside the code; the same commit may build now
    log = task.meta / "log" / last["log"]
    if not log.exists():
        return False
    result.log, result.unchanged = log, head
    text = log.read_text()
    failed = set(last.get("failed_commands", []))
    for command in commands:
        ok = command not in failed
        result.commands.append(CommandResult(command, ok, 0.0, "" if ok else _said_in(text, command)))
        if not ok:
            break
    return True


def run_gate(
    task: Task, pod: Pod, commands: list[str], risky_extra: list[str], java: str = "", timeout: float = 0
) -> GateResult:
    """timeout: seconds one command may take; 0 for no limit."""
    repo.check_protection(task.repo, task.meta)
    st = task.read_state()
    log = task.meta / "log" / f"verify-{st.iteration}-{time.strftime('%H%M%S')}.log"
    result = GateResult(log)
    head = repo.git("rev-parse", "HEAD", cwd=task.repo).stdout.strip()
    # Before the build, the two things that would make it meaningless: it would build a tree that
    # is not what was committed, or run a suite with a test switched off.
    result.uncommitted = uncommitted(task.repo)
    result.switched_off = switched_off_tests(task.repo, st.base_commit)
    if result.uncommitted:
        result.build_skipped = "there are uncommitted files, and only commits are verified"
    elif result.switched_off:
        result.build_skipped = "a test is switched off, so the suite would prove nothing"
    if result.build_skipped:
        log.write_text(f"# commit {head}: the build was not run: {result.build_skipped}\n")
    elif not _reuse(task, result, head, commands):
        _build(task, pod, commands, java, head, result, timeout)
    # Before the plan is accepted, the gate still runs the commands: a baseline check of the project.
    accepted = (task.meta / ACCEPTED_PLAN).exists()
    result.missing_criteria = missing_criteria(task) if accepted else ["(the plan is not accepted yet)"]
    result.reworded = reworded_criteria(task, result.missing_criteria) if accepted else {}
    result.commit_problems = commit_problems(task.repo, st.base_commit)
    result.hidden_characters = hidden_characters(task.repo, st.base_commit)
    result.no_red_evidence = red_evidence_missing(task, st.base_commit) if accepted else []
    result.removed_tests = removed_tests(task.repo, st.base_commit)
    result.risky = Approvals(task.meta, task.repo, risky_extra).changes()
    built = {"commit": head, "commands": commands} if not result.build_skipped else {}
    reused = {"reused": result.log.name} if result.unchanged else {}
    task.event("gate", iteration=st.iteration, **result.summary(), **built, **reused)
    return result


def _build(
    task: Task, pod: Pod, commands: list[str], java: str, head: str, result: GateResult, timeout: float = 0
) -> None:
    values = pod.passed_values()
    # Written as it goes, from before the container comes up: the log is what there is to look
    # at while the verification runs, and preparing the clone is the first thing that takes time.
    with result.log.open("w") as out:
        out.write(f"# fresh clone of commit {head}\n\n")
        out.flush()
        pod.gate_up()
        try:
            toolchain.install_declared(pod, gate=True)
            toolchain.ensure(pod, java, gate=True)
            for command in commands:
                out.write(f"$ {command}\n")
                out.flush()
                started = time.monotonic()
                said, code = _stream(pod, command, out, values, timeout)
                if code == "timeout":
                    said.append(f"\n[timeout after {timeout:g} s]")
                    out.write(said[-1] + "\n")
                    ok = False
                    result.environment = f"`{command}` did not finish in {timeout:g} s (verify_timeout)"
                else:
                    ok = code == 0
                    if not ok:
                        result.environment = environment_problem("".join(said))
                    if said and not said[-1].endswith("\n"):
                        out.write("\n")
                out.write(f"[exit {code}]\n\n")
                out.flush()
                took = round(time.monotonic() - started, 1)
                result.commands.append(
                    CommandResult(command, ok, took, "" if ok else log_excerpt("".join(said)))
                )
                if not ok:
                    break
        finally:
            pod.gate_down()


def _stream(pod: Pod, command: str, out, values: list[str], timeout: float) -> tuple[list[str], int | str]:
    """One command, its output into the log as it comes: the lines it said, masked, and its exit
    code, or "timeout" when it ran out of time."""
    said: list[str] = []

    def line(text: str) -> None:
        said.append(masked(text, values))
        out.write(said[-1])
        out.flush()

    try:
        return said, pod.gate_stream("bash", "-c", command, sink=line, timeout=timeout or None)
    except subprocess.TimeoutExpired:
        return said, "timeout"


def next_state(result: GateResult, iteration: int, max_iterations: int) -> State:
    if result.environment:
        # Nothing the agent could commit would change it: for you, and no attempt is spent.
        return State.CHECKPOINT_BLOCKED
    if not result.passed:
        return State.IMPLEMENT if iteration < max_iterations else State.CHECKPOINT_BLOCKED
    return State.APPROVAL_RISKY if result.risky else State.CHECKPOINT_FINAL


MAX_LISTED = 20


def _listed(items: list[str], line: str) -> list[str]:
    shown = [line.format(item) for item in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        shown.append(f"  … and {len(items) - MAX_LISTED} more")
    return shown


def feedback(result: GateResult) -> str:
    """What the agent reads in handoff/ before the next iteration: each failure with the lines
    that say why, so the log is there to consult, not to read through."""
    parts = ["# Verification failed\n"]
    if result.environment:
        parts.append(
            f"- Verification could not run: {result.environment}. That is outside the code; the user"
            " has been told and will run the verification again. Do not change code for it."
        )
    if result.build_skipped:
        parts.append(f"- The build was not run: {result.build_skipped}. Fix that first.")
    if result.unchanged:
        parts.append(
            f"- You committed nothing since the last verification (commit {result.unchanged[:10]}),"
            " so the build was not run again; its result stands:"
        )
    for c in result.commands:
        if not c.ok:
            parts.append(
                f"- Command failed: `{c.command}`. What it said (all of it: /task/handoff/verify.log):"
            )
            parts.append(f"  ```\n{c.said or '(no output)'}\n  ```")
    for c, wrote in result.reworded.items():
        parts.append(
            f"- Criterion reworded, so not counted (you wrote: {wrote}); restore this exact line"
            f" and tick it: {c}"
        )
    parts += [f"- Criterion not ticked: {c}" for c in result.missing_criteria if c not in result.reworded]
    parts += [f"- Commit rule: {p}" for p in result.commit_problems]
    parts += _listed(result.hidden_characters, "- Invisible character, remove it: {}")
    parts += [f"- Test switched off, switch it on again: {t}" for t in result.switched_off]
    if result.switched_off:
        parts.append(
            "  A test that does not run checks nothing. If something outside the code keeps it from"
            " running here (Docker, network, credentials, a service), write that to"
            " /task/handoff/question.md with the error and end the turn."
        )
    parts += _listed(result.uncommitted, "- Not committed (verification uses your commits only): {}")
    parts += _listed(
        result.no_red_evidence,
        "- No red evidence for test file {}: run its new or changed tests before the change that makes"
        " them pass and record the failing assertion in /task/handoff/red.md, naming the file; a line"
        " naming the file and saying why it has no new test counts too",
    )
    return "\n".join(parts) + "\n"


def write_feedback(task: Task, result: GateResult) -> None:
    handoff = task.meta / "handoff"
    (handoff / "verify-feedback.md").write_text(feedback(result))
    shutil.copyfile(result.log, handoff / "verify.log")
