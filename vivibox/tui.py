"""Interactive view: 'vivibox' with no arguments. Your tasks, live, and your decisions one key away.

Every action calls the same functions as the command line (actions.py); this module only shows state
and asks. Slow steps (starting a pod, accepting work) run in threads so the view stays responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from . import actions, code, context, gate, ide, keys, manual, providers, supervisor, ui
from . import init as project_init
from .config import ConfigError, config_dir, load_config, load_project
from .plan import PlanError, parse_plan
from .plan import body as plan_body
from .states import State
from .task import Task, TaskState, list_tasks

REFRESH_SECONDS = 2.0
# How often the view looks whether vivibox changed on disk: a git pull, not a keystroke.
CODE_CHECK_SECONDS = 10.0
CODE_CHANGED = "vivibox changed on disk: quit and start it again"
OLDER_SUPERVISOR = "runs an older vivibox; stop and start it (`s`) when it suits you"
SPIN_SECONDS = 0.1
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PROTECTED_BRANCHES = ("main", "master")


def criteria(task: Task) -> str:
    try:
        if (task.meta / gate.ACCEPTED_PLAN).exists():
            total = len(parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria)
            return f"{total - len(gate.missing_criteria(task))}/{total}"
        return f"0/{len(parse_plan(task.plan_path.read_text()).criteria)}"
    except (OSError, PlanError, gate.GateError):
        return "-"


def checklist(task: Task) -> list[str]:
    """Every criterion of the accepted plan, and which of them the agent reports as met."""
    try:
        wanted = [c.text for c in parse_plan((task.meta / gate.ACCEPTED_PLAN).read_text()).criteria]
        missing = set(gate.missing_criteria(task))
    except (OSError, PlanError, gate.GateError):
        return []
    return [f"- {'☐' if text in missing else '☑'} {text}" for text in wanted]


@dataclass
class PodView:
    """What the pod is doing right now: asked once, used for both the panel and the keys."""

    address: str = ""
    listening: list = field(default_factory=list)
    demo: bool = False
    log: str = ""

    @property
    def reachable(self) -> list:
        """The ports something outside the pod can actually open."""
        return [p for p in self.listening if p.reachable]

    @property
    def state(self) -> str:
        """What became of the demo, named once so the row and the panel cannot drift apart.
        `local` is its own state and not `starting`: nothing is coming, the app bound the wrong
        interface, and waiting for it is waiting forever."""
        if not self.demo:
            return "stopped" if self.log else ""
        if self.reachable:
            return "live"
        return "local" if self.listening else "starting"

    def lines(self) -> list[str]:
        """What the pod is doing, in the states that can be told apart from outside it."""
        if not self.address:
            return []
        where = [f"[{self.address}:{p.port}](http://{self.address}:{p.port})" for p in self.reachable]
        shown = ", ".join(where)
        closed = ", ".join(f"`{p.port}`" for p in self.listening if not p.reachable)
        state = {
            "live": f"running · open at {shown}",
            "local": f"running, but {closed} is bound to localhost and nothing outside can reach it",
            "starting": "running, nothing listening yet",
        }.get(self.state, "not running")
        out = [f"Pod `{self.address}` · demo {state}"]
        if not self.demo and self.log:
            # It ran and is gone. What it said last is the only thing that explains why.
            out += ["", "It stopped. Its last output:", "", f"```\n{self.log}\n```"]
        return out


def ticked_at(task: Task) -> float:
    """When the agent last touched its checklist. Cheap enough to ask on every refresh."""
    try:
        return (task.meta / "handoff" / gate.CRITERIA_FILE).stat().st_mtime
    except OSError:
        return 0.0


def pod_views(task_ids: list[str]) -> dict[str, PodView]:
    """What every task's pod is doing. One question for all the addresses, then one per pod that is
    up; asking each pod separately for each thing is what would make this too slow to do often."""
    pods = {task_id: actions.Pod(task_id, Path("."), "") for task_id in task_ids}
    found = actions.pod_module.addresses([pod.sidecar for pod in pods.values()])
    views = {}
    for task_id, pod in pods.items():
        if not (address := found.get(pod.sidecar, "")):
            views[task_id] = PodView()
            continue
        probe = pod.probe()
        views[task_id] = PodView(address, probe.listening, probe.demo, probe.log)
    return views


def pod_view(task_id: str) -> PodView:
    return pod_views([task_id])[task_id]


def removed_tests(task: Task) -> list[str]:
    """Tests the work removed, from the last verification: a refactoring does that rightly, and
    a shortcut does it too, so it is yours to judge."""
    for event in reversed(task.events()):
        if event["type"] == "gate":
            gone = event["data"].get("removed") or []
            n = event["data"].get("removed_tests", len(gone))
            if not n:
                return []
            return [
                "",
                f"**{n} test{'s' if n != 1 else ''} removed**; see whether the plan meant it:",
                *(f"- `{g}`" for g in gone),
            ]
    return []


def plans_verify(task: Task, st: TaskState) -> list[str]:
    """The command the plan brings for a project that has none yet: accepting the plan makes it the
    project's for good, so it is shown where you decide."""
    try:
        verify = parse_plan(read(task.plan_path)).verify
        project_has_one = bool(actions.load(st.id)[1].verify)
    except Exception:
        return []
    if not verify or project_has_one:
        return []
    return [
        "**Verification:** "
        + " && ".join(f"`{c}`" for c in verify)
        + ". From the plan; it becomes the project's command when you accept.",
        "",
    ]


def build_said(log: Path, feedback: str = "") -> list[str]:
    """The lines of the last verification's log that say what failed, and where the rest is. The
    feedback the gate writes now quotes them itself; then only the log's place is added."""
    text = read(log)
    if not text:
        return []
    if "```" in feedback:
        return [f"Full log: `{log}`", ""]
    return [
        "#### What the build said",
        "",
        f"```\n{gate.log_excerpt(text)}\n```",
        "",
        f"Full log: `{log}`",
        "",
    ]


def newest_log(task: Task) -> Path | None:
    """The log to read when something went wrong: the newest verification's, else the supervisor's."""
    logs = sorted((task.meta / "log").glob("verify-*.log"), key=lambda p: p.stat().st_mtime)
    if logs:
        return logs[-1]
    supervisor_log = task.meta / "log" / "supervisor.log"
    return supervisor_log if supervisor_log.exists() else None


def verification_running(task: Task, st: TaskState) -> list[str]:
    """What the verification is doing right now: how long, which command, its last lines. The
    log is written as the commands run, so it is the one thing that moves while you wait."""
    log = newest_log(task)
    text = read(log) if log and log.name.startswith("verify-") else ""
    commands = re.findall(r"^\$ (.+)$", text, re.MULTILINE)
    running = " ".join(filter(None, ["**Verification running**", ui.lasting(st.updated)]))
    lines = [running + (f": `{commands[-1]}`" if commands else "") + "."]
    if text:
        tail = "\n".join(text.splitlines()[-12:])
        lines += ["", f"```\n{tail}\n```", "", f"Full log: `{log}`"]
    return lines


def git_diff(task: Task, project) -> list[str]:
    """The work as a diff, from the review ref in your repository: what accepting would bring.
    git pages it itself, so this runs with the terminal handed over."""
    base = task.read_state().base_commit
    return ["git", "-C", str(project.repo), "diff", f"{base}...{actions.repo.review_ref(task.id)}"]


def pager_command(path: Path, follow: bool = False, at_end: bool = False) -> list[str]:
    """Your pager on the file. With less: following it as it is written (Ctrl-C stops following),
    or opened at its end, where a failed build says why."""
    pager = os.environ.get("PAGER") or shutil.which("less") or "more"
    command = pager.split()
    if Path(command[0]).name == "less":
        command += ["+F"] if follow else ["+G"] if at_end else []
    return [*command, str(path)]


def log_command(task: Task, st: TaskState, running: bool) -> list[str] | None:
    """What l opens: the log being written, followed, while the verification runs; else the
    newest one, at its end."""
    log = newest_log(task)
    if log is None:
        return None
    following = st.state is State.VERIFY and running and log.name.startswith("verify-")
    return pager_command(log, follow=following, at_end=not following)


def after_window(session_gone: bool) -> None:
    """Back from the agent's window: tmux prints [exited] when the session ended under the client,
    and the line would stay on the terminal you come back to after quitting. Wiped here."""
    if session_gone:
        sys.stdout.write("\x1b[1A\x1b[2K")
        sys.stdout.flush()


def next_steps(task: Task, st: TaskState, seen: ui.TaskView, running: bool, pod: PodView) -> str:
    """The keys that move this task on, first thing in the panel. Only keys the footer offers now:
    a hint the footer contradicts is worse than none."""
    watch = " · `w` look at the agent" if watchable(task, st, running) else ""
    if st.box and st.state is State.IMPLEMENT:
        if st.paused:
            return "`s` open the box again"
        return (
            "`w` enter the box (Ctrl-q leaves) · `a` close it and bring its work to review · `v` run the app"
        )
    if seen.problem:
        return "`s` try again"
    if seen.status in ("not started",):
        return "`e` write the plan yourself · `s` start"
    if seen.status in ("stopped", "not running"):
        return "`s` start; it goes on from where it was"
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        return "`c` copy the prompt for a browser · `C` for a CLI · `e` paste the plan"
    if st.state is State.CHECKPOINT_PLAN:
        return "`a` accept the plan · `r` send it back with a comment · `e` edit it"
    if st.state is State.CHECKPOINT_FINAL:
        return "`f` the diff · `o` open the review copy · `v` run the app · `a` accept · `r` ask for changes"
    if st.state is State.APPROVAL_RISKY:
        return "`p` approve the files as shown · `r` send the agent back"
    if st.state is State.CHECKPOINT_BLOCKED:
        if (task.meta / "handoff" / supervisor.QUESTION).exists():
            return "`r` answer · `g` verify again, once you have fixed what it names" + watch
        if ui.environment_problem(task):
            return "`g` verify again, once you have fixed it · `r` tell the agent" + watch
        return "`g` verify again, when what failed was outside the code · `r` tell the agent" + watch
    if st.state is State.VERIFY:
        look = " · `w` look at it as it runs" if watchable(task, st, running) else ""
        log = " · `l` read its log so far" if newest_log(task) else ""
        return "wait for the verification" + look + log
    return "wait" + watch + " · `s` stop"


def gate_failed(task: Task) -> bool:
    gates = [e for e in task.events() if e["type"] == "gate"]
    return bool(gates) and not gates[-1]["data"].get("passed")


def last_gate(task: Task) -> str:
    """How the last gate run went, so a checklist that has not moved still shows whether work has."""
    for event in reversed(task.events()):
        if event["type"] == "gate":
            outcome = "passed" if event["data"].get("passed") else "failed"
            return f"Verification {outcome} at {ui.clock(event['ts'])}."
    return "No verification yet."


PROJECT_ROW = "project:"
# The keys that decide something, first in the footer and never off it, however narrow the terminal.
DECISION_KEYS = ("a", "r", "p", "g")


def view_state_path() -> Path:
    return actions.history_path().with_name("view.json")


def load_collapsed() -> set[str]:
    """The projects you folded away; a choice of yours, so it outlives the view."""
    try:
        return set(json.loads(view_state_path().read_text()).get("collapsed", []))
    except (OSError, ValueError):
        return set()


def save_collapsed(names: set[str]) -> None:
    path = view_state_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"collapsed": sorted(names)}) + "\n")


def project_detail(name: str, tasks: int, problem: str) -> str:
    """A project as the panel shows it: where it is, how it is built and tested, and what would
    keep its tasks from starting."""
    path = config_dir() / "projects" / f"{name}.toml"
    lines = [f"### {name}", ""]
    if problem:
        lines += [f"**{problem}.**", ""]
    lines += [
        "**Next:** `n` new task · `e` edit the project's file · `o` open the repository in your IDE"
        + (" · `x` forget the project" if not tasks else ""),
        "",
        f"`{path}`",
        "",
        f"```toml\n{read(path)}\n```",
    ]
    return "\n".join(lines)


def edit_in_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or "vi"
    subprocess.run([*editor.split(), str(path)])


def project_repos() -> list[Path]:
    """The folder you started in and your projects' repositories: where a project's own
    opencode.json would be."""
    found = [Path.cwd()]
    for name in projects():
        with contextlib.suppress(ConfigError, OSError):
            found.append(load_project(name).repo)
    return list(dict.fromkeys(found))


def projects() -> list[str]:
    """The projects you can work in; one whose repository is gone is not offered."""
    broken = actions.broken_projects()
    return [p.stem for p in actions.project_files() if p.stem not in broken]


