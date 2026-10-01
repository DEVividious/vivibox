"""The host's reads of a task's clone, which the agent's pod mounts read-write: a link there is
followed only while it stays inside the clone, and a FIFO, a socket, a device or a folder in place
of a file is never read, nor waited on. What the host reads there reaches the agent's prompt, the
commands the verification runs, the risky files' approval and your review."""

import os
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import make_repo
from test_gate import FakePod, git

from vivibox import clone_read, gate, init, repo, reviewing, risky, supervisor
from vivibox.risky import Approvals, Change
from vivibox.states import State
from vivibox.task import create_task

PLAN = """+++
mode = "code-only"
+++

# Goal

## Acceptance criteria

- [ ] endpoint returns 200
"""
SECRET = "-PsecretFlag=HOSTVALUE"
OUTSIDE = f"the host's own file {SECRET}\n"


def in_time(call):
    """call's result, or the error it raised; a failure when it hangs (a FIFO opened for reading
    waits for a writer)."""
    done = []

    def run():
        try:
            done.append(call())
        except Exception as e:
            done.append(e)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert done, "the host hung on a special file"
    return done[0]


def odd(path: Path, kind: str, monkeypatch) -> None:
    """path made a FIFO, a socket, a folder or a whiteout device (a char device 0:0, the one an
    unprivileged process can make, and so the one an agent in the pod can)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        monkeypatch.chdir(path.parent)  # a socket's path has a length limit; its name has not
        socket.socket(socket.AF_UNIX).bind(path.name)
    elif kind == "folder":
        path.mkdir()
    else:
        try:
            os.mknod(path, stat.S_IFCHR | 0o600, 0)
        except OSError as e:
            pytest.skip(f"no whiteout device on this filesystem: {e}")


KINDS = ["fifo", "socket", "device", "folder"]


@pytest.fixture
def clone(tmp_path):
    path = tmp_path / "clone"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path):
    """A folder of the host's, beside the clone, with a file the agent must never get."""
    path = tmp_path / "host"
    path.mkdir()
    (path / "secret.txt").write_text(OUTSIDE)
    return path


# --- the walk ------------------------------------------------------------------------------------


def test_a_plain_file_and_a_link_that_stays_inside_are_read(clone):
    (clone / "build").mkdir()
    (clone / "build" / "pom.xml").write_text("<project/>\n")
    (clone / "pom.xml").symlink_to("build/pom.xml")
    (clone / "app").mkdir()
    (clone / "app" / "pom.xml").symlink_to("../build/pom.xml")
    (clone / "linked").symlink_to("build")
    for name in ("build/pom.xml", "pom.xml", "app/pom.xml", "linked/pom.xml", "linked/../pom.xml"):
        assert clone_read.read_bytes(clone, name) == b"<project/>\n", name
        assert clone_read.read_text(clone, clone / name) == "<project/>\n", name
        assert clone_read.is_file(clone, clone / name), name
    assert clone_read.read_text(clone, "none.xml") == "" and not clone_read.is_file(clone, "none.xml")


LINKS_OUT = {
    # name made a link: (the link, its target, the path read)
    "absolute": ("pom.xml", "{host}/secret.txt", "pom.xml"),
    "up": ("pom.xml", "../host/secret.txt", "pom.xml"),
    "up from a folder": ("sub/pom.xml", "../../host/secret.txt", "sub/pom.xml"),
    "through a linked folder": ("apps", "{host}", "apps/secret.txt"),
    "through a folder linked up": ("apps", "../host", "apps/secret.txt"),
    # A link git stores by an absolute path is a path on the host: never followed, wherever it goes.
    "absolute, to the clone itself": ("pom.xml", "{clone}/sub/pom.xml", "pom.xml"),
}


@pytest.mark.parametrize("link", LINKS_OUT)
def test_a_link_that_leads_out_of_the_clone_is_refused(clone, outside, link):
    name, target, read = LINKS_OUT[link]
    (clone / "sub").mkdir()
    (clone / name).symlink_to(target.format(host=outside, clone=clone))
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, read)
    assert clone_read.read_text(clone, read) == ""
    assert not clone_read.is_file(clone, clone / read)


