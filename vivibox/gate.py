"""Verification gate: what the orchestrator checks itself, whatever the agent reports.

1. The project's verify commands pass, run on a fresh clone of the committed work in a container of
   its own: no build outputs, files or processes the agent left behind, so nothing it did outside
   its commits counts. Uncommitted changes fail the gate; you only ever get the commits.
2. Every acceptance criterion of the plan you accepted is ticked in the agent's handoff/criteria.md.
   The criteria come from the accepted plan, so the agent cannot reword them.
3. New commits follow the commit rules: one short line, no co-author or AI signature.
   Added lines contain no invisible characters: zero-width, bidirectional controls ("Trojan Source")
   or Unicode tag characters, which hide text from you in a diff but not from a model.
4. Risky files unchanged since your last approval; changes need approval, not another iteration.
"""

from __future__ import annotations

import re
import shutil
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


def accept_plan(task: Task, project_verify: list[str] | tuple = ()) -> Plan:
    """Freezes the plan you accepted and gives the agent a checklist of its criteria to tick.
    A project with no verify command of its own (a new one) gets it from the plan."""
    text = task.plan_path.read_text()
    plan = parse_plan(text)
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


def hidden_characters(repo_dir: Path, base: str) -> list[str]:
    """Invisible characters in lines added since base, committed or not, and in new untracked files."""
    diff = repo.git("-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff", "--unified=0", base,
                    cwd=repo_dir).stdout  # fmt: skip
    found, path, number = [], "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            number = int(re.match(r"@@ -\S+ \+(\d+)", line).group(1))
        elif line.startswith("+") and path:
            found += _hidden_in(path, number, line[1:])
            number += 1
    untracked = repo.git("ls-files", "-z", "--others", "--exclude-standard", cwd=repo_dir).stdout
    for rel in filter(None, untracked.split("\0")):
        try:
            text = (repo_dir / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            found += _hidden_in(rel, number, line)
    return found


@dataclass
class CommandResult:
    command: str
    ok: bool
    seconds: float


@dataclass
class GateResult:
    log: Path
    commands: list[CommandResult] = field(default_factory=list)
    missing_criteria: list[str] = field(default_factory=list)
    commit_problems: list[str] = field(default_factory=list)
    hidden_characters: list[str] = field(default_factory=list)
    uncommitted: list[str] = field(default_factory=list)
    risky: list[Change] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Checks the agent can fix. Risky changes are for you to approve, not a failure."""
        return (
            all(c.ok for c in self.commands)
            and not self.missing_criteria
            and not self.commit_problems
            and not self.hidden_characters
            and not self.uncommitted
        )

    def summary(self) -> dict:
        return {
            "passed": self.passed,
            "failed_commands": [c.command for c in self.commands if not c.ok],
            "missing_criteria": len(self.missing_criteria),
            "commit_problems": len(self.commit_problems),
            "hidden_characters": len(self.hidden_characters),
            "uncommitted": len(self.uncommitted),
            "risky_changes": [c.path for c in self.risky],
            "log": self.log.name,
        }


def uncommitted(repo_dir: Path) -> list[str]:
    status = repo.git("status", "--porcelain", "--untracked-files=all", cwd=repo_dir).stdout
    return [line[3:] for line in status.splitlines()]


def masked(text: str, values: list[str]) -> str:
    """The log without the values of passed variables: the agent reads it, and you might paste it."""
    for value in values:
        text = text.replace(value, "***")
    return text


def run_gate(task: Task, pod: Pod, commands: list[str], risky_extra: list[str], java: str = "") -> GateResult:
    repo.check_protection(task.repo, task.meta)
    st = task.read_state()
    log = task.meta / "log" / f"verify-{st.iteration}-{time.strftime('%H%M%S')}.log"
    result = GateResult(log)
    pod.gate_up()
    try:
        toolchain.install_declared(pod, gate=True)
        toolchain.ensure(pod, java, gate=True)
        with log.open("w") as out:
            head = repo.git("rev-parse", "HEAD", cwd=task.repo).stdout.strip()
            out.write(f"# fresh clone of commit {head}\n\n")
            for command in commands:
                out.write(f"$ {command}\n")
                out.flush()
                started = time.monotonic()
                p = pod.gate_exec("bash", "-c", command, check=False)
                out.write(masked(p.stdout + p.stderr, pod.passed_values()) + f"\n[exit {p.returncode}]\n\n")
                result.commands.append(
                    CommandResult(command, p.returncode == 0, round(time.monotonic() - started, 1))
                )
                if p.returncode != 0:
                    break
    finally:
        pod.gate_down()
    # Before the plan is accepted, the gate still runs the commands: a baseline check of the project.
    accepted = (task.meta / ACCEPTED_PLAN).exists()
    result.missing_criteria = missing_criteria(task) if accepted else ["(the plan is not accepted yet)"]
    result.commit_problems = commit_problems(task.repo, st.base_commit)
    result.hidden_characters = hidden_characters(task.repo, st.base_commit)
    result.uncommitted = uncommitted(task.repo)
    result.risky = Approvals(task.meta, task.repo, risky_extra).changes()
    task.event("gate", iteration=st.iteration, **result.summary())
    return result


def next_state(result: GateResult, iteration: int, max_iterations: int) -> State:
    if not result.passed:
        return State.IMPLEMENT if iteration < max_iterations else State.CHECKPOINT_BLOCKED
    return State.APPROVAL_RISKY if result.risky else State.CHECKPOINT_FINAL


def feedback(result: GateResult) -> str:
    """What the agent reads in handoff/ before the next iteration."""
    parts = ["# Verification failed\n"]
    for c in result.commands:
        if not c.ok:
            parts.append(f"- Command failed: `{c.command}` (full output: /task/handoff/verify.log)")
    parts += [f"- Criterion not ticked: {c}" for c in result.missing_criteria]
    parts += [f"- Commit rule: {p}" for p in result.commit_problems]
    parts += [f"- Invisible character, remove it: {h}" for h in result.hidden_characters]
    parts += [f"- Not committed (verification uses your commits only): {f}" for f in result.uncommitted]
    return "\n".join(parts) + "\n"


def write_feedback(task: Task, result: GateResult) -> None:
    handoff = task.meta / "handoff"
    (handoff / "verify-feedback.md").write_text(feedback(result))
    shutil.copyfile(result.log, handoff / "verify.log")
