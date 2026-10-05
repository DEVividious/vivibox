import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# The suite's own git takes nothing from the git variables of the shell it is started from:
# inside a git hook GIT_DIR and GIT_INDEX_FILE name the repository being committed to, and the
# repositories the fixtures make would be that one. Taken out of the process before anything
# runs; a test that needs one sets it itself (monkeypatch).
for _name in [name for name in os.environ if name.startswith("GIT_")]:
    del os.environ[_name]

# Git's global configuration for the tests: an identity of their own, so a repository they make
# without one commits the same on this machine and on a runner with no ~/.gitconfig, and nothing
# the tests do reads or writes yours. It is the ~/.gitconfig of a home of the suite's own: the
# git a test runs itself is pointed at it (GIT_CONFIG_GLOBAL), and the git vivibox runs, which
# takes no GIT_* variable from the environment (repo.git_environment), reads it where git
# keeps your settings (own_home).
IDENTITY = "[user]\n\tname = Test User\n\temail = test@example.com\n"
# Submodules from local paths are blocked by default since git 2.38.
LOCAL_SUBMODULES = '[protocol "file"]\n\tallow = always\n'
HOME = Path(tempfile.mkdtemp(prefix="vivibox-tests-home-"))
GIT_CONFIG = HOME / ".gitconfig"
GIT_CONFIG.write_text(IDENTITY)
os.environ["GIT_CONFIG_GLOBAL"] = str(GIT_CONFIG)