def test_a_link_to_a_device_of_the_host_is_never_read(clone):
    (clone / "zero").symlink_to("/dev/zero")
    (clone / "null").symlink_to("/dev/null")
    for name in ("zero", "null"):
        assert in_time(lambda name=name: clone_read.read_text(clone, name)) == ""


@pytest.mark.parametrize("kind", KINDS)
def test_a_special_file_is_refused_without_being_opened(clone, monkeypatch, kind):
    odd(clone / "package.json", kind, monkeypatch)
    (clone / "linked.json").symlink_to("package.json")
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real_open(p, *a, **k))
    for name in ("package.json", "linked.json"):
        assert in_time(lambda name=name: clone_read.read_text(clone, name)) == ""
        assert isinstance(in_time(lambda name=name: clone_read.read_bytes(clone, name)), OSError)
    assert not [p for p in opened if p.endswith("package.json")], "refused on lstat, before any open"


def test_a_loop_of_links_is_refused(clone):
    (clone / "a").symlink_to("b")
    (clone / "b").symlink_to("a")
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, "a")


def test_a_link_swapped_out_of_the_clone_during_the_walk_is_not_followed(clone, outside, monkeypatch):
    """The agent changes the clone while the host walks it: a folder the walk is about to enter
    becomes a link out right after the walk read the link that led to it."""
    (clone / "real").mkdir()
    (clone / "real" / "secret.txt").write_text("the clone's own\n")
    (clone / "entry").symlink_to("real")
    real_readlink = os.readlink
    swapped = []

    def readlink(path, *args, **kwargs):
        target = real_readlink(path, *args, **kwargs)
        if not swapped:
            os.rename(clone / "real", clone / "was-real")
            (clone / "real").symlink_to(outside)
            swapped.append(target)
        return target

    monkeypatch.setattr(os, "readlink", readlink)
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, "entry/secret.txt")
    assert swapped == ["real"]


def test_a_name_swapped_for_a_link_after_it_was_looked_at_is_not_opened(clone, outside, monkeypatch):
    (clone / "pom.xml").write_text("<project/>\n")
    real_stat = os.stat

    def stat_then_swap(name, *args, **kwargs):
        info = real_stat(name, *args, **kwargs)
        if name == "pom.xml":
            (clone / "pom.xml").unlink()
            (clone / "pom.xml").symlink_to(outside / "secret.txt")
        return info

    monkeypatch.setattr(os, "stat", stat_then_swap)
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, "pom.xml")


# --- the build files: what the writer is told and what the verification runs ---------------------


@pytest.fixture
def task(tmp_path):
    source = make_repo(tmp_path / "source")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    return t


def host_pipeline(outside: Path) -> Path:
    path = outside / "ci.yml"
    path.write_text(f"build:\n  script:\n    - mvn -B verify {SECRET}\n")
    return path


def test_a_committed_link_to_a_pipeline_of_the_host_brings_no_command(task, outside):
    """The agent commits .gitlab-ci.yml as a link to a CI file of yours: its build lines would be
    offered to the writer as the project's own command, and written to the log it reads."""
    (task.repo / ".gitlab-ci.yml").symlink_to(host_pipeline(outside))
    git(task.repo, "add", ".")
    git(task.repo, "commit", "-q", "-m", "Add the pipeline")
    assert init.ci_commands(task.repo) == []
    assert all(SECRET not in command for command, _ in init.candidates(task.repo))
    gate.accept_plan(task)
    result = gate.run_gate(task, FakePod(), [], [])
    assert SECRET not in result.log.read_text() and result.build_files == []
    assert SECRET not in str(task.events()[-1])


