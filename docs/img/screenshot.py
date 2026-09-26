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
# Short: a throwaway path longer than the one shown in its place would cut lines that fit on a real
# screen.
ROOT = Path(tempfile.mkdtemp(prefix="v-"))
# The panel shows the review copy's path, and a temp path would be a lie about where it lives.
SHOWN_ROOT = "/srv/vivibox"
PROJECTS = ("payments-api", "storefront", "fixtures-feed")


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
    if planning:
        made.event("turn", state="plan", ok=True, cost=planning, tokens=int(planning * 6e6), error="")
    if implementing:
        made.event(
            "turn", state="implement", ok=True, cost=implementing, tokens=int(implementing * 6e6), error=""
        )


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


REVIEW_NOTE = """## Blocking

## Not blocking

- src/main/java/payments/HealthController.java:27 — the version is read from the manifest on
  every request; reading it once at start-up would do.
"""


def committed(made, *commits: tuple[str, str]) -> None:
    """Commits in the task's clone, (subject, file) each, so a diff stat has something to show."""
    for n, (subject, name) in enumerate(commits, 1):
        path = made.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"// {subject}\n" * (20 + 15 * n))
        git("add", ".", cwd=made.repo)
        git(
            "-c", "user.name=You", "-c", "user.email=you@example.com", "commit", "-qm", subject, cwd=made.repo
        )


def reviewed(made, note: str = REVIEW_NOTE, others: int = 1) -> None:
    """The gate passed and the reviewer read the work: the task waits for you with a review copy,
    from wherever it was (implementing, verifying, or already with the reviewer)."""
    state = made.read_state().state
    if state is State.IMPLEMENT:
        made.transition(State.VERIFY)
        state = State.VERIFY
    if state is State.VERIFY:
        made.event("gate", passed=True, iteration=1, log="verify-1-120000.log")
        made.transition(State.REVIEW, reason="verification passed")
    reviewing.keep(made, 1, note)
    made.set_reviews(1)
    made.event("review", round=1, blocking=0, not_blocking=others, problem="")
    made.transition(State.CHECKPOINT_FINAL, reason="verification passed; review 1: no blocking notes")
    actions.prepare_review(made, load_project(made.read_state().project))


def world() -> dict:
    """Three projects with tasks in every state worth showing; the ones the frames move later
    are returned by name."""
    moving = {}
    plan = task(
        "payments-api", "Reject expired cards at checkout", ["Expired card gives 402", "Covered by a test"], 9
    )
    spent(plan, 0.03)
    plan.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
    aged(plan, 2)

    health = task(
        "payments-api",
        "Add a /health endpoint with the database state",
        [
            "GET /health returns 200 with the version",
            "A failing database gives 503",
            "Both covered by a test",
        ],
        58,
    )
    spent(health, 0.04, 0.11)
    accepted(health, ticked=3)
    committed(
        health,
        ("Add the /health endpoint", "src/main/java/payments/HealthController.java"),
        ("Report the database state", "src/test/java/payments/HealthControllerTests.java"),
    )
    reviewed(health)
    SERVING[health.id] = tui.PodView("198.51.100.3", [Listener(8080, True)], demo=True)
    aged(health, 4)

    vat = task(
        "payments-api",
        "Round VAT per line, not per invoice",
        ["Totals match the ledger", "Old invoices unchanged"],
        35,
    )
    spent(vat, 0.03, 0.09)
    accepted(vat, ticked=1)
    vat.transition(State.VERIFY)
    vat.event("gate", passed=False, iteration=1, failed_commands=["./mvnw -B verify"])
    vat.transition(State.IMPLEMENT, reason="verification failed")
    aged(vat, 3)
    moving["vat"] = vat

    cart = task(
        "storefront",
        "Show the order total with VAT on the cart page",
        ["Total includes VAT", "Matches the checkout total"],
        27,
    )
    spent(cart, 0.02, 0.06)
    accepted(cart, ticked=2)
    committed(cart, ("Show the total with VAT", "src/features/cart/CartTotal.tsx"))
    cart.transition(State.VERIFY)
    aged(cart, 1)
    moving["cart"] = cart

    asks = task(
        "storefront",
        "Cache the product feed for an hour",
        ["Feed is cached for an hour", "Works offline"],
        41,
    )
    spent(asks, 0.02, 0.04)
    accepted(asks, ticked=1)
    (asks.meta / "handoff" / "question.md").write_text(
        "Redis is in compose.yml but unused. Use it, or a file?\n"
    )
    asks.transition(State.CHECKPOINT_BLOCKED, reason="question from the agent")
    aged(asks, 6)

    images = task("storefront", "Lazy-load product images below the fold", ["…"], 1)
    spent(images, 0.01)
    aged(images, 1)

    retry = task(
        "fixtures-feed",
        "Retry the upstream feed with backoff",
        ["Three retries, then an error", "Covered by a test"],
        22,
    )
    spent(retry, 0.02, 0.05)
    accepted(retry, ticked=2)
    committed(retry, ("Retry the feed with backoff", "fixtures_feed/upstream.py"))
    retry.transition(State.VERIFY)
    retry.event("gate", passed=True, iteration=1)
    retry.transition(State.REVIEW, reason="verification passed")
    aged(retry, 1)
    moving["retry"] = retry

    history = actions.history_path()
    history.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n, (project, title, cost, hours) in enumerate(
        (
            ("payments-api", "Throttle failed logins", 0.12, 3),
            ("fixtures-feed", "Parse kick-off times in the venue's zone", 0.07, 26),
            ("storefront", "Keep the cart across a sign-in", 0.09, 30),
        ),
        1,
    ):
        finished = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="milliseconds")
        entry = {"id": f"{project}-{n * 10}", "project": project, "title": title, "cost": cost,
                 "planning": 0.03, "commit": "5a1bcba9d2", "branch": "", "conflicts": [],
                 "criteria": [title], "created": finished, "finished": finished}  # fmt: skip
        lines.append(json.dumps(entry))
    history.write_text("\n".join(lines) + "\n")
    return moving


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


