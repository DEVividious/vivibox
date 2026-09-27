"""Three pieces under the view: a footer that redraws while the terminal has no focus, the
executor for the thread workers, which the view can leave without waiting for, and the way a
program that takes over the terminal is run.
"""

from __future__ import annotations

import signal
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor

from textual.app import ComposeResult
from textual.widgets import Footer, Static
from textual.widgets._footer import FooterKey

# The command bar's groups, in its order: your decisions on the selected task, what else the
# selected row offers, and what works anywhere, at the right end. A key in none is the row's.
DECISIONS = {"accept", "reply", "approve_risky", "verify_again"}
ANYWHERE = {"new", "new_project", "help", "quit"}


class LiveFooter(Footer):
    """The command bar: the keys that do something now, in three groups a rule apart (decisions,
    the row's actions, anywhere), the decisions' words in the foreground.

    Textual's Footer stops redrawing while the terminal has no focus (bindings_changed in
    widgets/_footer.py returns early). The keys are what tells you a task now needs you, so they
    must appear while you are in another window, not once you click back into the terminal."""

    def bindings_changed(self, screen) -> None:
        self._bindings_ready = True
        if self.is_attached and screen is self.screen:
            self.call_after_refresh(self.recompose)

    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return
        seen: set[str] = set()
        groups: dict[str, list] = {"decision": [], "row": [], "anywhere": []}
        for _, binding, enabled, tooltip in self.screen.active_bindings.values():
            if not binding.show or binding.action in seen:
                continue
            seen.add(binding.action)
            kind = (
                "decision"
                if binding.action in DECISIONS
                else "anywhere"
                if binding.action in ANYWHERE
                else "row"
            )
            groups[kind].append((binding, enabled, tooltip))
        # What fits the terminal: every key always; on a narrow one the keys that work anywhere
        # lose their words first, then the longest words go to their first.
        said = {id(b): b.description for group in groups.values() for b, _, _ in group}

        def width() -> int:
            keys = [b for group in groups.values() for b, _, _ in group]
            return sum(len(self.app.get_key_display(b)) + len(said[id(b)]) + 2 for b in keys) + 2

        room = self.size.width or self.app.size.width
        for binding, _, _ in groups["anywhere"]:
            if width() > room:
                said[id(binding)] = ""
        for binding, _, _ in (*groups["row"], *groups["decision"]):
            if width() > room and " " in said[id(binding)]:
                said[id(binding)] = said[id(binding)].split(" ")[0]
        for kind, found in groups.items():
            if kind == "anywhere":
                yield Static("", classes="bar-spacer")
            elif kind == "row" and groups["decision"] and found:
                yield Static("│", classes="bar-rule")
            for binding, enabled, tooltip in found:
                yield FooterKey(
                    binding.key,
                    self.app.get_key_display(binding),
                    said[id(binding)],
                    binding.action,
                    disabled=not enabled,
                    tooltip=tooltip or binding.description,
                    classes=f"-{kind}",
                ).data_bind(compact=Footer.compact)


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


def in_terminal(command: list[str], env: dict[str, str] | None = None) -> None:
    """A pager, an editor or an IDE, with the terminal to itself. Ctrl-C reaches every process on
    the terminal, the view waiting behind it too: in less +F it stops following, and it ended the
    view once you pressed q. A handler that does nothing, not SIG_IGN, which the program would
    inherit: a handler goes back to the default in the program started."""
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        subprocess.run(command, env=env)
    finally:
        signal.signal(signal.SIGINT, previous)
