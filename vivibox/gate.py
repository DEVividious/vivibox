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
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import init, repo, toolchain
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
# A commit about the task's own files, not the code: "Record test red evidence", "Tick criteria".
# These files never reach the project's history, so a message about them says nothing there.
TASK_FILES = re.compile(
    r"red\.md|criteria\.md|verify-feedback|question\.md|/task/|\bhandoff\b|red evidence"
    r"|\btick(ed|ing|s)?\b.*\bcriteri|\bcriteri\w*\b.*\btick",
    re.IGNORECASE,
)


class GateError(Exception):
    pass


# A build tool's command in backticks, and a word that says it went through: a criterion that is
# the verification itself, which the orchestrator runs after every turn, whatever the plan says.
BUILD_COMMAND = re.compile(
    r"`(?:\./)?(?:mvnw?|gradlew?|gradle|npm|npx|yarn|pnpm|pytest|uv run pytest|vitest|go test"
    r"|cargo test|make)\b[^`]*`"
)
WENT_THROUGH = re.compile(r"\b(?:pass(?:es|ed)?|green|succeeds?|exits? 0)\b", re.IGNORECASE)


def command_criteria(plan: Plan) -> list[str]:
    """The criteria that name the build command and ask for it to pass. The writer would run the
    whole build itself to tick them; the brief's sentence alone left them in three plans of four."""
    return [c.text for c in plan.criteria if BUILD_COMMAND.search(c.text) and WENT_THROUGH.search(c.text)]


def check_plan(plan: Plan) -> None:
    """What keeps a plan from being accepted, as a GateError; the planner is told the same. How the
    project is built is not the plan's to say: its writer proposes that, for you to accept."""
    if any(c.text == PLACEHOLDER for c in plan.criteria):
        raise GateError("the plan still carries the template's placeholder criterion; replace it")
    if not plan.criteria:
        # Name the heading: the criteria are usually written, just not where this looks for them.
        raise GateError("no '- [ ]' criteria under an 'Acceptance criteria' heading in the plan")
    if named := command_criteria(plan):
        raise GateError(
            "the verification command is not a criterion (the orchestrator runs it after every turn,"
            f" whatever the plan says); drop it from: {named[0][:120]}"
        )


def accept_plan(task: Task) -> Plan:
    """Freezes the plan you accepted and gives the agent a checklist of its criteria to tick."""
    text = task.plan_path.read_text()
    plan = parse_plan(text)
    check_plan(plan)
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
# credential, a full disk. Matched against the end of a failed command's output only: a test that
# checks a refusal on purpose prints the same words, but not as its last words.
ENVIRONMENT = re.compile(
    r"Could not find a valid Docker environment|Cannot connect to the Docker daemon"
    r"|docker: command not found|TESTCONTAINERS.*(?:not found|refused)"
    r"|\b(ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH)\b"
    r"|Could not resolve host|Could not transfer artifact|Could not resolve dependencies"
    r"|status code: (401|403|407)|\b(401 Unauthorized|403 Forbidden|407 Proxy)\b"
    r"|token.{0,40}(expired|invalid)|(expired|invalid).{0,40}token|credentials? (not found|expired|invalid)"
    r"|SSL certificate problem|unable to get local issuer certificate"
    r"|short read: expected \d+ ?bytes"
    r"|Cannot allocate memory|Out of memory|\bOOM\b|Killed process"
    r"|Temporary failure in name resolution|network is unreachable"
    # A tool the fresh clone lacks: nothing the agent commits brings it; an install in front of
    # the command does, or the image.
    r"|(?:^|\b)(?:sh|bash)(?:: \d+)?: [\w./-]+: (?:command )?not found$",
    re.IGNORECASE,
)
# Words no test prints on purpose, matched anywhere in the output: a build tool that runs many
# modules ends with its summary, dozens of lines below the test that could not pull an image.
ENVIRONMENT_ANYWHERE = re.compile(
    r"certificate signed by unknown authority|tls: failed to verify certificate|\bx509: "
    r"|No space left on device"
    # Testcontainers could not pull a test's image; Docker Hub's pull rate limit.
    r"|Can't get Docker image|\btoomanyrequests\b",
    re.IGNORECASE,
)
ENVIRONMENT_TAIL = 60


