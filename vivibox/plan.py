"""Task plan: a TOML header between '+++' lines and acceptance criteria as a '- [ ]' list."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

KINDS = ("feature", "bug", "other")
CRITERIA_HEADING = "Acceptance criteria"
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
MAX_SUMMARY = 100
CHECKBOX = re.compile(r"^\s*[-*] \[( |x|X)\] (.+)$")


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Criterion:
    text: str
    done: bool


# Header fields older plans have that nothing reads any more: accepted and ignored, not refused.
# (mode and requires_skills come back with skills, collab with a reviewer loop, each with the code
# that reads it.)
RETIRED = ("mode", "requires_skills", "collab")


@dataclass(frozen=True)
class Plan:
    criteria: list[Criterion] = field(default_factory=list)
    kind: str = "feature"
    # One line naming what the task does, written by the agent when it plans; shown in your list.
    summary: str = ""
    # How to build and test the project, when it did not have a command yet (a new project).
    verify: list[str] = field(default_factory=list)
    # The plan says there is nothing to build or test: verify = false in its header.
    no_build: bool = False

    @property
    def criteria_done(self) -> int:
        return sum(c.done for c in self.criteria)


def _split_header(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise PlanError("The plan must start with a header between '+++' lines")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "+++":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    raise PlanError("The plan header is not closed with a '+++' line")


def checkboxes(text: str) -> list[Criterion]:
    """Every '- [ ]' item in a markdown text, wrapped lines joined back on.

    The plan is written by an agent and the checklist is edited by one, so both come back
    reformatted. Reading one with wrapped lines joined and the other without compares a whole
    criterion against its first clause.
    """
    found: list[Criterion] = []
    open_item = False
    for line in text.splitlines():
        if m := CHECKBOX.match(line):
            found.append(Criterion(m.group(2).strip(), m.group(1) != " "))
            open_item = True
        elif open_item and line.startswith((" ", "\t")) and line.strip():
            last = found[-1]
            found[-1] = Criterion(f"{last.text} {line.strip()}", last.done)
        else:
            open_item = False
    return found


def _criteria(body: str) -> list[Criterion]:
    """The heading counts at any level. The template mixes '# Goal' with '## Acceptance criteria',
    and an agent that tidies them to one level would otherwise leave a plan whose criteria are all
    there and none of which are found."""
    section: list[str] = []
    inside = False
    for line in body.splitlines():
        if m := HEADING.match(line):
            inside = m.group(1).casefold() == CRITERIA_HEADING.casefold()
            continue
        if inside:
            section.append(line)
    return checkboxes("\n".join(section))


COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def without_notes(text: str) -> str:
    """The plan without the notes the template leaves for whoever writes it. They are guidance for
    writing a plan, not part of one, so they go as soon as a plan comes back written."""
    return COMMENT.sub("", text)


def body(text: str) -> str:
    """The plan as it concerns you: without the header the agent works from and without the
    instructions the template leaves for it."""
    try:
        _, rest = _split_header(text)
    except PlanError:
        rest = text
    return without_notes(rest).strip()


def parse_plan(text: str) -> Plan:
    header, body = _split_header(text)
    try:
        data = tomllib.loads(header)
    except tomllib.TOMLDecodeError as e:
        raise PlanError(f"Plan header: {e}") from None
    kind = data.get("kind", "feature")
    if kind not in KINDS:
        raise PlanError(f"kind must be one of {KINDS}")
    summary = data.get("summary", "")
    if not isinstance(summary, str) or "\n" in summary:
        raise PlanError("summary must be a single line")
    verify = data.get("verify", [])
    # false: there is nothing to build or test (a repository of documents); the gate checks the rest.
    no_build = verify is False
    verify = [] if no_build else verify
    if not isinstance(verify, list) or not all(isinstance(c, str) and c.strip() for c in verify):
        raise PlanError("verify must be a list of commands, or false when there is nothing to build")
    return Plan(_criteria(body), kind, summary.strip()[:MAX_SUMMARY], verify, no_build)
