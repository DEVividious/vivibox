"""Draws docs/img/view.svg, the picture of the view in the README: uv run python docs/img/screenshot.py

Made-up projects and tasks in a throwaway directory, one in every state worth showing. Nothing of
yours is read, and no pod is started.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
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

from vivibox import actions, gate, reviewing, tui  # noqa: E402
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
    if made.read_state().state is not State.CHECKPOINT_PLAN:
        made.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
    gate.accept_plan(made)
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
        (HERE / "view.svg").write_text(shown(app.export_screenshot()))


# --- docs/img/flow.svg: one task from the description to the commit, as an animated picture ---

SECONDS_PER_FRAME = 3
REVIEW = """## Blocking

## Not blocking

- src/main/java/shop/CardValidator.java:41 — the expiry check compares strings; comparing YearMonth
  values would survive a two-digit year. Fine as it is for the plan.
"""


TEXT = re.compile(r'(<text[^>]*textLength=")([\d.]+)("[^>]*>)(.*?)(</text>)', re.S)


def shown(svg: str) -> str:
    """The screenshot with the throwaway paths shown as the ones a person would have. Rich sizes
    every text run to its original length (textLength), so a shorter path is scaled with it, or
    the letters would be spread out to fill the old width."""
    replacements = (
        (str(ROOT / "srv" / "vivibox"), SHOWN_ROOT),
        (str(ROOT / "data" / "vivibox"), "~/.local/share/vivibox"),
        (str(ROOT), "~/projects"),
    )

    def fix(m: re.Match) -> str:
        before, width, mid, content, end = m.groups()
        new = content
        for real, seen in replacements:
            new = new.replace(real, seen)
        if new == content:
            return m.group(0)
        ratio = len(html.unescape(new)) / len(html.unescape(content))
        return f"{before}{float(width) * ratio:.1f}{mid}{new}{end}"

    return TEXT.sub(fix, svg)


def frame(app, frames: list[str]) -> None:
    frames.append(shown(app.export_screenshot()))


def animated(frames: list[str]) -> str:
    """The frames as one SVG that shows them in turn, forever: a group per frame, its opacity
    switched by a discrete animation, so no script and no external file is needed."""
    total = SECONDS_PER_FRAME * len(frames)
    head = frames[0].split(">", 1)[0] + ">"
    view = head.split('viewBox="')[1].split('"')[0]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">']
    for i, svg in enumerate(frames):
        start, end = i / len(frames), (i + 1) / len(frames)
        parts.append(
            f'<g opacity="0"><animate attributeName="opacity" calcMode="discrete" values="0;1;0;0" '
            f'keyTimes="0;{start:.4f};{end:.4f};1" dur="{total}s" repeatCount="indefinite"/>{svg}</g>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


async def flow() -> None:
    """Runs before world(): the list has one project and the task the frames follow."""
    from textual.widgets import TextArea

    from vivibox.dialogs import CommitWork
    from vivibox.widgets import Confirm

    frames: list[str] = []
    goal = "Reject expired cards at checkout: a card past its date is refused before payment"
    criteria = [
        "Expired card gives 402 at /checkout",
        "A card expiring this month is accepted",
        "Both covered by a test",
    ]
    app = tui.Vivibox()
    async with app.run_test(size=(128, 34)) as pilot:
        await pilot.pause(0.5)
        app.reload()
        await pilot.pause(0.5)
        ids = [str(key.value).removeprefix("project:") for key in app.table.rows]
        app.table.move_cursor(row=ids.index("shop"))
        await pilot.press("n")
        await pilot.pause(0.5)
        app.screen.query_one(TextArea).text = goal
        await pilot.pause(0.3)
        frame(app, frames)  # 1. the task, described
        await pilot.press("escape")
        await pilot.pause(0.3)

        made = task("shop", goal, criteria, 0)
        made.plan_path.write_text(
            made.plan_path.read_text().replace(
                'summary = ""', 'summary = "Refuse expired cards at checkout"', 1
            )
        )
        app.reload()
        await pilot.pause(0.5)
        app.table.move_cursor(row=[str(k.value) for k in app.table.rows].index(made.id))
        await pilot.press("d")
        await pilot.pause(0.5)
        frame(app, frames)  # 2. planning

        spent(made, 0.03)
        made.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
        app.reload()
        await pilot.pause(0.5)
        frame(app, frames)  # 3. review the plan

        accepted(made, ticked=2)
        spent(made, 0.0, 0.07)
        app.reload()
        await pilot.pause(0.5)
        frame(app, frames)  # 4. implementing, two criteria ticked

        ticks = made.meta / "handoff" / gate.CRITERIA_FILE
        ticks.write_text(ticks.read_text().replace("- [ ]", "- [x]"))
        for name, lines, subject in (
            ("CardValidatorTest.java", 45, "Cover expired and current cards at checkout"),
            ("CardValidator.java", 30, "Refuse a card past its expiry date"),
        ):
            (made.repo / name).write_text(f"class {name[:-5]} {{}}\n" * lines)
            git("add", ".", cwd=made.repo)
            git(
                "-c",
                "user.name=You",
                "-c",
                "user.email=you@example.com",
                "commit",
                "-qm",
                subject,
                cwd=made.repo,
            )
        made.transition(State.VERIFY)
        (made.meta / "log" / "verify-1-120000.log").write_text("# fresh clone\n\n$ ./mvnw -B verify\n")
        app.reload()
        await pilot.pause(0.5)
        frame(app, frames)  # 5. verifying

        made.event("gate", passed=True, iteration=1, log="verify-1-120000.log")
        made.transition(State.REVIEW, reason="verification passed")
        reviewing.keep(made, 1, REVIEW)
        made.set_reviews(1)
        made.event("review", round=1, blocking=0, not_blocking=1, problem="")
        made.transition(State.CHECKPOINT_FINAL, reason="verification passed; review 1: no blocking notes")
        actions.prepare_review(made, load_project("shop"))
        RUNNING.discard(made.id)
        app.reload()
        await pilot.pause(0.5)
        frame(app, frames)  # 6. review the work, the reviewer's note with it

        await pilot.press("a")
        await pilot.pause(0.3)
        assert isinstance(app.screen, Confirm)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause(0.5)
        assert isinstance(app.screen, CommitWork)
        frame(app, frames)  # 7. the commit, written from the plan and the agent's commits
        await pilot.press("escape")
        await pilot.pause(0.3)
    (HERE / "flow.svg").write_text(animated(frames))


asyncio.run(flow())
print(HERE / "flow.svg")
asyncio.run(draw())
print(HERE / "view.svg")
