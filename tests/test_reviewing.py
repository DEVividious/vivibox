from vivibox import reviewing
from vivibox.states import State
from vivibox.task import create_task

REVIEW = """# Review 1

## Blocking

- [ ] src/calc.py:12 — divide(a, 0) raises ZeroDivisionError; the plan asks for None
- [ ] test_calc.py:20 — test_divide asserts True, so it proves nothing

## Not blocking

- src/calc.py:3 — the docstring still says "adds"
"""


def test_a_review_has_blocking_and_not_blocking_notes_each_with_a_place():
    review = reviewing.parse_review(REVIEW)
    assert review.blocking == [
        "src/calc.py:12 — divide(a, 0) raises ZeroDivisionError; the plan asks for None",
        "test_calc.py:20 — test_divide asserts True, so it proves nothing",
    ]
    assert review.not_blocking == ['src/calc.py:3 — the docstring still says "adds"']
    assert reviewing.problem(REVIEW) == ""
    clean = reviewing.parse_review("# Review 2\n\n## Blocking\n\n## Not blocking\n\n- a.py:1 — nit\n")
    assert clean.blocking == [] and len(clean.not_blocking) == 1, "an empty section is no notes"


def test_a_review_without_the_sections_or_without_places_is_refused():
    """The rule has a check (ADR-0015): the sections are what the supervisor reads, and a note
    without a place is one the writer cannot act on."""
    assert "## Blocking" in reviewing.problem("Looks fine to me.")
    assert "place" in reviewing.problem(
        "## Blocking\n\n- [ ] the error handling is wrong\n\n## Not blocking\n"
    )
    assert reviewing.problem("## Blocking\n\n- [ ] src/x.py:4 — off by one\n\n## Not blocking\n") == ""


def test_reviews_are_numbered_and_the_newest_is_found(tmp_path):
    task = create_task(tmp_path, "demo", "goal", "")
    assert reviewing.latest(task) is None
    first = reviewing.keep(task, 1, REVIEW)
    assert first == task.meta / "handoff" / "review-1.md" and first.read_text() == REVIEW
    reviewing.keep(task, 2, "## Blocking\n\n## Not blocking\n")
    assert reviewing.latest(task) == task.meta / "handoff" / "review-2.md"
    assert reviewing.parse_review(reviewing.latest(task).read_text()).blocking == []


def test_the_review_state_sits_between_the_gate_and_the_final_checkpoint(tmp_path):
    from vivibox.states import TRANSITIONS

    assert State.REVIEW in TRANSITIONS[State.VERIFY]
    assert TRANSITIONS[State.REVIEW] == {State.IMPLEMENT, State.APPROVAL_RISKY, State.CHECKPOINT_FINAL}
    task = create_task(tmp_path, "demo", "goal", "")
    assert task.read_state().reviews == 0 and task.read_state().review_mode == ""
    task.set_reviews(2)
    assert task.read_state().reviews == 2
