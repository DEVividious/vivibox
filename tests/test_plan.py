import pytest

from vivibox.plan import PlanError, body, parse_plan, without_notes

PLAN = """+++
mode = "code-only"
+++

# Goal

## Acceptance criteria

- [x] endpoint returns 200
- [ ] test covers the error path
  - [X] nested items count too

## Out of scope

- [ ] not a criterion
"""


def test_parses_header_and_criteria():
    plan = parse_plan(PLAN)
    assert plan.mode == "code-only"
    assert plan.collab == "supervised"
    assert [c.text for c in plan.criteria] == [
        "endpoint returns 200",
        "test covers the error path",
        "nested items count too",
    ]
    assert plan.criteria_done == 2


def test_packaged_template_is_valid():
    from importlib.resources import files

    plan = parse_plan(
        files("vivibox").joinpath("templates/plan.md").read_text().replace("{{kind}}", "feature")
    )
    assert plan.mode == "code-only"


@pytest.mark.parametrize(
    "text",
    [
        "# no header",
        "+++\nmode = 'code-only'\n",
        '+++\nmode = "other"\n+++\n',
        '+++\nmode = "full-system"\n+++\n',
        '+++\ncollab = "free"\n+++\n',
        "+++\nmode = \n+++\n",
    ],
)
def test_rejects_invalid_plan(text):
    with pytest.raises(PlanError):
        parse_plan(text)


def test_full_system_with_skill():
    plan = parse_plan('+++\nmode = "full-system"\nrequires_skills = ["local-system"]\n+++\n')
    assert plan.requires_skills == ["local-system"]


def test_body_is_the_plan_without_what_the_agent_works_from():
    text = (
        '+++\nkind = "bug"\nsummary = "Fix it"\n+++\n\n# Goal\n\nBroken dates\n\n'
        "## Reproduction\n\n<!-- Write the failing test first. -->\n\nA test that fails\n"
    )
    assert parse_plan(text).summary == "Fix it"
    assert body(text) == "# Goal\n\nBroken dates\n\n## Reproduction\n\nA test that fails"


def test_notes_go_but_the_header_you_edit_stays():
    text = (
        '+++\n# feature: new behaviour.\nkind = "bug"\n+++\n\n# Goal\n\nBroken dates\n\n'
        "## Root cause\n\n<!-- Found in the code, not guessed. -->\n\nAn off-by-one\n"
    )
    cleaned = without_notes(text)
    assert "<!--" not in cleaned
    assert cleaned.startswith('+++\n# feature: new behaviour.\nkind = "bug"\n+++')
    assert cleaned.endswith("## Root cause\n\nAn off-by-one\n")


def test_every_plan_carries_the_standing_criterion_about_failing_tests():
    """A test that cannot fail reads as cover, so seeing it fail first is not left to memory: it is
    a criterion of every plan, and the agent has to tick it like any other."""
    from importlib.resources import files

    for name in ("plan.md", "plan-bug.md"):
        text = (files("vivibox") / "templates" / name).read_text()
        criteria = " ".join(c.text for c in parse_plan(text.replace("{{kind}}", "feature")).criteria)
        assert "red.md" in criteria, f"{name}: nowhere to record the evidence"
        assert "fail" in criteria, f"{name}: nothing says the test must be seen failing first"


def test_the_agent_is_told_how_to_record_it():
    from importlib.resources import files

    rules = (files("vivibox") / "templates" / "instructions.md").read_text()
    assert "/task/handoff/red.md" in rules
    assert "cannot fail is worse than no test" in rules


def test_the_criteria_heading_is_found_at_any_level():
    """The template mixes '# Goal' with '## Acceptance criteria'. An agent that tidies the levels to
    match wrote '# Acceptance criteria', and the plan silently parsed as having no criteria at all:
    the gate then refused it saying it needed criteria it already had."""
    plan = parse_plan(PLAN.replace("## Acceptance criteria", "# Acceptance criteria"))
    assert [c.text for c in plan.criteria] == [
        "endpoint returns 200",
        "test covers the error path",
        "nested items count too",
    ]


def test_a_heading_of_any_level_ends_the_criteria():
    """'# Out of scope' has to close the section too, or its checkboxes become criteria."""
    plan = parse_plan(PLAN.replace("## Out of scope", "# Out of scope"))
    assert "not a criterion" not in [c.text for c in plan.criteria]