def global_git_config(monkeypatch: pytest.MonkeyPatch, home: Path, text: str) -> Path:
    """Your global git configuration for one test: text as the ~/.gitconfig of home, read by the
    git the test runs itself and by the git vivibox runs alike. "" is a machine with none."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".gitconfig").write_text(text)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    return home / ".gitconfig"


def git_with(monkeypatch: pytest.MonkeyPatch, folder: Path, **variables: str) -> None:
    """From here on in the test, the git first on PATH is the one found there now with these
    variables set, for the test's own git and the git vivibox runs alike. For what vivibox's
    git is never handed from the environment (repo.git_environment takes every GIT_* out):
    GIT_CONFIG_NOSYSTEM, in a test that must hold on a machine whose /etc/gitconfig says
    otherwise."""
    real = shutil.which("git")
    folder.mkdir(parents=True, exist_ok=True)
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in variables.items())
    (folder / "git").write_text(f'#!/bin/sh\n{exports}exec {shlex.quote(real)} "$@"\n')
    (folder / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def make_repo(path: Path) -> Path:
    """A repository with one commit and a local identity, like a real project."""
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)  # noqa: E731
    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main")
    run("config", "user.name", "Test User")
    run("config", "user.email", "test@example.com")
    (path / "README.md").write_text("demo\n")
    run("add", ".")
    run("commit", "-q", "-m", "Initial commit")
    return path


def shown_risky(task_id: str) -> str:
    """What `vivibox risky` shows of the task now, as its last line names it: the identity an
    approval of exactly that carries ("" while something cannot be shown or approved)."""
    from vivibox import actions

    return actions.risky_view(*actions.load(task_id)).identity


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config dir with one project 'demo' backed by an empty git repo."""
    cfg = tmp_path / "config"
    (cfg / "projects").mkdir(parents=True)
    repo = tmp_path / "repo"
    make_repo(repo)
    (cfg / "config.toml").write_text(
        f'tasks_dir = "{tmp_path / "tasks"}"\n'
        '[roles.planner]\nharness = "opencode"\nmodel = "m"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )
    (cfg / "projects" / "demo.toml").write_text(f'repo = "{repo}"\nverify = ["true"]\n')
    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # keys and history, never yours
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # opencode's configuration, never yours
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # a task's secrets, never yours
    # What vivibox remembers of the models and the providers, never another test's: the suite's
    # home is one per worker process, and a list one test left there was the next one's for a day.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    # A test run from an agent's CLI would record that CLI's session with a task, and look for it.
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# The suite's own temporary folder: what vivibox copies there for a pager (display.terminal_copy)
# never lands in yours.
TEMPORARY = Path(tempfile.mkdtemp(prefix="vivibox-tests-tmp-"))


@pytest.fixture(autouse=True)
def own_temporary_folder(monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(TEMPORARY))


def marked(request, name: str) -> bool:
    """Whether the test carries the mark: its own, its class's or its module's. Never whether the
    word is among its keywords: a parameter's id, a folder on the way to the test's file and an
    attribute of its function are keywords too, and a test with a parameter called docker would
    be handed this machine's Docker."""
    return request.node.get_closest_marker(name) is not None


def real_machine(request) -> bool:
    """Whether the test runs pods on this machine's Docker: one marked docker, and one marked
    model, which runs agents in real pods (tests/behavioural) and is selected by -m model alone,
    never by -m docker, so that no run of the live suites spends money."""
    return marked(request, "docker") or marked(request, "model")


# The folders a shell may name apart from HOME: git reads its global settings in
# $XDG_CONFIG_HOME/git/config beside ~/.gitconfig, and vivibox keeps its configuration, its keys
# and history, and what it remembers of the models in them.
OWN_FOLDERS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


def suites_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME is the suite's, and no folder is named apart from it (OWN_FOLDERS): git and vivibox
    look each up under the home, as on a machine whose shell names none, which is most."""
    monkeypatch.setenv("HOME", str(HOME))
    for name in OWN_FOLDERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def own_home(request, monkeypatch):
    """HOME is the suite's, where its git configuration is (GIT_CONFIG), and the folders your
    shell may name apart from it are not followed (suites_own): the git vivibox runs reads its
    settings in the suite's home and never yours, hooks and all, and vivibox writes its cache
    there. Not for a test on this machine's Docker, whose client keeps its own settings in
    your home."""
    if not real_machine(request):
        suites_own(monkeypatch)


@pytest.fixture(autouse=True)
def twenty_cpus(request, monkeypatch):
    """The CPUs a pod's containers may use default to the machine's, up to six (podspec): the
    suite's figures (6) hold on any machine, and a test of the count sets its own. Not for a
    test marked docker: its pod is made from the machine's real limits, and on a machine with
    fewer CPUs a pinned count would ask Docker for more than it has."""
    if real_machine(request):
        return
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(20)), raising=False)


@pytest.fixture(autouse=True)
def no_real_start(request, monkeypatch):
    """A decision now starts a task nobody is working on, and starting means Docker. Tests get a
    stand-in that records the call in vivibox.actions.started; one marked real_start gets the real
    thing."""
    from vivibox import actions, probe

    if marked(request, "real_start"):
        return
    started: list[str] = []
    monkeypatch.setattr(actions, "started", started, raising=False)
    monkeypatch.setattr(
        actions, "start", lambda task_id, resume=False, on_step=None: started.append(task_id) or "m"
    )
    # The view asks the daemon for the header's warning; the tests have no daemon to ask.
    monkeypatch.setattr(probe, "docker_running", lambda: True)
    # Nor a DNS forwarder to ask about, but for a test that has one of its own to ask (real_hostd,
    # docker): to that one the header says what it would say to a person.
    if not marked(request, "real_hostd") and not real_machine(request):
        monkeypatch.setattr(probe, "forwarder_problem", lambda config: "")
    # vivibox new asks for the agent image before it creates a task; nor is there an image.
    from vivibox import image

    monkeypatch.setattr(image, "exists", lambda ref, runner=None: True)


# What a test may ask a Docker that is not there: whether something exists or runs. Anything else
# would change the machine's containers, images or networks, a real task's among them.
# A throwaway container that lists the models (roles.py) leaves nothing behind either.
READ_ONLY_DOCKER = (("inspect",), ("image", "inspect"), ("info",), ("version",), ("ps",), ("run", "--rm"))
FAKE_DOCKER = """#!/bin/sh
[ -z "$VIVIBOX_TEST_DOCKER_LOG" ] || printf '%s\\n' "$*" >> "$VIVIBOX_TEST_DOCKER_LOG"
echo "the tests reach no Docker (tests/conftest.py: no_real_docker)" >&2
exit 1
"""


@pytest.fixture(autouse=True)
def no_real_hostd(request, monkeypatch, tmp_path_factory):
    """No test starts the host's DNS forwarder but one marked real_hostd or docker: a pod's start
    asks for it (hostd.ensure), and it would outlive the test. Nor takes its locks in your
    runtime folder, nor reads your /sys/class/net: every test has its own."""
    if real_machine(request):
        return
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))
    # The kernel's interfaces as a folder of the test's own: a fake Docker makes its bridges there.
    from vivibox import pod_network

    monkeypatch.setattr(pod_network, "SYS_NET", tmp_path_factory.mktemp("sys-class-net"))
    if marked(request, "real_hostd"):
        return
    from vivibox.egress import hostd

    monkeypatch.setattr(hostd, "ensure", lambda address, port=0, upstream="", timeout=0: None)


def networks_up(pod) -> None:
    """The pod's networks as up() leaves them, for a test that starts the gate or the review
    without up(): allocated, and each on its bridge as this test's /sys/class/net shows it (a
    runner that answers everything with 0 makes no bridge; the review checks for one)."""
    from vivibox import pod_network

    for net in pod.ensure_networks():
        (pod_network.SYS_NET / net.bridge / "bridge").mkdir(parents=True, exist_ok=True)


# Seconds the suite's own git may take in a clone a test made: one whose refs are a named pipe
# (tests/test_git_timeout.py) would keep it, and the git it started, waiting long after the
# suite is over.
OWN_GIT_SECONDS = 20


def as_verified(task) -> str:
    """The task as a green verification leaves it: the commit its clone stands on, recorded as
    the verified one. A review is made of that commit, and of nothing when there is none. The
    question is asked within OWN_GIT_SECONDS: git that does not answer is ended, not left."""
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(task.repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=OWN_GIT_SECONDS,
    )  # fmt: skip
    task.set_verified_commit(done.stdout.strip())
    return done.stdout.strip()


def review_made(pod) -> None:
    """The review directory as reviewing.up leaves it before the container starts, for a test
    that starts the review container itself: emptied, with a working directory and a view of
    the host's making in it (stand-ins: a .git folder each, no commits)."""
    from pathlib import Path

    pod.review_fresh()
    for folder in (pod.review_work, Path(pod.review_src)):
        (folder / ".git").mkdir()


