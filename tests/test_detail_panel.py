"""The details panel (d): a header, Next, then the same sections in the same order in every state."""

from test_tui import implementing, with_mode, with_reviewer

from vivibox import gate, reviewing
from vivibox.panel import detail
from vivibox.states import State


def turns(task, *spent):
    """(state, role, cost) turn events, as the supervisor writes them in a mode where every role
    is an agent of its own."""
    for state, role, cost in spent:
        task.event("turn", state=state, role=role, agent=role, ok=True, cost=cost, tokens=1)


def test_the_panel_counts_turns_and_cost_per_role(env):
    with_reviewer(env)
    task = implementing()
    turns(task, ("plan", "planner", 0.02), ("plan", "planner", 0.01), ("implement", "writer", 0.04),
          ("implement", "writer", 0.05), ("review", "reviewer", 0.02))  # fmt: skip
    shown = detail(task, task.read_state(), 3, running=True)
    assert "#### Roles" in shown
    assert "| Planner | m | 2 | $0.03 |" in shown
    assert "| Writer | m | 2 | $0.09 |" in shown
    assert "| Reviewer | other/strong | 1 | $0.02 |" in shown
    assert "| Total | | 5 | $0.14 |" in shown
    assert "planning + implementation" not in shown, "the split is the table now, not a sum to read"


def test_roles_one_agent_plays_share_a_row(env):
    with_mode(env, "single_agent")
    task = implementing()
    for state, role, cost in (
        ("plan", "planner", 0.02),
        ("implement", "writer", 0.04),
        ("implement", "reviewer", 0.01),
    ):
        task.event("turn", state=state, role=role, agent="planner", ok=True, cost=cost, tokens=1)
    shown = detail(task, task.read_state(), 3, running=True)
    assert "| Planner + writer + reviewer | m | 3 | $0.07 |" in shown
    assert "| Writer |" not in shown and "| Total |" not in shown, "one row needs no total"


def test_turns_from_before_roles_were_recorded_count_by_state(env):
    with_reviewer(env)
    task = implementing()
    task.event("turn", state="plan", cost=0.03, tokens=1)
    task.event("turn", state="implement", cost=0.04, tokens=1)
    task.event("turn", state="review", cost=0.02, tokens=1)
    shown = detail(task, task.read_state(), 3, running=True)
    assert "| Planner | m | 1 | $0.03 |" in shown and "| Reviewer | other/strong | 1 | $0.02 |" in shown


def test_the_sections_come_in_one_order(env):
    with_reviewer(env)
    task = implementing()
    turns(task, ("plan", "planner", 0.02), ("implement", "writer", 0.04))
    task.transition(State.VERIFY)
    task.event("gate", passed=True, iteration=1)
    task.transition(State.REVIEW)
    reviewing.keep(task, 1, "# Review 1\n\n## Blocking\n\n- [ ] a.py:1 — wrong\n\n## Not blocking\n")
    task.event("review", round=1, blocking=1, not_blocking=0, problem="")
    task.set_reviews(1)
    task.transition(State.IMPLEMENT, reason="review 1: 1 blocking note")
    shown = detail(task, task.read_state(), 3, running=True)
    order = ["### demo-1 · implementing", "**Next:**", "#### Review 1: 1 blocking, 0 not blocking",
             "#### Acceptance criteria 0/2", "#### Roles", "#### The plan"]  # fmt: skip
    at = [shown.index(heading) for heading in order]
    assert at == sorted(at), shown
    assert "a.py:1 — wrong" in shown, "the notes the writer is fixing now"


def test_the_header_line_says_where_and_how_long(env):
    task = implementing()
    turns(task, ("plan", "planner", 0.02), ("implement", "writer", 0.04))
    shown = detail(task, task.read_state(), 3, running=True)
    meta = shown.splitlines()[2]
    assert meta.startswith("*demo · created ") and "updated " in meta and "$0.06 so far*" in meta


def test_the_plan_at_its_review_is_the_body_without_a_second_copy(env):
    from test_tui import at_plan_checkpoint, new_task

    task = new_task()
    at_plan_checkpoint(task)
    shown = detail(task, task.read_state(), 3, running=True)
    assert shown.count("it works") == 1 and "#### Roles" in shown
    assert shown.index("it works") < shown.index("#### Roles"), "what you decide on comes first"


def test_the_criteria_of_an_accepted_plan_are_one_section(env):
    task = implementing()
    ticks = task.meta / "handoff" / gate.CRITERIA_FILE
    ticks.write_text(ticks.read_text().replace("- [ ] it works", "- [x] it works"))
    shown = detail(task, task.read_state(), 3, running=True)
    assert "#### Acceptance criteria 1/2" in shown and "- ☑ it works" in shown
    assert "As the agent reports them." in shown


def test_the_reviews_own_headings_stay_under_its_section(env):
    """A review is written with a title and `## Blocking`; in the panel they would stand larger
    than the section they are in."""
    task = implementing()
    reviewing.keep(task, 1, "# Review 1\n\n## Blocking\n\n- [ ] a.py:1 — wrong\n\n## Not blocking\n")
    task.set_reviews(1)
    shown = detail(task, task.read_state(), 3, running=True)
    section = shown[shown.index("#### Review 1") :]
    assert "**Blocking**" in section and "**Not blocking**" in section
    assert "\n## " not in section.split("#### Acceptance criteria")[0]
    assert "# Review 1" not in section.replace("#### Review 1", ""), "the heading says it already"


def test_the_accepted_plan_leaves_its_criteria_to_their_section(env):
    task = implementing()
    shown = detail(task, task.read_state(), 3, running=True)
    plan = shown[shown.index("#### The plan") :]
    assert "**Goal**" in plan and "\n# " not in plan and "\n## " not in plan, "headings under the section's"
    assert "it works" not in plan, "the criteria are one section, with their ticks"
    assert shown.count("it works") == 1


def test_the_plan_under_review_keeps_its_criteria_under_smaller_headings(env):
    from test_tui import at_plan_checkpoint, new_task

    task = new_task()
    at_plan_checkpoint(task)
    shown = detail(task, task.read_state(), 3, running=True)
    assert "**Acceptance criteria**" in shown and "- [ ] it works" in shown
    assert "\n# " not in shown and "\n## " not in shown


def test_a_reviewer_without_a_role_of_its_own_runs_on_the_writers_model(env):
    """The default mode reviews without [roles.reviewer], on the writer's model (ADR-0029): the
    table said "Reviewer | -" after a review, and nothing before one."""
    task = implementing()
    shown = detail(task, task.read_state(), 3, running=True)
    assert "| Reviewer | m (the writer's) | 0 | $0.00 |" in shown, "listed before its first turn"
    turns(task, ("review", "reviewer", 0.02))
    shown = detail(task, task.read_state(), 3, running=True)
    assert "| Reviewer | m (the writer's) | 1 | $0.02 |" in shown
    assert "| Reviewer | - |" not in shown
