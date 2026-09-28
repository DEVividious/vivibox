"""What the view says of a task: its status, its details panel, its next steps, and the
small readers behind them. Pure functions of the task's files; the app in tui.py draws them.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    actions,
    code,
    gate,
    manual,
    progress,
    proposal,
    providers,
    repo,
    review,
    supervisor,
    timeline,
    ui,
)
from . import pod as pod_module
from .app_support import in_terminal
from .config import ConfigError, config_dir, load_project
from .plan import body as plan_body
from .plan import parse_plan
from .sections import (  # noqa: F401 (criteria and checklist: the list reads them from here)
    build_files_left,
    checklist,
    criteria,
    criteria_section,
    gate_failed,
    last_gate,
    plan_section,
    plan_text,
    reviewers_notes,
    roles_section,
)
from .states import State
from .task import Task, TaskState

REFRESH_SECONDS = 2.0
# How often the view looks whether vivibox changed on disk: a git pull, not a keystroke.
CODE_CHECK_SECONDS = 10.0
CODE_CHANGED = "vivibox changed on disk: quit and start it again"
OLDER_SUPERVISOR = "runs an older vivibox; stop and start it (`s`) when it suits you"
SPIN_SECONDS = 0.1
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


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


def ticked_at(task: Task) -> tuple:
    """Checklist and live todos can change without a state transition."""
    changed = []
    for path in (task.meta / "handoff" / gate.CRITERIA_FILE, task.meta / progress.FILE):
        try:
            stat = path.stat()
            changed.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except OSError:
            changed.append(None)
    return tuple(changed)


def live_at(task: Task) -> float:
    """When the turn under way last wrote its running cost; 0 with no turn running."""
    try:
        return (task.meta / task.LIVE_TURN).stat().st_mtime
    except OSError:
        return 0.0


def pod_views(task_ids: list[str]) -> dict[str, PodView]:
    """What every task's pod is doing. One question for all the addresses, then one per pod that is
    up; asking each pod separately for each thing is what would make this too slow to do often."""
    pods = {task_id: pod_module.Pod(task_id, Path("."), "") for task_id in task_ids}
    found = pod_module.addresses([pod.sidecar for pod in pods.values()])
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
                f"**{ui.count(n, 'test')} removed**; see whether the plan meant it:",
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


def command_to_review(task: Task) -> list[str]:
    """The command the writer proposed, before the first verification: what it is, what is odd
    about it, and that keeping it settles the project's verification."""
    command = proposal.proposed(task)
    if not command:
        return [
            f"**No command came from the writer:** it wrote none to `/task/handoff/{proposal.PROPOSAL}`.",
            "Type the one that builds the project and runs its tests, or ask the writer for one.",
            "",
        ]
    lines = [f"**Proposed command:** `{command}`", ""]
    if (selection := gate.narrowed_proposal(command)) and gate.DEBUG_OUTPUT.fullmatch(selection):
        lines += [
            f"*This command has debug output on ({selection}): every verification log would be"
            " megabytes of it. Take it out, or ask the writer for the command without it.*",
            "",
        ]
    elif selection:
        lines += [
            f"*This command is narrowed to {selection}: verified by a part of the project alone, this task"
            " and every one after it pass whatever they break elsewhere. Change it to the whole build,"
            " or ask the writer for it.*",
            "",
        ]
    lines += [
        "Kept, it is how every task of this project is verified from now on (`e` on the project changes it).",
        "",
    ]
    return lines


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
    return ["git", "-C", str(project.repo), "diff", f"{base}...{repo.review_ref(task.id)}"]


def git_env() -> dict[str, str]:
    """The environment for git's own pager. With LESS unset git sets it to FRX, and -X keeps less
    on the terminal's main screen: the diff scrolls among what the terminal showed before, and
    stays there after q. R alone puts it on the alternate screen, as the pager under l is. Your
    own LESS stays as it is."""
    return {**os.environ, "LESS": os.environ.get("LESS") or "R"}


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
        if st.state is State.CHECKPOINT_BLOCKED:
            return "`s` start · `g` verify again, once you have fixed it · `r` tell the agent"
        return "`s` start; it goes on from where it was"
    if st.awaiting_review and st.state is State.REVIEW:
        return "your agent's CLI reviews this round · `m` put the supervisor on a model, then `s`"
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN and st.plan_in_cli:
        return "`C` copy the prompt for your agent's CLI · `e` paste the plan"
    if st.awaiting_plan and st.state is State.CHECKPOINT_PLAN:
        return "`c` copy the prompt for a browser · `C` for a CLI · `e` paste the plan"
    if ui.planner_asks(task, st):
        return "`r` answer, and it plans again · `e` write the plan yourself"
    if st.state is State.CHECKPOINT_PLAN:
        return "`a` accept the plan · `r` send it back with a comment · `e` edit it"
    if st.state is State.CHECKPOINT_COMMAND:
        return "`a` keep it for every task · `e` change it · `r` ask the writer for another"
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
    if st.state is State.REVIEW:
        return "wait for the reviewer" + watch
    if st.state is State.VERIFY:
        look = " · `w` look at it as it runs" if watchable(task, st, running) else ""
        log = " · `l` read its log so far" if newest_log(task) else ""
        return "wait for the verification" + look + log
    return "wait" + watch + " · `s` stop"