# The programs of this machine a test finds a stand-in for, each with what the stand-in does.
# docker answers as a daemon with nothing in it and writes down what it was asked. sudo refuses:
# the root helper is the person's to run, through host/setup.sh and a pod's start. tmux answers
# as a machine with no server of vivibox's: the agent's windows are sessions of one tmux server
# (tmux -L vivibox), named by the task's id, and every stop, accept and delete closes the task's;
# the real one would close the window of a task of yours that has a test's id (demo-1).
STOOD_IN_FOR = {
    "docker": FAKE_DOCKER,
    "sudo": '#!/bin/sh\necho "the tests reach no sudo (tests/conftest.py)" >&2\nexit 1\n',
    "tmux": '#!/bin/sh\necho "no server running: the tests reach no tmux (tests/conftest.py)" >&2\nexit 1\n',
}
# What every subprocess call starts its process with, as the standard library has it.
STARTS_A_PROCESS = subprocess.Popen._execute_child


def stand_ins(folder: Path) -> Path:
    """A stand-in for each of STOOD_IN_FOR in folder, to put first on PATH."""
    for name, script in STOOD_IN_FOR.items():
        (folder / name).write_text(script)
        (folder / name).chmod(0o755)
    return folder


# Stand-ins for the whole run, behind each test's own: for what is started when no test's PATH
# holds (a thread of the view that outlives its test asks Docker where a pod answers). They write
# nothing down and fail no test: there is none to fail by then. In the suite's home, which is
# there as long as anything the run started may be.
LATE = HOME / "stand-ins"
LATE.mkdir()
stand_ins(LATE)


def stand_ins_for_the_run(items) -> bool:
    """Whether the run gets stand-ins on PATH from its collection on: one with no test marked
    docker or model, whose fixtures of a module or a session are made on this machine's Docker
    before any fixture of a test could take a stand-in away."""
    return not any(item.get_closest_marker("docker") or item.get_closest_marker("model") for item in items)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items) -> None:
    if stand_ins_for_the_run(items) and str(LATE) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{LATE}{os.pathsep}{os.environ.get('PATH', '')}"


def past_the_stand_ins(folder: Path, args, executable, env) -> str:
    """What a process about to be started would run in place of the stand-in for docker, sudo
    or tmux in folder, or "": the stand-ins are found by PATH, so a docker named by its whole
    path (/usr/bin/docker), or looked up on a PATH of the caller's own that does not lead to
    them (an env= without PATH is looked up in the system's folders), is this machine's."""
    program = os.fsdecode(executable or (args if isinstance(args, str | bytes | os.PathLike) else args[0]))
    name = os.path.basename(program)
    if name not in STOOD_IN_FOR:
        return ""
    found = shutil.which(program, path=os.pathsep.join(os.get_exec_path(env)))
    return "" if found in (None, str(folder / name)) else found