def read(path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def deleted_detail(entry: dict) -> str:
    """A task you deleted: what it was for and how far it got; nothing of it is left."""
    when = ui.when_deleted(entry["deleted"])
    return "\n".join(
        [
            f"### {entry['id']} · deleted",
            "",
            f"*{entry['project']} · {ui.ago(entry['finished'])} · {ui.finished_cost(entry)}*",
            "",
            entry["title"],
            "",
            f"Deleted{f' {when}' if when else ''}; its files and its work went with it.",
            "",
            "Press `x` to delete it from the history, `h` to hide finished tasks.",
        ]
    )


def finished_detail(entry: dict) -> str:
    """A task you accepted: what it left in your repository; or one you deleted."""
    if entry.get("deleted"):
        return deleted_detail(entry)
    if entry["conflicts"]:
        where = f"with conflicts in {', '.join(entry['conflicts'])}, also on `{entry['branch']}`"
    elif entry["branch"]:
        where = f"on branch `{entry['branch']}`"
    else:
        where = "in your checkout"
    # Tasks accepted before criteria were kept have none; say so rather than show an empty list.
    met = entry.get("criteria")
    delivered = (
        ["**It was accepted as meeting:**", "", *(f"- ☑ {text}" for text in met), ""]
        if met
        else ["*Its criteria were not recorded; it finished before vivibox kept them.*", ""]
    )
    kept = actions.archive_path(entry["id"])
    plan = read(kept / gate.ACCEPTED_PLAN) or read(kept / "plan.md")
    archived = (
        [f"Its plan and its events are kept in `{kept}`.", "", "#### The plan", "", plan_body(plan)]
        if plan
        else []
    )
    return "\n".join(
        [
            f"### {entry['id']} · done",
            "",
            f"*{entry['project']} · {ui.ago(entry['finished'])} · {ui.finished_cost(entry)}*",
            "",
            entry["title"],
            "",
            f"Its work is {where}, from commit `{entry['commit']}`.",
            "",
            *delivered,
            "Press `x` to delete it from the history, `h` to hide finished tasks.",
            "",
            *archived,
        ]
    )


def watchable(task: Task, st: TaskState, running: bool) -> bool:
    """Whether w has something to show, and only while the task is at work: the verification's
    log once the gate has opened it, else the agent, which only opencode has a window for. The
    footer and every hint that names w ask here."""
    if not running or st.paused:
        return False
    if st.state is State.VERIFY:
        return actions.verification_log(task, st) is not None
    return bool(actions.watchable_session(task, st))


def detail(
    task: Task, st: TaskState, max_iterations: int, running: bool = True, pod: PodView | None = None
) -> str:
    """What you need to decide on this task, as markdown."""
    seen = ui.view(task, st, running, max_iterations)
    watch = " Look at the agent with `w`." if watchable(task, st, running) else ""
    head = [
        f"### {st.id} · {seen.status}",
        "",
        f"*criteria {criteria(task)} · updated {ui.ago(st.updated)}"
        + (f" · cost {ui.cost(task)}*" if st.box else f" · planning + implementation {ui.cost(task)}*"),
        "",
        f"**Next:** {next_steps(task, st, seen, running, pod if pod is not None else pod_view(st.id))}",
        "",
    ]
    if running and code.older_supervisor(task.meta):
        head += [f"*This task's supervisor {OLDER_SUPERVISOR}.*", ""]
    # Before the plan: what the agent sees of the files you mentioned may not be what you have.
    if st.state in (State.PLAN, State.CHECKPOINT_PLAN) and (notes := actions.context_notes(task)):
        head += [*(f"*{note}.*" for note in notes), ""]
    said = [e for e in task.events() if e["type"] == "serena"]
    if said and (said[-1]["data"].get("on") or providers.serena_mode() == "auto"):
        head += [f"*Serena: {said[-1]['data']['why']}*", ""]
    if shown := (pod if pod is not None else pod_view(st.id)).lines():
        head += [*shown, ""]
    handoff = task.meta / "handoff"
    if seen.problem:
        # What happened, why in the failing tool's own words, what to do.
        head += [
            f"**{seen.status.capitalize()}.**",
            "",
            f"```\n{seen.problem}\n```",
            "",
            "The task goes on from where it was once started again.",
            "",
        ]
    elif seen.status == "not started":
        head += ["**Not started yet.**", ""]
    elif seen.group != "Working" and st.state in (State.PLAN, State.IMPLEMENT, State.VERIFY):
        head += ["**The agent is not working on this task.**", ""]
    if st.box and st.state is State.IMPLEMENT:
        body = [
            "**Box.** The project's clone in a pod, with your keys, opencode and the pod's tools, and",
            "no agent: yours to work in by hand, where a mode without permission prompts is safe and",
            "Docker works. Closing commits what you left and brings the work to review, the way a",
            "task's comes; risky files wait for your approval like a task's.",
            "",
            f"Clone: `{task.repo}`",
        ]
    elif st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        body = [
            "**Plan this task in your own chat.** The prompt for a chat in your browser, or for a CLI",
            "in your checkout (claude, gemini). When the plan is final, paste the chat's answer into the",
            "answer file and save. A CLI writes it itself.",
            "",
            # What leaves your machine with the browser prompt, written by an agent: worth a look
            # before it goes to another provider. The CLI prompt carries none of it.
            "#### What the browser prompt tells the chat about the repository",
            "",
            read(handoff / manual.CONTEXT) or "Nothing: this is a new project.",
            "",
            "#### The plan to fill in",
            "",
            plan_body(read(task.plan_path)),
        ]
    elif st.state in (State.PLAN, State.CHECKPOINT_PLAN):
        body = [*plans_verify(task, st), plan_body(read(task.plan_path))]
    elif st.state is State.CHECKPOINT_FINAL:
        try:
            _, project = actions.load(st.id)
            copy = actions.repo.review_worktree_path(project.repo, task.root)
            stat = actions.changed_files(task, project)
        except Exception as e:  # shown, not fatal: the view must keep working
            copy, stat = "?", f"({e})"
        body = [
            "**Ready for your review.** In your IDE the agent's work shows as uncommitted changes;",
            "accepting it puts them in your checkout.",
            "",
            f"Review copy: `{copy}`",
            "",
            f"```\n{stat.rstrip() or 'no changes fetched yet'}\n```",
            *removed_tests(task),
        ]
    elif st.state is State.APPROVAL_RISKY:
        try:
            diffs = actions.risky_diffs(task, actions.load(st.id)[1])
        except Exception as e:
            diffs = [str(e)]
        body = [
            "**Risky files changed.** They run code on your machine when your IDE imports the project.",
            "",
            *(f"```diff\n{d.rstrip()}\n```" for d in diffs),
        ]
    elif st.state is State.CHECKPOINT_BLOCKED:
        question = read(handoff / supervisor.QUESTION)
        broken = ui.environment_problem(task)
        body = (
            ["**The agent asks:**", "", question]
            if question
            else [
                f"**Verification could not run:** {broken}" if broken else "**Verification keeps failing.**",
                "",
                read(handoff / "verify-feedback.md"),
                "",
                *build_said(handoff / "verify.log", read(handoff / "verify-feedback.md")),
            ]  # fmt: skip
        )
    elif st.state is State.VERIFY and seen.group == "Working":
        body = verification_running(task, st)
    elif items := checklist(task):
        # What the task is still short of. The agent ticks these itself and the gate only checks
        # that none is left open, so a tick is what the agent claims, not something vivibox saw.
        doing = ""
        if st.state is State.IMPLEMENT and seen.group == "Working" and (lasting := ui.lasting(st.updated)):
            doing = f"**Implementing** {lasting}."
        body = [
            *([doing, ""] if doing else []),
            "**Acceptance criteria**, as the agent reports them:",
            "",
            *items,
            "",
            f"{last_gate(task)}{watch}",
        ]
        if gate_failed(task):  # what the agent is fixing now, in the build's own words
            feedback = read(handoff / "verify-feedback.md")
            body += ["", feedback, "", *build_said(handoff / "verify.log", feedback)]
        body += ["", "#### The plan", "", plan_body(read(task.meta / gate.ACCEPTED_PLAN))]
    else:
        events = task.events()[-8:]
        body = ["**Recent events**", ""] + [
            f"- `{ui.clock(e['ts'])}` {e['type']} "
            + " ".join(
                f"{k}={v}" for k, v in e["data"].items() if k in ("current", "reason", "passed", "cost")
            )
            for e in events
        ]
    return "\n".join(head + body)


# --- dialogs ------------------------------------------------------------------------------------


NO_PROJECTS = """No projects yet, so your agents are sitting idle.

Press i to give them one: a repository you already have, or an empty folder to start a project
from scratch. Then n hands them a task."""


class Fields(VerticalScroll, can_focus=False, inherit_bindings=False):
    """A dialog's fields, scrolling when the terminal is short. No keys of its own: the arrows move
    between fields, as everywhere in a dialog, and scrolling follows the field you are on."""


class Dialog(ModalScreen):
    """Arrow keys move between fields and buttons wherever the focused field does not use them
    itself: left and right move the cursor in a text field, up and down open a list."""

    BINDINGS = [
        Binding("up", "app.focus_previous", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("down", "app.focus_next", show=False),
        Binding("right", "app.focus_next", show=False),
    ]


class Confirm(Dialog):
    """A yes or no. Red is for a yes that destroys or interrupts something; agreeing to go on is not
    a warning."""

    def __init__(self, question: str, yes: str = "Yes", destructive: bool = False):
        super().__init__()
        self.question, self.yes, self.destructive = question, yes, destructive

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="error" if self.destructive else "primary", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


class DeleteTask(Dialog):
    """Deleting a task, or a finished one's line in the history: what goes, what stays. Cancel has
    the focus, so an Enter pressed out of habit deletes nothing."""

    def __init__(self, title: str, about: str, goes: str, stays: str, warning: str = ""):
        super().__init__()
        self.title_text, self.about, self.goes, self.stays, self.warning = title, about, goes, stays, warning

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"[b]{escape(self.title_text)}[/]")
            yield Label(escape(self.about), classes="wrap")
            if self.warning:
                yield Label(f"[yellow]{escape(self.warning)}[/]", classes="wrap")
            yield Label(f"[red]Deleted:[/] {escape(self.goes)}", classes="wrap")
            yield Label(f"[green]Kept:[/] {escape(self.stays)}", classes="wrap")
            with Horizontal(classes="buttons"):
                yield Button("Delete", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#no").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def key_escape(self) -> None:
        self.dismiss(False)


NOTHING_TO_SEND = "Write a comment first, or leave with Cancel."


class Reply(Dialog):
    def __init__(self, task_id: str, prompt: str = ""):
        super().__init__()
        self.task_id = task_id
        self.prompt = prompt or f"Your comment for the agent on {task_id}"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.prompt} (ctrl+s sends):")
            yield EdgeTextArea(id="comment")
            with Horizontal(classes="buttons"):
                yield Button("Send", variant="primary", id="send")
                yield Button("Cancel", id="cancel")

    def send(self) -> None:
        """An empty comment is not sent, and the dialog says so. Closing as if it had been sent
        left you waiting for an agent nobody had told anything."""
        text = self.query_one(TextArea).text
        if text.strip():
            self.dismiss(text)
        else:
            self.notify(NOTHING_TO_SEND, severity="warning")

    def key_ctrl_s(self) -> None:
        self.send()

    def key_escape(self) -> None:
        self.dismiss("")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.send()
        else:
            self.dismiss("")


class ReplyWithCriteria(Dialog):
    """Sending finished or stuck work back: a comment, and criteria for what you found. A remark is
    something the agent may act on; a criterion is something the gate holds the work to."""

    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Your comment for the agent on {self.task_id} (ctrl+s sends):")
            yield EdgeTextArea(id="comment")
            yield Label("New acceptance criteria, one per line (optional). The gate checks them like the")
            yield Label("ones you accepted, and the agent ticks them when they are met.")
            yield EdgeTextArea(id="criteria", classes="criteria")
            with Horizontal(classes="buttons"):
                yield Button("Send", variant="primary", id="send")
                yield Button("Cancel", id="cancel")

    def answer(self) -> dict:
        lines = self.query_one("#criteria", TextArea).text.splitlines()
        return {
            "comment": self.query_one("#comment", TextArea).text,
            "criteria": [c for c in lines if c.strip()],
        }

    def send(self) -> None:
        answer = self.answer()
        if answer["comment"].strip() or answer["criteria"]:
            self.dismiss(answer)
        else:
            self.notify(NOTHING_TO_SEND, severity="warning")

    def key_ctrl_s(self) -> None:
        self.send()

    def key_escape(self) -> None:
        self.dismiss({})

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.send()
        else:
            self.dismiss({})


MENTION_AT_CURSOR = re.compile(r"(?:^|\s)@(\S*)$")


def leave_at_edge(area: TextArea, event: events.Key) -> bool:
    """Up on the first line or down on the last moves on to the previous or next field."""
    row, _ = area.cursor_location
    if (event.key == "up" and row == 0) or (event.key == "down" and row == area.document.line_count - 1):
        event.stop()
        event.prevent_default()
        (area.screen.focus_previous if event.key == "up" else area.screen.focus_next)()
        return True
    return False


class EdgeTextArea(TextArea):
    async def _on_key(self, event: events.Key) -> None:
        if not leave_at_edge(self, event):
            await super()._on_key(event)


class DescriptionArea(TextArea):
    """A text area that suggests paths after '@', like Claude Code: a folder's entries, or any path in
    the project that contains what you typed; arrows pick, Tab or Enter take one, Escape closes the
    list."""

    def __init__(self, suggestions: OptionList, cwd: Path, **kwargs):
        super().__init__(**kwargs)
        self.suggestions, self.cwd = suggestions, cwd
        self._repo: Path | None = None
        self.paths: list[str] = []

    @property
    def repo(self) -> Path | None:
        """The selected project's repository: its files are suggested too, and they are the ones
        the agent has in its clone. Its tree is listed once here, not on every keystroke."""
        return self._repo

    @repo.setter
    def repo(self, path: Path | None) -> None:
        self._repo = path
        self.paths = context.project_paths(path) if path else []

    def mention(self) -> str | None:
        row, col = self.cursor_location
        m = MENTION_AT_CURSOR.search(self.document.get_line(row)[:col])
        return m.group(1) if m else None

    def suggest(self) -> None:
        partial = self.mention()
        found = (
            context.complete(partial, self.cwd, repo=self.repo, paths=self.paths)
            if partial is not None
            else []
        )
        self.suggestions.set_options(found)
        self.suggestions.display = bool(found)
        if found:
            self.suggestions.highlighted = 0

    def take(self, choice: str) -> None:
        partial = self.mention() or ""
        row, col = self.cursor_location
        self.replace(choice, (row, col - len(partial)), (row, col))
        if not choice.endswith("/"):
            self.insert(" ")
        self.suggest()  # a directory opens its contents

    async def _on_key(self, event: events.Key) -> None:
        if not self.suggestions.display and leave_at_edge(self, event):
            return
        if self.suggestions.display and event.key in ("up", "down", "tab", "enter", "escape"):
            event.stop()
            event.prevent_default()
            options = self.suggestions
            if event.key == "escape":
                options.display = False
            elif event.key == "up":
                options.action_cursor_up()
            elif event.key == "down":
                options.action_cursor_down()
            elif options.highlighted is not None:
                self.take(str(options.get_option_at_index(options.highlighted).prompt))
            return
        await super()._on_key(event)

    @on(TextArea.Changed)
    def changed(self) -> None:
        self.suggest()


NEW_PROJECT = "+ set up another project…"


class NewProject(Dialog):
    """A repository vivibox does not know yet, or a folder where one should start. How to build and
    test it is detected from its build files, with what its pipeline runs a pick away, or left to
    the first plan you accept."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Folder of the project: where you started vivibox, or another you browse to.")
            with Horizontal(classes="role"):
                yield Label("", id="path", classes="wrap")
                yield Button("Browse…", id="browse")
            yield Label("Name")
            yield Input(id="name")
            yield Label("Verification")
            with Horizontal(classes="role"):
                yield Label("", id="verify", classes="wrap")
                yield Button("Change…", id="change")
            yield Label("", id="notes")
            with Horizontal(classes="buttons"):
                yield Button("Set up", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.look(Path.cwd())
        self.query_one("#browse", Button).focus()

    def use_folder(self, where: Path | None) -> None:
        if where is not None:
            self.look(where)
            self.query_one("#create", Button).focus()

    def look(self, where: Path) -> None:
        """Following the folder you point at: its name, and what setting it up would mean."""
        self.where = where
        self.query_one("#path", Label).update(f"[b]{escape(shown_path(where))}[/]")
        found = actions.propose_project(where)
        root = actions.git_root(where)
        self.taken = actions.project_at(root) if root else ""
        self.candidates = project_init.candidates(root or where)
        self.verify, self.source, self.no_build = list(found.verify), found.source, False
        name = self.query_one("#name", Input)
        name.value = self.taken or found.name
        name.disabled = bool(self.taken)
        if self.taken:
            notes = [f"already a project: {self.taken}; Set up opens a task for it instead"]
        elif root:
            notes = [f"repository {root}", *found.notes]
        else:
            notes = [f"a new repository starts in {where}"]
        self.query_one("#notes", Label).update(" · ".join(notes))
        self.query_one("#change", Button).display = not self.taken
        self.show_verify()
        self.query_one("#create", Button).label = "Open a task" if self.taken else "Set up"

    def show_verify(self) -> None:
        if self.no_build:
            how = actions.NO_BUILD
        elif self.verify:
            how = f"{escape(self.verify[0])}  [dim]from {escape(self.source)}[/]"
        else:
            how = "the first plan you accept decides"
        self.query_one("#verify", Label).update(how)

    def pick_verify(self, choice: dict) -> None:
        if not choice:
            return
        self.verify, self.no_build = choice["verify"], choice["no_build"]
        self.source = (
            dict((c, s) for c, s in self.candidates).get(self.verify[0], "you") if self.verify else ""
        )
        self.show_verify()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse":
            self.app.push_screen(Browse(FOLDER, "Pick the project's folder"), self.use_folder)
        elif event.button.id == "change":
            name = self.query_one("#name", Input).value.strip() or "this project"
            picker = ChooseVerify(name, self.verify, self.no_build, self.candidates, exists=False)
            self.app.push_screen(picker, self.pick_verify)
        elif event.button.id != "create":
            self.dismiss({})
        elif self.taken:
            self.dismiss({"use": self.taken})
        else:
            name = self.query_one("#name", Input).value.strip()
            self.dismiss(
                {"path": str(self.where), "name": name, "verify": self.verify, "no_build": self.no_build}
            )

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})


class ChooseVerify(ModalScreen[dict]):
    """How a project is verified: a command its build files or its pipeline name, no build, one of
    your own, or the file itself for the rest of it. Before the project exists (from i), the file
    is not offered, and leaving it to the first plan is. Arrows pick, Enter takes, Escape leaves
    it as it is."""

    EDIT = "edit the project file in your editor, for pass_env and host services too…"
    PLAN_DECIDES = "leave it to the first plan you accept"

    def __init__(
        self,
        name: str,
        verify: list[str],
        no_build: bool,
        candidates: list[tuple[str, str]],
        exists: bool = True,
    ):
        super().__init__()
        self.project_name, self.verify, self.no_build = name, verify, no_build
        self.candidates, self.exists = candidates, exists

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"How {self.project_name} is verified:")
            now = "  ← now"
            labels = [
                f"{escape(c)}  [dim]from {escape(source)}[/]" + (now if [c] == self.verify else "")
                for c, source in self.candidates
            ]
            labels.append(escape(actions.NO_BUILD) + (now if self.no_build else ""))
            labels.append(self.EDIT if self.exists else self.PLAN_DECIDES)
            yield OptionList(*labels, id="choices")
            with Horizontal(classes="role"):
                yield Label("Other")
                yield Input(placeholder="a command of your own; Enter takes it", id="other")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        i = event.option_index
        if i < len(self.candidates):
            self.dismiss({"verify": [self.candidates[i][0]], "no_build": False})
        elif i == len(self.candidates):
            self.dismiss({"verify": [], "no_build": True})
        elif self.exists:
            self.dismiss({"edit": True})
        else:
            self.dismiss({"verify": [], "no_build": False})

    @on(Input.Submitted)
    def typed(self, event: Input.Submitted) -> None:
        if command := event.value.strip():
            self.dismiss({"verify": [command], "no_build": False})

    def key_escape(self) -> None:
        self.dismiss({})


class ChooseEditor(ModalScreen[str]):
    """What to open review copies with, asked once: arrows pick, Enter takes, Escape leaves it."""

    def __init__(self, found: list[ide.Editor]):
        super().__init__()
        self.found = found

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Open the review copy with:")
            options = OptionList(*[e.label for e in self.found], id="editors")
            yield options
            yield Label("Your choice is kept in config.toml; change it there, or per project.")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.found[event.option_index].command)

    def key_escape(self) -> None:
        self.dismiss("")


class ChooseSession(ModalScreen[str]):
    """Which conversation w opens when the task has two. Arrows pick, Enter takes, Escape leaves."""

    def __init__(self, rows: list[tuple[str, str]]):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Look at:")
            yield OptionList(*[f"{role}  {doing}" for role, doing in self.rows], id="sessions")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.rows[event.option_index][0])

    def key_escape(self) -> None:
        self.dismiss("")


class ChooseRole(ModalScreen[str]):
    """Which role to put on another model for this task. Arrows pick, Enter takes, Escape leaves."""

    def __init__(self, rows: list[tuple[str, str, bool]]):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Run this task's roles on:")
            labels = [f"{name}  {model}" + ("  (this task)" if own else "") for name, model, own in self.rows]
            yield OptionList(*labels, id="roles")
            yield Label("A change applies the next time the task starts.")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.rows[event.option_index][0])

    def key_escape(self) -> None:
        self.dismiss("")


class ChooseModel(ModalScreen["actions.Choice | None"]):
    """What one role runs on, from what you can run: planning yourself, or a model of a provider
    you have a key for. None leaves it alone; config.toml's own choice gives the role back to it."""

    def __init__(self, role: str, offered: list, configured, current):
        super().__init__()
        self.role, self.offered, self.configured, self.current = role, offered, configured, current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Run {self.role} on:")
            labels = [
                actions.choice_label(c, self.configured) + ("  ← now" if c == self.current else "")
                for c in self.offered
            ]
            yield OptionList(*labels, id="models")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.offered[event.option_index])

    def key_escape(self) -> None:
        self.dismiss(None)