def animated(frames: list[tuple[str, float]]) -> str:
    """The frames, each with its seconds, as one SVG that shows them in turn, forever: a group per
    frame, its opacity switched by a discrete animation, so no script and no other file is needed."""
    total = sum(seconds for _, seconds in frames)
    view = frames[0][0].split('viewBox="')[1].split('"')[0]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">']
    at = 0.0
    for svg, seconds in frames:
        start, end = at / total, (at + seconds) / total
        at += seconds
        parts.append(
            f'<g opacity="0"><animate attributeName="opacity" calcMode="discrete" values="0;1;0;0" '
            f'keyTimes="0;{start:.4f};{min(end, 1):.4f};1" dur="{total:.1f}s" repeatCount="indefinite"/>'
            f"{svg}</g>"
        )
    parts.append("</svg>")
    return "\n".join(parts)


GOAL = "Add GET /invoices/{id}/pdf: the invoice rendered as a PDF, 404 when there is no such invoice"
CRITERIA = [
    "GET /invoices/{id}/pdf answers 200 with application/pdf",
    "The PDF carries the invoice number, the lines and the total",
    "An unknown id gives 404",
    "Both covered by a test",
]
APPROACH = """1. `InvoicePdf` renders an `Invoice` with the PDF library already in the pom.
2. `InvoiceController.pdf(id)` looks the invoice up and answers 404 through `ResponseStatusException`.
3. `InvoiceControllerTests` covers the found and the missing invoice."""
SUMMARY = "Serve invoices as PDF at GET /invoices/{id}/pdf"
COMMITS = (
    ("Render an invoice as a PDF document", "src/main/java/payments/InvoicePdf.java"),
    (
        "Serve the PDF at GET /invoices/{id}/pdf, 404 when missing",
        "src/main/java/payments/InvoiceController.java",
    ),
    ("Cover the PDF endpoint and the missing invoice", "src/test/java/payments/InvoiceControllerTests.java"),
)