@pytest.fixture(autouse=True)
def no_real_docker(request, monkeypatch, tmp_path_factory):
    """No test but one marked docker reaches the machine's Docker, nor sudo, nor tmux
    (STOOD_IN_FOR): a docker first on PATH answers every call as a daemon with nothing in it
    would, and a test that asked it to change something fails, so a stand-in that stops
    intercepting (a pod built another way) is caught. A task's pod (actions.Pod) answers the
    same way without asking, as a test's own stand-in may still do. A docker, a sudo or a tmux
    that would be found past the stand-ins is not started at all (past_the_stand_ins), and the
    test fails."""
    if real_machine(request):
        yield
        return
    from functools import partial

    from vivibox import actions
    from vivibox.pod import Pod

    def nothing_there(cmd, timeout=None):
        if list(cmd)[:1] == ["sudo"] and ('"absent"' in cmd[-1] or cmd[3] == "detach-disk"):
            # No rules: none to remove; no disk: detached already, as the helper says of one.
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        return subprocess.CompletedProcess(list(cmd), 1, "", "Error: No such object")

    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=nothing_there))
    folder = stand_ins(tmp_path_factory.mktemp("docker"))
    log = folder / "calls.log"
    monkeypatch.setenv("VIVIBOX_TEST_DOCKER_LOG", str(log))
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    strayed: list[str] = []
    start = subprocess.Popen._execute_child  # STARTS_A_PROCESS, or what a test's own tool put over it

    def start_unless_it_strays(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest):
        if real := past_the_stand_ins(folder, args, executable, env):
            strayed.append(real)
            raise AssertionError(f"{real} is not the tests' stand-in (tests/conftest.py: no_real_docker)")
        return start(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest)

    monkeypatch.setattr(subprocess.Popen, "_execute_child", start_unless_it_strays)
    yield strayed  # to the test of this guard itself, which empties it
    calls = log.read_text().splitlines() if log.exists() else []
    changing = [c for c in calls if not any(tuple(c.split()[: len(r)]) == r for r in READ_ONLY_DOCKER)]
    if changing:
        pytest.fail(f"asked Docker to change something: {changing[:5]}; fake it, or mark the test docker")
    if strayed:
        pytest.fail(f"would have run {strayed[:5]}, not the stand-in on PATH; mark the test docker")


@pytest.fixture(autouse=True)
def no_real_notifications(request, monkeypatch):
    """No test reaches your desktop's notifications nor an ntfy topic: the supervisor's notify
    says what it says in its window and records what it would have shown on the desktop, and
    ntfy.post records what it would have posted. A test marked real_notify gets the real thing,
    and stands in for notify-send itself."""
    from types import SimpleNamespace

    from vivibox import ntfy, supervisor

    quiet = SimpleNamespace(desktop=[], ntfy=[])
    if marked(request, "real_notify"):
        return quiet
    real = supervisor.notify

    # Ports' default and supervise.notifier look notify up in the module at each call, so this is
    # what they get.
    def in_the_window_only(task_id, message, desktop=True, actions=()):
        if desktop:
            quiet.desktop.append((task_id, message))
        real(task_id, message, desktop=False)

    monkeypatch.setattr(supervisor, "notify", in_the_window_only)
    # A channel takes the module's post when it is made (ntfy.Channel), so one made by
    # supervise.channel_for from a configuration with a topic gets this.
    monkeypatch.setattr(ntfy, "post", lambda url, headers, body: quiet.ntfy.append((url, headers, body)))
    return quiet


# --- the view, as the tests drive it (Textual's run_test and its Pilot) -------------------------------

# Seconds a view may take to come to rest, or its threads to end, before the test fails saying
# so, never hanging the run; what Pilot's own wait for the screen's messages is bounded by.
REST_WITHIN = 30.0


def threads_of_no_worker(app) -> int:
    """How many threads of the view's executor (app_support.LeavingExecutor) are at work that
    are not a running worker's: Textual's own (it parses a Markdown text in one, so the panel's
    words arrive after the key that asked for them) and those of a worker the view cancelled,
    which run on and may still report back. All the view's workers are thread workers."""
    executor = getattr(app, "executor", None)
    if executor is None:
        return 0
    return executor.unfinished() - sum(1 for worker in app.workers if worker.is_running)


