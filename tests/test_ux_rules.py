"""The mechanical rules of docs/ux-guidelines.md. A change that breaks one of these changed what a
person sees; update the guidelines with it, or fix the change."""

import asyncio
import re
from pathlib import Path

import pytest
from test_tui import at_plan_checkpoint, implementing, new_task, rows, run

from vivibox import (
    actions,
    box,
    branches,
    browse,
    dialogs,
    logs,
    manual,
    panel,
    providers_ui,
    settings,
    tui,
    ui,
    widgets,
)
from vivibox.states import State

GUIDELINES = Path(__file__).parent.parent / "docs" / "ux-guidelines.md"
# round n/N and N× vary; everything else is a fixed word from the table.
VARIABLE = re.compile(r" \(round \d+/\d+(: [^)]*)?\)$|\d+×$| · stopped$")


def allowed_labels() -> set[str]:
    """The Label column of the status table in the guidelines."""
    rows = re.findall(r"^\| [^|]+ \| `([^`]+)` \| [^|]+ \|$", GUIDELINES.read_text(), re.MULTILINE)
    assert len(rows) >= 12, "the status table moved or changed its shape"
    return {VARIABLE.sub("", label.replace(" (round n/N: why)", "").replace("N×", "")) for label in rows}


def situations(env):
    """One task per situation the guidelines name, with whether its supervisor runs."""
    found = [(new_task("never started"), False)]
    planning = new_task("planning")
    planning.event("started", model="m")
    found.append((planning, True))
    plan = new_task("plan")
    at_plan_checkpoint(plan)
    found.append((plan, True))
    yours = new_task("manual")
    for prompt in (manual.PROMPT, manual.PROMPT_CLI):  # written before the task waits for a plan
        (yours.meta / prompt).write_text("prompt")
    yours.transition(State.CHECKPOINT_PLAN)
    yours.set_awaiting_plan(True)
    found.append((yours, True))
    instead = new_task("planner asks")
    (instead.meta / "handoff" / "question.md").write_text("The goal is met already.\n")
    instead.transition(State.CHECKPOINT_PLAN)
    found.append((instead, True))
    found.append((implementing("implementing"), True))
    preparing = implementing("preparing")
    preparing.event("prepare_started", commands=["npm ci"])
    found.append((preparing, True))
    second = implementing("second attempt")
    second.transition(State.VERIFY)
    second.transition(State.IMPLEMENT)
    found.append((second, True))
    verifying = implementing("verifying")
    verifying.transition(State.VERIFY)
    found.append((verifying, True))
    reviewing = implementing("reviewing")
    reviewing.transition(State.VERIFY)
    reviewing.transition(State.REVIEW)
    found.append((reviewing, True))
    watched = implementing("verifying with a log")
    watched.transition(State.VERIFY)
    (watched.meta / "log" / "verify-1-120000.log").write_text("# fresh clone of commit abc\n\n$ npm test\n")
    found.append((watched, True))
    risky = implementing("risky")
    risky.transition(State.APPROVAL_RISKY, then=str(State.CHECKPOINT_FINAL))
    found.append((risky, True))
    command = implementing("command")
    command.transition(State.CHECKPOINT_COMMAND)
    found.append((command, True))
    asks = implementing("asks")
    (asks.meta / "handoff" / "question.md").write_text("Which one?\n")
    asks.transition(State.CHECKPOINT_BLOCKED)
    found.append((asks, True))
    failing = implementing("failing")
    failing.transition(State.VERIFY)
    failing.transition(State.CHECKPOINT_BLOCKED)
    found.append((failing, True))
    broken = implementing("environment")
    broken.transition(State.VERIFY)
    broken.event("gate", passed=False, environment="Cannot connect to the Docker daemon")
    broken.transition(State.CHECKPOINT_BLOCKED, reason="verification could not run")
    found.append((broken, True))
    stopped_blocked = implementing("stopped after environment failure")
    stopped_blocked.transition(State.VERIFY)
    stopped_blocked.event("gate", passed=False, environment="Cannot connect to the Docker daemon")
    stopped_blocked.transition(State.CHECKPOINT_BLOCKED, reason="verification could not run")
    stopped_blocked.set_paused(True)
    found.append((stopped_blocked, False))
    forced = implementing("stopped by force")
    forced.set_paused(True, problem="stopped by force")
    found.append((forced, False))
    final = implementing("final")
    final.transition(State.VERIFY)
    final.transition(State.CHECKPOINT_FINAL)
    found.append((final, True))
    for state in (State.CHECKPOINT_PLAN, State.CHECKPOINT_FINAL, State.APPROVAL_RISKY):
        stopped_review = implementing(f"stopped pod with {state.name.lower()}")
        st = stopped_review.read_state()
        st.state = state
        stopped_review._write_state(st)
        stopped_review.set_paused(True)
        found.append((stopped_review, False))
    stopped = implementing("stopped")
    stopped.set_paused(True)
    found.append((stopped, False))
    for problem in (
        "agent turn failed: 429",
        "stopped on an error: boom",
        "could not start: X not set",
        "cost limit reached: $2.10 of $2.00",
    ):
        failed = implementing(problem)
        failed.set_paused(True, problem=problem)
        found.append((failed, True))
    found.append((implementing("dead"), False))
    box = actions.open_box("demo")
    found.append((box, False))
    closed = actions.open_box("demo")
    closed.set_paused(True)
    found.append((closed, False))
    return found