def find_providers(catalog: list[tuple[str, str]], typed: str) -> list[tuple[str, str]]:
    """The providers whose name or id holds what you typed, as (id, label). What you typed is
    offered as a name of its own too, for a provider the list lacks or a list that could not be read."""
    text = typed.strip().casefold()
    found = [(pid, f"{escape(name)}  [dim]{escape(pid)}[/]") for pid, name in catalog
             if text in pid.casefold() or text in name.casefold()]  # fmt: skip
    if text and text not in {pid for pid, _ in found}:
        found.append((text, f"use “{escape(text)}” as the provider's name"))
    return found


STATUS = {
    "replaces": "  [yellow]differs from yours: tick to overwrite[/]",
    "same": "  [dim]same as yours[/]",
    "builtin": "  comes with vivibox; set its mode in Manage",
}


def import_label(f: providers.Found) -> str:
    """One line per provider or server, short enough that what it says of yours stays in view."""
    if f.status == "builtin":  # greyed whole: there is nothing here to choose
        return f"[dim]{escape(f.name)}  {escape(ui.shorten(f.what, 40))}{STATUS['builtin']}[/]"
    what, key = escape(ui.shorten(f.what, 48)), escape(ui.shorten(f.key, 30))
    return f"{escape(f.name)}  {what}, key {key}{STATUS.get(f.status, '')}"


class ChooseImport(Dialog):
    """What an opencode configuration brings, in two groups, providers and MCP servers. New ones are
    ticked; one that would replace yours is not, for you to decide; one you already have is shown
    and cannot be picked. Dismisses with those you keep ticked, or [] when you leave."""

    def __init__(self, source: Path, reading: providers.Reading):
        super().__init__()
        self.source, self.reading = source, reading

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"In {escape(shown_path(self.source))}; untick what to leave out.")
            for kind, heading in ((providers.PROVIDER, "Providers"), (providers.MCP, "MCP servers")):
                rows = [
                    Selection(
                        import_label(f),
                        i,
                        f.status == "new",
                        disabled=f.status in ("same", "builtin"),
                    )
                    for i, f in enumerate(self.reading.found)
                    if f.kind == kind
                ]
                if rows:
                    yield Label(heading, classes="group")
                    yield SelectionList[int](*rows, id=f"found-{kind}", classes="found")
            if self.reading.left:
                yield Label(f"Left in the file, not for vivibox: {', '.join(self.reading.left)}.")
            with Horizontal(classes="buttons"):
                yield Button("Import", variant="primary", id="import")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query(".found").first().focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        ticked = []
        if event.button.id == "import":
            for found in self.query(".found").results(SelectionList):
                ticked += found.selected
        self.dismiss([self.reading.found[i] for i in sorted(ticked)])

    def key_escape(self) -> None:
        self.dismiss([])


def shown_path(path: Path) -> str:
    home = str(Path.home())
    return str(path).replace(home, "~", 1) if str(path).startswith(home + "/") else str(path)


def judge(path: Path) -> tuple[bool, str]:
    """Whether a file is an opencode configuration vivibox can bring something over from, said."""
    try:
        reading = providers.read_opencode(path)
    except ConfigError as e:
        text = e.args[0]
        if "is not JSON" in text:
            return False, "broken: not JSON" + (f" ({text.split(': ', 1)[1]})" if ": " in text else "")
        return False, "JSON, but not an opencode configuration with providers or MCP servers"
    parts = []
    for kind, what in ((providers.PROVIDER, "provider"), (providers.MCP, "MCP server")):
        if n := sum(f.kind == kind for f in reading.found):
            parts.append(f"{n} {what}{'s' * (n != 1)}")
    return True, "opencode configuration: " + ", ".join(parts)


# Folders nobody picks anything from, and which would bury what they do pick.
SKIPPED = {".git", "node_modules", "__pycache__", ".venv", ".cache", ".npm", ".m2", ".gradle"}
JSON, FOLDER, ANY = "json", "folder", "any"


def is_repository(path: Path) -> bool:
    return (path / ".git").exists()


def browse_start() -> Path:
    """Where browsing starts: home, where your projects and downloads are."""
    return Path.home()


class PathTree(DirectoryTree):
    """Your folders, and in them only what can be picked: JSON files, folders, or anything. What can
    be picked is marked; hidden folders are dimmed."""

    def __init__(self, path: Path, mode: str, **kwargs):
        self.mode = mode
        super().__init__(path, **kwargs)

    def filter_paths(self, paths):
        return [p for p in paths if shows(self.mode, p)]

    async def _on_click(self, event: events.Click) -> None:
        """A click marks, and opens or closes a folder; it picks nothing. Picking is Enter, Select,
        or a double click: a single one picked whatever the mouse passed over."""
        event.prevent_default()  # the tree's own handler would select, and so pick, on every click
        meta = event.style.meta
        if "line" not in meta:
            return
        self.cursor_line = meta["line"]
        node = self.cursor_node
        if event.chain >= 2:
            self.action_select_cursor()
        elif node is not None and node.allow_expand:
            node.toggle()

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        path = node.data.path if node.data else None
        if path is None:
            return label
        if (self.mode == JSON and path.is_file()) or (self.mode == FOLDER and is_repository(path)):
            label.stylize("bold green")
        elif path.name.startswith("."):
            label.stylize("dim")
        return label


def shows(mode: str, path: Path) -> bool:
    if path.is_dir():
        return path.name not in SKIPPED
    if mode == JSON:
        return path.suffix in (".json", ".jsonc")
    return mode == ANY


def folder_verdict(path: Path) -> tuple[bool, str]:
    """What setting up a project here would mean, in a few words."""
    root = actions.git_root(path)
    if root and (taken := actions.project_at(root)):
        return True, f"already the project {taken}"
    if root:
        return True, "a git repository" if root == path else f"inside the repository {shown_path(root)}"
    if not any(path.iterdir()):
        return True, "an empty folder: a new project starts here"
    return True, "a folder without git: a repository starts here"


class NameFolder(Dialog):
    """The name of a new folder, in the one the browser is at. Dismisses with it, or ""."""

    def __init__(self, parent: Path):
        super().__init__()
        self.folder = parent

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"New folder in {escape(shown_path(self.folder))}:")
            yield Input(placeholder="name", id="name")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#name").focus()

    def answer(self) -> str:
        name = self.query_one("#name", Input).value.strip()
        return name if name and "/" not in name and name not in (".", "..") else ""

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.answer() if event.button.id == "create" else "")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.dismiss(self.answer())

    def key_escape(self) -> None:
        self.dismiss("")


