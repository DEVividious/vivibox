"""A program whose output is read within a limit (vivibox/counted.py): of bytes, counted as they
come on both streams, and of time, whichever is first; past either its session is ended, both
pipes are closed, and nothing it printed is handed on.

The program is a stand-in for git, first on PATH, that prints what a test says. No test here
writes much (a megabyte at most) or asserts how long anything took. A reader that waits for
ever would stop the run instead of failing a test, so every call into it is held to a time of
the test's own (within), and whatever a stand-in started is ended when its test is over.
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_repo
from test_git_timeout import in_time

from vivibox import counted, repo

BLOCK = 65536  # bytes the stand-in for git writes at a time: what a pipe holds
BLOCKS = 256  # and how many of them it has to write, were it let
# What the stand-in does to print without end, as far as a test lets it: a block at a time,
# each one written counted, to stdout or where the second %s says, and then it waits.
PRINTS_ON = "n=0; while [ $n -lt %d ]; do cat {block} %s && echo >> {written}; n=$((n + 1)); done\n"
PRINTS_ON += "exec sleep 600"
ASKED = {"stdin": subprocess.DEVNULL}


def within(call, seconds: float = 30):
    """call's result, or its error, raised here; a failure when it has not come back in seconds
    (test_git_timeout.in_time), which is long past any time the call was given: a reader that
    waits for ever fails its test instead of stopping the run."""
    done = in_time(call, seconds)
    if isinstance(done, BaseException):
        raise done
    return done


def _there(pid: int) -> bool:
    """Whether the process is there and alive: not gone, and not dead and waiting to be collected."""
    try:  # the state follows the name, which is in brackets and may hold any character
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] not in "ZX"
    except (OSError, IndexError):
        return False


def ended(pids: Path) -> bool:
    """Whether the processes the stand-in named are gone, or dead and not yet collected. Looked
    at again for a few seconds, since a process that was killed takes a moment to go; any that
    is still there after that is ended here, by its id, so that the test leaves none."""
    found = [int(pid) for pid in pids.read_text().split()] if pids.exists() else []
    for _ in range(100):
        if not any(_there(pid) for pid in found):
            return True
        time.sleep(0.1)
    for pid in filter(_there, found):
        os.kill(pid, signal.SIGKILL)
    return False


@pytest.fixture
def stand_in():
    """Makes a git to put first on PATH that is git for everything but one word of its
    arguments (log, ls-tree, a commit's id). Asked for that, it writes its process id to a file
    (pids), with that of a process it starts first, which holds the pipes it prints to (holds),
    and goes on as `then` says (sh; {block} is a file of BLOCK bytes of `listing`, {written} one
    to count in, {left} one for the id of a process that leaves). Whatever a stand-in started
    and a test did not see ended is ended after the test, by its id."""
    made = []

    def make(folder: Path, word: str, then: str, listing: str = "x\n", holds: bool = True) -> SimpleNamespace:
        folder.mkdir()
        there = SimpleNamespace(path=folder, pids=folder / "pids", written=folder / "written")
        there.block, there.left = folder / "block", folder / "left"
        whole = listing * (BLOCK // len(listing))
        there.block.write_text(whole + "\n" * (BLOCK - len(whole)))
        there.written.write_text("")
        (folder / "git").write_text(
            "#!/bin/sh\n"
            f'case " $* " in *" {word} "*) ;; *) exec {shutil.which("git")} "$@" ;; esac\n'
            + (f'sleep 600 &\necho "$$ $!" > {there.pids}\n' if holds else f'echo "$$" > {there.pids}\n')
            + f"{then.format(**vars(there))}\n"
        )
        (folder / "git").chmod(0o755)
        made.append(there)
        return there

    yield make
    for there in made:
        for pids in (there.pids, there.left):
            for pid in (int(pid) for pid in pids.read_text().split()) if pids.exists() else ():
                if _there(pid):
                    os.kill(pid, signal.SIGKILL)


def on_path(monkeypatch, there: SimpleNamespace) -> None:
    monkeypatch.setenv("PATH", f"{there.path}:{os.environ['PATH']}")


def test_git_read_within_a_limit_answers_as_git_does(tmp_path, monkeypatch, stand_in):
    """repo.run_git with a limit: what git printed on each stream and its exit code, as bytes,
    where it stayed within the limit and the time; past the limit, how much was read by then,
    which is the limit and at most one read more."""
    there = make_repo(tmp_path / "there")
    head = subprocess.run(["git", "-C", str(there), "rev-parse", "HEAD"], capture_output=True).stdout
    asked = dict(limit=4096, timeout=20, **ASKED)
    done = within(lambda: repo.run_git(["-C", str(there), "rev-parse", "HEAD"], **asked))
    assert (done.returncode, done.stdout, done.stderr) == (0, head, b"")
    done = within(lambda: repo.run_git(["-C", str(there), "rev-parse", "--verify", "no-such-ref"], **asked))
    assert done.returncode == 128 and done.stdout == b"" and done.stderr.startswith(b"fatal: ")
    git_ = stand_in(tmp_path / "stand-in", "log", PRINTS_ON % (BLOCKS, ""))
    on_path(monkeypatch, git_)
    with pytest.raises(counted.TooMuch) as stopped:
        within(lambda: repo.run_git(["log"], **asked))
    assert 4096 < stopped.value.read <= 4096 + counted.CHUNK and ended(git_.pids)
    waits = stand_in(tmp_path / "waits", "log", "exec sleep 600")
    on_path(monkeypatch, waits)
    with pytest.raises(subprocess.TimeoutExpired):
        within(lambda: repo.run_git(["log"], limit=4096, timeout=0.5, **ASKED))
    assert ended(waits.pids), "past its time, git's session is ended as it is past the limit"


LIMIT = 100_000
SIZES = {
    "the limit, to the byte": (LIMIT, 0, True),
    "a byte more": (LIMIT + 1, 0, False),
    "the limit, and a byte of what git says besides": (LIMIT, 1, False),
    "the limit, between the two": (60_000, 40_000, True),
}


@pytest.mark.parametrize("size", SIZES)
def test_an_answer_of_the_limit_is_whole_and_a_byte_more_is_none(tmp_path, monkeypatch, stand_in, size):
    """The limit is the most that is read, to the byte, of both streams together: an answer of
    exactly that many bytes is git's answer, whole; one byte more, on either stream, is none."""
    out, err, whole = SIZES[size]
    then = f"head -c {out} /dev/zero; head -c {err} /dev/zero >&2"
    on_path(monkeypatch, stand_in(tmp_path / "stand-in", "log", then, holds=False))
    read = lambda: repo.run_git(["log"], limit=LIMIT, timeout=20, **ASKED)  # noqa: E731
    if whole:
        done = within(read)
        assert (done.returncode, len(done.stdout), len(done.stderr)) == (0, out, err)
    else:
        with pytest.raises(counted.TooMuch) as stopped:
            within(read)
        assert stopped.value.read == LIMIT + 1


def test_a_git_that_closes_what_it_prints_to_and_goes_on_is_held_to_the_time(tmp_path, monkeypatch, stand_in):
    """Both pipes at their end is not git at its end: one that closed its output and runs on
    is waited on until the time is out and no longer, and ended then."""
    git_ = stand_in(tmp_path / "stand-in", "log", "exec >&- 2>&-; exec sleep 600", holds=False)
    on_path(monkeypatch, git_)
    with pytest.raises(subprocess.TimeoutExpired):
        within(lambda: repo.run_git(["log"], limit=4096, timeout=1, **ASKED))
    assert ended(git_.pids), "a git that closed its output was left running"


def test_no_descriptor_is_left_open_however_the_reading_ends(tmp_path, monkeypatch, stand_in):
    """The reader opens two pipes for each git. Answered, past the limit or past the time, it
    closes what it opened: this process has as many descriptors after as before."""
    there = make_repo(tmp_path / "there")
    path = os.environ["PATH"]
    much = stand_in(tmp_path / "much", "log", PRINTS_ON % (4, ""))
    waits = stand_in(tmp_path / "waits", "log", "exec sleep 600")
    opened = lambda: sorted(os.listdir("/proc/self/fd"))  # noqa: E731
    asked = dict(limit=4096, **ASKED)
    before = opened()
    for _ in range(3):
        assert within(lambda: repo.run_git(["-C", str(there), "rev-parse", "HEAD"], timeout=20, **asked))
        monkeypatch.setenv("PATH", f"{much.path}:{path}")
        with pytest.raises(counted.TooMuch):
            within(lambda: repo.run_git(["log"], timeout=20, **asked))
        monkeypatch.setenv("PATH", f"{waits.path}:{path}")
        with pytest.raises(subprocess.TimeoutExpired):
            within(lambda: repo.run_git(["log"], timeout=0.2, **asked))
        assert ended(much.pids) and ended(waits.pids)
        monkeypatch.setenv("PATH", path)
    assert opened() == before, "a pipe of a git that was read is still open"


@pytest.mark.skipif(not shutil.which("setsid"), reason="no setsid to leave a session with")
def test_a_process_that_left_gits_session_is_not_waited_on_and_not_ended(tmp_path, monkeypatch, stand_in):
    """What the reader ends is git's session. A process git started that left it, holding the
    pipe git printed to, is beyond that: the reader does not wait on it past the time (it
    closes its end of the pipe), and it runs on. No git of vivibox's starts such a process."""
    git_ = stand_in(
        tmp_path / "stand-in", "log", "setsid sh -c 'echo $$ > {left}; exec sleep 600' &\nexit 0", holds=False
    )
    on_path(monkeypatch, git_)
    with pytest.raises(subprocess.TimeoutExpired):
        within(lambda: repo.run_git(["log"], limit=4096, timeout=1, **ASKED))
    assert ended(git_.pids), "git itself"
    assert _there(int(git_.left.read_text())), "the process that left the session was ended after all"
