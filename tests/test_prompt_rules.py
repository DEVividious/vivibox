"""The mechanical rules of docs/prompt-guidelines.md. A change that breaks one of these changed
what an agent reads; update the guidelines with it, or fix the change."""

import dataclasses
import re
from importlib.resources import files
from pathlib import Path

import pytest

from vivibox import actions, brief, gate, manual, reviewing, risky, supervisor

GUIDELINES = Path(__file__).parent.parent / "docs" / "prompt-guidelines.md"
# The brief each role gets: the common part and its own, as the agent reads them.
BRIEFS = {
    role: brief.common("demo-1", "/srv/vivibox/demo-1/repo", "vivibox/demo-1", ["npm test"])
    + "\n"
    + brief.role_text(role)
    for role in brief.ROLES
}
TEMPLATES = [(files("vivibox") / "templates" / n).read_text() for n in ("plan.md", "plan-bug.md")]
# What starts a turn of an agent, wherever vivibox keeps it.
TURN_PROMPTS = {
    **{n: t for n, t in vars(supervisor).items() if n.endswith("_PROMPT") and len(t.split()) > 5},
    "RECON_PROMPT": manual.RECON_PROMPT,
    "DEMO_ASK": actions.DEMO_ASK,
}
MAX_BRIEF_WORDS = 800
MAX_PROMPT_WORDS = 160
# The files of /task the pod mounts for the agent (actions.task_pod), by the names the brief gives.
MOUNTED = {"/task/plan.md", "/task/context", "/task/harness", "/task/review/review.md"} | {
    f"/task/handoff/{name}"
    for name in (
        "plan-draft.md", "criteria.md", "red.md", "question.md", "comments.md", "verify-feedback.md",
        "verify.log", "context.md", "demo.md", "demo-question.md", "review-N.md", "review-N-reply.md",
        "prepare.log", "verify-proposal.md",
    )
}  # fmt: skip
TASK_PATH = re.compile(r"/task/[\w./-]*[\w/]")


def test_every_check_the_table_names_exists():
    rows = re.findall(r"^\| [^|]+ \| `([\w.]+)` \|$", GUIDELINES.read_text(), re.MULTILINE)
    assert len(rows) >= 6, "the table moved or changed its shape"
    for dotted in rows:
        module, *attrs = dotted.split(".")
        found = {"gate": gate, "risky": risky, "reviewing": reviewing}[module]
        for attr in attrs:
            found = getattr(found, attr)
        assert callable(found), dotted


@pytest.mark.parametrize("name", sorted(TURN_PROMPTS))
def test_every_turn_prompt_says_what_ends_the_turn(name):
    text = TURN_PROMPTS[name]
    assert "End the turn when" in text, f"{name}: a turn ends on a condition, not on a feeling"


@pytest.mark.parametrize("name", [*sorted(BRIEFS), *sorted(TURN_PROMPTS)])
def test_every_task_path_is_one_the_pod_mounts(name):
    text = BRIEFS[name] if name in BRIEFS else TURN_PROMPTS[name]
    for path in TASK_PATH.findall(text):
        path = path.rstrip("/")
        assert path in MOUNTED or path.startswith("/task/context/") or path in ("/task/handoff",), (
            f"{name} names {path}, which the agent does not have"
        )


def sentences(text: str) -> set[str]:
    plain = re.sub(r"[`*]", "", " ".join(text.split()))
    found = {s.strip().casefold() for s in re.split(r"(?<=[.!?])\s+", plain)}
    return {s for s in found if len(s.split()) >= 8}


@pytest.mark.parametrize("name", sorted(set(TURN_PROMPTS) - {"DEMO_ASK"}))
def test_no_sentence_is_in_both_the_brief_and_a_turn_prompt(name):
    for role, text in BRIEFS.items():
        twice = sentences(text) & sentences(TURN_PROMPTS[name])
        assert not twice, f"{name} repeats the {role}'s brief: {twice}"


def test_the_common_brief_names_no_role():
    common = brief.common("demo-1", "/r", "b", [])
    assert "writer" not in common.casefold() and "planner" not in common.casefold(), "the role is a parameter"


def test_every_failure_the_gate_records_has_a_line_in_its_feedback():
    for field in dataclasses.fields(gate.GateResult):
        if field.name in (
            "log",
            "risky",
            "removed_tests",
            "build_files",
        ):  # the log is where the rest is; the others go to you
            continue
        result = gate.GateResult(Path("/dev/null"))
        marker = f"z{field.name}z"[:10]  # short enough to survive a shortened commit hash
        if field.name == "commands":
            result.commands = [gate.CommandResult(marker, False, 0.1)]
        elif field.type == "str":
            setattr(result, field.name, marker)
        elif field.type.startswith("dict"):
            setattr(result, field.name, {marker: marker})
        else:
            setattr(result, field.name, [marker])
        assert marker in gate.feedback(result), f"{field.name} fails the gate but the agent is not told"


def test_the_templates_placeholder_is_the_one_the_gate_refuses():
    for template in TEMPLATES:
        assert f"- [ ] {gate.PLACEHOLDER}" in template


def test_the_stuck_path_names_the_file_by_its_full_path():
    for name, text in {**BRIEFS, **TURN_PROMPTS}.items():
        if re.search(r"(?<![\w-])question\.md", text):
            assert "/task/handoff/question.md" in text, name


def test_word_budgets():
    for role, text in BRIEFS.items():
        assert len(text.split()) <= MAX_BRIEF_WORDS, f"{role}: {len(text.split())} words"
    for name, text in TURN_PROMPTS.items():
        if name == "DEMO_ASK":  # a conversation of its own, with no brief behind it: it is its own
            continue
        assert len(text.split()) <= MAX_PROMPT_WORDS, f"{name}: {len(text.split())} words"


def test_the_reviewer_is_told_what_a_note_on_the_code_itself_looks_for():
    """Four of six reviews on the trial run of 2026-09-26 were empty: the brief named only what
    keeps the work from being what the plan says. What a senior reviewer sends back besides
    that is listed, as cases, not as the names of principles."""
    text = BRIEFS["reviewer"]
    for case in (
        "repeats what the repository already has",
        "one caller",
        "did not ask for",
        "restates the line",
        "how the code is written instead of what it does",
        "the type, not the purpose",
    ):
        assert case in text, case
