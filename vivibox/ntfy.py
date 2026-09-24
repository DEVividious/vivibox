"""Notifications on your phone, or wherever else you read ntfy (https://ntfy.sh, or a server of your
own): what the supervisor says, posted to a topic. Off unless config.toml names the topic.

A topic on ntfy.sh is read by anyone who knows its name, so a message says what happened, never
what the agent or the build said: "question from the agent", not the question.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from . import keys, ui
from .config import NTFY_LEVELS
from .states import State
from .task import TaskState

DECISIONS, ALL = NTFY_LEVELS
TOKEN = keys.NTFY
TIMEOUT = 5

Post = Callable[[str, dict[str, str], bytes], None]


def post(url: str, headers: dict[str, str], body: bytes) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT):
        pass


def terse(message: str) -> str:
    """The message up to its first colon: the kind of event, without what came after it."""
    return message.split(": ", 1)[0]


def said(text: str) -> None:
    """A failure to send, in the supervisor window, like every other message there."""
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def in_background(run: Callable[[], None]) -> None:
    threading.Thread(target=run, daemon=True).start()


@dataclass(frozen=True)
class Channel:
    """One topic, and what goes to it: decisions, or every stage too. Sending never holds the
    supervisor up and never stops a task: a failure is said in its window and that is all."""

    url: str
    level: str = DECISIONS
    token: str = ""
    post: Post = post
    report: Callable[[str], None] = said
    spawn: Callable[[Callable[[], None]], None] = in_background

    def decision(self, task_id: str, message: str, waiting: bool, trouble: bool = False) -> None:
        """A message meant for you. High priority when the task has stopped or now waits for you."""
        if trouble:
            self.send(task_id, terse(message), "high", "warning")
        elif waiting:
            self.send(task_id, terse(message), "high", "hourglass")
        else:
            self.send(task_id, terse(message), "default", "")

    def send(self, task_id: str, body: str, priority: str, tags: str) -> None:
        headers = {"Title": f"vivibox {task_id}", "Priority": priority}
        if tags:
            headers["Tags"] = tags
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        def run() -> None:
            try:
                self.post(self.url, headers, body.encode())
            except (OSError, urllib.error.URLError, ValueError) as e:
                self.report(f"ntfy: not sent ({e})")

        self.spawn(run)


@dataclass
class Stages:
    """A word when the supervisor takes the task up, at every level: the first message after
    setting the topic up, so it says the channel works. Then, at level all only, what the task is
    doing whenever that changes; what waits for you comes as a decision, so only the working
    states are said here."""

    channel: Channel
    # A task that had turns before this supervisor is started again, not started.
    again: bool = False
    last: str = field(default="")

    def seen(self, st: TaskState) -> None:
        if str(st.state) == self.last:
            return
        first, self.last = not self.last, str(st.state)
        doing = ui.WORKING.get(st.state, "")
        if st.state is State.IMPLEMENT and st.iteration > 1:
            doing += f", attempt {st.iteration}"
        if first:
            began = "started again" if self.again else "started"
            self.channel.send(st.id, f"{began}: {doing}" if doing else began, "default", "")
        elif self.channel.level == ALL and doing:
            self.channel.send(st.id, doing, "default", "")