class Browse(Dialog):
    """Walks your folders from home to pick a file or a folder, so no path is typed from memory.
    Arrows move, right opens a folder, left closes it, Enter picks, Backspace goes up a folder.
    Dismisses with the path, or None."""

    BINDINGS = [
        Binding("backspace", "up", show=False),
        Binding("right", "open", show=False),
        Binding("left", "close", show=False),
    ]

    def __init__(self, mode: str, title: str, start: Path | None = None):
        super().__init__()
        self.mode, self.title_text = mode, title
        self.start = start or browse_start()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"[b]{escape(self.title_text)}[/]")
            yield Label(
                "→ opens a folder · ← closes it · Enter or Select picks · Backspace goes up", classes="files"
            )
            tree = PathTree(self.start, self.mode, id="tree", classes="tree")
            tree.auto_expand = False  # Enter picks; the arrows open and close
            yield tree
            yield Label("", id="verdict")
            with Horizontal(classes="buttons"):
                yield Button("Select", variant="primary", id="select")
                if self.mode == FOLDER:  # a project from scratch starts in a folder not made yet
                    yield Button("New folder…", id="new-folder")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#tree").focus()

    def here(self) -> Path:
        """The folder the cursor is in, or on."""
        node = self.query_one("#tree", PathTree).cursor_node
        path = (
            node.data.path if node is not None and node.data else Path(self.query_one("#tree", PathTree).path)
        )
        return path if path.is_dir() else path.parent

    @on(Button.Pressed, "#select")
    def select_marked(self) -> None:
        node = self.query_one("#tree", PathTree).cursor_node
        if node is None or not node.data:
            return
        path = node.data.path
        ok, said = self.judged(path)
        if ok:
            self.dismiss(path)
        else:
            self.notify(said or "Pick one of the marked entries.", severity="warning")

    @on(Button.Pressed, "#new-folder")
    def new_folder(self) -> None:
        parent = self.here()

        def named(name: str) -> None:
            if not name:
                return
            made = parent / name
            try:
                made.mkdir()
            except OSError as e:
                self.notify(f"Cannot make {shown_path(made)}: {e.strerror}", severity="error")
                return
            self.dismiss(made)

        self.app.push_screen(NameFolder(parent), named)

    def judged(self, path: Path) -> tuple[bool, str]:
        if self.mode == JSON:
            return judge(path) if path.is_file() else (False, shown_path(path))
        if self.mode == FOLDER:
            return folder_verdict(path) if path.is_dir() else (False, "")
        return True, shown_path(path)

    @on(DirectoryTree.NodeHighlighted)
    def looked_at(self, event) -> None:
        path = event.node.data.path if event.node.data else None
        verdict = self.query_one("#verdict", Label)
        if path is None:
            verdict.update("")
            return
        ok, said = self.judged(path)
        colour = "green" if ok else "red" if path.is_file() else "dim"
        verdict.update(f"[{colour}]{escape(said)}[/]")

    @on(DirectoryTree.FileSelected)
    def file_picked(self, event: DirectoryTree.FileSelected) -> None:
        self.pick(event.path)

    @on(DirectoryTree.DirectorySelected)
    def folder_picked(self, event: DirectoryTree.DirectorySelected) -> None:
        if self.mode == JSON:  # a folder is no answer here: Enter opens it instead
            event.node.toggle()
        else:
            self.pick(event.path)

    def pick(self, path: Path) -> None:
        if self.judged(path)[0]:
            self.dismiss(path)

    def action_open(self) -> None:
        node = self.query_one("#tree", PathTree).cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_close(self) -> None:
        tree = self.query_one("#tree", PathTree)
        node = tree.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None:
            tree.move_cursor(node.parent)

    def action_up(self) -> None:
        tree = self.query_one("#tree", PathTree)
        tree.path = Path(tree.path).parent

    @on(Button.Pressed, "#cancel")
    def cancelled(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ImportSource(Dialog):
    """Which opencode configuration to bring over from: those found where opencode keeps one, and a
    file just downloaded, or one you find by browsing. Dismisses with its path, or None."""

    def __init__(self, found: list[tuple[Path, int]]):
        super().__init__()
        self.found = found

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            if self.found:
                yield Label("Import from an opencode configuration:")
            else:
                yield Label("No opencode configuration where opencode keeps one, nor one just downloaded.")
            rows = [f"{escape(shown_path(p))}  [dim]{n} to bring over[/]" for p, n in self.found]
            yield OptionList(*rows, "Browse…", id="sources")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#sources").focus()

    @on(OptionList.OptionSelected, "#sources")
    def chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option_index < len(self.found):
            self.dismiss(self.found[event.option_index][0])
        else:
            self.app.push_screen(Browse(JSON, "Find the opencode configuration"), self.browsed)

    def browsed(self, path: Path | None) -> None:
        # A statement, not a lambda returning dismiss(): Textual awaits what a callback returns,
        # and awaiting a screen's dismiss() from its own message handler is an error.
        if path:
            self.dismiss(path)

    @on(Button.Pressed, "#cancel")
    def cancelled(self) -> None:
        self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


def provider_rows() -> list[tuple[str, str, str, bool]]:
    """Your providers and MCP servers, and vivibox's own: (kind, name, what it is, whether it is on)."""
    stored, defined = keys.list_keys(), providers.load()
    rows = []
    for name in sorted({n for n in stored if not providers.is_mcp_secret(n)} | set(defined)):
        key = (
            f"key {stored[name]}"
            if name in stored
            else "no key needed"
            if providers.keyless(name)
            else "no key"
        )
        said = [key]
        if name in defined:
            n = len(defined[name].get("models", {}))
            said.append(f"your endpoint, {n} model{'s' * (n != 1)}")
        rows.append((providers.PROVIDER, name, ", ".join(said), providers.enabled(providers.PROVIDER, name)))
    servers = {**providers.load_mcp(), **providers.BUILTIN_MCP}
    for name, entry in sorted(servers.items()):
        where = (
            entry.get("url", "") if entry.get("type") == "remote" else " ".join(entry.get("command", [])[:1])
        )
        if name == providers.SERENA:
            said = f"MCP server, comes with vivibox, {SERENA_MODE_SAID[providers.serena_mode()]}"
        else:
            said = f"MCP server, {entry.get('type', 'local')} {where}"
        rows.append((providers.MCP, name, said, providers.enabled(providers.MCP, name)))
    return rows


SERENA_MODE_SAID = {
    "auto": f"auto: on for a project with {providers.SERENA_MIN_FILES}+ source files",
    "on": "on for every task",
    "off": "off",
}


def row_label(name: str, said: str, on: bool) -> str:
    state = "" if on else "  [yellow]off[/]"
    return f"{escape(name)}  [dim]{escape(ui.shorten(said, 70))}[/]{state}"


class ManageProviders(Dialog):
    """Your providers and MCP servers: add a provider, import an opencode.json, or manage what is on."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Providers & MCP")
            yield OptionList(id="providers", classes="catalog")
            with Horizontal(classes="buttons"):
                yield Button("Add provider…", variant="primary", id="add")
                yield Button("Import opencode.json…", id="import")
                yield Button("Manage…", id="manage")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.fill()
        self.query_one("#add").focus()

    def fill(self) -> None:
        self.rows = provider_rows()
        options = self.query_one("#providers", OptionList)
        options.clear_options()
        options.add_options([row_label(n, said, on) for _, n, said, on in self.rows])
        mine = [r for r in self.rows if r[1] not in providers.BUILTIN_MCP]
        if not mine:
            options.add_option(Option("no providers yet: add one, or import an opencode.json", disabled=True))

    def changed(self, names) -> None:
        if names:
            self.fill()
            self.app.refresh_models(names)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add":
            self.app.push_screen(AddProvider(self.app.catalog, importing=False), self.changed)
        elif event.button.id == "import":
            self.app.import_opencode(self.changed)
        elif event.button.id == "manage":
            self.app.push_screen(ManageItems(), self.changed)
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


class ManageItems(Dialog):
    """What is on: a ticked provider's models are offered for a task, a ticked MCP server is given to
    every task. Unticking keeps it, and its key, for later; Remove takes it away. Save applies."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Tick what is on; Remove takes the highlighted one away with its secrets.")
            yield Label("A change reaches a task the next time it starts.", classes="files")
            self.rows = [r for r in provider_rows() if r[1] != providers.SERENA]
            yield SelectionList[int](
                *(Selection(row_label(n, said, True), i, on) for i, (_, n, said, on) in enumerate(self.rows)),
                id="items",
                classes="catalog",
            )
            with Horizontal(classes="role"):
                yield Label("Serena", classes="role-name")
                yield Select(
                    [(said, mode) for mode, said in SERENA_MODE_SAID.items()],
                    value=providers.serena_mode(),
                    allow_blank=False,
                    id="serena-mode",
                )
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Remove", variant="error", id="remove")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#items").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        items = self.query_one("#items", SelectionList)
        if event.button.id == "save":
            ticked = set(items.selected)
            changed = []
            for i, (kind, name, _, on) in enumerate(self.rows):
                if (i in ticked) != on:
                    providers.set_enabled(kind, name, i in ticked)
                    changed.append(name)
            mode = str(self.query_one("#serena-mode", Select).value)
            if mode != providers.serena_mode():
                providers.set_serena_mode(mode)
                changed.append(providers.SERENA)
            self.dismiss(changed)
        elif event.button.id == "remove":
            at = items.highlighted
            if at is None or at >= len(self.rows):
                return
            kind, name, _, _ = self.rows[at]
            what = "MCP server" if kind == providers.MCP else "provider"

            def answered(yes: bool) -> None:
                if yes and providers.forget(name, kind):
                    self.notify(f"Removed {name}.")
                    self.dismiss([name])

            self.app.push_screen(
                Confirm(
                    f"Remove the {what} {name} and its secrets from vivibox?", "Remove", destructive=True
                ),
                answered,
            )
        else:
            self.dismiss([])

    def key_escape(self) -> None:
        self.dismiss([])


class AddProvider(Dialog):
    """A provider opencode knows, picked from its list, with your key; or every provider of an
    opencode.json you already use, such as your employer's endpoint. Dismisses with the providers
    added, or [] when you leave."""

    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, catalog: list[tuple[str, str]] | None = None, importing: bool = True):
        super().__init__()
        # None while the list is still being read; [] when it cannot be, and you type the name.
        self.catalog = catalog
        # Opened from the providers screen, which has its own import button, it does without one.
        self.importing = importing
        self.shown: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Add a provider: type to search the ones opencode knows, arrows to pick")
            yield Input(placeholder="search, e.g. deep", id="search")
            yield OptionList(id="catalog", classes="catalog")
            yield Input(
                placeholder="API key for the provider picked above (not shown)", password=True, id="key"
            )
            yield Label("", id="problem")
            with Horizontal(classes="buttons"):
                yield Button("Add", variant="primary", id="add")
                yield Button("Import opencode.json…", id="import")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#search", Input).focus()
        self.query_one("#import").display = self.importing
        if self.catalog is None:
            self.query_one("#catalog", OptionList).add_option(
                Option("reading the providers opencode knows…", disabled=True)
            )
            self.read_catalog()
        else:
            self.show()

    @work(thread=True)
    def read_catalog(self) -> None:
        found = actions.provider_catalog()
        self.app.call_from_thread(self.loaded, found)

    def loaded(self, found: list[tuple[str, str]]) -> None:
        self.catalog = self.app.catalog = found
        self.show()

    @on(Input.Changed, "#search")
    def show(self) -> None:
        if self.catalog is None:
            return
        self.shown = find_providers(self.catalog, self.query_one("#search", Input).value)
        options = self.query_one("#catalog", OptionList)
        options.clear_options()
        options.add_options([label for _, label in self.shown])
        if self.shown:
            options.highlighted = 0

    def picked(self) -> str:
        at = self.query_one("#catalog", OptionList).highlighted
        return self.shown[at][0] if at is not None and at < len(self.shown) else ""

    def action_move(self, step: int) -> None:
        """In the search field the arrows walk the list; elsewhere they move between fields."""
        if self.focused is self.query_one("#search"):
            options = self.query_one("#catalog", OptionList)
            options.action_cursor_down() if step > 0 else options.action_cursor_up()
        else:
            self.app.action_focus_next() if step > 0 else self.app.action_focus_previous()

    @on(OptionList.OptionSelected, "#catalog")
    def chosen(self) -> None:
        self.query_one("#key", Input).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        try:
            if event.button.id == "add":
                name = self.picked()
                if not name:
                    raise ConfigError("pick a provider from the list, or type its name")
                keys.set_key(name, self.query_one("#key", Input).value)
                self.dismiss([name])
            elif event.button.id == "import":
                self.app.import_opencode(self.imported)
            else:
                self.dismiss([])
        except (keys.KeyStoreError, ConfigError) as e:
            self.query_one("#problem", Label).update(f"[red]{e.args[0]}[/]")

    def imported(self, names: list[str]) -> None:
        if names:  # nothing imported: back to this dialog, to add a provider by name instead
            self.dismiss(names)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#key", Input).focus()
        else:
            self.query_one("#add", Button).press()

    def key_escape(self) -> None:
        self.dismiss([])


class NewTask(Dialog):
    """A form: labels on the left, one field per row, the description and the closing buttons
    the only boxes. Every field is on the screen at once; a short terminal shrinks the
    description before anything scrolls."""

    def __init__(self, preselect: str = "", available: dict[str, list[str]] | None = None):
        super().__init__()
        self.preselect = preselect
        # The models of the providers you have keys for; None while the view is still asking.
        self.available = available

    def compose(self) -> ComposeResult:
        names = projects()
        chosen = self.preselect if self.preselect in names else names[0]
        with Vertical(classes="dialog form"):
            with Fields(classes="fields"):
                with Vertical(id="task", classes="section"):
                    # A list even with one project in it: the dialog looks the same however many
                    # you have. What is not on it is set up from it, like a provider from a model list.
                    with Horizontal(classes="row"):
                        yield Label("Project", classes="key")
                        yield Select(
                            [*((n, n) for n in names), (NEW_PROJECT, NEW_PROJECT)],
                            value=chosen, allow_blank=False, compact=True, id="project",
                        )  # fmt: skip
                    with Horizontal(classes="row gap", id="kind-row"):
                        yield Label("Kind", classes="key")
                        yield Select(
                            [("Feature: new behaviour", "feature"), ("Bug: something works wrong", "bug"),
                             ("Other: refactoring, tests, upkeep", "other")],
                            value="feature", allow_blank=False, compact=True, id="kind",
                        )  # fmt: skip
                    suggestions = OptionList(id="suggestions")
                    suggestions.display = False
                    with Horizontal(classes="row", id="task-row"):
                        yield Label("Task", classes="key")
                        yield DescriptionArea(
                            suggestions, Path.cwd(), id="goal", classes="description",
                            placeholder="What should the agent do? A line, or a whole ticket with its"
                            " context and constraints.",
                        )  # fmt: skip
                    yield suggestions
                    # Attach fills the description, so it stands under it, not among the lists.
                    with Horizontal(classes="row"):
                        yield Label("", classes="key")
                        yield Button("Attach…", compact=True, id="attach")
                        yield Label("or @path, for a copy of a file or folder.", classes="hint")
                with Vertical(id="planning", classes="section"):
                    # One question, not two boxes that could both be ticked.
                    with Horizontal(classes="row"):
                        yield Label("Plan", classes="key")
                        yield Select(
                            [("Stop for my review of the plan", "review"),
                             ("Accept the agent's plan without stopping (--auto)", "auto"),
                             ("Only create the task, to write the plan myself (--draft)", "draft")],
                            value="review", allow_blank=False, compact=True, id="plan",
                        )  # fmt: skip
                    # Each role on config.toml's choice unless you pick another; m changes it later.
                    config = load_config()
                    for name in sorted(config.roles):
                        offered = actions.choices(name, config, self.available)
                        configured = actions.configured_choice(config, name)
                        with Horizontal(classes="row gap"):
                            yield Label(name.capitalize(), classes="key")
                            options = [(actions.choice_label(c, configured), c) for c in offered]
                            yield Select(
                                options, value=configured, allow_blank=False, compact=True,
                                id=f"role-{name}", classes="model",
                            )  # fmt: skip
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")
                yield Label("ctrl+s creates the task", classes="hint keys")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "attach":
            self.app.push_screen(Browse(ANY, "Attach a file or a folder for the agent"), self.attach)
            return
        if event.button.id != "create":
            self.dismiss({})
            return
        self.dismiss(
            {
                "project": self.query_one("#project", Select).value,
                "goal": self.query_one("#goal", TextArea).text.strip(),
                "kind": self.query_one("#kind", Select).value,
                "auto": self.query_one("#plan", Select).value == "auto",
                "draft": self.query_one("#plan", Select).value == "draft",
                "roles": {s.id.removeprefix("role-"): s.value for s in self.query(".model").results(Select)},
            }
        )

    # The dialog's frame, padding and buttons: what the fields leave room for.
    CHROME = 9

    def on_mount(self) -> None:
        self.for_project(str(self.query_one("#project", Select).value))
        self.call_after_refresh(self.fit)

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit)

    def fit(self) -> None:
        """The description as tall as the screen leaves after the other rows, a line at least, so
        the whole form stays in view and the description scrolls inside itself. The blank rows
        between the lists of a group go first, before the description would shrink below three
        lines."""
        fields = self.query_one(Fields)
        goal = self.query_one("#goal", TextArea)
        dialog = self.query_one(".dialog")
        room = int(self.size.height * 0.9) - self.CHROME
        fields.styles.max_height = max(5, room)
        others = fields.virtual_size.height - self.query_one("#task-row").outer_size.height
        gaps = len([row for row in self.query(".row.gap") if row.display])
        if not dialog.has_class("tight"):
            others -= gaps  # the rows without their gaps, whichever way they are drawn now
        tight = room - others - gaps < 3
        dialog.set_class(tight, "tight")
        goal.styles.height = max(3, min(12, room - others - (0 if tight else gaps)))
        goal.focus()

    @on(Select.Changed, "#project")
    def switched(self, event: Select.Changed) -> None:
        if event.value == NEW_PROJECT:
            self.dismiss({"project": NEW_PROJECT})
            return
        self.for_project(str(event.value))

    def for_project(self, name: str) -> None:
        """An empty project has nothing that could work wrong, so what kind of task this is is not asked."""
        self.query_one("#kind-row").display = not actions.empty_project(name)
        with contextlib.suppress(ConfigError):
            self.query_one("#goal", DescriptionArea).repo = load_project(name).repo

    def attach(self, path: Path | None) -> None:
        """The picked file or folder as an @mention, where the cursor is in the description."""
        goal = self.query_one("#goal", DescriptionArea)
        if path is not None:
            goal.insert(f"@{shown_path(path)} ")
        goal.focus()

    def key_ctrl_s(self) -> None:
        self.query_one("#create", Button).press()

    def key_escape(self) -> None:
        self.dismiss({})


