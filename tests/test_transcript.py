"""A role's log summed up in a line: its turns, the failed ones, what they cost."""

from vivibox import transcript


def logged(tmp_path, *footers):
    log = tmp_path / "writer.log"
    turns = [f"=== 10:00:00 writer · writing ===\nPrompt: go\n\nDone.\n{footer}\n" for footer in footers]
    log.write_text("\n".join(turns))
    return log


def test_a_failed_turn_whose_error_holds_a_middle_dot_is_counted_with_its_cost(tmp_path):
    log = logged(
        tmp_path,
        "--- 10:00:30 · ok · $0.1000 · 12 tokens · 30 s",
        "--- 10:01:00 · failed: pod gone · exit 137 · $0.2000 · 5 tokens · 3 s",
    )
    assert transcript.describe(log, "writer") == "the writer's conversation · 2 turns · 1 failed · $0.30"


def test_ok_turns_and_failed_turns_without_a_middle_dot_are_still_counted(tmp_path):
    log = logged(
        tmp_path,
        "--- 10:00:30 · ok · $0.1000 · 12 tokens · 30 s",
        "--- 10:01:00 · failed: pod gone · $0.2500 · 5 tokens · 3 s",
        "--- 10:02:00 · ok · $0.0500 · 7 tokens · 4 s",
    )
    assert transcript.describe(log, "writer") == "the writer's conversation · 3 turns · 1 failed · $0.40"