def test_the_writer_is_not_told_a_command_from_a_linked_pipeline_of_the_host(tmp_path, outside):
    from test_supervisor import DRAFT, TEMPLATE, FakeHarness, gate_result

    t = create_task(tmp_path / "named", "demo", "Goal", TEMPLATE)
    t.plan_path.write_text(DRAFT)
    t.transition(State.CHECKPOINT_PLAN)
    supervisor.accept_plan(t, "plan accepted")
    t.repo.mkdir(parents=True, exist_ok=True)
    (t.repo / ".gitlab-ci.yml").symlink_to(host_pipeline(outside))
    (t.repo / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    harness = FakeHarness(t)
    ports = supervisor.Ports(run_gate=lambda _: gate_result(), risky_changes=lambda: [])
    supervisor.Supervisor(t, harness, ports, max_rounds=1, project_verify=[]).step()
    asked = harness.prompts[0]
    assert "`go test ./...` (go.mod)" in asked, "the clone's own build file is still named"
    assert SECRET not in asked and "secretFlag" not in asked


def test_a_pipeline_in_the_clone_or_linked_inside_it_is_read_as_before(clone):
    (clone / "ci").mkdir()
    (clone / "ci" / "pipeline.yml").write_text("build:\n  script:\n    - mvn -B verify -Pci\n")
    (clone / ".gitlab-ci.yml").symlink_to("ci/pipeline.yml")
    (clone / ".github" / "workflows").mkdir(parents=True)
    workflow = "jobs:\n  t:\n    steps:\n      - run: npm test\n"
    (clone / ".github" / "workflows" / "ci.yml").write_text(workflow)
    assert init.ci_commands(clone) == [
        ("npm test", ".github/workflows/ci.yml"),
        ("mvn -B verify -Pci", ".gitlab-ci.yml"),
    ]


def test_a_linked_build_file_inside_the_clone_is_read_as_before(clone):
    """A monorepo that links its root package.json to one in a folder keeps its detection."""
    (clone / "config").mkdir()
    package = '{"packageManager": "pnpm@9.0.0", "scripts": {"test": "x"}}'
    (clone / "config" / "package.json").write_text(package)
    (clone / "package.json").symlink_to("config/package.json")
    (clone / "pnpm-lock.yaml").write_text("")
    assert init.candidates(clone)[0] == ("pnpm install --frozen-lockfile && pnpm test", "package.json")
    assert init.with_dependencies(clone, ["pnpm test"]) == ["pnpm install --frozen-lockfile", "pnpm test"]


def test_a_workspace_folder_linked_out_of_the_clone_is_no_module_or_command(clone, outside):
    """A folder the workspaces name, linked to a folder of yours: its folders' names and their
    package.json never reach the modules the verification builds or the commands offered."""
    (outside / "private-app").mkdir()
    (outside / "private-app" / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (clone / "package.json").write_text('{"workspaces": ["apps/*"], "scripts": {}}')
    (clone / "package-lock.json").write_text("{}")
    (clone / "apps").symlink_to(outside)
    assert init.modules(clone) == []
    assert all("private-app" not in command for command, _ in init.candidates(clone))


SITES = {
    "with_dependencies": lambda repo: init.with_dependencies(repo, ["npm test"]),
    "candidates": init.candidates,
    "modules": init.modules,
    "detect": lambda repo: (d := init.detect(repo)) and (d.verify, d.notes, d.modules, d.tools, d.prepare),
}


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("kind", KINDS)
def test_detection_does_not_wait_on_a_special_build_file(tmp_path, monkeypatch, site, kind):
    """Read as a package.json with nothing in it: the same detection, never a wait."""
    special, empty = tmp_path / "special", tmp_path / "empty"
    for clone in (special, empty):
        clone.mkdir()
        (clone / "package-lock.json").write_text("{}")
    (empty / "package.json").write_text("")
    odd(special / "package.json", kind, monkeypatch)
    monkeypatch.chdir(tmp_path)
    got = in_time(lambda: SITES[site](special))
    assert not isinstance(got, BaseException), got
    want = SITES[site](empty)
    assert str(got).replace("special", "empty") == str(want)


@pytest.mark.parametrize("site", SITES)
def test_detection_reads_nothing_through_a_link_out_of_the_clone(clone, outside, site):
    """Your build files, linked from the clone: the modules they list and the toolchain they name
    would become the verification's command and what the pod installs."""
    (outside / "package.json").write_text('{"packageManager": "bun@HOSTVALUE", "scripts": {"test": "x"}}')
    (outside / "pom.xml").write_text("<project><modules><module>HOSTVALUE</module></modules></project>")
    (outside / "settings.gradle").write_text("include 'HOSTVALUE'\n")
    for name in ("package.json", "pom.xml", "settings.gradle"):
        (clone / name).symlink_to(outside / name)
    (clone / "bun.lock").write_text("")
    assert "HOSTVALUE" not in str(in_time(lambda: SITES[site](clone)))


# --- the checks on the agent's files: invisible characters, risky files -------------------------


def untracked_link(task, name: str, target) -> None:
    (task.repo / name).symlink_to(target)


def test_hidden_characters_do_not_wait_on_an_untracked_link_to_a_fifo(task):
    os.mkfifo(task.repo / "pipe")  # git lists no FIFO; the link to it, it does
    untracked_link(task, "notes.txt", "pipe")
    assert in_time(lambda: gate.hidden_characters(task.repo, task.read_state().base_commit)) == []


def test_hidden_characters_read_no_host_file_through_an_untracked_link(task, outside):
    (outside / "hidden.txt").write_text("a\u200bb\n")
    untracked_link(task, "notes.txt", outside / "hidden.txt")
    untracked_link(task, "zero.txt", "/dev/zero")
    found = in_time(lambda: gate.hidden_characters(task.repo, task.read_state().base_commit))
    assert found == [], "a link is what git would commit: its target, not the host's file"


def test_hidden_characters_check_what_git_commits_of_a_link_its_target(task):
    untracked_link(task, "notes.txt", "rea\u200bdme.md")
    assert gate.hidden_characters(task.repo, task.read_state().base_commit) == ["notes.txt:1 U+200B"]


def test_a_plain_untracked_file_is_checked_as_before(task):
    (task.repo / "notes.txt").write_text("fine\nnot\u200bfine\n")
    assert gate.hidden_characters(task.repo, task.read_state().base_commit) == ["notes.txt:2 U+200B"]


@pytest.mark.parametrize("kind", ["fifo", "socket", "device"])
def test_hidden_characters_count_a_special_file_they_could_not_read(task, monkeypatch, kind):
    """Swapped in after git listed it: never read, so never passed; the check fails closed."""
    odd(task.repo / "notes.txt", kind, monkeypatch)
    real_git = repo.git

    def listed(*args, **kwargs):
        if args[:1] == ("ls-files",):
            return subprocess.CompletedProcess(args, 0, "notes.txt\0", "")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(repo, "git", listed)
    found = in_time(lambda: gate.hidden_characters(task.repo, task.read_state().base_commit))
    assert len(found) == 1 and found[0].startswith("notes.txt") and "vivibox does not read" in found[0]


def test_the_risky_files_do_not_wait_on_a_fifo_named_like_one(task):
    """A FIFO named like a build file is new to the approval, not a hang of the gate or the view."""
    os.mkfifo(task.repo / "Makefile")
    approvals = Approvals(task.meta, task.repo)
    assert in_time(approvals.changes) == [Change("Makefile", "added")]
    assert "vivibox does not read it" in in_time(lambda: approvals.diff(Change("Makefile", "added")))


@pytest.mark.parametrize("kind", ["fifo", "socket", "device"])
def test_a_risky_file_replaced_by_a_special_one_is_a_change(task, monkeypatch, kind):
    """Refused, it is never the same as the file you approved: the check fails closed."""
    (task.repo / "Makefile").write_text("test:\n\ttrue\n")
    approvals = Approvals(task.meta, task.repo)
    approvals.approve()
    (task.repo / "Makefile").unlink()
    odd(task.repo / "Makefile", kind, monkeypatch)
    assert in_time(approvals.changes) == [Change("Makefile", "changed")]


def test_approving_does_not_wait_on_a_special_file_or_copy_one_through_a_link(task, outside):
    os.mkfifo(task.repo / "Makefile")
    (task.repo / "pom.xml").symlink_to(outside / "secret.txt")
    approvals = Approvals(task.meta, task.repo)
    approved = in_time(approvals.approve)
    assert not isinstance(approved, BaseException), approved
    assert set(approved) >= {"Makefile", "pom.xml"}
    kept = task.meta / "risky" / "files"
    assert not (kept / "Makefile").exists() and not (kept / "pom.xml").exists()
    assert in_time(approvals.changes) == [], "approved as they are"


def test_a_risky_link_out_of_the_clone_is_its_target_alone(task, outside):
    """What git stores of a link is its target; a file of yours it points at is not the agent's."""
    (task.repo / "pom.xml").symlink_to(outside / "secret.txt")
    assert risky.scan(task.repo)["pom.xml"] == f"symlink:{outside / 'secret.txt'}"


def test_a_risky_link_inside_the_clone_changes_when_its_target_does(task):
    (task.repo / "build").mkdir()
    (task.repo / "build" / "pom.xml").write_text("<project/>\n")
    (task.repo / "pom.xml").symlink_to("build/pom.xml")
    approvals = Approvals(task.meta, task.repo)
    approvals.approve()
    (task.repo / "build" / "pom.xml").write_text("<project><build/></project>\n")
    assert Change("pom.xml", "changed") in approvals.changes()


# --- the review in your CLI ---------------------------------------------------------------------


@pytest.fixture
def at_review(tmp_path):
    task = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    gate.accept_plan(task)
    (task.meta / "handoff" / "red.md").write_text("tests/test_view.py failed before the fix\n")
    return task


def copies(task) -> Path:
    return task.meta / reviewing.CLI_INPUT


def asked_of(task) -> str:
    return (task.meta / reviewing.CLI_PROMPT).read_text()


def test_the_cli_review_reads_the_hosts_copies_never_the_handoff(at_review):
    task = at_review
    reviewing.ask_cli(task, 1, None)
    asked = asked_of(task)
    assert str(copies(task) / "criteria.md") in asked and str(copies(task) / "red.md") in asked
    assert "handoff" not in asked, "nothing in the prompt points into handoff/"
    assert (copies(task) / "red.md").read_text() == "tests/test_view.py failed before the fix\n"
    assert (copies(task) / "criteria.md").read_text() == (task.meta / "handoff" / "criteria.md").read_text()


def test_the_cli_review_says_why_a_linked_checklist_was_left_out(at_review, outside):
    task = at_review
    criteria = task.meta / "handoff" / "criteria.md"
    reviewing.ask_cli(task, 1, None)
    criteria.unlink()
    criteria.symlink_to(outside / "secret.txt")
    reviewing.ask_cli(task, 1, None)
    asked = asked_of(task)
    assert not (copies(task) / "criteria.md").exists(), "no copy of the last round is left behind"
    assert "vivibox left out the writer's criteria.md: it is a link or not a plain file" in asked
    assert str(copies(task) / "red.md") in asked
    assert "handoff" not in asked and SECRET not in asked
    assert not any(SECRET in p.read_text() for p in copies(task).iterdir())


def test_the_cli_review_does_not_wait_on_a_special_file(at_review):
    task = at_review
    (task.meta / "handoff" / "red.md").unlink()
    os.mkfifo(task.meta / "handoff" / "red.md")
    assert not isinstance(in_time(lambda: reviewing.ask_cli(task, 1, None)), BaseException)
    asked = asked_of(task)
    assert "vivibox left out the writer's red.md" in asked and "handoff" not in asked


def test_the_cli_review_copies_the_writers_answer_to_the_last_round(at_review):
    task = at_review
    (task.meta / "handoff" / "review-1-reply.md").write_text("It does prove it.\n")
    reviewing.ask_cli(task, 2, None)
    asked = asked_of(task)
    assert str(copies(task) / "review-1-reply.md") in asked and "handoff" not in asked
    assert (copies(task) / "review-1-reply.md").read_text() == "It does prove it.\n"


def test_a_copy_for_the_approval_is_never_a_links_file_or_a_special_one(
    clone, outside, monkeypatch, tmp_path
):
    """The approval keeps its copies in /task, where the agent reads: a link swapped in after the
    scan, inside the clone or out of it, is refused, and so is a FIFO."""
    (clone / "pom.xml").write_text("<project/>\n")
    (clone / "inside.xml").symlink_to("pom.xml")
    (clone / "outside.xml").symlink_to(outside / "secret.txt")
    odd(clone / "Makefile", "fifo", monkeypatch)
    for name in ("inside.xml", "outside.xml", "Makefile"):
        refused = in_time(lambda name=name: clone_read.copy(clone, name, tmp_path / "copy"))
        assert isinstance(refused, clone_read.Refused), name
        assert not (tmp_path / "copy").exists(), name
    clone_read.copy(clone, "pom.xml", tmp_path / "copy")
    assert (tmp_path / "copy").read_text() == "<project/>\n"
