"""Draws the README tour and still view: uv run python docs/img/screenshot.py

Fictional projects in a throwaway directory; no provider calls or pods. Add --gif to render
the GitHub hero (requires Pillow, Playwright and Chromium; see CONTRIBUTING.md).
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
# Short: a throwaway path longer than the one shown in its place would cut lines that fit on a real
# screen.
ROOT = Path(tempfile.mkdtemp(prefix="v-"))
# The panel shows the review copy's path, and a temp path would be a lie about where it lives.
SHOWN_ROOT = "/srv/vivibox"
PROJECTS = ("payments-api", "storefront")
# No personal Git hooks, signing settings or identities enter the disposable repositories.
os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


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
    '[roles.reviewer]\nharness = "opencode"\nmodel = "deepseek/deepseek-v4-pro"\n'
)
for name in PROJECTS:
    make_repo(ROOT / name)
    (config / "projects" / f"{name}.toml").write_text(f'repo = "{ROOT / name}"\nverify = ["true"]\n')
# Browser binaries belong to the recording tool, not the isolated app fixture's cache.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"),
)
os.environ.update(
    VIVIBOX_CONFIG_DIR=str(config),
    XDG_DATA_HOME=str(ROOT / "data"),
    XDG_CONFIG_HOME=str(ROOT / "xdg"),
    XDG_CACHE_HOME=str(ROOT / "cache"),
)

# Never discover the recording session or read its transcript.
for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
    os.environ.pop(name, None)

# Render the product palette even when the invoking shell disables terminal colour.
os.environ.pop("NO_COLOR", None)
os.environ["TZ"] = "UTC"
time.tzset()

from vivibox import actions, gate, keys, probe, reviewing, skill, table, tui, ui  # noqa: E402
from vivibox import task as task_module  # noqa: E402
from vivibox.config import ORCHESTRATION_MODES, load_project  # noqa: E402
from vivibox.states import State  # noqa: E402

PLAN = """## Approach

{approach}

## Acceptance criteria

