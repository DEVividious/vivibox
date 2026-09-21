"""Draws docs/img/view.svg, the picture of the view in the README: uv run python docs/img/screenshot.py

Made-up projects and tasks in a throwaway directory, one in every state worth showing. Nothing of
yours is read, and no pod is started.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ROOT = Path(tempfile.mkdtemp(prefix="vivibox-readme-"))
# The panel shows the review copy's path, and a temp path would be a lie about where it lives.
SHOWN_ROOT = "/srv/vivibox"
PROJECTS = ("shop", "fantasy")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.name", "You", cwd=path)
    git("config", "user.email", "you@example.com", cwd=path)
    (path / "README.md").write_text(f"# {path.name}\n")
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "Initial commit", cwd=path)


config = ROOT / "config"
(config / "projects").mkdir(parents=True)
(config / "config.toml").write_text(
    f'tasks_dir = "{ROOT / "srv" / "vivibox"}"\n'
    '[roles.planner]\nharness = "opencode"\nmodel = "deepseek/deepseek-v4-pro"\n'
    '[roles.writer]\nharness = "opencode"\nmodel = "deepseek/deepseek-v4-flash"\n'
)
for name in PROJECTS:
    make_repo(ROOT / name)
    (config / "projects" / f"{name}.toml").write_text(f'repo = "{ROOT / name}"\nverify = ["true"]\n')
os.environ.update(
    VIVIBOX_CONFIG_DIR=str(config),
    XDG_DATA_HOME=str(ROOT / "data"),
    XDG_CONFIG_HOME=str(ROOT / "xdg"),
    XDG_CACHE_HOME=str(ROOT / "cache"),
)

from vivibox import actions, gate, tui  # noqa: E402
from vivibox.config import load_project  # noqa: E402
from vivibox.pod import Listener  # noqa: E402
from vivibox.states import State  # noqa: E402

PLAN = """## Approach

{approach}

## Acceptance criteria

{criteria}
"""
RUNNING: set[str] = set()
SERVING: dict[str, tui.PodView] = {}
actions.supervisor_running = lambda task: task.id in RUNNING
actions.available_models = lambda refresh=False: {}
actions.provider_catalog = lambda refresh=False: []
tui.pod_views = lambda ids: {i: SERVING.get(i, tui.PodView()) for i in ids}


def task(project: str, goal: str, criteria: list[str], minutes_ago: int, approach: str = "…"):
    made = actions.create(project, goal, cwd=ROOT)
    head = made.plan_path.read_text().split("## ", 1)[0]
    listed = "\n".join(f"- [ ] {c}" for c in criteria)
    made.plan_path.write_text(head + PLAN.format(approach=approach, criteria=listed))
    made.event("started", model="deepseek/deepseek-v4-flash")
    RUNNING.add(made.id)
    made.minutes_ago = minutes_ago
    return made


def spent(made, planning: float, implementing: float = 0.0) -> None:
    made.event("turn", state="plan", ok=True, cost=planning, tokens=0, error="")
    if implementing:
        made.event("turn", state="implement", ok=True, cost=implementing, tokens=0, error="")


def accepted(made, ticked: int = 0):
    made.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
    gate.accept_plan(made, load_project(made.read_state().project).verify)
    made.transition(State.IMPLEMENT, reason="plan accepted")
    ticks = made.meta / "handoff" / gate.CRITERIA_FILE
    ticks.write_text(ticks.read_text().replace("- [ ]", "- [x]", ticked))


def aged(made, updated_minutes_ago: int) -> None:
    """The times a task that has been around for a while would show."""
    st = made.read_state()
    now = datetime.now(UTC)
    st.created = (now - timedelta(minutes=made.minutes_ago)).isoformat(timespec="milliseconds")
    st.updated = (now - timedelta(minutes=updated_minutes_ago)).isoformat(timespec="milliseconds")
    made._write_state(st)


def world() -> str:
    """Builds the tasks; returns the one the picture has selected."""
    plan = task(
        "shop", "Reject expired cards at checkout", ["Expired card gives 402", "Covered by a test"], 9
    )
    spent(plan, 0.03)
    plan.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
    aged(plan, 2)

    asks = task("fantasy", "Cache the fixtures feed", ["Feed is cached for an hour", "Works offline"], 41)
    spent(asks, 0.02, 0.04)
    accepted(asks, ticked=1)
    (asks.meta / "handoff" / "question.md").write_text(
        "Redis is in compose.yml but unused. Use it, or a file?\n"
    )
    asks.transition(State.CHECKPOINT_BLOCKED, reason="question from the agent")
    aged(asks, 6)

    review = task(
        "shop",
        "Add a /health endpoint with the database state",
        [
            "GET /health returns 200 with the version",
            "A failing database gives 503",
            "Both covered by a test",
        ],
        58,
    )
    spent(review, 0.04, 0.11)
    accepted(review, ticked=3)
    for name, text in (("HealthController.java", "class HealthController {}\n" * 40),
                       ("HealthControllerIT.java", "class HealthControllerIT {}\n" * 60)):  # fmt: skip
        (review.repo / name).write_text(text)
    git("add", ".", cwd=review.repo)
    git(
        "-c",
        "user.name=You",
        "-c",
        "user.email=you@example.com",
        "commit",
        "-qm",
        "Add /health",
        cwd=review.repo,
    )
    review.transition(State.VERIFY)
    review.event("gate", passed=True)
    review.transition(State.CHECKPOINT_FINAL, reason="verification passed")
    actions.prepare_review(review, load_project("shop"))
    SERVING[review.id] = tui.PodView("198.51.100.3", [Listener(8080, True)], demo=True)
    aged(review, 4)

    fixing = task(
        "shop",
        "Round VAT per line, not per invoice",
        ["Totals match the ledger", "Old invoices unchanged"],
        35,
    )
    spent(fixing, 0.03, 0.09)
    accepted(fixing, ticked=1)
    fixing.transition(State.VERIFY)
    fixing.event("gate", passed=False)
    fixing.transition(State.IMPLEMENT, reason="verification failed")
    aged(fixing, 3)

    verifying = task(
        "fantasy", "Show the dream team on one page", ["Page lists 11 players", "Loads under a second"], 27
    )
    spent(verifying, 0.02, 0.06)
    accepted(verifying, ticked=2)
    verifying.transition(State.VERIFY)
    aged(verifying, 1)

    planning = task("fantasy", "Export a gameweek to CSV", ["…"], 1)
    spent(planning, 0.01)
    aged(planning, 1)

    history = actions.history_path()
    history.parent.mkdir(parents=True, exist_ok=True)
    finished = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="milliseconds")
    entry = {"id": "fantasy-0", "project": "fantasy", "title": "Throttle failed logins", "cost": 0.12,
             "planning": 0.03, "commit": "5a1bcba9d2", "branch": "", "conflicts": [],
             "criteria": ["Five failures lock the account for a minute"], "created": finished,
             "finished": finished}  # fmt: skip
    history.write_text(json.dumps(entry) + "\n")
    return review.id


async def draw() -> None:
    selected = world()
    app = tui.Vivibox()
    async with app.run_test(size=(128, 34)) as pilot:
        await pilot.pause(0.5)
        app.reload()
        await pilot.pause(0.5)
        ids = [st.id for _, st in app.pairs]
        app.table.move_cursor(row=ids.index(selected))
        await pilot.press("d")
        await pilot.pause(0.5)
        svg = app.export_screenshot().replace(str(ROOT / "srv" / "vivibox"), SHOWN_ROOT)
        (HERE / "view.svg").write_text(svg)


asyncio.run(draw())
print(HERE / "view.svg")
