"""A task's timeline, for a person: its events one line each, on your clock. States and why they
changed, turns with what they cost and how long they took, verifications with what failed, the
agent's questions, your decisions. events.jsonl is the record; this is how it reads."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import ui
from .states import State
from .task import Task

# What a state event says when the change was yours.
YOURS = {"your reply", "accepted", "verify again"}
FILE = "timeline.txt"
# A state in the words the list uses for it (docs/ux-guidelines.md), never its raw name.
WORDS = {
    **{str(state): words for state, (words, _) in ui.WAITING.items()},
    **{str(state): words for state, words in ui.WORKING.items()},
    str(State.DONE): "done",
}


def state_words(name: str) -> str:
    return WORDS.get(name, name.replace(":", " "))


def _took(start: str | None, end: str) -> str:
    if not start:
        return ""
    seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min"


def _gate(data: dict) -> str:
    n = data.get("iteration")
    text = f"verification{f' {n}' if n else ''}: " + ("passed" if data.get("passed") else "failed")
    if failed := data.get("failed_commands"):
        text += ", " + ", ".join(f"`{c}`" for c in failed)
    if why := data.get("environment"):
        text += f"; outside the code: {why}"
    if skipped := data.get("build_skipped"):
        text += f"; build not run: {skipped}"
    if narrowed := data.get("narrowed"):
        text += f"; proposed command narrowed to {narrowed}"
    for key, said in (
        ("missing_criteria", "criteria not met"),
        ("commit_problems", "commit problems"),
        ("uncommitted", "uncommitted files"),
        ("switched_off_tests", "tests switched off"),
        ("no_red_evidence", "tests without red evidence"),
    ):
        if data.get(key):
            text += f"; {data[key]} {said}"
    if risky := data.get("risky_changes"):
        text += f"; risky: {', '.join(risky)}"
    if log := data.get("log"):
        text += f" ({log})"
    return text


def _generic(kind: str, data: dict) -> str:
    said = ", ".join(f"{k} {v}" for k, v in data.items() if v not in ("", None, [], {}))
    return f"{kind}: {said}" if said else kind


def entries(task: Task) -> list[tuple[str, str]]:
    """(time of day, what happened), oldest first."""
    found: list[tuple[str, str]] = []
    began: dict[str, str] = {}  # role -> when its turn under way started
    for event in task.events():
        kind, data, ts = event["type"], event.get("data", {}), event["ts"]
        if kind == "turn_started":
            began[data.get("role", "")] = ts
            continue
        if kind == "created":
            text = f"created: {data.get('goal', '')}"
        elif kind == "goal":
            text = f"goal: {data.get('goal', '')}"
        elif kind == "started":
            text = f"started on {data.get('model', '?')}"
        elif kind == "state":
            reason = data.get("reason", "")
            text = f"→ {state_words(data.get('current', '?'))}"
            if reason in YOURS:
                text = f"you: {reason} {text}"
            elif reason:
                text += f" ({reason})"
        elif kind == "turn":
            role = data.get("role", data.get("kind", "agent"))
            took = _took(began.pop(data.get("role", ""), None), ts)
            text = f"{role} turn: ${data.get('cost') or 0:.2f}, {data.get('tokens') or 0} tokens"
            if took:
                text += f", {took}"
            if data.get("ok") is False:
                text += f"; failed: {data.get('error', '')}"
        elif kind == "gate":
            text = _gate(data)
        elif kind == "turn_retry":
            text = f"turn failed, trying again in {data.get('wait', '?')} s: {data.get('error', '')}"
        elif kind == "paused":
            text = "stopped" + (f": {data['problem']}" if data.get("problem") else "")
        elif kind == "resumed":
            text = "started again"
        elif kind == "prepare_started":
            text = f"preparing: {', '.join(data.get('commands') or [])}"
        elif kind == "prepared":
            text = (
                "prepared"
                if data.get("ok")
                else f"preparation failed, exit {data.get('code')}; its output is under l"
            )
        elif kind == "cost_warning":
            text = f"cost ${data.get('spent', 0):.2f}, past the warning of ${data.get('warning', 0):.2f}"
        elif kind == "review" and "round" in data:
            n, b, o = data.get("round"), data.get("blocking", 0), data.get("not_blocking", 0)
            text = f"review {n}: {b} blocking, {o} not blocking" + (
                f"; unreadable: {p}" if (p := data.get("problem")) else ""
            )
        elif kind == "review":
            text = "work fetched for your review"
        elif kind == "risky_approved":
            text = f"you: risky files approved: {', '.join(data.get('paths') or [])}"
        elif kind == "criteria_added":
            text = f"you: {len(data.get('criteria') or [])} criteria added"
        elif kind == "plan_imported":
            text = "you: plan brought in"
        elif kind == "auto_plan":
            text = "auto: the plan is accepted without you" if data.get("enabled") else "auto off"
        else:
            text = _generic(kind, data)
        found.append((ui.clock(ts), ui.shorten(text.replace("\n", " "), 160)))
    return found


def lines(task: Task) -> list[str]:
    return [f"{when}  {what}" for when, what in entries(task)]


def latest(task: Task, count: int = 3) -> list[str]:
    return lines(task)[-count:]


def render(task: Task) -> str:
    st = task.read_state()
    return f"# {st.id}: {st.goal}\n\n" + "\n".join(lines(task)) + "\n"


def write(task: Task) -> Path:
    """The timeline as a file, for your pager; written anew each time it is asked for."""
    path = task.meta / "log" / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(task))
    return path
