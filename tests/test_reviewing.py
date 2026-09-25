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


# What a reviewer on deepseek-flash wrote in the behavioural run of 2026-09-25: one line a note,
# "path:line — …", as REVIEW_PROMPT asks, and no "- " in front. Read as no notes at all, it let
# the work through with three blocking ones.
AS_ASKED = """## Blocking

test_calc.py:10 — `test_subtract` asserts `assertTrue(True)`, a tautology that can never fail.
test_calc.py:6 — the import still reads `from calc import add`, so `subtract` is never imported.
red.md — the red evidence records only "AssertionError" with no expected vs actual values.

## Not blocking

"""


def test_a_note_is_a_line_of_its_section_as_the_prompt_asks_with_or_without_a_list_mark():
    review = reviewing.parse_review(AS_ASKED)
    assert len(review.blocking) == 3 and review.blocking[0].startswith("test_calc.py:10 — ")
    assert review.not_blocking == []
    assert "red.md" in reviewing.problem(AS_ASKED), "a note without its line: the reviewer rewrites it"
    marked = "## Blocking\n\n* a.py:1 — x\n1. b.py:2 — y\n- [x] c.py:3 — z\n\n## Not blocking\n\nNone.\n"
    assert reviewing.parse_review(marked).blocking == ["a.py:1 — x", "b.py:2 — y", "c.py:3 — z"]
    assert reviewing.parse_review(marked).not_blocking == [], "None. is an empty section"
    wrapped = "## Blocking\n\n- a.py:1 — a long note\n  that goes on\n\n## Not blocking\n"
    assert reviewing.parse_review(wrapped).blocking == ["a.py:1 — a long note that goes on"]
    assert reviewing.problem(wrapped) == ""


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


def test_the_reviewers_pod_side_gets_its_own_key_harness_files_and_a_place_to_write(env, monkeypatch):
    """Only the reviewer's provider key goes to its container; the writer's stays with the writer.
    The brief names the clone the reviewer reads, and the review is written outside the handoff."""
    import json

    from vivibox import actions, opencode, secrets
    from vivibox import pod as pods
    from vivibox.config import load_config

    cfg = env / "config" / "config.toml"
    cfg.write_text(cfg.read_text() + '[roles.reviewer]\nharness = "opencode"\nmodel = "other/strong"\n')
    task = actions.create("demo", "Add divide", auto=True)
    prepared = {}

    def prepare(task_id, providers):
        prepared[task_id] = providers
        return env / "rt"

    monkeypatch.setattr(secrets, "prepare", prepare)
    (env / "rt").mkdir()
    calls = []

    class Runner:
        def __call__(self, cmd):
            calls.append(list(cmd))
            import subprocess

            return subprocess.CompletedProcess(cmd, 0, "", "")

    pod = pods.Pod(task.id, task.repo, "img", runner=Runner(), review_dir=task.root / "review")
    out = reviewing.up(task, pod, load_config())
    assert prepared == {f"{task.id}-review": ["other"]}, "the reviewer's key, nobody else's"
    assert out == task.root / "review" / "out" and out.is_dir()
    assert (task.meta / "review").is_dir(), "the mount point, inside the read-only /task"
    harness = task.root / "review" / "harness"
    config = json.loads((harness / "opencode.json").read_text())
    assert config["model"] == "other/strong" and list(config["provider"]) == ["other"]
    assert pod.review_src in (harness / "instructions.md").read_text(), "the clone is the reviewer's repo"
    run = next(c for c in calls if c[:3] == ["docker", "run", "-d"])
    joined = " ".join(run)
    assert f"{task.meta}:/task:ro" in joined and f"{harness}:{opencode.HARNESS_MOUNT}:ro" in joined
    assert f"{out}:{reviewing.MOUNT}" in joined and f"{env / 'rt'}:{secrets.MOUNT}:ro" in joined
    assert f"OPENCODE_CONFIG={opencode.CONFIG}" in run


def test_the_reviewers_key_goes_with_the_tasks(tmp_path, monkeypatch):
    from vivibox import secrets

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    secrets.prepare(reviewing.secrets_id("demo-1"), [])
    assert secrets.runtime_dir("demo-1-review").is_dir()
    reviewing.forget("demo-1")
    assert not secrets.runtime_dir("demo-1-review").exists()