@pytest.fixture(autouse=True)
def no_box_pod(monkeypatch):
    monkeypatch.setattr(actions, "start_box", lambda task_id: None, raising=False)
    monkeypatch.setattr(box, "start_box", lambda task_id: None, raising=False)


def test_every_status_is_a_label_from_the_guidelines(env):
    allowed = allowed_labels()
    for task, running in situations(env):
        status = ui.view(task, task.read_state(), running, 3).status
        assert VARIABLE.sub("", status) in allowed, f"'{status}' is not in docs/ux-guidelines.md"


def test_no_raw_state_names_or_retired_words_reach_the_screen(env):
    for task, running in situations(env):
        st = task.read_state()
        shown = ui.view(task, st, running, 3).status + panel.detail(task, st, 3, running=running)
        for word in ("checkpoint:", "approval:", "iteration"):
            assert word not in shown.replace(str(task.meta), ""), f"'{word}' shown for {st.goal}"


@pytest.mark.parametrize("size", [(140, 40)])
def test_every_key_the_panel_names_is_a_key_the_footer_offers(env, monkeypatch, size):
    tasks = situations(env)
    running = {task.id for task, alive in tasks if alive}
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id in running)
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: panel.PodView() for i in ids})
    by_key: dict[str, list[str]] = {}
    for binding in tui.Vivibox.BINDINGS:
        by_key.setdefault(binding.key, []).append(binding.action)

    async def go():
        app = tui.Vivibox()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app.reload()
            for task, _ in tasks:
                app.table.move_cursor(row=rows(app).index(task.id))
                await pilot.pause()
                st = task.read_state()
                shown = panel.detail(task, st, 3, app.agent_running(st.id), app.pod)
                for key in re.findall(r"`([a-zA-Z])`", shown):
                    offered = any(app.check_action(action, ()) for action in by_key.get(key, []))
                    assert offered, f"{st.goal}: the panel names `{key}`, the footer does not offer it"

    asyncio.run(go())


def test_help_says_which_build_this_is(env):
    """The version at the bottom of the help, where a person looks when asked which build."""
    from ux import screen_text

    from vivibox import version

    async def scenario(app, pilot):
        await pilot.press("?")
        await pilot.pause()
        assert f"vivibox {version.current()}" in screen_text(app)

    run(scenario)


def test_help_fits_in_eighty_columns_without_wrapping():
    """The help is one column so it never wraps (§3); a line longer than the modal wraps after all,
    and a wrapped line can read as something else ("no agent, for you to work in")."""
    for line in dialogs.HELP.splitlines():
        assert len(line) <= 78, f"{len(line)} columns: {line!r}"


def test_help_says_that_a_capital_key_is_shift_and_the_letter():
    """S, C and H read as the letter itself on a screen of lowercase keys; the line names Shift."""
    for line in dialogs.HELP.splitlines():
        if re.match(r"  (\w, )?[A-Z] ", line):
            assert "Shift+" in line, line


def test_a_dialog_has_at_most_one_primary_button():
    """One primary button per dialog (§4), checked in the source: a class at a time, in every
    module that defines a screen."""
    for module in (tui, dialogs, branches, browse, providers_ui, widgets, settings, logs):
        source = Path(module.__file__).read_text()
        for chunk in source.split("\nclass ")[1:]:
            name = chunk.split("(")[0].split(":")[0]
            assert chunk.count('"primary"') <= 1, (
                f"{module.__name__}: {name} has more than one primary button"
            )
