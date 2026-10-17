"""The code index's lifecycle as the planner's turns meet it: an index the project had no
language for at the pod's start comes once a commit gives it one; what a start of I came to
decides what a later turn tries again, each try bounded by the commits; and a turn never waits
for a provisioning run: it begins one and goes on, and a later turn finds it done. Serena is
played by the host's side alone here (no container): tests/test_index_serena_live.py runs it."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace

from test_index_container import a_pod, commit
from test_index_lifecycle import PROVISIONING
from test_pod import starts

from vivibox import pod_index, serena_index

JAVA = {f"src/A{i}.java": "class A {}\n" for i in range(12)}


def runs(pod, name: str) -> list[list[str]]:
    return [c for c in pod.runner.find("docker", "run", "-d", "--name") if c[4] == name]


def languages(cmd: list[str]) -> str:
    return cmd[cmd.index("--languages") + 1]


def serena_pod(tmp_path):
    return a_pod(tmp_path, server=serena_index.server(tmp_path / "repo"))


def finished(pod, status: str = "0") -> None:
    """The provisioning run ended by itself between two turns, with this exit status."""
    pod.runner.states[pod.index_provision] = "exited"
    pod.runner.exits[pod.index_provision] = status


class Timers:
    """threading.Timer as the pod arms it, held instead of run: fire() is its time running out."""

    def __init__(self) -> None:
        self.armed: list[tuple[float, object]] = []
        self.cancelled = 0

    def __call__(self, seconds, action):
        timers = self

        class Timer:
            daemon = False

            def start(self) -> None:
                timers.armed.append((seconds, action))

            def cancel(self) -> None:
                timers.cancelled += 1

        return Timer()

    def fire(self) -> None:
        for _, action in self.armed:
            action()


def timed(pod) -> Timers:
    pod.timer = Timers()
    return pod.timer


# --- N1: a language that comes later ---------------------------------------------------------------


def test_a_project_without_a_language_at_the_start_has_an_index_once_a_commit_gives_it_one(tmp_path):
    """The task's whole life in one pod: no index at its start, none while the project has no
    language Serena reads here, one at a later turn once the verified commit has Java; the
    provisioning run begun before a turn and found done before the next."""
    pod = serena_pod(tmp_path)
    timed(pod)
    pod.up()
    assert pod.index_failure.startswith("the code index reads none of this project's languages")
    assert not runs(pod, pod.index) and not runs(pod, pod.index_provision)
    still = commit(pod.repo, {"README.md": "still no code\n"})
    assert pod.index_refresh(still) is False
    assert not runs(pod, pod.index) and not runs(pod, pod.index_provision)
    verified = commit(pod.repo, JAVA)
    started = time.monotonic()
    assert pod.index_refresh(verified) is False, "not yet: its store is being provisioned"
    assert time.monotonic() - started < 5
    [run] = runs(pod, pod.index_provision)
    assert languages(run) == "java" and not runs(pod, pod.index)
    assert pod.index_failure.startswith("the code index is being provisioned")
    finished(pod)
    assert pod.index_refresh(verified) is True, "the next turn: provisioned, and I made on the commit"
    [steady] = runs(pod, pod.index)
    assert steady[steady.index("-c") + 2 : steady.index("--")] == [str(pod.repo), verified]
    assert pod.runner.states.get(pod.index_provision) is None
    assert "vivibox-shop-1-i" not in pod.runner.networks
    assert json.loads(pod.index_provisioned.read_text()) == list(pod.index_server.provision)
    assert not pod.index_provisioning.exists() and pod.index_failure == ""


def test_a_project_still_without_a_language_is_looked_at_again_only_on_another_commit(tmp_path):
    pod = serena_pod(tmp_path)
    pod.up()
    head = pod.index_attempt.commit
    pod.runner.calls.clear()
    assert pod.index_refresh(head) is False
    assert not [c for c in pod.runner.calls if c[:2] == ["docker", "run"]], "nothing started"
    assert pod.index_attempt.kind == pod_index.NOT_APPLICABLE


def test_a_start_that_failed_is_tried_once_more_on_its_commit_and_again_on_the_next(tmp_path):
    pod = a_pod(tmp_path)
    real = pod.runner
    refused = {"count": 0}

    def failing(cmd, timeout=None):
        cmd = list(cmd)
        if cmd[:5] == ["docker", "run", "-d", "--name", pod.index] and refused["count"] < 3:
            refused["count"] += 1
            real.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 125, stdout="", stderr="Docker could not")
        return real(cmd, timeout)

    failing.calls, failing.find, failing.states = real.calls, real.find, real.states
    failing.networks, failing.exits = real.networks, real.exits
    pod.runner = failing
    pod.up()
    head = pod.index_attempt.commit
    assert pod.index_attempt.kind == pod_index.TRANSIENT and "Docker could not" in pod.index_failure
    assert pod.index_refresh(head) is False, "tried once more on its commit"
    assert pod.index_refresh(head) is False and len(runs(pod, pod.index)) == 2, "and no more"
    later = commit(pod.repo, {"a.txt": "a\n"})
    assert pod.index_refresh(later) is False and len(runs(pod, pod.index)) == 3, "another commit"
    assert pod.index_refresh(later) is True and len(runs(pod, pod.index)) == 4


def test_an_index_that_ended_by_itself_is_started_again_once_on_a_commit_and_on_the_next(tmp_path):
    from test_index_serena import ended

    pod = a_pod(tmp_path)
    pod.up()
    verified = commit(pod.repo, {"a.txt": "verified\n"})
    assert pod.index_refresh(verified) is True
    ended(pod, oom=True, code=137)
    assert pod.index_refresh(verified) is True
    assert pod.index_failure == "the code index ran out of memory and was started again"
    ended(pod, oom=False, code=1)
    count = len(runs(pod, pod.index))
    assert pod.index_refresh(verified) is False and len(runs(pod, pod.index)) == count
    assert pod.index_failure == (
        "the code index stopped with exit status 1 again on this commit; it starts again on the next"
    )
    assert pod.runner.states.get(pod.index) is None
    assert pod.index_refresh(verified) is False and len(runs(pod, pod.index)) == count
    later = commit(pod.repo, {"a.txt": "later\n"})
    assert pod.index_refresh(later) is True and len(runs(pod, pod.index)) == count + 1


def test_what_no_later_turn_changes_waits_for_the_pods_next_start(tmp_path):
    pod = a_pod(tmp_path, server=PROVISIONING)
    pod.allocation = tmp_path / "network.json"
    old = [{"role": "w", "subnet": "198.51.100.0/28"}, {"role": "r", "subnet": "198.51.100.16/29"},
           {"role": "p", "subnet": "198.51.100.24/29"}]  # fmt: skip
    pod.allocation.write_text(json.dumps({"networks": old}))
    pod.up()
    assert pod.index_attempt.kind == pod_index.HARD and "delete the task" in pod.index_failure
    later = commit(pod.repo, {"a.txt": "a\n"})
    pod.runner.calls.clear()
    assert pod.index_refresh(later) is False
    assert not [c for c in pod.runner.calls if c[:2] == ["docker", "run"]]


def test_a_pod_that_is_down_has_no_index_started_by_a_refresh(tmp_path):
    pod = a_pod(tmp_path)
    pod.up()
    pod.down()
    count = len(runs(pod, pod.index))
    assert pod.index_refresh(commit(pod.repo, {"a.txt": "a\n"})) is False
    assert len(runs(pod, pod.index)) == count


def test_a_start_of_the_pod_tries_everything_again(tmp_path):
    pod = serena_pod(tmp_path)
    pod.up()
    commit(pod.repo, JAVA)
    pod.down()
    pod.up()
    assert languages(runs(pod, pod.index_provision)[-1]) == "java" and runs(pod, pod.index)


# --- N3: a turn never waits for a provisioning run ------------------------------------------------


def test_a_turn_begins_a_provisioning_run_and_never_waits_for_it(tmp_path):
    pod = a_pod(tmp_path, server=PROVISIONING)
    timers = timed(pod)
    pod.index_server = replace(PROVISIONING, provision=())  # provisioned at the start
    pod.up()
    pod.index_server = PROVISIONING  # its program now needs a run
    later = commit(pod.repo, {"a.txt": "a\n"})
    pod.runner.calls.clear()
    assert pod.index_refresh(later) is False
    assert runs(pod, pod.index_provision) and not pod.runner.find("docker", "wait"), "begun, not waited for"
    assert pod.runner.states.get(pod.index) is None, "no I while its store is being provisioned"
    assert [seconds for seconds, _ in timers.armed] == [pod.PROVISION_SECONDS]
    assert pod.index_refresh(commit(pod.repo, {"a.txt": "b\n"})) is False, "still under way"
    assert len(runs(pod, pod.index_provision)) == 1 and not pod.runner.find("docker", "wait")
    assert pod.runner.states.get(pod.index) is None


def test_a_run_past_its_time_is_killed_by_its_timer_and_found_failed_by_the_next_turn(tmp_path):
    pod = a_pod(tmp_path, server=PROVISIONING)
    timers = timed(pod)
    pod.index_server = replace(PROVISIONING, provision=())
    pod.up()
    pod.index_server = PROVISIONING
    later = commit(pod.repo, {"a.txt": "a\n"})
    pod.index_refresh(later)
    timers.fire()
    assert ["docker", "kill", pod.index_provision] in pod.runner.calls
    finished(pod, "137")
    assert pod.index_refresh(later) is False
    assert pod.index_failure == "the code index's provisioning run ended with exit status 137"
    assert "vivibox-shop-1-i" not in pod.runner.networks and not pod.index_provisioning.exists()
    pod.runner.calls.clear()
    assert pod.index_refresh(commit(pod.repo, {"a.txt": "c\n"})) is False
    assert not runs(pod, pod.index_provision), "the same languages are not provisioned again"
    assert "provisioned again when the pod starts" in pod.index_failure


def test_a_run_past_its_time_whose_timer_is_gone_is_taken_down_by_the_next_turn(tmp_path):
    """The timer is the process's that began the run; a run that outlived it, past its time, is
    removed at the first turn that finds it."""
    pod = a_pod(tmp_path, server=PROVISIONING)
    timed(pod)
    pod.index_server = replace(PROVISIONING, provision=())
    pod.up()
    pod.index_server = PROVISIONING
    later = commit(pod.repo, {"a.txt": "a\n"})
    pod.index_refresh(later)
    record = json.loads(pod.index_provisioning.read_text())
    record["started"] -= pod.PROVISION_SECONDS + 1
    pod.index_provisioning.write_text(json.dumps(record))
    assert pod.index_refresh(later) is False
    assert pod.runner.states.get(pod.index_provision) is None
    assert pod.index_failure == "the code index's provisioning run ran out of time"


def test_a_pod_life_has_a_bounded_number_of_provisioning_runs(tmp_path):
    """A writer that adds and removes a language at every commit provisions a few times, then
    not again until the pod starts again."""
    pod = serena_pod(tmp_path)
    timed(pod)
    commit(pod.repo, JAVA)
    pod.up()
    python = {f"py/p{i}.py": "x = 1\n" for i in range(12)}
    said = []
    for n in range(2 * pod_index.PROVISION_RUNS):
        flip = commit(pod.repo, python if n % 2 == 0 else dict.fromkeys(python))
        pod.index_refresh(flip)
        if pod.runner.states.get(pod.index_provision) == "running":
            finished(pod)
            pod.index_refresh(flip)
        said.append(pod.index_failure)
    assert len(runs(pod, pod.index_provision)) == pod_index.PROVISION_RUNS
    assert said[2] == "the code index was provisioned 3 times in this pod's life; " + pod_index.AGAIN_AT_START
    assert said[3] == "", "the languages last provisioned for need no run: I starts"


def test_while_a_run_is_under_way_the_pods_start_takes_it_down_and_waits_for_its_own(tmp_path):
    pod = a_pod(tmp_path, server=PROVISIONING)
    timed(pod)
    pod.index_server = replace(PROVISIONING, provision=())
    pod.up()
    pod.index_server = PROVISIONING
    pod.index_refresh(commit(pod.repo, {"a.txt": "a\n"}))
    pod.down()
    assert not pod.index_provisioning.exists() and pod.timer.cancelled == 1, "its timer with it"
    pod.up()
    assert pod.runner.find("docker", "wait"), "a pod's start is no planner's turn: it waits"
    assert pod.runner.states.get(pod.index) == "running" and pod.index_failure == ""
    assert pod.index in starts(pod.runner.calls)


def test_a_run_that_is_neither_running_nor_ended_is_not_waited_for_by_a_turn(tmp_path):
    """A run left created (its start interrupted) or paused never ends by itself: a turn that
    finds it takes it down at once, without waiting for an exit status Docker has none of."""
    pod = a_pod(tmp_path, server=PROVISIONING)
    timed(pod)
    pod.index_server = replace(PROVISIONING, provision=())
    pod.up()
    pod.index_server = PROVISIONING
    later = commit(pod.repo, {"a.txt": "a\n"})
    pod.index_refresh(later)
    pod.runner.states[pod.index_provision] = "created"
    pod.runner.calls.clear()
    assert pod.index_refresh(later) is False
    assert not pod.runner.find("docker", "wait") and pod.runner.states.get(pod.index_provision) is None
    assert pod.index_failure == "the code index's provisioning run was stopped"