def environment_problem(output: str) -> str:
    """The line of a failed command's output that says the trouble is outside the code, or "".
    Words only the environment says count anywhere; the rest only at the end."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    tail = lines[-ENVIRONMENT_TAIL:]
    found = next((line for line in tail if ENVIRONMENT.search(line)), "")
    found = found or next((line for line in lines if ENVIRONMENT_ANYWHERE.search(line)), "")
    return found[:200]


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
        if TASK_FILES.search(subject):
            problems.append(
                f"{sha}: the message names the task's files (red.md, criteria.md), not the change"
            )
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
# A test is code in one of these; a fixture, a snapshot or a table under test/ is data it reads.
CODE = (
    ".py", ".go", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".scala",
    ".groovy", ".rb", ".rs", ".cs", ".php", ".swift", ".sh",
)  # fmt: skip


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
    return path.endswith(CODE) and bool(TEST_FILE.search(path))


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
    # The commit the clone stood on: what was verified, and what the review and you are shown.
    commit: str = ""
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
    # The build files at this commit and the commands they name, when the project runs nothing and
    # has not said it never will: a new product's first task made its build. For you to pick.
    build_files: list[tuple[str, str]] = field(default_factory=list)
    # The selection of tests in the command the writer proposed, when it picked some instead of
    # the whole build: the build is not run, and the writer proposes again.
    narrowed: str = ""

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
            and not self.narrowed
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
            "narrowed": self.narrowed,
            "no_red_evidence": len(self.no_red_evidence),
            "removed_tests": len(self.removed_tests),
            "removed": self.removed_tests[:MAX_LISTED],
            "build_files": [list(pair) for pair in self.build_files],
        }


# What picks some tests out of a build: a test class or pattern given to Maven, Gradle, pytest,
# Jest or Vitest, Go, or a test file named as the argument. A whole build names none of these.
SELECTS_TESTS = re.compile(
    r"(?<![\w-])(?:-Dtest=\S+|-Dit\.test=\S+|--tests(?:=|\s+)\S+|-k\s+\S+|-t\s+\S+"
    r"|--testNamePattern(?:=|\s+)\S+|--testPathPattern(?:=|\s+)\S+|-run\s+\S+"
    r"|\S*\.(?:test|spec)\.[cm]?[jt]sx?\b|\S*(?:/|^)test_\w+\.py\b|\S*_test\.py\b"
    r"|unittest\s+(?:-v\s+)?\S*test_\w+)"
)


def narrowed_proposal(command: str) -> str:
    """The selection of tests in a proposed command, or "" when it builds the whole project. A
    writer verified by the tests it wrote alone would pass whatever it broke elsewhere."""
    found = SELECTS_TESTS.search(command)
    return found.group(0) if found else ""


def uncommitted(repo_dir: Path) -> list[str]:
    status = repo.git("status", "--porcelain", "--untracked-files=all", cwd=repo_dir).stdout
    return [line[3:] for line in status.splitlines()]


def masked(text: str, values: list[str]) -> str:
    """The log without the values of passed variables: the agent reads it, and you might paste it."""
    for value in values:
        text = text.replace(value, "***")
    return text


def _after_a_turn(task: Task) -> bool:
    """Whether this verification follows a turn the gate's own feedback sent the agent on. After
    your 'verify again', or your reply (the agent may rightly commit nothing to "it is fixed"),
    whatever the commit is built again: what you fixed was outside it."""
    states = [e["data"] for e in task.events() if e["type"] == "state"]
    if not states or states[-1].get("previous") != str(State.IMPLEMENT):
        return False
    entered = next((s for s in reversed(states[:-1]) if s.get("current") == str(State.IMPLEMENT)), None)
    return entered is not None and entered.get("previous") == str(State.VERIFY)


def _last_build(task: Task) -> dict | None:
    for event in reversed(task.events()):
        if event["type"] == "gate" and event["data"].get("commands") is not None:
            return event["data"]
    return None


def _said_in(log_text: str, command: str) -> str:
    """The output one command left in a verification log."""
    m = re.search(
        rf"^\$ {re.escape(command)}\n(.*?)^\[exit -?\d+( after [^\]]*)?\]$",
        log_text,
        re.MULTILINE | re.DOTALL,
    )
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


# A command runs a file of the repository when it starts with ./ or hands one to a shell.
SHELLS = {"bash", "sh"}


def missing_program(repo_dir: Path, commands: list[str]) -> str:
    """The first file a command runs from the repository that its commits do not have, as the
    reason verification cannot run; "" when every one is there. The build would fail on "not
    found" (a project moved from Maven to Gradle, a wrapper never committed), and an attempt of
    the agent's would go on what the code cannot fix. Programs of the image are not looked for."""
    for command in commands:
        cwd = ""
        for segment in re.split(r"&&|\|\||;|\|", command):
            try:
                words = shlex.split(segment)
            except ValueError:
                break
            if not words:
                continue
            if words[0] == "cd" and len(words) > 1:
                cwd = os.path.normpath(os.path.join(cwd, words[1]))
                continue
            shell = words[0] in SHELLS
            program = words[1] if shell and len(words) > 1 else words[0]
            if not (shell or program.startswith("./")) or program.startswith(("-", "/")):
                continue
            path = os.path.normpath(os.path.join(cwd, program))
            if path.startswith(".."):
                continue
            if repo.git("cat-file", "-e", f"HEAD:{path}", cwd=repo_dir, check=False).returncode != 0:
                return (
                    f"`{command}` runs {path}, which the repository does not have in its commits; "
                    "change the command under e on the project"
                )
    return ""


