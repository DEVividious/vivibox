"""The mechanical rules of docs/ux-guidelines.md. A change that breaks one of these changed what a
person sees; update the guidelines with it, or fix the change."""

import asyncio
import re
from pathlib import Path

import pytest
from test_tui import at_plan_checkpoint, implementing, new_task, rows

from vivibox import actions, manual, tui, ui
from vivibox.states import State

GUIDELINES = Path(__file__).parent.parent / "docs" / "ux-guidelines.md"
# attempt n/N and N× vary; everything else is a fixed word from the table.
VARIABLE = re.compile(r" \(attempt \d+/\d+\)$|\d+×$")


def allowed_labels() -> set[str]:
    """The Label column of the status table in the guidelines."""
    rows = re.findall(r"^\| [^|]+ \| `([^`]+)` \| [^|]+ \|$", GUIDELINES.read_text(), re.MULTILINE)
    assert len(rows) >= 12, "the status table moved or changed its shape"
    return {VARIABLE.sub("", label.replace(" (attempt n/N)", "").replace("N×", "")) for label in rows}


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
    found.append((implementing("implementing"), True))
    second = implementing("second attempt")
    second.transition(State.VERIFY)
    second.transition(State.IMPLEMENT)
    found.append((second, True))
    verifying = implementing("verifying")
    verifying.transition(State.VERIFY)
    found.append((verifying, True))
    risky = implementing("risky")
    risky.transition(State.APPROVAL_RISKY, then=str(State.CHECKPOINT_FINAL))
    found.append((risky, True))
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
    final = implementing("final")
    final.transition(State.VERIFY)
    final.transition(State.CHECKPOINT_FINAL)
    found.append((final, True))
    stopped = implementing("stopped")
    stopped.set_paused(True)
    found.append((stopped, False))
    for problem in ("agent turn failed: 429", "stopped on an error: boom", "could not start: X not set"):
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


def test_every_status_is_a_label_from_the_guidelines(env):
    allowed = allowed_labels()
    for task, running in situations(env):
        status = ui.view(task, task.read_state(), running, 3).status
        assert VARIABLE.sub("", status) in allowed, f"'{status}' is not in docs/ux-guidelines.md"


def test_no_raw_state_names_or_retired_words_reach_the_screen(env):
    for task, running in situations(env):
        st = task.read_state()
        shown = ui.view(task, st, running, 3).status + tui.detail(task, st, 3, running=running)
        for word in ("checkpoint:", "approval:", "iteration"):
            assert word not in shown.replace(str(task.meta), ""), f"'{word}' shown for {st.goal}"


@pytest.mark.parametrize("size", [(140, 40)])
def test_every_key_the_panel_names_is_a_key_the_footer_offers(env, monkeypatch, size):
    tasks = situations(env)
    running = {task.id for task, alive in tasks if alive}
    monkeypatch.setattr(actions, "supervisor_running", lambda t: t.id in running)
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: tui.PodView() for i in ids})
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
                shown = tui.detail(task, st, 3, app.agent_running(st.id), app.pod)
                for key in re.findall(r"`([a-zA-Z])`", shown):
                    offered = any(app.check_action(action, ()) for action in by_key.get(key, []))
                    assert offered, f"{st.goal}: the panel names `{key}`, the footer does not offer it"

    asyncio.run(go())