async def flow(moving: dict) -> None:
    """One task from the description to the commit, among the others: the frames of flow.svg, and
    the still of view.svg from the same view."""
    from textual.widgets import TextArea

    from vivibox.dialogs import CommitWork
    from vivibox.widgets import Confirm

    frames: list[tuple[str, float]] = []

    def shot(seconds: float) -> None:
        frames.append((shown(app.export_screenshot()), seconds))

    async def settle() -> None:
        app.reload()
        await pilot.pause(0.4)

    def select(row_id: str) -> None:
        app.table.move_cursor(row=[str(k.value) for k in app.table.rows].index(row_id))

    app = tui.Vivibox()
    async with app.run_test(size=(128, 36)) as pilot:
        await pilot.pause(0.5)
        await settle()
        health = next(st.id for _, st in app.pairs if "health" in st.goal)
        select(health)
        await pilot.press("d")
        await pilot.pause(0.4)
        (HERE / "view.svg").write_text(shown(app.export_screenshot()))
        shot(2.5)  # the view: three projects, tasks in every state, one waiting with its review

        select("project:payments-api")
        await pilot.press("n")
        await pilot.pause(0.4)
        shot(0.8)  # n: a new task
        field = app.screen.query_one(TextArea)
        for cut in (18, 44, 70):
            field.text = GOAL[:cut]
            await pilot.pause(0.2)
            shot(0.5)
        field.text = GOAL
        await pilot.pause(0.2)
        shot(1.2)
        await pilot.press("escape")
        await pilot.pause(0.3)

        made = task("payments-api", GOAL, CRITERIA, 0, approach=APPROACH)
        made.plan_path.write_text(
            made.plan_path.read_text().replace('summary = ""', f'summary = "{SUMMARY}"', 1)
        )
        await settle()
        select(made.id)
        await pilot.pause(0.3)
        shot(1.5)  # planning, the panel on the new task

        spent(made, 0.03)
        made.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
        moving["cart"].event("gate", passed=True, iteration=1)
        moving["cart"].transition(State.REVIEW, reason="verification passed")
        await settle()
        shot(3.0)  # review the plan: the approach and the criteria

        accepted(made, ticked=0)
        reviewed(moving["retry"], others=0)
        RUNNING.discard(moving["retry"].id)
        await settle()
        shot(1.5)  # implementing, nothing ticked yet; a task of another project came to review

        ticks = made.meta / "handoff" / gate.CRITERIA_FILE
        ticks.write_text(ticks.read_text().replace("- [ ]", "- [x]", 2))
        spent(made, 0.0, 0.08)
        moving["vat"].transition(State.VERIFY)
        await settle()
        shot(1.5)  # two criteria ticked

        ticks.write_text(ticks.read_text().replace("- [ ]", "- [x]"))
        committed(made, *COMMITS)
        made.transition(State.VERIFY)
        (made.meta / "log" / "verify-1-120000.log").write_text("# fresh clone\n\n$ ./mvnw -B verify\n")
        reviewed(moving["cart"], others=0)
        RUNNING.discard(moving["cart"].id)
        await settle()
        shot(1.5)  # verifying

        made.event("gate", passed=True, iteration=1, log="verify-1-120000.log")
        made.transition(State.REVIEW, reason="verification passed")
        await settle()
        shot(1.2)  # the reviewer reads

        note = (
            "## Blocking\n\n## Not blocking\n\n- src/main/java/payments/InvoicePdf.java:52 — the font is"
            " loaded per document; once, in a field, would do.\n"
        )
        reviewing.keep(made, 1, note)
        made.set_reviews(1)
        made.event("review", round=1, blocking=0, not_blocking=1, problem="")
        made.transition(State.CHECKPOINT_FINAL, reason="verification passed; review 1: no blocking notes")
        actions.prepare_review(made, load_project("payments-api"))
        RUNNING.discard(made.id)
        await settle()
        shot(3.0)  # review the work: the diff, the reviewer's note

        await pilot.press("a")
        await pilot.pause(0.3)
        assert isinstance(app.screen, Confirm)
        shot(1.0)
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause(0.5)
        assert isinstance(app.screen, CommitWork)
        shot(3.0)  # the commit, from the plan's summary and the agent's commits
        await pilot.press("escape")
        await pilot.pause(0.3)
        await settle()
        shot(2.0)  # done, among the rest
    (HERE / "flow.svg").write_text(animated(frames))


asyncio.run(flow(world()))
print(HERE / "flow.svg", HERE / "view.svg")
