"""What an agent's CLI session spent on a task planned there (ADR-0036), read from the counters in
the session's transcript when the task brings in a plan or a review, and when it ends. Only the
model, the token counts and the time of each reply are read; nothing of the conversation.

The windows follow on from each other: planning is everything of the session before the plan
came in, since the last task of the same session was counted (a small ledger per session keeps
where); each review round runs from its request to its import; the rest is conversation. A
transcript is not the provider's contract: what cannot be read leaves the sum unknown, and the
task goes on."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import pricing
from .cli_session import CLAUDE_CODE
from .pricing import Tokens
from .task import Task


@dataclass(frozen=True)
class Reply:
    at: datetime
    model: str
    tokens: Tokens
    fast: bool = False
    us: bool = False


def _tokens(usage: dict) -> Tokens:
    written = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        five, hour = split.get("ephemeral_5m_input_tokens") or 0, split.get("ephemeral_1h_input_tokens") or 0
    else:
        five, hour = written, 0
    return Tokens(
        input=usage.get("input_tokens") or 0,
        output=usage.get("output_tokens") or 0,
        cache_read=usage.get("cache_read_input_tokens") or 0,
        cache_write_5m=five,
        cache_write_1h=hour,
    )


def _replies(text: str, seen: set[str]) -> list[Reply]:
    found = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            message = entry["message"]
            id_, usage = message["id"], message["usage"]
            at = datetime.fromisoformat(entry["timestamp"])
            tokens = _tokens(usage)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        # One reply is written over several lines, each with the reply's usage.
        if id_ in seen or not tokens.total:
            continue
        seen.add(id_)
        found.append(
            Reply(
                at if at.tzinfo else at.replace(tzinfo=UTC),
                str(message.get("model") or ""),
                tokens,
                fast=usage.get("speed") == "fast",
                us=usage.get("inference_geo") == "us",
            )
        )
    return found


def claude_replies(path: Path) -> list[Reply]:
    """The session's replies, its subagents' too, each once; OSError when the transcript is gone."""
    seen: set[str] = set()
    found = _replies(path.read_text(errors="replace"), seen)
    for sub in sorted((path.with_suffix("") / "subagents").glob("*.jsonl")):
        try:
            found += _replies(sub.read_text(errors="replace"), seen)
        except OSError:
            continue
    return found


def _ledger(session: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "vivibox" / "cli-sessions" / f"{session}.json"


def counted_until(session: str) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(_ledger(session).read_text())["counted_until"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _keep(session: str, until: datetime) -> None:
    path = _ledger(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"counted_until": until.isoformat()}))
    temporary.replace(path)


def _record(task: Task, stage: str, replies: list[Reply], since: datetime | None, until: datetime) -> None:
    by_model: dict[str, list[Reply]] = defaultdict(list)
    for reply in replies:
        if (since is None or reply.at > since) and reply.at <= until:
            by_model[reply.model].append(reply)
    for model, group in by_model.items():
        prices = [pricing.price(model, r.tokens, fast=r.fast, us=r.us) for r in group]
        tokens = sum((r.tokens for r in group), Tokens())
        task.event(
            "cli_usage",
            stage=stage,
            since=since.isoformat() if since else "",
            until=until.isoformat(),
            model=model,
            replies=len(group),
            tokens=asdict(tokens),
            cost=None if None in prices else round(sum(prices), 6),
        )


def _count(task: Task, windows: list[tuple[str, datetime]]) -> None:
    session = task.read_state().cli_session
    if session.get("tool") != CLAUDE_CODE:
        return
    since = counted_until(session["id"])
    try:
        replies = claude_replies(Path(session["path"]))
    except OSError as e:
        task.event(
            "cli_usage", stage=windows[-1][0], cost=None, problem=f"transcript not read: {e.strerror or e}"
        )
        return
    for stage, until in windows:
        if since is not None and until <= since:
            continue
        _record(task, stage, replies, since, until)
        since = until
    if since is not None:
        _keep(session["id"], since)


def count(task: Task, stage: str, until: datetime | None = None) -> None:
    """The session's use since it was last counted, up to until (now), as this stage of the task."""
    _count(task, [(stage, until or datetime.now(UTC))])


def count_review(task: Task, asked: datetime | None = None, until: datetime | None = None) -> None:
    """A review round brought in: the conversation up to its request, then the round itself."""
    if asked is None:
        requests = [e["ts"] for e in task.events() if e["type"] == "review_asked"]
        asked = datetime.fromisoformat(requests[-1]) if requests else None
    until = until or datetime.now(UTC)
    _count(task, ([("conversation", asked)] if asked else []) + [("review", until)])
