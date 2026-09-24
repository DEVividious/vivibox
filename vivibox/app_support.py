"""Two pieces under the view: a footer that redraws while the terminal has no focus, and the
executor for the thread workers, which the view can leave without waiting for.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor

from textual.widgets import Footer


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
