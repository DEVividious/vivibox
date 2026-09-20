"""Task plan: a TOML header between '+++' lines and acceptance criteria as a '- [ ]' list."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

MODES = ("code-only", "full-system")
KINDS = ("feature", "bug", "other")
COLLAB = ("supervised", "loop")
CRITERIA_HEADING = "## Acceptance criteria"
MAX_SUMMARY = 100
CHECKBOX = re.compile(r"^\s*[-*] \[( |x|X)\] (.+)$")


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Criterion:
    text: str
    done: bool


@dataclass(frozen=True)
class Plan:
    mode: str
    collab: str
    requires_skills: list[str]
    criteria: list[Criterion] = field(default_factory=list)
    kind: str = "feature"
    # One line naming what the task does, written by the agent when it plans; shown in your list.
    summary: str = ""
    # How to build and test the project, when it did not have a command yet (a new project).
    verify: list[str] = field(default_factory=list)

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


def _criteria(body: str) -> list[Criterion]:
    criteria, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line.strip() == CRITERIA_HEADING
            continue
        if inside and (m := CHECKBOX.match(line)):
            criteria.append(Criterion(m.group(2).strip(), m.group(1) != " "))
    return criteria


COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def body(text: str) -> str:
    """The plan as it concerns you: without the header the agent works from and without the
    instructions the template leaves for it."""
    try:
        _, rest = _split_header(text)
    except PlanError:
        rest = text
    return COMMENT.sub("", rest).strip()


def parse_plan(text: str) -> Plan:
    header, body = _split_header(text)
    try:
        data = tomllib.loads(header)
    except tomllib.TOMLDecodeError as e:
        raise PlanError(f"Plan header: {e}") from None
    mode = data.get("mode", "code-only")
    if mode not in MODES:
        raise PlanError(f"mode must be one of {MODES}")
    collab = data.get("collab", "supervised")
    if collab not in COLLAB:
        raise PlanError(f"collab must be one of {COLLAB}")
    skills = data.get("requires_skills", [])
    if not isinstance(skills, list) or not all(isinstance(s, str) and s for s in skills):
        raise PlanError("requires_skills must be a list of names")
    if mode == "full-system" and not skills:
        raise PlanError("mode = 'full-system' requires at least one skill in requires_skills")
    kind = data.get("kind", "feature")
    if kind not in KINDS:
        raise PlanError(f"kind must be one of {KINDS}")
    summary = data.get("summary", "")
    if not isinstance(summary, str) or "\n" in summary:
        raise PlanError("summary must be a single line")
    verify = data.get("verify", [])
    if not isinstance(verify, list) or not all(isinstance(c, str) and c.strip() for c in verify):
        raise PlanError("verify must be a list of commands")
    return Plan(mode, collab, skills, _criteria(body), kind, summary.strip()[:MAX_SUMMARY], verify)
