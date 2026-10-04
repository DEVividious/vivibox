"""l on a finished task: a review the archive kept is said by what is known of it. What a round
kept when the reviewer's file was not read is no review, and counted as one it would read as a
clean one."""

from pathlib import Path

from vivibox import actions, logs, reviewing

A_REVIEW = "## Blocking\n\n- a.py:3 — the test cannot fail\n\n## Not blocking\n\n## Checked\n\n- a.py\n"
WHY = "/task/review/review.md is a link or not a plain file, and vivibox does not read through one"


def archived(env, text: str, problem: str, events: bool = True) -> Path:
    """The archive of a finished task with one review kept: text, and the supervisor's record of
    the round (problem: why it could not read the reviewer's file, "" when it did)."""
    from test_tui import implementing

    task = implementing("Add health endpoint")
    reviewing.keep(task, 1, text)
    review = reviewing.parse_review(text) if not problem else reviewing.Review()
    task.event(
        "review",
        round=1,
        blocking=len(review.blocking),
        not_blocking=len(review.not_blocking),
        problem=problem,
    )
    kept = actions.archive(task)
    if not events:
        (kept / "events.jsonl").unlink()
    return kept


def said_of_the_review(kept: Path) -> str:
    return next(e.said for e in logs.archived_entries(kept) if e.label == "review-1.md")


def test_l_on_a_finished_task_counts_the_notes_of_a_review(env):
    assert said_of_the_review(archived(env, A_REVIEW, "")) == "review 1 · 1 blocking · 0 not blocking"


def test_l_on_a_finished_task_says_a_refused_review_was_not_read(env):
    """What the round kept is why the reviewer's file was not read: no notes to count, and
    "0 blocking" would read as a clean review."""
    said = said_of_the_review(archived(env, WHY, WHY))
    assert said == f"review 1 · unreadable: {WHY.rsplit('/', 1)[-1]}"
    assert "0 blocking" not in said


def test_an_archive_without_its_events_says_what_the_file_itself_says(env):
    """Nothing there records how the round went: a text that is no review has no counts either."""
    said = said_of_the_review(archived(env, WHY, WHY, events=False))
    assert said == "review 1 · not a review: the section ## Blocking is missing"
    counted = said_of_the_review(archived(env, A_REVIEW, "", events=False))
    assert counted == "review 1 · 1 blocking · 0 not blocking", "a review is counted, with or without"


def test_a_review_from_before_the_checked_section_is_still_counted(env):
    """An earlier vivibox asked for two sections: its reviews have notes to count."""
    older = "## Blocking\n\n- a.py:1 — wrong\n\n## Not blocking\n"
    assert (
        said_of_the_review(archived(env, older, "", events=False)) == "review 1 · 1 blocking · 0 not blocking"
    )


def test_an_archive_whose_events_cannot_be_read_does_not_end_l(env):
    kept = archived(env, WHY, WHY)
    (kept / "events.jsonl").write_text("{not json\n")
    assert said_of_the_review(kept) == "review 1 · not a review: the section ## Blocking is missing"
