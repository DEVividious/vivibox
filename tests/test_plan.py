import pytest

from vivibox.plan import PlanError, body, parse_plan

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