def run_gate(
    task: Task,
    pod: Pod,
    commands: list[str],
    risky_extra: list[str],
    java: str = "",
    timeout: float = 0,
    no_build: bool = False,
    no_command: str = "",
    narrowed: str = "",
    tools: list[str] | tuple = (),
) -> GateResult:
    """timeout: seconds one command may take; 0 for no limit. no_build: the project has said it
    has nothing to build, so build files it gains are not pointed out. no_command: why there is no
    command when there should be one; the task then waits for you, with no attempt spent.
    narrowed: the selection of tests in the command the writer proposed (narrowed_proposal); the
    build is not run and the writer is told to propose the whole one."""
    began = time.monotonic()
    repo.check_protection(task.repo, task.meta)
    st = task.read_state()
    log = task.meta / "log" / f"verify-{st.iteration}-{time.strftime('%H%M%S')}.log"
    head = repo.git("rev-parse", "HEAD", cwd=task.repo).stdout.strip()
    result = GateResult(log, commit=head)
    commands = init.with_dependencies(task.repo, commands)
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
    elif narrowed:
        result.narrowed = narrowed
        log.write_text(
            f"# commit {head}: the build was not run: the proposed command is narrowed to {narrowed}\n"
        )
    elif not commands and no_command:
        log.write_text(f"# commit {head}: the build was not run: {no_command}\n")
        result.environment = no_command
    elif missing := missing_program(task.repo, commands):
        log.write_text(f"# commit {head}: the build was not run: {missing}\n")
        result.environment = missing
    elif not commands:
        # A project with no build: the criteria and the commits are checked, and no clone or
        # container is made for nothing to run.
        log.write_text(f"# commit {head}: no build to run; the criteria and the commits are checked\n")
        if not no_build:
            # The tree is the commit's: uncommitted files were ruled out above.
            result.build_files = init.candidates(task.repo)
            with log.open("a") as out:
                for command, source in result.build_files:
                    out.write(f"# {source} names `{command}`, and the project runs nothing yet: pick it\n")
    elif not _reuse(task, result, head, commands):
        _build(task, pod, commands, java, head, result, timeout, tools)
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
    took = {"seconds": round(time.monotonic() - began, 1)}
    task.event("gate", iteration=st.iteration, **result.summary(), **built, **reused, **took)
    return result