PROJECT_ROW = "project:"
# The keys that decide something, first in the footer and never off it, however narrow the terminal.
DECISION_KEYS = ("a", "r", "p", "g")


def view_state_path() -> Path:
    return actions.history_path().with_name("view.json")


def load_view() -> dict:
    """Your choices about the list, the ones that outlive the view: the projects you folded away,
    whether the tasks you accepted or deleted are shown."""
    try:
        return json.loads(view_state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_view(**choices) -> None:
    state = {**load_view(), **choices}
    path = view_state_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state) + "\n")


def load_collapsed() -> set[str]:
    return set(load_view().get("collapsed", []))


def save_collapsed(names: set[str]) -> None:
    save_view(collapsed=sorted(names))


def project_detail(name: str, tasks: int, problem: str) -> str:
    """A project as the panel shows it: where it is, how it is built and tested, and what would
    keep its tasks from starting."""
    path = config_dir() / "projects" / f"{name}.toml"
    lines = [f"### {name}", ""]
    if problem:
        lines += [f"**{problem}.**", ""]
    lines += [
        "**Next:** `n` new task · `e` its settings · `o` open the repository in your IDE"
        + (" · `x` forget the project" if not tasks else ""),
        "",
        f"`{path}`",
        "",
        f"```toml\n{read(path)}\n```",
    ]
    return "\n".join(lines)


def edit_in_editor(path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or "vi"
    in_terminal([*editor.split(), str(path)])


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
            "Press `x` to delete it from the history, `H` to hide deleted tasks.",
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
        [f"Its plan, its events and its logs are kept in `{kept}`.", "", "#### The plan", "", plan_body(plan)]
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
            "Press `l` for its timeline, logs and reviews, `x` to delete it from the history, `h` to hide"
            " accepted tasks.",
            "",
            *archived,
        ]
    )


WAITING_ONLY = {
    State.CHECKPOINT_PLAN, State.CHECKPOINT_COMMAND, State.CHECKPOINT_FINAL, State.CHECKPOINT_BLOCKED,
    State.APPROVAL_RISKY,
}  # fmt: skip
# The keys that act on the selected task; the rest of the view's keys are always there.
TASK_ACTIONS = (
    "accept", "reply", "edit_plan", "edit_command", "open_ide", "approve_risky", "watch", "start_task",
    "stop_task", "stop_pod", "force_stop", "remove", "demo", "demo_stop", "models", "copy_prompt",
    "copy_prompt_cli", "verify_again", "show_log", "show_diff", "enter_box",
)  # fmt: skip


def planned_by_you(task: Task) -> bool:
    return (task.meta / manual.PROMPT).exists()