HELP = """[b]Your decisions[/b], on the selected task
  a     accept the plan, or the finished work
  r     reply: reject, ask for changes, or answer the agent
  p     approve changes to risky files
  g     verify a blocked task again, without the agent

[b]The selected task[/b]
  d, Enter  show or hide its details
  e     edit the plan; with a manual planner, paste your chat's plan
  c, C  copy the planning prompt for a chat, or for a CLI
  o     open the review copy in your IDE
  v     run the app in its pod, or stop it
  w     watch or talk to the agent; while verifying, its log (Ctrl-q leaves);
        with two conversations, asks which; in a box, a shell in it
  l     the newest log in your pager: followed while the verification runs,
        else opened at its end
  s     stop the task, or start it again
  m     what each role runs on, for this task
  x     delete the task; on a finished one, its line in the history

[b]The selected project[/b] (Enter folds or unfolds its tasks)
  n     new task in it
  b     open a box: its pod for you to work in by hand, opencode included
  e     edit its file
  o     open its repository in your IDE
  x     forget it, once it has no tasks

[b]Anywhere[/b]
  i     set up a project
  k     providers & MCP
  h     show or hide finished tasks
  q     quit
"""


class Help(ModalScreen):
    """Every key, and when it does something. The footer shows only what applies right now."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog help"):
            yield Static(HELP, id="help")
            yield Label("Esc closes.", classes="files")

    def key_escape(self) -> None:
        self.dismiss()

    def key_question_mark(self) -> None:
        self.dismiss()


class CommitWork(Dialog):
    """After accepting: the work is staged in your checkout; commit it now, or leave it for your IDE."""

    def __init__(self, done: actions.Finished):
        super().__init__()
        self.done = done
        self.branch = actions.current_branch(done.source)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.done.task_id} is done. Its work is uncommitted in {self.done.source}:")
            yield Label(self.done.status.rstrip() or "(no changes)", classes="files")
            warn = " (your main branch)" if self.branch in PROTECTED_BRANCHES else ""
            yield Label(f"Commit to {self.branch}{warn} with this message?")
            yield Input(self.done.message, id="message")
            with Horizontal(classes="buttons"):
                # On a main branch the safe choice comes first.
                commit = Button("Commit", variant="primary", id="commit")
                later = Button("Leave uncommitted", id="later")
                yield from ((later, commit) if warn else (commit, later))

    def on_mount(self) -> None:
        self.query_one("#later" if self.branch in PROTECTED_BRANCHES else "#message").focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if event.button.id == "commit" else "")

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.dismiss(self.query_one(Input).value)

    def key_escape(self) -> None:
        self.dismiss("")


# --- the view -----------------------------------------------------------------------------------

WAITING_ONLY = {State.CHECKPOINT_PLAN, State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED, State.APPROVAL_RISKY}


class LiveFooter(Footer):
    """Textual's Footer stops redrawing while the terminal has no focus (bindings_changed in
    widgets/_footer.py returns early). The keys are what tells you a task now needs you, so they
    must appear while you are in another window, not once you click back into the terminal."""

    def bindings_changed(self, screen) -> None:
        self._bindings_ready = True
        if self.is_attached and screen is self.screen:
            self.call_after_refresh(self.recompose)


class LeavingExecutor(ThreadPoolExecutor):
    """Where Textual runs the thread workers (a start, a stop, the app being run): the loop's
    default executor, but one the view can close without waiting for. asyncio waits for the
    default executor at the end, so a pod start that hung on Docker held the window until Ctrl-C,
    which showed a traceback. Nothing is lost by leaving: the pod and the supervisor are
    processes of their own, and a docker command finishes on its own."""

    def __init__(self) -> None:
        super().__init__(thread_name_prefix="vivibox-step")
        self.at_work: set[Future] = set()

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = super().submit(fn, *args, **kwargs)
        self.at_work.add(future)
        future.add_done_callback(self.at_work.discard)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        super().shutdown(wait=False, cancel_futures=cancel_futures)

    def unfinished(self) -> int:
        return sum(1 for future in self.at_work if not future.done())


class Vivibox(App):
    TITLE = "vivibox"
    # Textual's own palette (themes, screenshots) took a tenth of a narrow footer.
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    DataTable { height: 1fr; }
    .catalog { height: 8; }
    .tree { height: 16; }
    .found { height: auto; max-height: 8; }
    .group { padding: 1 0 0 0; text-style: bold; }
    #empty { height: 1fr; padding: 2 4; color: $text-muted; }
    #detail { height: 60%; border-top: solid $primary; padding: 0 1; }
    #detail.hidden { display: none; }
    .dialog { width: 90; height: auto; max-height: 90%; border: thick $primary; background: $surface;
              padding: 1 2; }
    .dialog.help { width: 76; }
    .dialog TextArea { height: 8; }
    .dialog TextArea.description { height: 10; }
    .dialog TextArea.criteria { height: 6; }
    #suggestions { max-height: 8; border: none; background: $boost; }
    #editors { max-height: 12; margin: 1 0; }
    .buttons { height: auto; margin-top: 1; }
    .fields { height: auto; }
    .wrap { width: 100%; }
    /* Text beside a button takes what the button leaves. */
    .role > .wrap { width: 1fr; }
    .role > Button { margin-left: 2; }
    .buttons Button { margin-right: 2; }
    .files { color: $text-muted; margin: 1 0; }
    .role { height: auto; }
    .role > Label { padding: 1 0; }
    .role > .role-name { width: 10; }
    .role > Select { width: 1fr; }
    /* A form: a column of labels, one field per row, only the description and the buttons boxed. */
    .dialog.form { max-width: 100%; }
    .form .section { height: auto; margin-top: 1; }
    .form #task { margin-top: 0; }
    .form .row { height: auto; }
    /* Lists of a group a row apart, unless the terminal is short (fit() decides). */
    .form .row.gap { margin-top: 1; }
    .form.tight .row.gap { margin-top: 0; }
    .form .key { width: 10; color: $text-muted; }
    .form .hint { width: 1fr; color: $text-muted; }
    .form .row > Select, .form .row > TextArea { width: 1fr; }
    .form .row > Button { margin: 0 2 0 0; min-width: 9; }
    .form .buttons > .keys { width: auto; padding: 1 0; }
    .form Select > SelectCurrent { background: $boost; }
    /* Focus is one signal: the focused control's text as the cursor block, as on a button. */
    .form Select:focus > SelectCurrent > Static#label {
        color: $block-cursor-foreground; background: $block-cursor-background; text-style: bold;
    }
    Help, Confirm, DeleteTask, Reply, ReplyWithCriteria, NewTask, NewProject, CommitWork, ChooseEditor,
    AddProvider, ChooseImport, ImportSource, ManageProviders, ManageItems, Browse, NameFolder {
        align: center middle;
    }
    """
    # In the footer's order: your decisions first, then the selected row's actions, then what
    # works anywhere. Keys that matter less often are under ? and off the footer, which is short.
    BINDINGS = [
        Binding("a", "accept", "Accept"),
        Binding("r", "reply", "Reply"),
        Binding("p", "approve_risky", "Approve risky"),
        Binding("g", "verify_again", "Verify again"),
        Binding("d", "details", "Details"),
        Binding("e", "edit_plan", "Edit plan"),
        Binding("c", "copy_prompt", "Prompt"),
        Binding("C", "copy_prompt_cli", "CLI prompt"),
        Binding("f", "show_diff", "Diff"),
        Binding("o", "open_ide", "IDE"),
        Binding("v", "demo", "Run app"),
        Binding("v", "demo_stop", "Stop app"),
        Binding("w", "watch", "Watch"),
        Binding("w", "enter_box", "Enter"),
        Binding("b", "new_box", "Box"),
        Binding("l", "show_log", "Log"),
        # One key, two meanings: the footer shows the one that applies to the selected task. At a
        # checkpoint the agent is not working, so stopping is only taking the pod down.
        Binding("s", "start_task", "Start"),
        Binding("s", "stop_task", "Stop"),
        Binding("s", "stop_pod", "Stop pod"),
        Binding("m", "models", "Model", show=False),
        Binding("x", "remove", "Delete"),
        Binding("e", "edit_project", "Edit project"),
        Binding("o", "open_repo", "IDE"),
        Binding("x", "forget_project", "Forget"),
        Binding("n", "new", "New"),
        Binding("i", "new_project", "New project", show=False),
        Binding("h", "toggle_done", "Show/hide done", show=False),
        Binding("k", "providers", "Providers & MCP", show=False),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.executor = LeavingExecutor()
        self.shown = ""
        # What the table was last drawn from; a refresh that matches it does no work.
        self.drawn: tuple = ()
        self.pairs: list[tuple[Task, TaskState]] = []
        self.done: list[dict] = []
        self.show_done = True
        self.has_done = False
        self.collapsed = load_collapsed()
        # Every project by name, with why its tasks could not start; refreshed with the tasks.
        self.problems: dict[str, str] = {}
        # Tasks whose demo is being started or worked out, and what it is doing: shown as working.
        self.starting: dict[str, str] = {}
        self.frame = 0
        self.table: DataTable = None  # type: ignore[assignment]  # set when the view mounts
        self.columns: tuple[str, ...] = ()  # the ones the terminal's width has room for
        self.running: set[str] = set()
        self.views: dict[str, ui.TaskView] = {}
        # The vivibox this view runs, against what is on disk: they part at git pull.
        self.code_started = code.signature()
        self.code_changed = False
        self.code_checked = 0.0
        # The models of the providers you have keys for: asked once, in the background, and kept
        # for a day, so a dialog never waits on a container.
        self.available: dict[str, list[str]] | None = None
        # The providers opencode knows, for adding one; read with the models, None until then.
        self.catalog: list[tuple[str, str]] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
        yield Static("", id="empty")  # in the table's place while there is nothing to list
        with VerticalScroll(id="detail", classes="hidden"):
            yield Markdown("", id="detail-text")
        yield LiveFooter(compact=True)

    def on_mount(self) -> None:
        asyncio.get_running_loop().set_default_executor(self.executor)
        # Kept by hand: a dialog on top changes what a query would find, and the timers keep running.
        self.pods: dict[str, PodView] = {}  # what each task's pod is doing, refreshed off the loop
        self.waiting = self.working = 0
        self.waiting_ids: set[str] | None = None  # None until the first refresh: nothing is new then
        self.table = self.query_one(DataTable)
        self.panel = self.query_one("#detail")
        self.text = self.query_one("#detail-text", Markdown)
        self.set_columns()
        self.reload()
        self.set_interval(SPIN_SECONDS, self.spin)
        self.set_interval(REFRESH_SECONDS, self.reload)
        self.call_after_refresh(self.check_projects)
        self.call_after_refresh(self.offer_restart)
        self.call_after_refresh(self.hint_opencode)
        self.load_models()

    def action_providers(self) -> None:
        self.push_screen(ManageProviders())

    def import_opencode(self, done) -> None:
        """Which opencode configuration, then which of its providers; done gets the names brought over."""

        def picked(path: Path | None) -> None:
            if path is None:
                done([])
                return
            try:
                reading = providers.read_opencode(path)
            except ConfigError as e:
                self.notify(e.args[0], severity="error", timeout=10)
                done([])
                return
            self.push_screen(ChooseImport(path, reading), chosen)

        def chosen(found: list[providers.Found]) -> None:
            if found:
                providers.bring_over(found)
                self.notify(f"Imported {', '.join(f.name for f in found)}.", timeout=8)
            done([f.name for f in found])

        self.push_screen(ImportSource(providers.discover(project_repos())), picked)

    @work(thread=True)
    def refresh_models(self, added: list[str] = ()) -> None:
        """The models again, after providers changed. One just added that lists none is most
        likely a name opencode does not know; a task's list would only show that it has nothing."""
        self.available = actions.available_models(refresh=True)
        if missing := [name for name in added if not self.available.get(name)]:
            self.call_from_thread(
                self.notify,
                f"opencode lists no models for {', '.join(missing)}; check the name.",
                severity="warning",
            )

    def hint_opencode(self) -> None:
        """Someone who uses opencode has providers set up already; say they can be brought over."""
        if keys.list_keys() or providers.load():
            return
        if found := providers.discover(project_repos()):
            where = shown_path(found[0][0])
            self.notify(
                f"Found your opencode configuration, {where}. Press k to bring its providers over.",
                timeout=15,
            )

    @work(thread=True)
    def load_models(self) -> None:
        """A container per provider the first time in a day; a file read after that."""
        try:
            self.available = actions.available_models()
        except Exception:  # the dialogs fall back to the models config.toml names
            self.available = None
        try:
            self.catalog = actions.provider_catalog()
        except Exception:  # the dialog reads it itself, or you type the name
            self.catalog = None

    def offer_restart(self) -> None:
        """After a reboot the tasks that were at work have no supervisor. Asked once, on start,
        instead of a row-by-row s; a task you stopped yourself stays stopped."""
        idle = [st.id for task, st in self.pairs if self.views[st.id].status == "not running"]
        if not idle:
            return
        one = len(idle) == 1
        were, them = ("This task was", "it") if one else ("These tasks were", "them")
        question = f"{were} running before: {', '.join(idle)}.\n\nStart {them} again?"

        def answered(yes: bool) -> None:
            for task_id in idle if yes else ():
                self.start(task_id, resume=True)

        self.push_screen(Confirm(question, "Start"), answered)

    def check_code(self) -> None:
        """Whether vivibox on disk is still the one running. Said once, and kept in the title."""
        if self.code_changed or time.monotonic() - self.code_checked < CODE_CHECK_SECONDS:
            return
        self.code_checked = time.monotonic()
        with contextlib.suppress(AttributeError):  # a test may have replaced the cached function
            code.signature.cache_clear()
        if code.signature() != self.code_started:
            self.code_changed = True
            self.notify(CODE_CHANGED.capitalize() + ".", severity="warning", timeout=15)
            self.drawn = ()  # the title and the panels' older-supervisor notes changed

    def check_projects(self) -> None:
        """A project whose repository is gone is offered for removal. With none left, the view says
        how to add one rather than open a dialog you did not ask for."""
        broken = actions.broken_projects()
        if not broken:
            return
        listed = "\n".join(f"{name}: {why}" for name, why in broken.items())
        question = f"These projects cannot be worked in:\n\n{listed}\n\nForget them?"

        def answered(yes: bool) -> None:
            for name in broken if yes else ():
                try:
                    actions.forget_project(name)
                except ConfigError as e:
                    self.fail(e)
            self.reload()

        self.push_screen(Confirm(question, "Forget", destructive=True), answered)

    # --- data ---

    def snapshot(self) -> tuple:
        """What the view shows, cheaply. Rebuilding the table and the panel costs Textual several
        hundred retained objects, and a refresh that finds nothing changed used to pay it anyway:
        a session left open overnight reached 5 GB and a quarter of a core with the tasks idle."""
        tasks = tuple(
            (
                st.id,
                str(st.state),
                st.iteration,
                st.paused,
                st.problem,
                st.updated,
                st.id in self.running,
                # The agent ticks criteria while it works, and st.updated only moves between
                # states, so without this the column freezes for the whole of a long turn --
                # exactly when watching it fill in is the only sign of progress. A stat, not a
                # parse: the redraw does the reading.
                ticked_at(task),
            )
            for task, st in self.pairs
        )
        pods = tuple(sorted((k, v.state, len(v.reachable)) for k, v in self.pods.items()))
        # With no project the panel says how to add one, and n is hidden until there is one.
        return (
            tasks,
            pods,
            self.show_done,
            self.selected_id(),
            len(self.done),
            tuple(sorted(self.problems.items())),
            tuple(sorted(self.collapsed)),
            self.has_done,
            tuple(sorted(self.starting.items())),
        )

    def reload(self) -> None:
        """Re-reads every task; the only place that does, so key checks stay cheap."""
        self.check_code()
        selected = self.selected_id()
        pairs = [(t, t.read_state()) for t in list_tasks(self.config.tasks_dir)]
        self.running = {st.id for task, st in pairs if actions.supervisor_running(task)}
        # Worked out once per refresh; the list, the panel and the keys all read it from here.
        self.views = {
            st.id: ui.view(task, st, st.id in self.running, self.config.max_iterations) for task, st in pairs
        }
        self.pairs = pairs = sorted(pairs, key=lambda p: self.views[p[1].id].rank)
        live = {st.id for _, st in pairs}
        self.done = [e for e in actions.history() if e["id"] not in live] if self.show_done else []
        self.problems = {name: actions.project_problem(name) for name in projects()}
        # A stat, not a read: whether h has any finished task to show.
        kept = actions.history_path()
        self.has_done = kept.is_file() and kept.stat().st_size > 0
        now = self.snapshot()
        if now == self.drawn:
            self.look_at_pods([st.id for _, st in pairs])
            return
        self.drawn = now
        self.fill_table(pairs, selected)

    # The columns a terminal has room for, narrowest first: task, status and goal always.
    COLUMNS = ("TASK", "STATUS", "DEMO", "CRITERIA", "COST PLAN + IMPL", "CREATED", "UPDATED", "GOAL")
    NARROW = ("TASK", "STATUS", "GOAL")
    MEDIUM = ("TASK", "STATUS", "CRITERIA", "UPDATED", "GOAL")

    def columns_for(self, width: int) -> tuple[str, ...]:
        return self.NARROW if width < 100 else self.MEDIUM if width < 130 else self.COLUMNS

    def set_columns(self) -> None:
        wanted = self.columns_for(self.size.width)
        if wanted == self.columns:
            return
        self.columns = wanted
        table = self.table
        table.clear(columns=True)
        keys = table.add_columns(*wanted)
        by_name = dict(zip(wanted, keys, strict=True))
        self.status_column, self.demo_column = by_name["STATUS"], by_name.get("DEMO")
        self.drawn = ()

    def on_resize(self) -> None:
        if self.table is not None:  # a resize before the view is built has nothing to lay out
            self.set_columns()
            self.reload()

    def project_order(self, pairs: list) -> list[str]:
        """Projects with a task waiting for you first, then by name. A project a task belongs to
        is listed even when its file is gone, so the task is not orphaned off the screen."""
        ranks: dict[str, int] = {}
        for _, st in pairs:
            ranks[st.project] = min(ranks.get(st.project, ui.FINISHED), self.views[st.id].rank)
        names = set(self.problems) | set(ranks) | {e.get("project", "") for e in self.done}
        return sorted(names, key=lambda n: (ranks.get(n, ui.FINISHED), n))

    def project_summary(self, name: str, tasks: list, done: int) -> str:
        """The project row's status: what keeps its tasks from starting, else what they are doing,
        so a collapsed project still says what waits for you."""
        waiting = sum(self.views[st.id].group == "Waiting for you" for _, st in tasks)
        working = sum(self.busy(st) for _, st in tasks)
        parts = [f"[yellow]{waiting} waiting for you[/]"] * bool(waiting)
        parts += [f"[cyan]{working} working[/]"] * bool(working)
        parts += [f"[grey50]{len(tasks) - waiting - working} stopped[/]"] * bool(
            len(tasks) - waiting - working
        )
        parts += [f"[green]{done} done[/]"] * bool(done)
        return " · ".join(parts) or "[grey50]no tasks · n creates one[/]"

    def fill_table(self, pairs: list, selected: str | None) -> None:
        table = self.table
        table.clear()
        planned: list[tuple[dict[str, str], str]] = []
        for name in self.project_order(pairs):
            own = [(t, st) for t, st in pairs if st.project == name]
            done = [e for e in self.done if e.get("project", "") == name]
            folded = name in self.collapsed
            problem, summary = self.problems.get(name, ""), self.project_summary(name, own, len(done))
            # The problem is the status; otherwise the tasks say what they do, and only a folded
            # project needs its row to say what waits, in a few characters.
            waiting = sum(self.views[st.id].group == "Waiting for you" for _, st in own)
            status = (
                f"[red]{escape(problem)}[/]" if problem
                else f"[yellow]{waiting} waiting for you[/]" if folded and waiting
                else ""
            )  # fmt: skip
            planned.append((
                {"TASK": f"[b]{'▸' if folded else '▾'} {escape(name)}[/]", "STATUS": status, "GOAL": summary},
                PROJECT_ROW + name,
            ))  # fmt: skip
            if folded:
                continue
            for task, st in own:
                spent = ui.cost(task)
                planned.append((
                    {"TASK": f"  {st.id}", "STATUS": self.status(st), "DEMO": self.demo_cell(st.id),
                     "CRITERIA": criteria(task), "COST PLAN + IMPL": str(spent) if spent else "-",
                     "CREATED": ui.ago(st.created), "UPDATED": ui.ago(st.updated), "GOAL": st.goal},
                    st.id,
                ))  # fmt: skip
            for entry in done:
                planned.append((
                    {"TASK": f"  {entry['id']}",
                     "STATUS": "[grey50]  deleted[/]" if entry.get("deleted") else "[green]  done[/]",
                     "DEMO": "-", "CRITERIA": "-", "COST PLAN + IMPL": ui.finished_cost(entry),
                     "CREATED": ui.ago(entry["created"]) if entry.get("created") else "-",
                     "UPDATED": ui.ago(entry["finished"]), "GOAL": entry["title"]},
                    entry["id"],
                ))  # fmt: skip
        # The goal gets what the other columns leave: a goal that runs off the screen is a goal
        # nobody reads. Its width comes from the widest thing each other column shows.
        taken = 0
        for name in self.columns:
            if name != "GOAL":
                widest = max(
                    (Text.from_markup(cells.get(name, "")).cell_len for cells, _ in planned), default=0
                )
                taken += max(widest, len(name)) + 2
        goal_width = max(self.size.width - taken - 3, 16)
        ids = [key for _, key in planned]
        for cells, key in planned:
            cells = {**cells, "GOAL": ui.shorten(cells.get("GOAL", ""), goal_width)}
            table.add_row(*(cells.get(name, "") for name in self.columns), key=key)
        empty = self.query_one("#empty", Static)
        table.display, empty.display = bool(ids), not ids
        if not ids:
            # Nothing left to show details of: the view is back to how it starts.
            self.panel.add_class("hidden")
        empty.update("" if ids else NO_PROJECTS)
        if selected in ids:
            table.move_cursor(row=ids.index(selected))
        elif ids:
            # The first task waiting for you; a project row is a heading, not what you came for.
            first = next((i for i, key in enumerate(ids) if not key.startswith(PROJECT_ROW)), 0)
            table.move_cursor(row=first)
        waiting_now = {st.id for _, st in pairs if self.views[st.id].group == "Waiting for you"}
        # A task that starts to wait for you rings the bell, once: the sign you can hear from
        # another window when the desktop's notifications are off.
        if self.waiting_ids is not None and waiting_now - self.waiting_ids:
            self.bell()
        self.waiting_ids = waiting_now
        self.waiting = len(waiting_now)
        self.working = sum(self.busy(st) for _, st in pairs)
        self.set_sub_title()
        self.show_detail()
        self.refresh_bindings()
        self.look_at_pods([st.id for _, st in pairs])

    def demo_cell(self, task_id: str) -> str:
        """Whether this task is serving anything, on the row itself: the list is what you look at.
        The addresses stay in the panel, where they are clickable and all of them fit; a single port
        here would have to pick one of a front end and a back end, and pick it silently."""
        view = self.pods.get(task_id)
        if view is None or not view.address or not (state := view.state):
            return "-"
        color = {"live": "green", "local": "yellow", "starting": "yellow", "stopped": "red"}[state]
        # How many came up separates "the back end died" from "everything is there".
        count = f" ×{len(view.reachable)}" if len(view.reachable) > 1 else ""
        return f"[{color}]{state}{count}[/]"

    def set_sub_title(self) -> None:
        parts = [f"{self.waiting} waiting for you" if self.waiting else "nothing waiting for you"]
        if self.working:
            parts.append(f"{self.working} working")
        if self.code_changed:
            parts.append(CODE_CHANGED)
        self.sub_title = " · ".join(parts)
        # The window's title, for the taskbar: how many wait for you.
        self.title = f"vivibox ({self.waiting})" if self.waiting else "vivibox"

    @property
    def pod(self) -> PodView:
        """The selected task's pod, for the panel and for which keys the footer offers."""
        return self.pods.get(self.selected_id() or "", PodView())

    def busy(self, st: TaskState) -> bool:
        """The agent or the gate is at work and nothing is needed from you, or the demo is starting."""
        return (self.seen(st).group == "Working" and not st.box) or st.id in self.starting

    def seen(self, st: TaskState) -> ui.TaskView:
        """The task as the last refresh saw it; worked out now for one that refresh has not met."""
        found = self.views.get(st.id)
        if found is None:
            task = next(t for t, s in self.pairs if s.id == st.id)
            found = ui.view(task, st, self.agent_running(st.id), self.config.max_iterations)
        return found

    def status(self, st: TaskState) -> str:
        seen = self.seen(st)
        color = {"yellow": "yellow", "cyan": "cyan", "dim": "grey50", "green": "green"}[ui.COLORS[seen.group]]
        text = seen.status
        if doing := self.starting.get(st.id):
            color, text = "cyan", doing
        mark = SPINNER[self.frame % len(SPINNER)] if self.busy(st) else " "
        return f"[{color}]{mark} {text}[/]"

    def spin(self) -> None:
        """Turns the spinner of busy tasks between full refreshes, touching only their status cells."""
        self.frame += 1
        table = self.table
        for _, st in self.pairs:
            if self.busy(st):
                table.update_cell(st.id, self.status_column, self.status(st))

    def selected_id(self) -> str | None:
        table = self.table
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def selected(self) -> tuple[Task, TaskState] | None:
        task_id = self.selected_id()
        return next(((t, st) for t, st in self.pairs if st.id == task_id), None)

    def selected_project(self) -> str:
        """The project row under the cursor, or the project of the task under it."""
        key = self.selected_id() or ""
        if key.startswith(PROJECT_ROW):
            return key.removeprefix(PROJECT_ROW)
        if pick := self.selected():
            return pick[1].project
        if entry := self.finished_entry(key):
            return entry["project"]
        return ""

    def on_project_row(self) -> bool:
        return (self.selected_id() or "").startswith(PROJECT_ROW)

    def agent_running(self, task_id: str) -> bool:
        return task_id in self.running

    def show_detail(self) -> None:
        if self.panel.has_class("hidden"):
            return
        pick = self.selected()
        if pick:
            text = detail(*pick, self.config.max_iterations, self.agent_running(pick[1].id), self.pod)
        elif entry := self.finished_entry(self.selected_id()):
            text = finished_detail(entry)
        elif self.on_project_row():
            name = self.selected_project()
            own = sum(st.project == name for _, st in self.pairs)
            text = project_detail(name, own, self.problems.get(name, ""))
        else:
            text = NO_PROJECTS
        if text != self.shown:  # redrawing resets the scroll position
            self.shown = text
            self.text.update(text)

    def action_toggle_done(self) -> None:
        self.show_done = not self.show_done
        self.reload()

    def finished_entry(self, task_id: str | None) -> dict | None:
        return next((e for e in self.done if e["id"] == task_id), None)

    def action_details(self) -> None:
        """The list is what you look at; the plan, the diff or the agent's question on request."""
        self.panel.set_class(not self.panel.has_class("hidden"), "hidden")
        self.show_detail()

    @on(DataTable.RowSelected)
    def selected_row(self) -> None:
        if self.on_project_row():
            self.collapsed ^= {self.selected_project()}
            save_collapsed(self.collapsed)
            self.reload()
        else:
            self.action_details()

    @on(DataTable.RowHighlighted)
    def highlighted(self) -> None:
        # Queued messages can still arrive after the view is gone, and both of these reach for the
        # screen. There is nothing to redraw for a view that has closed.
        if not self.screen_stack:
            return
        self.show_detail()
        self.refresh_bindings()

    @work(thread=True, exclusive=True, group="pod-view")
    def look_at_pods(self, task_ids: list[str]) -> None:
        """Asking the pods means running docker, which is far too slow for the event loop.
        Exclusive: a refresh that arrives while one is in flight replaces it."""
        found = pod_views(task_ids) if task_ids else {}
        # Docker can take longer than the app lives, and a closed view has nobody to tell.
        if self.screen_stack:
            self.call_from_thread(self.pods_answered, found)

    def pods_answered(self, found: dict) -> None:
        # Checked again here: the app can close between that check and this call.
        if found == self.pods or not self.screen_stack:
            return
        self.pods = found
        for task_id in found:
            if self.demo_column is None:
                break
            with contextlib.suppress(Exception):  # the row may have gone while docker was thinking
                self.table.update_cell(task_id, self.demo_column, self.demo_cell(task_id))
        self.show_detail()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Only the keys that do something for the selected task show in the footer."""
        task_actions = ("accept", "reply", "edit_plan", "open_ide", "approve_risky", "watch",
                        "start_task", "stop_task", "stop_pod", "remove", "demo", "demo_stop",
                        "models", "copy_prompt", "copy_prompt_cli", "verify_again", "show_log",
                        "show_diff", "enter_box")  # fmt: skip
        if action == "new":
            return bool(projects())  # a task needs a project to be in
        if action in ("edit_project", "open_repo", "forget_project", "new_box"):
            if not self.on_project_row():
                return False
            if action == "new_box":
                return not self.problems.get(self.selected_project())
            # A project with tasks is not forgotten from under them: delete those first.
            return action != "forget_project" or not any(
                st.project == self.selected_project() for _, st in self.pairs
            )
        if action == "details":  # nothing to show details of; an open panel can still be closed
            return bool(projects()) or (self.is_mounted and not self.panel.has_class("hidden"))
        if action == "toggle_done":
            return self.has_done
        if action not in task_actions:  # quit, and moving focus in dialogs
            return True
        pick = self.selected()
        if not pick:
            # A finished task is history: you can only look at it or forget it.
            return action == "remove" and self.finished_entry(self.selected_id()) is not None
        state, running = pick[1].state, self.agent_running(pick[1].id)
        at_work = running and not pick[1].paused
        box = pick[1].box
        if box and state is State.IMPLEMENT:
            # A box has no agent to reply to, watch or model; its keys are the pod's.
            open_ = not pick[1].paused and pick[1].id not in self.starting
            return {
                "enter_box": open_,
                "accept": open_,
                "start_task": pick[1].paused and pick[1].id not in self.starting,
                "stop_task": open_,
                "remove": True,
                "demo": open_,
                "demo_stop": self.pod.demo,
                "show_log": newest_log(pick[0]) is not None,
            }.get(action, False)
        allowed = {
            # A manual planner's checkpoint before your plan is in has nothing to accept, and a
            # reply would reach nobody: the planner is your own chat.
            "accept": state in (State.CHECKPOINT_PLAN, State.CHECKPOINT_FINAL) and not pick[1].awaiting_plan,
            "reply": state in WAITING_ONLY and not pick[1].awaiting_plan and not box,
            "models": state is not State.DONE and not box,
            # Not while the agent may be writing its own draft.
            "edit_plan": state is State.CHECKPOINT_PLAN or (state is State.PLAN and not running),
            "open_ide": state is State.CHECKPOINT_FINAL,
            "show_diff": state is State.CHECKPOINT_FINAL,
            # Also once a plan is in: going back to the same chat is how you change it.
            "copy_prompt": state is State.CHECKPOINT_PLAN and self.planned_by_you(pick[0]),
            "copy_prompt_cli": state is State.CHECKPOINT_PLAN and self.planned_by_you(pick[0]),
            "approve_risky": state is State.APPROVAL_RISKY,
            # With a question too: the agent asks about the environment more often than the gate
            # recognises one, and once that is fixed the build is the answer.
            "verify_again": state is State.CHECKPOINT_BLOCKED,
            "show_log": newest_log(pick[0]) is not None,
            "watch": watchable(pick[0], pick[1], running),
            # Not again while one of them is under way. A task that stopped on a failure still has
            # its supervisor, and what it needs is a start, not a stop followed by a start.
            "start_task": state is not State.DONE and not at_work and pick[1].id not in self.starting,
            "stop_task": at_work and state not in WAITING_ONLY and pick[1].id not in self.starting,
            "stop_pod": at_work and state in WAITING_ONLY and pick[1].id not in self.starting,
            "remove": True,
            # Once the work is back with you, not while the agent builds in the same tree. Running
            # it again while it runs is a restart, which is what you want after a change.
            "demo": actions.demo_allowed(pick[1]),
            "demo_stop": self.pod.demo,
        }
        return allowed.get(action, True)

    def fail(self, error: Exception) -> None:
        self.notify(str(error.args[0] if error.args else error), severity="error", timeout=10)

    # --- actions ---

    def action_accept(self) -> None:
        task, st = self.selected()
        if st.box and st.state is State.IMPLEMENT:
            self.close_box(task.id)
            return
        if st.state is State.CHECKPOINT_PLAN:
            project = actions.load(task.id)[1]

            def accept(yes: bool = True) -> None:
                if not yes:
                    return
                try:
                    actions.accept_plan(task, project)
                except Exception as e:
                    self.fail(e)
                else:
                    self.go_on(task, "Plan accepted")
                self.reload()

            # What the first plan settles for the project, said once, where you decide.
            if settles := actions.verify_from_plan(task, project):
                asked = f"The plan sets how {project.name} is verified from now on: {settles}."
                self.push_screen(Confirm(f"{asked}\n\nAccept the plan?", "Accept"), accept)
            else:
                accept()
        else:
            self.push_screen(
                Confirm(f"Accept the work of {task.id} into your checkout and remove the task?", "Accept"),
                lambda yes: yes and self.finish(task.id),
            )

    @work(thread=True)
    def close_box(self, task_id: str) -> None:
        self.call_from_thread(self.busy_with, task_id, "closing the box…")
        try:
            task, project = actions.load(task_id)
            where = actions.close_box(task, project)
        except Exception as e:
            self.call_from_thread(self.fail, e)
        else:
            said = f"{task_id} closed; " + (
                f"its work is ready for your review in {where}" if where else "it changed risky files"
            )
            self.call_from_thread(self.notify, said, timeout=8)
        self.call_from_thread(self.busy_with, task_id, "")

    def action_enter_box(self) -> None:
        task, _ = self.selected()
        try:
            command = actions.box_shell_command(task.id)
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)
        self.reload()

    def action_new_box(self) -> None:
        name = self.selected_project()
        self.notify(f"Opening a box in {name}…")
        self.open_box(name)

    @work(thread=True)
    def open_box(self, name: str) -> None:
        try:
            task = actions.open_box(name)
        except Exception as e:
            self.call_from_thread(self.fail, e)
        else:
            self.call_from_thread(self.notify, f"{task.id} is open; w enters it, a brings its work back.")
            self.call_from_thread(self.select, task.id)
        self.call_from_thread(self.reload)

    def select(self, task_id: str) -> None:
        """Puts the cursor on a row the next refresh will list: what you just made is what you
        look at next."""
        self.reload()
        ids = [str(key.value) for key in self.table.rows]
        if task_id in ids:
            self.table.move_cursor(row=ids.index(task_id))

    @work(thread=True)
    def finish(self, task_id: str) -> None:
        try:
            task, project = actions.load(task_id)
            done = actions.finish(task, project)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.finished, done)

    def finished(self, done: actions.Finished) -> None:
        self.reload()
        if done.conflicts:
            files = ", ".join(done.conflicts[:5])
            text = f"{done.task_id}: conflicts in {files}; resolve them in your IDE. Also on {done.branch}."
            self.notify(text, severity="warning", timeout=15)
            return

        def commit(message: str) -> None:
            if not message:
                self.notify("Left uncommitted; commit it in your IDE when you are ready.")
                return
            try:
                self.notify(f"Committed: {actions.commit_work(done.source, message)}")
            except Exception as e:
                self.fail(e)

        self.push_screen(CommitWork(done), commit)

    def action_reply(self) -> None:
        task, st = self.selected()

        def send(comment: str, criteria: list[str] = ()) -> None:
            if not comment.strip() and not criteria:
                return
            try:
                actions.reply(task, comment, criteria)
            except Exception as e:
                self.fail(e)
            else:
                added = f" with {len(criteria)} new criteria" if criteria else ""
                self.go_on(task, f"Sent{added}")
            self.reload()

        if st.state in (State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED):
            self.push_screen(ReplyWithCriteria(task.id), lambda a: a and send(a["comment"], a["criteria"]))
        else:
            self.push_screen(Reply(task.id), send)

    def go_on(self, task: Task, done: str = "") -> None:
        """After a decision of yours the task goes on. With nobody working on it (after a reboot,
        or once you stopped it) that takes a start, and saying "moves on" without one was a lie."""
        st = task.read_state()
        starts = actions.needs_start(task)
        if done and starts:
            self.notify(f"{done}; starting {task.id}, nobody was working on it.")
        elif done:
            self.notify(f"{done}; {task.id} is {ui.WORKING.get(st.state, 'waiting for you')} now.")
        if starts:
            self.start(task.id, resume=True)

    def planned_by_you(self, task: Task) -> bool:
        return (task.meta / manual.PROMPT).exists()

    def action_edit_plan(self) -> None:
        task, st = self.selected()
        manual_plan = st.state is State.CHECKPOINT_PLAN and self.planned_by_you(task)
        # With a manual planner you edit your chat's answer, which is then brought in again: the
        # plan and the answer cannot drift apart, and the chat's next answer does not undo yours.
        path = actions.answer_path(task) if manual_plan else task.plan_path
        with self.suspend():
            edit_in_editor(path)
        if manual_plan and path.exists() and path.read_text().strip():
            self.bring_in_plan(task)
        self.reload()

    def bring_in_plan(self, task: Task) -> None:
        try:
            actions.import_plan(task)
        except PlanError as e:
            self.to_clipboard(manual.repair_prompt(str(e)))
            self.notify(
                f"That is not a plan yet: {e}. A message asking your chat to fix it is in your clipboard.",
                severity="warning",
                timeout=15,
            )
            return
        except Exception as e:
            self.fail(e)
            return
        count = len(parse_plan(task.plan_path.read_text()).criteria)
        self.reload()
        self.push_screen(
            Confirm(f"Plan brought in, {count} criteria. Accept it and start implementing?", "Accept"),
            lambda yes: yes and self.action_accept(),
        )

    def to_clipboard(self, text: str) -> str:
        """Through the desktop's own tool where there is one; the terminal's clipboard escape
        (OSC 52) is the fallback, and not every terminal honours it."""
        for command in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(command[0]):
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(
                        command, input=text, text=True, check=True, timeout=5,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )  # fmt: skip
                    return command[0]
        self.copy_to_clipboard(text)
        return "the terminal"

    def action_copy_prompt(self, cli: bool = False) -> None:
        task, _ = self.selected()
        try:
            where = self.to_clipboard(actions.plan_prompt(task, cli=cli))
        except Exception as e:
            self.fail(e)
            return
        file = task.meta / (manual.PROMPT_CLI if cli else manual.PROMPT)
        self.notify(f"Copied the prompt ({where}); it is also in {file}.", timeout=8)

    def action_copy_prompt_cli(self) -> None:
        self.action_copy_prompt(cli=True)

    def action_open_ide(self) -> None:
        task_id = self.selected()[1].id
        if actions.editor_command(self.config, actions.load(task_id)[1]):
            self.open_ide(task_id)
            return
        found = ide.candidates()
        if not found:
            self.fail(ConfigError('no editor found; set [review] ide in config.toml, e.g. "code {path}"'))
            return

        def chosen(command: str) -> None:
            if not command:
                return
            ide.remember(command)
            self.config = load_config()
            self.open_ide(task_id)

        self.push_screen(ChooseEditor(found), chosen)

    def open_ide(self, task_id: str) -> None:
        task, project = actions.load(task_id)
        command = actions.editor_command(self.config, project)
        try:
            path = actions.review_copy(task, project)
        except Exception as e:
            self.fail(e)
            return
        if ide.is_terminal(command):
            with self.suspend():  # it takes over the terminal, like the plan editor does
                subprocess.run(ide.command_for(path, command))
            self.reload()
            return
        try:
            actions.open_in_ide(self.config, path, project)
            self.notify(f"Opening {path}")
        except Exception as e:
            self.fail(e)

    def action_verify_again(self) -> None:
        task, _ = self.selected()
        try:
            actions.verify_again(task)
        except Exception as e:
            self.fail(e)
        else:
            self.go_on(task, f"Verifying {task.id} again")
        self.reload()

    def diff_command(self) -> list[str]:
        task, _ = self.selected()
        return git_diff(task, actions.load(task.id)[1])

    def action_show_diff(self) -> None:
        """The work as a diff, in git's own pager, before you accept it."""
        try:
            command = self.diff_command()
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)

    def action_show_log(self) -> None:
        """The newest verification log, or the supervisor's, in your pager."""
        task, st = self.selected()
        if command := log_command(task, st, self.agent_running(task.id)):
            with self.suspend():
                subprocess.run(command)

    def action_approve_risky(self) -> None:
        task, _ = self.selected()

        def approve(yes: bool) -> None:
            if not yes:
                return
            try:
                count, review = actions.approve_risky(task, actions.load(task.id)[1])
                self.notify(
                    f"Approved {count} change(s)." + (f" Ready for review in {review}" if review else "")
                )
            except Exception as e:
                self.fail(e)
            else:
                self.go_on(task)
            self.reload()

        self.push_screen(Confirm(f"Approve the risky files of {task.id} as shown?", "Approve"), approve)

    @on(Markdown.LinkClicked)
    def open_link(self, event: Markdown.LinkClicked) -> None:
        """An address in the details panel: what the agent is running, opened in your browser."""
        event.prevent_default()
        self.open_url(event.href)

    def action_demo_stop(self) -> None:
        task, _ = self.selected()
        self.stop_demo(task.id)

    @work(thread=True)
    def stop_demo(self, task_id: str) -> None:
        try:
            actions.demo_stop(task_id)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        self.call_from_thread(self.notify, "Stopped.", timeout=3)
        self.call_from_thread(self.reload)

    def action_demo(self) -> None:
        """Runs the project in its pod so you can open it. When nothing says how, the agent works it
        out without asking first: you pressed the key that means run it, and there is no second
        answer you could give. An instruction an earlier task left behind is a real choice, though,
        because it may be stale, so that one is still yours to confirm."""
        task, project = actions.load(self.selected()[1].id)
        if actions.demo_commands(project, task)[0]:
            self.run_demo(task.id)
            return
        if earlier := actions.demo_from_history(project.name):
            asked = f"The last task you accepted was run like this:\n\n{earlier}\n\nStill right?"
            self.push_screen(
                Confirm(asked, "Use it"),
                # Saying no means work it out again, not do nothing: you asked for it to run.
                lambda yes: self.run_demo(task.id, use=earlier) if yes else self.run_demo(task.id, ask=True),
            )
            return
        self.run_demo(task.id, ask=True)

    def busy_with(self, task_id: str, doing: str) -> None:
        """What a slow step (the demo, starting or stopping the task) is doing, in the task's
        status, with the spinner a working agent has; "" when it is done, one way or the other."""
        if doing:
            self.starting[task_id] = doing
        else:
            self.starting.pop(task_id, None)
        self.reload()

    @work(thread=True)
    def run_demo(self, task_id: str, ask: bool = False, use: str = "", reply: str = "") -> None:
        doing = "working out how to run it" if ask or reply else "starting the demo"
        self.call_from_thread(self.busy_with, task_id, doing)
        try:
            self.demo_outcome(task_id, ask, use, reply)
        finally:
            self.call_from_thread(self.busy_with, task_id, "")

    def demo_outcome(self, task_id: str, ask: bool, use: str, reply: str) -> None:
        try:
            if use:
                result = actions.use_instruction(task_id, use)
            else:
                result = actions.demo(task_id, ask=ask or bool(reply), reply=reply)
        except Exception as e:
            self.call_from_thread(self.fail, e)
            return
        if result.stopped:
            what = "; ".join(result.stopped)
            self.call_from_thread(
                self.notify, f"Stopped what the agent left running: {what}", severity="warning", timeout=8
            )
        if result.question:
            self.call_from_thread(self.answer_demo, task_id, result.question)
        elif urls := result.urls:
            self.call_from_thread(self.open_url, urls[0])
        elif blocked := result.unreachable:
            self.call_from_thread(
                self.notify, f"Port {blocked[0].port} is {blocked[0].why_not}", severity="warning", timeout=10
            )
        elif not result.commands:
            self.say("Still nothing says how to run it")
        elif result.starting:
            # It is alive and installing or compiling. The DEMO column is watching and will say when.
            self.call_from_thread(
                self.notify, "Still starting; the DEMO column says when it listens", timeout=8
            )
        else:
            self.say("It stopped without listening; press d for what it said")

    def say(self, message: str) -> None:
        self.call_from_thread(self.notify, message, severity="error", timeout=8)

    def answer_demo(self, task_id: str, question: str) -> None:
        """The agent asked something only you can decide. Answering carries the same conversation on,
        and none of it can move the task between states."""
        self.push_screen(
            Reply(task_id, f"Working out how to run it, the agent asks:\n\n{question}\n\nYour answer"),
            lambda text: self.run_demo(task_id, reply=text) if text else None,
        )

    def action_models(self) -> None:
        """Which model each role runs on, for this task only. The machine's config.toml is the
        default and stays untouched; a task that needs more, or less, says so here."""
        pick = self.selected()
        if not pick:
            return
        task = pick[0]
        try:
            config = load_config()
            st = task.read_state()
            rows = [
                (name, actions.choice_label(self.current_choice(task, name, config)),
                 name in st.models or name in st.harnesses)
                for name in sorted(config.roles)
            ]  # fmt: skip
        except (ConfigError, OSError) as e:
            self.fail(e)
            return

        def role_picked(role: str) -> None:
            if not role:
                return
            offered = actions.choices(role, config, self.available)
            configured = actions.configured_choice(config, role)
            self.push_screen(
                ChooseModel(role, offered, configured, self.current_choice(task, role, config)),
                lambda choice: self.set_choice(task, role, choice, config),
            )

        self.push_screen(ChooseRole(rows), role_picked)

    @staticmethod
    def current_choice(task: Task, role: str, config) -> actions.Choice:
        r = actions.role_of(task, role, config)
        return r.harness, r.model if r.harness != manual.NAME else ""

    def set_choice(self, task: Task, role: str, choice: actions.Choice | None, config) -> None:
        if choice is None:
            return
        harness, model = choice
        if not model and harness != manual.NAME:
            return  # "no model yet" is where the role is, not a model to put it on
        if choice == actions.configured_choice(config, role):
            task.set_role(role)  # back to config.toml, and following it when it changes
        else:
            task.set_role(role, harness if harness != config.roles[role].harness else "", model)
        self.notify(f"{role} runs on {actions.choice_label(choice)} from the next start.", timeout=6)
        self.reload()

    def action_watch(self) -> None:
        """The agent, or the verification as it runs. A task with two conversations, the planner's
        done and the writer's under way, asks which; the verification's log is the writer's turn."""
        task, st = self.selected()
        sessions = [] if st.box or st.state is State.VERIFY else actions.watchable_sessions(task, st)
        if len(sessions) < 2:
            self.watch(task.id)
            return
        at_work = "planner" if st.state is State.PLAN else "writer"
        rows = [
            (role, "at work now" if role == at_work else "finished; its conversation") for role, _ in sessions
        ]
        self.push_screen(ChooseSession(rows), lambda role: role and self.watch(task.id, role))

    def watch(self, task_id: str, role: str = "") -> None:
        try:
            command = actions.attach_command(task_id, role)
        except Exception as e:
            self.fail(e)
            return
        with self.suspend():
            subprocess.run(command)
            after_window(session_gone=not actions.tmux_has(actions.tmux_session(task_id)))
        self.reload()

    def action_start_task(self) -> None:
        task, _ = self.selected()
        self.start(task.id, resume=any(e["type"] == "started" for e in task.events()))

    def action_stop_task(self) -> None:
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Stop {task.id}? Its work is kept; start it again with s.", "Stop", destructive=True),
            lambda yes: yes and self.stop(task.id),
        )

    def action_stop_pod(self) -> None:
        """At a checkpoint nothing runs but the pod; your decision starts it again by itself."""
        task, _ = self.selected()
        self.push_screen(
            Confirm(f"Take the pod of {task.id} down? Your next decision starts it again.", "Stop pod"),
            lambda yes: yes and self.stop(task.id),
        )

    def action_help(self) -> None:
        self.push_screen(Help())

    @work(thread=True)
    def start(self, task_id: str, resume: bool = False) -> None:
        step = lambda doing: self.call_from_thread(self.busy_with, task_id, doing)  # noqa: E731
        step("starting…")
        try:
            model = actions.start(task_id, resume=resume, on_step=step)
            self.call_from_thread(self.notify, f"{task_id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.busy_with, task_id, "")

    @work(thread=True)
    def stop(self, task_id: str) -> None:
        # Taking the pod down takes a while; without this the row looked as if nothing happened.
        self.call_from_thread(self.busy_with, task_id, "stopping…")
        try:
            actions.stop(actions.load(task_id)[0])
            self.call_from_thread(self.notify, f"{task_id} stopped.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.busy_with, task_id, "")

    def project_file(self) -> Path:
        return config_dir() / "projects" / f"{self.selected_project()}.toml"

    def action_edit_project(self) -> None:
        """How the project is verified, picked from what its build files name; the file itself
        for the rest, and when it cannot be read at all."""
        name = self.selected_project()
        try:
            project = load_project(name)
        except ConfigError:
            self.edit_project_file()
            return

        def chosen(choice: dict) -> None:
            if not choice:
                return
            if choice.get("edit"):
                self.edit_project_file()
                return
            actions.save_verify(project, choice["verify"], choice["no_build"])
            how = actions.NO_BUILD if choice["no_build"] else ", ".join(f"`{c}`" for c in choice["verify"])
            self.notify(f"{name} is verified from now on: {how}", timeout=6)
            self.drawn = ()
            self.reload()

        self.push_screen(
            ChooseVerify(name, project.verify, project.no_build, project_init.candidates(project.repo)),
            chosen,
        )

    def edit_project_file(self) -> None:
        with self.suspend():
            edit_in_editor(self.project_file())
        self.drawn = ()  # verify, pass_env, the repository: any of it may have changed
        self.reload()

    def action_open_repo(self) -> None:
        try:
            project = load_project(self.selected_project())
            actions.open_in_ide(self.config, project.repo, project)
            self.notify(f"Opening {shown_path(project.repo)}")
        except Exception as e:
            self.fail(e)

    def action_forget_project(self) -> None:
        name = self.selected_project()
        dialog = DeleteTask(
            f"Forget the project {name}?",
            self.problems.get(name, "") or "vivibox will no longer offer it for tasks.",
            "its project file, with its verify commands and settings.",
            "the repository, exactly as it is.",
        )

        def forget(yes: bool) -> None:
            if yes:
                try:
                    actions.forget_project(name)
                except ConfigError as e:
                    self.fail(e)
                self.reload()

        self.push_screen(dialog, forget)

    def action_new(self, preselect: str = "") -> None:
        if not projects():
            self.notify("A task needs a project first; press i to add one.")
            return
        if actions.needs_provider(load_config()):
            self.notify("A task needs a provider first; press k to add one.")
            return
        preselect = preselect or self.selected_project()

        def create(form: dict) -> None:
            if form.get("project") == NEW_PROJECT:
                self.new_project()
                return
            if not form:
                return
            if not form["goal"] or form["project"] is Select.NULL:
                self.notify("A task needs a project and a description.", severity="error")
                return
            self.notify(f"Creating a task in {form['project']}…")
            self.create(form)

        self.push_screen(NewTask(preselect, self.available), create)

    def action_new_project(self) -> None:
        self.new_project()

    def new_project(self) -> None:
        """Sets up a project, starting a repository when the folder has none, then asks for its first task."""

        def done(form: dict) -> None:
            if not form:
                if not projects():
                    self.notify("vivibox needs a project to work on; press i to set one up.", timeout=10)
                return
            if used := form.get("use"):
                self.action_new(used)  # the folder is a project already: straight to its next task
                return
            try:
                target = actions.setup_project(
                    Path(form["path"]), form["name"], form["verify"], create=True, no_build=form["no_build"]
                )
            except Exception as e:
                self.fail(e)
                return
            self.notify(f"Set up {form['name']} in {target}")
            self.action_new(form["name"])

        self.push_screen(NewProject(), done)

    @work(thread=True)
    def create(self, form: dict) -> None:
        try:
            task = actions.create(
                form["project"], form["goal"], auto=form["auto"], kind=form["kind"], roles=form.get("roles")
            )
            self.call_from_thread(self.reload)
            for note in actions.context_notes(task):
                self.call_from_thread(self.notify, f"{task.id}: {note}.", severity="warning", timeout=12)
            if form["draft"]:
                self.call_from_thread(
                    self.notify, f"Created {task.id}; edit its plan with e, start it with s."
                )
                return
            step = lambda doing: self.call_from_thread(self.busy_with, task.id, doing)  # noqa: E731
            step("starting…")
            try:
                model = actions.start(task.id, on_step=step)
            finally:
                step("")
            self.call_from_thread(self.notify, f"{task.id} started ({model}).")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)

    def action_remove(self) -> None:
        if entry := self.finished_entry(self.selected_id()):
            kind = "deleted" if entry.get("deleted") else "done"
            kept = actions.archive_path(entry["id"])
            dialog = DeleteTask(
                f"Delete {entry['id']} from the history?",
                f"{entry['title']} ({kind}, {ui.finished_cost(entry)})",
                "its line in this list"
                + (", and its archive (the plan, the events)." if kept.exists() else "."),
                "everything else: "
                + ("nothing of it was left anyway." if kind == "deleted" else "its work in your repository."),
            )

            def forget(yes: bool) -> None:
                if yes:
                    actions.forget(entry["id"])
                    self.reload()

            self.push_screen(dialog, forget)
            return
        task, st = self.selected()
        running = self.agent_running(task.id)
        met = criteria(task)
        about_task = [self.seen(st).status]
        about_task += [f"criteria {met}"] if met != "-" else []
        about_task.append(str(ui.cost(task)))
        dialog = DeleteTask(
            f"Delete {task.id}?",
            f"{st.goal} ({', '.join(about_task)})",
            "its clone with every commit the agent made, its plan, its pod and anything running in it, "
            "and its review copy. It is not accepted: none of its work reaches your repository.",
            "a line in the history, with what it was for, what it cost and how far it got. "
            "Your repository is untouched.",
            warning="The agent is working on it now; it is stopped first." if running else "",
        )
        self.push_screen(dialog, lambda yes: self.remove_task(task.id) if yes else None)

    @work(thread=True)
    def remove_task(self, task_id: str) -> None:
        try:
            task, project = actions.load(task_id)
            actions.remove(task, project)
            self.call_from_thread(self.notify, f"Deleted {task_id}.")
        except Exception as e:
            self.call_from_thread(self.fail, e)
        self.call_from_thread(self.reload)


def run() -> int:
    app = Vivibox()
    app.run()
    if left := app.executor.unfinished():
        # asyncio would wait for these at the end, and so would the interpreter's exit; the pod and
        # the supervisor are processes of their own, so the tasks go on without this window.
        step = "step" if left == 1 else "steps"
        print(f"{left} {step} still finishing in the background, a pod starting or stopping; the tasks go on")
        sys.stdout.flush()
        os._exit(0)
    return 0
