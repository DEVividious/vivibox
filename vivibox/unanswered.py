"""A verification that git on the host did not answer in, in the task's clone, or that vivibox
runs no git for while the task's own addition is there (repo.Blocked): a problem of the
environment, for you, with no round of the writer's spent. Never a check that found nothing: none
of what the verification had found by then is kept, and nothing of it reads as passed."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import repo

if TYPE_CHECKING:
    from .task import Task


def ends_verification(make: Callable[..., Any]) -> Callable[[Callable], Callable]:
    """A verification (task first) that ends, when git in the clone times out or the clone is
    blocked, in make(log, environment=why): the result the gate gives for a failure outside the
    code. The task then waits for you at a checkpoint your reply moves on from."""

    def wrap(verify: Callable) -> Callable:
        @functools.wraps(verify)
        def bounded(task: Task, *args, **kwargs):
            try:
                return verify(task, *args, **kwargs)
            except repo.GitTimeout as e:
                return could_not_verify(task, make, str(e))
            except repo.Blocked as e:  # with what blocks it, for the writer's feedback
                return could_not_verify(task, make, e.why, e.logged)

        return bounded

    return wrap


def could_not_verify(task: Task, make: Callable[..., Any], why: str, logged: str = "") -> Any:
    """The result, its log and the gate's event, as the gate records a verification. logged:
    what the log says, when why holds a step that is yours and the writer reads the log too."""
    st = task.read_state()
    log = task.meta / "log" / f"verify-{st.iteration}-{time.strftime('%H%M%S')}.log"
    with Path(log).open("a") as out:
        out.write(f"# the verification could not run: {logged or why}\n")
    result = make(log, environment=why)
    task.event("gate", iteration=st.iteration, **result.summary())
    return result