def keys_for(task: Task, st: TaskState, running: bool, busy: bool, demo_running: bool) -> dict[str, bool]:
    """Which of the task's keys do something now, one table for every state: the footer, the
    hints and the actions all ask here. busy: a start or a stop of this task is under way."""
    at_work = running and not st.paused
    if st.box and st.state is State.IMPLEMENT:
        # A box has no agent to reply to, watch or model; its keys are the pod's.
        open_ = not st.paused and not busy
        allowed = {
            "enter_box": open_,
            "accept": open_,
            "start_task": st.paused and not busy,
            "stop_task": open_,
            # Whatever is under way: a stop that hangs on the container is what it is for.
            "force_stop": True,
            "remove": True,
            "demo": open_,
            "demo_stop": demo_running,
            "show_log": True,  # the timeline, at least
        }
        return {action: allowed.get(action, False) for action in TASK_ACTIONS}
    allowed = {
        # A manual planner's checkpoint before your plan is in has nothing to accept, and a
        # reply would reach nobody: the planner is your own chat.
        "accept": (
            st.state in (State.CHECKPOINT_PLAN, State.CHECKPOINT_COMMAND, State.CHECKPOINT_FINAL)
            and not st.awaiting_plan
            and not ui.planner_asks(task, st)
        ),
        "edit_command": st.state is State.CHECKPOINT_COMMAND,
        "reply": st.state in WAITING_ONLY and not st.awaiting_plan and not st.box,
        "models": st.state is not State.DONE and not st.box,
        # Not while the agent may be writing its own draft.
        "edit_plan": st.state is State.CHECKPOINT_PLAN or (st.state is State.PLAN and not running),
        "open_ide": st.state is State.CHECKPOINT_FINAL,
        "show_diff": st.state is State.CHECKPOINT_FINAL,
        # Also once a plan is in: going back to the same chat is how you change it.
        "copy_prompt": st.state is State.CHECKPOINT_PLAN and planned_by_you(task) and not st.plan_in_cli,
        "copy_prompt_cli": st.state is State.CHECKPOINT_PLAN and planned_by_you(task),
        "approve_risky": st.state is State.APPROVAL_RISKY,
        # With a question too: the agent asks about the environment more often than the gate
        # recognises one, and once that is fixed the build is the answer.
        "verify_again": st.state is State.CHECKPOINT_BLOCKED,
        "show_log": True,  # the timeline, at least
        "watch": watchable(task, st, running),
        # w's other binding: without this, a task whose w was off opened a shell in its pod.
        "enter_box": False,
        # Not again while one of them is under way. A task that stopped on a failure still has
        # its supervisor, and what it needs is a start, not a stop followed by a start.
        "start_task": st.state is not State.DONE and not at_work and not busy,
        "stop_task": at_work and st.state not in WAITING_ONLY and not busy,
        "stop_pod": at_work and st.state in WAITING_ONLY and not busy,
        # Whenever the task is not done, busy or not: a start or a stop that hangs on docker, or a
        # supervisor that is gone while its pod is up, is what it is for.
        "force_stop": st.state is not State.DONE,
        "remove": True,
        # Once the work is back with you, not while the agent builds in the same tree. Running
        # it again while it runs is a restart, which is what you want after a change.
        "demo": actions.demo_allowed(st),
        "demo_stop": demo_running,
    }
    return {action: allowed.get(action, True) for action in TASK_ACTIONS}


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
        f"*{st.project} · created {ui.ago(st.created)} · updated {ui.ago(st.updated)}"
        f" · {ui.money(ui.cost(task).total)} so far"
        # During a turn: the cost above grows with it, and this says the agent is still at it.
        + (f" · last step {ui.ago(live['at'])}" if (live := task.live_turn()) else "")
        + "*",
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
    elif st.awaiting_review and st.state is State.REVIEW:
        body = [
            "**Review in your agent's CLI.** The verification passed, and the CLI that planned the task",
            "reviews this round; `vivibox wait` told it so. With that conversation closed, a new one",
            f"takes the round with `vivibox review {task.id} --prompt`, or `m` puts the supervisor on a",
            "model, which reviews from the next start.",
        ]
    elif st.awaiting_plan and st.state is State.CHECKPOINT_PLAN and st.plan_in_cli:
        body = [
            "**Plan this task in your agent's CLI.** C copies the prompt for it: the CLI reads your",
            "checkout, and when the plan is final it writes it to the answer file itself.",
            "",
            "#### The plan to fill in",
            "",
            plan_text(read(task.plan_path)),
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
            plan_text(read(task.plan_path)),
        ]
    elif ui.planner_asks(task, st):
        body = ["**The agent asks, instead of a plan:**", "", read(handoff / supervisor.QUESTION)]
    elif st.state in (State.PLAN, State.CHECKPOINT_PLAN):
        body = [*plans_verify(task, st), plan_text(read(task.plan_path))]
    elif st.state is State.CHECKPOINT_COMMAND:
        body = command_to_review(task)
    elif st.state is State.CHECKPOINT_FINAL:
        try:
            _, project = actions.load(st.id)
            copy = repo.review_worktree_path(project.repo, task.root)
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
    elif (task.meta / gate.ACCEPTED_PLAN).exists():
        doing = ""
        if st.state is State.IMPLEMENT and seen.group == "Working" and (lasting := ui.lasting(st.updated)):
            doing = f"**Implementing** {lasting}."
        body = [*([doing, ""] if doing else []), f"{last_gate(task)}{watch}"]
        if left := build_files_left(task):
            body += ["", *left]
        if gate_failed(task):  # what the agent is fixing now, in the build's own words
            feedback = read(handoff / "verify-feedback.md")
            body += ["", feedback, "", *build_said(handoff / "verify.log", feedback)]
        if st.state is State.IMPLEMENT:
            # A long turn is a still row; what happened lately says it is a turn, not a hang.
            body += ["", "**Lately**", "", *(f"- `{line}`" for line in timeline.latest(task)), "",
                     "`l` reads the whole timeline."]  # fmt: skip
    else:
        events = task.events()[-8:]
        body = ["**Recent events**", ""] + [
            f"- `{ui.clock(e['ts'])}` {e['type']} "
            + " ".join(
                f"{k}={v}" for k, v in e["data"].items() if k in ("current", "reason", "passed", "cost")
            )
            for e in events
        ]
    if st.state is State.CHECKPOINT_FINAL and (message := review.proposed_message(task).get("message")):
        body += ["", "#### Proposed commit", "", f"```text\n{message}\n```"]
    if st.state is State.IMPLEMENT and (todos := progress.read(task)):
        marks = {"completed": "☑", "in_progress": "→", "pending": "☐", "cancelled": "—"}
        body += [
            "",
            "#### Writer's steps",
            "",
            "Its working list; acceptance criteria are listed separately.",
            "",
            *(f"- {marks[t['status']]} {t['content']}" for t in todos),
        ]
    # The same sections in the same order in every state: the plan under review is the body.
    if not st.box and st.state not in (State.PLAN, State.CHECKPOINT_PLAN):
        body += [*reviewers_notes(task), *criteria_section(task)]
    body += roles_section(task, st)
    if not st.box and st.state not in (State.PLAN, State.CHECKPOINT_PLAN):
        body += plan_section(task)
    return "\n".join(head + body)


# --- dialogs ------------------------------------------------------------------------------------