{criteria}
"""
# A key in the throwaway store, or the header would warn that no task can start.
keys.set_key("deepseek", "sk-made-up")
RUNNING: set[str] = set()
SERVING: dict[str, tui.PodView] = {}
actions.supervisor_running = lambda task: task.id in RUNNING
actions.available_models = lambda refresh=False: {}
actions.provider_catalog = lambda refresh=False: []
tui.pod_views = lambda ids: {i: SERVING.get(i, tui.PodView()) for i in ids}
probe.docker_running = lambda: True
skill.copies = lambda: []


class DemoClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 1, 15, 12, 0, tzinfo=UTC)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


# Stable visible times, without changing asyncio's monotonic clock.
task_module.datetime = ui.datetime = table.datetime = DemoClock


def task(project: str, goal: str, criteria: list[str], minutes_ago: int, approach: str = "…", **options):
    made = actions.create(project, goal, cwd=ROOT, **options)
    head = made.plan_path.read_text().split("## ", 1)[0]
    listed = "\n".join(f"- [ ] {c}" for c in criteria)
    made.plan_path.write_text(head + PLAN.format(approach=approach, criteria=listed))
    made.event("started", model="deepseek/deepseek-v4-flash")
    RUNNING.add(made.id)
    made.minutes_ago = minutes_ago
    return made


def spent(made, planning: float, implementing: float = 0.0, review: float = 0.0) -> None:
    for state, cost in (("plan", planning), ("implement", implementing), ("review", review)):
        if cost:
            made.event("turn", state=state, ok=True, cost=cost, tokens=int(cost * 6e6), error="")


def subscription(made, stage: str, cost: float) -> None:
    """Fictional usage records; no CLI session or transcript is opened."""
    made.event("cli_usage", stage=stage, cost=cost, tokens={"input": 12000, "output": 1000})


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
    now = DemoClock.now(UTC)
    st.created = (now - timedelta(minutes=made.minutes_ago)).isoformat(timespec="milliseconds")
    st.updated = (now - timedelta(minutes=updated_minutes_ago)).isoformat(timespec="milliseconds")
    made._write_state(st)


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


def world():
    """All four flows will have a task; the CLI supervisor starts with no plan."""
    waiting = task(
        "payments-api",
        "Reject expired cards",
        ["Expired card gives 402"],
        9,
        approach="Validate the expiry date before submitting the charge.",
        orchestration="planner_executor",
        plan_in_cli=True,
    )
    subscription(waiting, "planning", 0.41)
    waiting.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
    aged(waiting, 2)
    working = task(
        "storefront",
        "Show the cart total with VAT",
        ["Total includes VAT"],
        12,
        orchestration="single_agent",
    )
    spent(working, 0.02, 0.06)
    accepted(working)
    aged(working, 1)
    cli = task(
        "storefront",
        "Add CSV export",
        [],
        2,
        orchestration="supervisor_worker",
        plan_in_cli=True,
    )
    # The CLI has not imported a plan yet.
    head = cli.plan_path.read_text().split("## ", 1)[0]
    cli.plan_path.write_text(head + "## Approach\n\n## Acceptance criteria\n")
    cli.transition(State.CHECKPOINT_PLAN, reason="waiting for the CLI's plan")
    cli.set_awaiting_plan(True)
    aged(cli, 0)
    return cli


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

    svg = re.sub(r"@font-face\s*\{[^}]*\}", "", svg)
    svg = svg.replace("Fira Code", "DejaVu Sans Mono")
    return "\n".join(line.rstrip() for line in TEXT.sub(fix, svg).splitlines()) + "\n"


GOAL = "Add PDF downloads for invoices"
CRITERIA = [
    "GET /invoices/{id}/pdf returns a PDF with the invoice lines and total",
    "An unknown invoice returns 404",
    "Both cases are covered by tests",
]
APPROACH = "Use the existing PDF library; add the endpoint and tests."
SUMMARY = "Serve invoices as PDF at GET /invoices/{id}/pdf"
COMMITS = (
    ("Render an invoice as a PDF document", "src/main/java/payments/InvoicePdf.java"),
    (
        "Serve the PDF at GET /invoices/{id}/pdf, 404 when missing",
        "src/main/java/payments/InvoiceController.java",
    ),
    ("Cover the PDF endpoint and the missing invoice", "src/test/java/payments/InvoiceControllerTests.java"),
)


async def flow(cli) -> None:
    """Eleven scenes, 44 seconds; synthetic progress, real screens and flow descriptions."""
    from textual.widgets import Select, TextArea

    frames: list[tuple[str, float, str, str]] = []

    def shot(seconds: float, title: str, detail: str) -> None:
        frames.append((shown(app.export_screenshot()), seconds, title, detail))

    async def settle() -> None:
        app.reload()
        await pilot.pause(0.4)

    def select(row_id: str) -> None:
        app.table.move_cursor(row=[str(k.value) for k in app.table.rows].index(row_id))

    app = tui.Vivibox()
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause(0.5)
        await settle()
        select(cli.id)
        await pilot.press("d")
        await pilot.pause(0.3)
        shot(3, "Plan in your CLI", "This task waits for a plan from Claude Code or Codex.")

        await pilot.press("d")
        select("project:payments-api")
        await pilot.press("n")
        await pilot.pause(0.4)
        app.screen.query_one(TextArea).text = GOAL
        app.screen.query_one("#orchestration").focus()
        await pilot.pause(0.3)
        captions = (
            "One session plans, writes and self-reviews; then verification and you.",
            "Planner hands off; Executor writes and self-reviews; you accept the result.",
            "You approve the plan; Writer codes; verification passes; Reviewer reads.",
            "Supervisor plans and reviews each verified round; you accept the result.",
        )
        for (name, mode), caption in zip(ORCHESTRATION_MODES.items(), captions, strict=True):
            app.screen.query_one("#orchestration", Select).value = name
            await pilot.pause(0.3)
            shot(4, mode.label, caption)
        await pilot.press("escape")
        await pilot.pause(0.3)

        made = task("payments-api", GOAL, CRITERIA, 0, approach=APPROACH)
        made.plan_path.write_text(
            made.plan_path.read_text().replace('summary = ""', f'summary = "{SUMMARY}"', 1)
        )
        spent(made, 0.03)
        made.transition(State.CHECKPOINT_PLAN, reason="plan ready for review")
        await settle()
        select(made.id)
        await pilot.press("d")
        await pilot.pause(0.3)
        app.panel.scroll_home(animate=False)
        await pilot.pause(0.2)
        shot(4, "You approve the plan", "Planner → Writer → Reviewer: agree on the criteria before coding.")

        accepted(made, ticked=3)
        spent(made, 0.0, 0.08)
        committed(made, *COMMITS)
        made.transition(State.VERIFY)
        (made.meta / "log" / "verify-1-120000.log").write_text(
            "# fresh clone of the committed work\n\n$ ./mvnw -B verify\nBUILD SUCCESS\n"
        )
        await settle()
        shot(3, "Verify the committed work", "Build and test a fresh clone before independent review.")

        made.event("gate", passed=True, iteration=1, log="verify-1-120000.log")
        made.transition(State.REVIEW, reason="verification passed")
        await settle()
        shot(3, "Review after verification", "A separate agent reads the changes against the accepted plan.")

        note = (
            "## Blocking\n\n- src/main/java/payments/InvoicePdf.java:52 — totals lose cents; "
            "preserve decimal precision and add a fractional-total test.\n\n## Not blocking\n\n"
            "## Checked\n\n- PDF endpoint, missing invoice and decimal-total coverage.\n"
        )
        assert not reviewing.problem(note)
        reviewing.keep(made, 1, note)
        made.set_reviews(1)
        spent(made, 0.0, review=0.02)
        made.event("review", round=1, blocking=1, not_blocking=0, problem="")
        made.transition(State.IMPLEMENT, reason="review 1: 1 blocking", why="1 blocking note")
        await settle()
        app.panel.scroll_to(y=5, animate=False)
        await pilot.pause(0.2)
        shot(4, "Send blocking feedback back", "The writer fixes it. Verification and review run again.")

        committed(
            made, ("Keep decimal precision in invoice totals", "src/main/java/payments/InvoicePdf.java")
        )
        spent(made, 0.0, 0.03)
        made.transition(State.VERIFY)
        made.event("gate", passed=True, iteration=2, log="verify-2-120000.log")
        made.transition(State.REVIEW, reason="verification passed")
        clean = "## Blocking\n\n## Not blocking\n\n## Checked\n\n- Endpoint and regression tests.\n"
        assert not reviewing.problem(clean)
        reviewing.keep(made, 2, clean)
        made.set_reviews(2)
        spent(made, 0.0, review=0.02)
        made.event("review", round=2, blocking=0, not_blocking=0, problem="")
        made.transition(State.CHECKPOINT_FINAL, reason="verification passed; review 2: no blocking notes")
        actions.prepare_review(made, load_project("payments-api"))
        RUNNING.discard(made.id)
        # A different task: the same CLI that planned it reads the second verified round.
        head = cli.plan_path.read_text().split("## ", 1)[0]
        cli.plan_path.write_text(
            head
            + PLAN.format(
                approach="Export each order once, with its identifier and total.",
                criteria="- [ ] Export includes each order exactly once",
            )
        )
        cli.set_awaiting_plan(False)
        subscription(cli, "planning", 0.62)
        accepted(cli, ticked=1)
        committed(cli, ("Export orders as CSV", "src/orders/export.ts"))
        spent(cli, 0, 0.06)
        cli.transition(State.VERIFY)
        cli.event("gate", passed=True, iteration=1)
        cli.transition(State.REVIEW)
        cli_note = (
            "## Blocking\n\n- src/orders/export.ts:8 — repeated page cursors "
            "duplicate orders; deduplicate and cover overlapping pages.\n\n"
            "## Not blocking\n\n## Checked\n\n- Export and pagination tests.\n"
        )
        assert not reviewing.problem(cli_note)
        reviewing.keep(cli, 1, cli_note)
        cli.set_reviews(1)
        subscription(cli, "review", 0.28)
        cli.event("review", round=1, blocking=1, not_blocking=0, problem="")
        cli.transition(State.IMPLEMENT, why="1 blocking note")
        committed(cli, ("Deduplicate orders across export pages", "src/orders/export.ts"))
        spent(cli, 0, 0.02)
        cli.transition(State.VERIFY)
        cli.event("gate", passed=True, iteration=2)
        cli.transition(State.REVIEW)
        copy = actions.prepare_review(cli, load_project("storefront"))
        reviewing.ask_cli(cli, 2, copy)
        await settle()
        select(cli.id)
        app.panel.scroll_home(animate=False)
        await pilot.pause(0.3)
        shot(
            6,
            "Supervisor ⇄ Worker · review in your CLI",
            "Same CLI, next review. $… sub is estimated usage, apart from API spending.",
        )

        made.set_paused(True)
        await settle()
        select(made.id)
        await pilot.pause(0.3)
        app.panel.scroll_home(animate=False)
        await pilot.pause(0.2)
        (HERE / "view.svg").write_text(shown(app.export_screenshot()))
        shot(5, "You decide what lands", "Pod stopped. Inspect the review copy; accept, or ask for changes.")
    if "--gif" in sys.argv:
        from animation import render_gif

        await render_gif(frames, HERE / "flow.gif")


try:
    asyncio.run(flow(world()))
finally:
    shutil.rmtree(ROOT)
print("Generated README tour and still view in", HERE)