async def at_rest(min_sleep: float = 0.0, max_sleep: float = 1.0) -> None:
    """Until the view has nothing left to do with what it was given: no task of its loop can run,
    no timer is due, no thread that is not a running worker's is at work (threads_of_no_worker),
    and no screen has a layout, a repaint or a call after one outstanding (which is done here,
    at once, as Pilot.pause does for the screen on top).

    In place of Textual's wait_for_idle, which Pilot calls after every key and in every pause.
    That one sleeps 20 ms and takes the app for idle when the process used under a millisecond
    of CPU meanwhile: true of an app that is done, and as true of one the machine gave no CPU
    to. On a busy machine a test went on while its key was still on the way to the widget, or a
    dialog was half built, and asserted on what it found. It also gave up after a second.

    What a running worker's thread is still doing is not waited for: a test that needs its
    result says so (workers.wait_for_complete), and some hold one on purpose to look at the
    view meanwhile."""
    import asyncio

    from textual._context import active_app

    loop = asyncio.get_running_loop()
    if min_sleep:
        await asyncio.sleep(min_sleep)
    app = active_app.get(None)
    give_up = loop.time() + REST_WITHIN
    while loop.time() < give_up:
        await asyncio.sleep(0)  # behind every task that can run now
        if loop._ready or any(not t.cancelled() and t.when() <= loop.time() for t in loop._scheduled):
            continue
        if app is not None and threads_of_no_worker(app) > 0:
            await asyncio.sleep(0.001)  # nothing to run until the thread is back
            continue
        try:
            screens = list(app.screen_stack) if app is not None else []
        except Exception:  # no screens: the app is closing
            screens = []
        unfinished = [screen for screen in screens if screen._update_timer._active.is_set()]
        if not unfinished:
            return
        for screen in unfinished:
            screen._on_timer_update()
    raise AssertionError(f"the view did not come to rest in {REST_WITHIN:g} s: it always has work")


@pytest.fixture(autouse=True)
def the_view_is_waited_for_until_it_is_at_rest(monkeypatch):
    """Pilot's keys and pauses wait on what the view has left to do (at_rest), not on how much
    CPU the process got in 20 ms."""
    import textual.app
    import textual.pilot

    monkeypatch.setattr(textual.pilot, "wait_for_idle", at_rest)
    monkeypatch.setattr(textual.app, "wait_for_idle", at_rest)


@pytest.fixture(autouse=True)
def a_tests_view_closes_as_quit_closes_it(monkeypatch):
    """Textual's run_test takes the app down at the end of its block without the exit that q
    is. A running view that exits is marked so first, and from then on Textual's timers call
    nothing back; the test's was not, so its two-second refresh could fall due while its
    screens were being removed, and refilled a list whose screen was gone (NoMatches: '#empty',
    on a busy machine, in whichever test the refresh fell in). Here the test's app is marked
    as one that exited before it is taken down, as the running view is."""
    from textual.app import App

    take_down = App._shutdown

    async def exit_then_take_down(app):
        app._exit = True
        await take_down(app)

    monkeypatch.setattr(App, "_shutdown", exit_then_take_down)


async def threads_done(workers, chosen=None) -> None:
    """Until the view's workers have ended and no thread of its executor is at work, one the
    view cancelled included; a worker that failed raises, as it does in Textual's wait.

    In place of WorkerManager.wait_for_complete, which raises WorkerCancelled when one of the
    workers was cancelled: the view cancels its own on purpose (a refresh that arrives while one
    looks at the pods replaces it, a dialog that closes drops the thread reading its branch),
    and whether the earlier one had ended by then was a matter of how busy the machine was. A
    cancelled worker's thread runs on and may still report back; it is waited for too, so that
    what it says lands before the test looks, not while."""
    import asyncio

    from textual.worker import WorkerCancelled

    loop = asyncio.get_running_loop()
    give_up = loop.time() + REST_WITHIN
    executor = getattr(workers._app, "executor", None)
    while loop.time() < give_up:
        for worker in list(chosen or workers):
            with contextlib.suppress(WorkerCancelled):
                await worker.wait()
        if executor is None or not executor.unfinished():
            return
        await asyncio.sleep(0.001)  # nothing to do here until the thread is back
    raise AssertionError(f"a thread of the view was still at work after {REST_WITHIN:g} s")


@pytest.fixture(autouse=True)
def a_cancelled_worker_is_one_that_ended(monkeypatch):
    from textual.worker_manager import WorkerManager

    monkeypatch.setattr(WorkerManager, "wait_for_complete", threads_done)


@pytest.fixture(autouse=True)
def nothing_remembered_of_another_test_s_code_blocks():
    """The panel's code blocks remember what they read and folded, and the width each was drawn
    at, for the whole process: a test that counts that work starts with none done."""
    import sys

    if blocks := sys.modules.get("vivibox.blocks"):
        blocks._read.cache_clear()
        blocks._folded.cache_clear()
        blocks._DRAWN_AT.clear()