def _build(
    task: Task,
    pod: Pod,
    commands: list[str],
    java: str,
    head: str,
    result: GateResult,
    timeout: float = 0,
    tools: list[str] | tuple = (),
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
            toolchain.ensure(pod, java, gate=True, tools=tools)
            for command in commands:
                out.write(f"# started {time.strftime('%H:%M:%S')}\n$ {command}\n")
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
                took = round(time.monotonic() - started, 1)
                out.write(f"[exit {code} after {took:g} s]\n\n")
                out.flush()
                result.commands.append(
                    CommandResult(command, ok, took, "" if ok else log_excerpt("".join(said)))
                )
                if not ok:
                    break
            out.write(log_summary(result, commands, result.log.read_text()))
        finally:
            pod.gate_down()


def log_summary(result: GateResult, commands: list[str], text: str) -> str:
    """The end of the log, where a pager opened at the end lands: each command's outcome and time,
    and for one that failed the line its first trouble is on, so it is a jump away."""
    lines = text.splitlines()
    said = ["# summary"]
    ran = {c.command: c for c in result.commands}
    for command in commands:
        if (c := ran.get(command)) is None:
            said.append(f"# {command}: not run")
            continue
        outcome = "ok" if c.ok else "failed"
        line = f"# {command}: {outcome} after {c.seconds:g} s"
        if not c.ok and (at := first_trouble(lines, command)):
            line += f", first trouble at line {at}"
        said.append(line)
    return "\n".join(said) + "\n"


def first_trouble(lines: list[str], command: str) -> int:
    """The 1-based line of the log where the command's output first looks like trouble; 0 when
    nothing in it does."""
    inside = False
    for n, line in enumerate(lines, 1):
        if line == f"$ {command}":
            inside = True
        elif inside and line.startswith("[exit "):
            return 0
        elif inside and TROUBLE.search(line):
            return n
    return 0


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


def why_red(result: GateResult) -> str:
    """Why the verification failed, in a few words for the task's row and the supervisor's log:
    the first two of what went wrong, counted. "" for a result that passed."""
    said = []
    if failed := [c.command for c in result.commands if not c.ok]:
        said.append(f"`{failed[0]}` failed" if len(failed) == 1 else f"{len(failed)} commands failed")
    for items, one, many in (
        (result.missing_criteria, "criterion not met", "criteria not met"),
        (result.commit_problems, "commit problem", "commit problems"),
        (result.uncommitted, "uncommitted file", "uncommitted files"),
        (result.switched_off, "test switched off", "tests switched off"),
        (result.no_red_evidence, "test without red evidence", "tests without red evidence"),
        (result.hidden_characters, "invisible character", "invisible characters"),
    ):
        if items:
            said.append(f"{len(items)} {one if len(items) == 1 else many}")
    if result.narrowed:
        said.append(f"command narrowed to {result.narrowed}")
    return ", ".join(said[:2])


def next_state(result: GateResult, rounds: int, max_rounds: int) -> State:
    """rounds: the fix turns the writer has had; another only while they are under the limit."""
    if result.environment:
        # Nothing the agent could commit would change it: for you, and no round is spent.
        return State.CHECKPOINT_BLOCKED
    if not result.passed:
        return State.IMPLEMENT if rounds < max_rounds else State.CHECKPOINT_BLOCKED
    return State.APPROVAL_RISKY if result.risky else State.CHECKPOINT_FINAL


MAX_LISTED = 20
