"""The code index's project data and its lifecycle: the host's read-only view for I, I's private
copy of the commits made and brought up to date by I's own fixed operation (index_sync), the
discard of all I wrote when that fails, and the provisioning run on a network of its own that
exists only while the run lasts. I is taken to be compromised: the host never runs git in, nor
reads, what I writes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_index_container import COMMIT, SERVER, a_clone, a_pod, commit, git, sources
from test_pod import flag, starts

from vivibox import control, index_sync, podspec, reading
from vivibox.pod import Mount, PodError
from vivibox.podspec import IndexServer

PROVISIONING = IndexServer(SERVER.mounts, SERVER.command, provision=("python3", "/opt/index/provision.py"))


# --- index_sync, the operation I runs on its copy ------------------------------------------------


@pytest.fixture
def source(tmp_path, monkeypatch) -> Path:
    """A view of the host's, as reading.view makes it: what index_sync reads at SOURCE."""
    clone = tmp_path / "repo"
    a_clone(clone)
    view = tmp_path / "view"
    view.mkdir()
    reading.view(clone, "vivibox/shop-1", view, whose="the code index's")
    monkeypatch.setattr(index_sync, "SOURCE", str(view))
    return view


def advance(tmp_path: Path, files: dict[str, str | None]) -> str:
    """A new commit on the task's branch, and the host's view brought to it."""
    head = commit(tmp_path / "repo", files)
    reading.view(tmp_path / "repo", "vivibox/shop-1", tmp_path / "view", whose="the code index's")
    return head


def tree(folder: Path) -> dict[str, str]:
    """Every file under folder but .git, with what it holds (a link: what it names)."""
    found = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if rel.parts[0] == ".git" or (path.is_dir() and not path.is_symlink()):
            continue
        found[str(rel)] = f"-> {os.readlink(path)}" if path.is_symlink() else path.read_text()
    return found


def test_the_copy_is_the_commit_then_the_next_commit_and_nothing_else(tmp_path, source):
    """A, then B: what B deleted or renamed away is gone, an untracked and an ignored file of I's
    are gone, a committed link is a plain file holding its path (as in the planner's view)."""
    copy = tmp_path / "copy"
    copy.mkdir()
    a = advance(tmp_path, {"src/app.py": "A\n", "old.txt": "old\n", ".gitignore": "build/\n"})
    assert index_sync.sync(str(copy), a) == 0
    assert tree(copy) == {
        ".gitignore": "build/\n",
        "README.md": "shop\n",
        "old.txt": "old\n",
        "src/app.py": "A\n",
    }
    (copy / "junk.txt").write_text("I's own\n")
    (copy / "build").mkdir()
    (copy / "build" / "out.class").write_text("ignored\n")
    (copy / "src" / "app.py").write_text("changed in the copy\n")
    (tmp_path / "repo" / "lib").mkdir()
    git(tmp_path / "repo", "mv", "src/app.py", "lib/app.py")
    os.symlink("/etc/passwd", tmp_path / "repo" / "link")
    git(tmp_path / "repo", "add", "link")
    b = advance(tmp_path, {"old.txt": None, "lib/app.py": "B\n"})
    assert index_sync.sync(str(copy), b) == 0
    assert tree(copy) == {
        ".gitignore": "build/\n",
        "README.md": "shop\n",
        "lib/app.py": "B\n",
        "link": "/etc/passwd",
    }
    assert not (copy / "link").is_symlink()
    assert git(copy, "rev-parse", "HEAD") == b


def test_a_source_on_another_commit_than_the_hosts_leaves_the_copy_as_it_was(tmp_path, source):
    copy = tmp_path / "copy"
    copy.mkdir()
    a = advance(tmp_path, {"a.txt": "A\n"})
    assert index_sync.sync(str(copy), a) == 0
    advance(tmp_path, {"a.txt": "B\n"})
    assert index_sync.sync(str(copy), a) == index_sync.OTHER_COMMIT
    assert (copy / "a.txt").read_text() == "A\n"


def test_the_program_runs_only_after_a_copy_that_is_made(tmp_path, source, monkeypatch):
    copy = tmp_path / "copy"
    copy.mkdir()
    a = advance(tmp_path, {"a.txt": "A\n"})
    ran = []
    monkeypatch.setattr(os, "execvp", lambda file, args: ran.append(args))
    assert index_sync.main([str(copy), a, "--", "server", "--flag"]) == 1
    assert ran == [["server", "--flag"]]
    ran.clear()
    assert index_sync.main([str(copy), "b" * 40, "--", "server"]) == index_sync.OTHER_COMMIT
    (copy / ".git").rename(tmp_path / "moved")
    (copy / ".git").write_text("gitdir: /elsewhere\n")  # a copy I broke
    assert index_sync.main([str(copy), a, "--", "server"]) == 1
    assert ran == []
    for wrong in ([], [str(copy)], [str(copy), a, "server"], [str(copy), a, "--"]):
        assert index_sync.main(wrong) == index_sync.USAGE, wrong


def test_the_script_runs_as_the_host_hands_it_over(tmp_path, source):
    """python3 -I -c with the file's text, as in the container: the same copy."""
    copy = tmp_path / "copy"
    copy.mkdir()
    a = advance(tmp_path, {"a.txt": "A\n"})
    text = Path(index_sync.__file__).read_text().replace('SOURCE = "/source"', f"SOURCE = {str(source)!r}")
    done = subprocess.run([sys.executable, "-I", "-c", text, str(copy), a], capture_output=True)
    assert done.returncode == 0, done.stderr
    assert (copy / "a.txt").read_text() == "A\n"


# --- the host's side: view, refresh, discard -----------------------------------------------------


def test_the_index_starts_on_the_hosts_view_of_the_branch(tmp_path):
    pod = a_pod(tmp_path)
    head = git(pod.repo, "rev-parse", "HEAD")
    pod.up()
    assert git(pod.index_source, "rev-parse", "HEAD") == head
    [run] = [c for c in pod.runner.find("docker", "run", "-d", "--name") if c[4] == pod.index]
    assert run[run.index("-c") + 2 : run.index("--")] == [str(pod.repo), head]
    assert pod.index_failure == ""


def test_a_refresh_brings_the_view_to_the_planners_commit_and_asks_i_for_its_copy(tmp_path):
    pod = a_pod(tmp_path)
    pod.up()
    verified = commit(pod.repo, {"a.txt": "verified\n"})
    commit(pod.repo, {"a.txt": "after the verification\n"})
    silent = []
    pod.silent = lambda cmd, timeout: silent.append((list(cmd), timeout)) or 0
    assert pod.index_refresh(verified) is True
    assert git(pod.index_source, "rev-parse", "HEAD") == verified
    assert (pod.index_source / "a.txt").read_text() == "verified\n", "the writer's later commit is not there"
    [(cmd, timeout)] = silent
    assert cmd[:3] == ["docker", "exec", pod.index] and cmd[3:7] == ["python3", "-I", "-c", podspec_script()]
    assert cmd[7:] == [str(pod.repo), verified], "the host's words only: the copy's path and the commit"
    assert timeout == pod.SYNC_SECONDS
    exec_of_i = [c for c in pod.runner.find("docker", "exec") if pod.index in c]
    assert not exec_of_i, "I's output never reaches the host"


def podspec_script() -> str:
    return Path(index_sync.__file__).read_text()


def test_the_planners_refresh_refreshes_the_index_on_the_same_commit(tmp_path, monkeypatch):
    pod = a_pod(tmp_path)
    pod.up()
    asked = []
    monkeypatch.setattr(reading, "view", lambda *a, **k: None)
    monkeypatch.setattr(control, "workdir", lambda pod: None)
    monkeypatch.setattr(
        type(pod), "index_refresh", lambda self, commit="", depth=0: asked.append((commit, depth))
    )
    task = type("T", (), {"id": "shop-1", "repo": pod.repo})()
    control.refresh(task, pod, COMMIT, depth=7)
    assert asked == [(COMMIT, 7)]
    pod.index_server = None
    control.refresh(task, pod, COMMIT, depth=7)
    assert asked == [(COMMIT, 7)], "no index, no refresh of one"


def written_by_i(pod) -> None:
    """What I leaves in its folders, and the host's record of its provisioning."""
    (pod.index_store / "cache").mkdir(parents=True, exist_ok=True)
    (pod.index_store / "cache" / "symbols").write_text("I's\n")
    (pod.index_copy / ".git").mkdir(parents=True, exist_ok=True)
    (pod.index_copy / ".git" / "config").write_text("[core]\n\tfsmonitor = /config/run-me\n")
    os.symlink("/", pod.index_copy / "root")
    pod.index_provisioned.write_text("[]")


@pytest.mark.parametrize("ending", ["failed", "timed out"])
def test_a_copy_i_could_not_bring_up_to_date_is_discarded_with_all_i_wrote(tmp_path, ending):
    pod = a_pod(tmp_path)
    pod.up()
    written_by_i(pod)
    outside = tmp_path / "outside"
    outside.write_text("kept\n")
    os.symlink(outside, pod.index_store / "out")

    def silent(cmd, timeout):
        if ending == "failed":
            return 1
        raise subprocess.TimeoutExpired(cmd, timeout)

    pod.silent = silent
    assert pod.index_refresh() is False
    assert ["docker", "rm", "-f", pod.index, pod.index_provision] in pod.runner.calls
    assert pod.runner.states.get(pod.index) is None, "I is down: its copy is never answered from"
    assert list(pod.index_store.iterdir()) == [] and list(pod.index_copy.iterdir()) == []
    assert not pod.index_provisioned.exists(), "provisioned again at the next start"
    assert outside.read_text() == "kept\n", "a link of I's is deleted, not followed"
    assert (pod.index_source / "README.md").exists(), "the host's view stays"
    assert "from scratch" in pod.index_failure


def test_a_view_that_cannot_be_made_takes_i_down_and_keeps_what_it_wrote(tmp_path):
    pod = a_pod(tmp_path)
    pod.up()
    written_by_i(pod)
    pod.silent = lambda cmd, timeout: pytest.fail("I is not asked for a copy of a view not made")
    assert pod.index_refresh("b" * 40) is False
    assert pod.runner.states.get(pod.index) is None
    assert (pod.index_store / "cache" / "symbols").exists()
    assert "view of the commits could not be made" in pod.index_failure


def test_an_index_not_running_is_left_so_by_a_refresh(tmp_path):
    pod = a_pod(tmp_path)
    pod.silent = lambda cmd, timeout: pytest.fail("not asked")
    assert pod.index_refresh() is False
    assert not pod.runner.find("docker", "run")


def test_a_folder_of_is_own_that_is_a_link_is_never_emptied_through_it(tmp_path):
    pod = a_pod(tmp_path)
    pod.up()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("kept\n")
    os.rmdir(pod.index_copy)
    os.symlink(elsewhere, pod.index_copy)
    with pytest.raises(PodError, match="link"):
        pod.index_discard()
    assert (elsewhere / "keep").read_text() == "kept\n"


def test_the_host_never_runs_git_in_what_i_writes(tmp_path, monkeypatch):
    """Through a start, refreshes that work, one that fails and its discard, and a provisioning
    run: every git the host runs is recorded, with where it runs and on which repository; none is
    in I's copy or store. The host's own view is where it runs (the positive control)."""
    pod = a_pod(tmp_path, server=PROVISIONING)
    pod.ensure_networks()
    seen: list[tuple[list[str], str, dict]] = []
    real = subprocess.Popen

    def recording(args, *a, **kw):
        argv = [str(x) for x in (args if isinstance(args, list | tuple) else [args])]
        if Path(argv[0]).name == "git":
            seen.append((argv, str(kw.get("cwd") or os.getcwd()), dict(kw.get("env") or {})))
        return real(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", recording)
    written_by_i(pod)
    pod.index_provisioned.unlink()
    pod.up()
    pod.silent = lambda cmd, timeout: 0
    pod.index_refresh()
    commit(pod.repo, {"b.txt": "B\n"})
    pod.index_refresh()
    pod.silent = lambda cmd, timeout: 1
    pod.index_refresh()
    mine = (pod.index_copy, pod.index_store)
    assert seen, "the host ran git"
    for argv, cwd, env in seen:
        where = [cwd, env.get("GIT_DIR", ""), env.get("GIT_WORK_TREE", "")]
        where += [argv[i + 1] for i, a in enumerate(argv) if a in ("-C", "--git-dir", "--work-tree")]
        inside = [w for w in where if w and any(Path(w) == m or m in Path(w).parents for m in mine)]
        assert not inside, (argv, where)
    assert any(cwd == str(pod.index_source) for _, cwd, _ in seen), "its own view"


# --- the provisioning run ------------------------------------------------------------------------


def provisioning_pod(tmp_path: Path):
    pod = a_pod(tmp_path, server=PROVISIONING)
    return pod


def test_the_provisioning_run_has_is_hardening_on_the_index_network_with_the_internet_alone(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        type(a_pod(tmp_path)), "ca_mounts", lambda self: [Mount("/etc/ca.pem", "/etc/ssl/ca.pem", True)]
    )
    pod = provisioning_pod(tmp_path)
    pod.ensure_networks()
    cmd = pod.provision_command(COMMIT)
    assert cmd[:5] == ["docker", "run", "-d", "--name", "vivibox-shop-1-index-provision"]
    assert flag(cmd, "--network") == "vivibox-shop-1-i" and flag(cmd, "--ip") == "198.51.100.30"
    assert flag(cmd, "--dns") == pod.host_ip and flag(cmd, "--dns-search") == "."
    for absent in ("--add-host", "-e", "--env", "-p", "--privileged", "--cap-add", "--device", "--pid"):
        assert absent not in cmd, absent
    assert flag(cmd, "--cap-drop") == "ALL" and flag(cmd, "--security-opt") == "no-new-privileges"
    assert "--read-only" in cmd and flag(cmd, "--ipc") == "private"
    assert flag(cmd, "--memory") == str(pod.limits.memory_of("index"))
    assert flag(cmd, "--log-driver") == "json-file"
    found = sources(cmd)
    assert found["/etc/ca.pem"] == "/etc/ssl/ca.pem:ro", "its downloads through your VPN's authority"
    assert str(pod.index_sockets) not in found
    assert cmd[cmd.index("--") + 1 :] == ["python3", "/opt/index/provision.py"]


def test_a_steady_index_is_never_on_a_network(tmp_path):
    pod = provisioning_pod(tmp_path)
    pod.up()
    [run] = [c for c in pod.runner.find("docker", "run", "-d", "--name") if c[4] == pod.index]
    assert flag(run, "--network") == "none"


def test_the_provisioning_run_has_its_network_and_rules_only_while_it_runs(tmp_path):
    """Before I's first start: the index's network made and checked, the host's rules loaded with
    it (no host service), the run, its end awaited, the run removed, its network removed, its
    rules removed; then I, with no network. Recorded as provisioned for the next start."""
    pod = provisioning_pod(tmp_path)
    pod.up()
    calls = pod.runner.calls
    order = [
        i
        for i, c in enumerate(calls)
        if c[:3] == ["docker", "network", "create"] and c[-1] == "vivibox-shop-1-i"
    ]
    assert len(order) == 1
    made = order[0]
    applied = [i for i, c in enumerate(calls) if c[:1] == ["sudo"] and c[3] == "apply-policy"]
    with_index = [i for i in applied if "vbx-shop-1-i" in calls[i][5] and '"present"' in calls[i][5]]
    run = calls.index(
        next(c for c in calls if c[:5] == ["docker", "run", "-d", "--name", pod.index_provision])
    )
    waited = calls.index(["docker", "wait", pod.index_provision])
    removed = max(i for i, c in enumerate(calls) if c == ["docker", "rm", "-f", pod.index_provision])
    net_gone = max(i for i, c in enumerate(calls) if c == ["docker", "network", "rm", "vivibox-shop-1-i"])
    rules_gone = [i for i in applied if '"absent"' in calls[i][5]]
    steady = calls.index(next(c for c in calls if c[:5] == ["docker", "run", "-d", "--name", pod.index]))
    assert made < with_index[0] < run < waited < removed < net_gone < rules_gone[-1] < steady
    present = json.loads(calls[with_index[0]][5])
    [index_net] = [n for n in present["networks"] if n["role"] == "i"]
    assert index_net["hostsvc"] == [] and index_net["subnet"] == "198.51.100.28/30"
    assert json.loads(calls[rules_gone[-1]][5])["networks"] == [{"role": "i", "bridge": "vbx-shop-1-i"}]
    assert "vivibox-shop-1-i" not in pod.runner.networks
    assert pod.index_provisioned.read_text() == json.dumps(list(PROVISIONING.provision))
    assert pod.index_failure == ""
    pod.runner.calls.clear()
    pod.down()
    pod.up()
    assert pod.index_provision not in starts(pod.runner.calls), "provisioned once"


@pytest.mark.parametrize("ending", ["exit", "time"])
def test_a_failed_provisioning_run_leaves_nothing_attached_and_no_index(tmp_path, ending, monkeypatch):
    pod = provisioning_pod(tmp_path)
    if ending == "exit":
        pod.runner.exits[pod.index_provision] = "1"
    else:
        real = pod.runner

        def late(cmd, timeout=None):
            if list(cmd)[:2] == ["docker", "wait"]:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return real(cmd)

        late.calls, late.find, late.states, late.networks = real.calls, real.find, real.states, real.networks
        pod.runner = late
    pod.up()  # the pod goes on
    assert pod.runner.states.get(pod.index_provision) is None
    assert "vivibox-shop-1-i" not in pod.runner.networks
    absent = [c for c in pod.runner.find("sudo") if c[3] == "apply-policy" and '"absent"' in c[5]]
    assert absent, "its rules gone"
    assert pod.index not in starts(pod.runner.calls)
    assert not pod.index_provisioned.exists()
    said = "exit status 1" if ending == "exit" else "ran out of time"
    assert said in pod.index_failure


def test_a_provisioning_run_left_by_a_vivibox_that_stopped_is_cleaned_up_first(tmp_path):
    pod = provisioning_pod(tmp_path)
    pod.ensure_networks()
    pod.runner.states[pod.index_provision] = "running"
    pod.runner.networks.add("vivibox-shop-1-i")
    pod.index_provisioned.parent.mkdir(parents=True, exist_ok=True)
    pod.up()
    calls = pod.runner.calls
    first_rm = calls.index(["docker", "rm", "-f", pod.index_provision])
    first_run = calls.index(
        next(c for c in calls if c[:5] == ["docker", "run", "-d", "--name", pod.index_provision])
    )
    assert first_rm < first_run, "the leftover gone before a run is made"
    assert pod.index_failure == ""


@pytest.mark.parametrize("how", ["down", "kill", "remove"])
def test_a_stop_of_the_pod_takes_the_provisioning_run_and_its_network(tmp_path, how):
    pod = provisioning_pod(tmp_path)
    pod.ensure_networks()
    pod.runner.states[pod.index_provision] = "running"
    pod.runner.networks.add("vivibox-shop-1-i")
    getattr(pod, how)()
    assert pod.runner.states.get(pod.index_provision) is None
    assert "vivibox-shop-1-i" not in pod.runner.networks


def test_a_task_allocated_before_the_index_had_a_range_is_not_provisioned(tmp_path):
    pod = provisioning_pod(tmp_path)
    pod.allocation = tmp_path / "network.json"
    old = [{"role": "w", "subnet": "198.51.100.0/28"}, {"role": "r", "subnet": "198.51.100.16/29"},
           {"role": "p", "subnet": "198.51.100.24/29"}]  # fmt: skip
    pod.allocation.write_text(json.dumps({"networks": old}))
    pod.up()
    assert not [c for c in pod.runner.find("docker", "network", "create") if c[-1] == "vivibox-shop-1-i"]
    assert pod.index_provision not in starts(pod.runner.calls) and pod.index not in starts(pod.runner.calls)
    assert "delete the task and create it again" in pod.index_failure
    assert "vivibox-shop-1-control" in starts(pod.runner.calls), "the rest of the pod starts"


def test_rules_that_cannot_be_loaded_start_no_provisioning_run(tmp_path):
    pod = provisioning_pod(tmp_path)
    pod.ensure_networks()
    pod.apply_firewall()
    real_apply = pod.apply_firewall

    def refusing(extra=()):
        if extra:
            raise PodError("the helper refused")
        return real_apply()

    pod.apply_firewall = refusing
    pod.up()
    assert pod.index_provision not in starts(pod.runner.calls)
    assert "vivibox-shop-1-i" not in pod.runner.networks
    assert "could not start" in pod.index_failure and pod.index not in starts(pod.runner.calls)


def what_the_run_meets(pod, monkeypatch, seen: dict) -> None:
    """pod's runner made to do, as the provisioning run's container starts, what the run does
    first, on the host's folders the run mounts: the real index_sync on its copy, from the
    host's view at SOURCE (as /source is in the run). seen: what the run found in its store and
    its copy before that, and the copy's .git configuration and commit after it."""
    real = pod.runner
    monkeypatch.setattr(index_sync, "SOURCE", str(pod.index_source))

    def runner(cmd, timeout=None):
        cmd = list(cmd)
        if cmd[:5] == ["docker", "run", "-d", "--name", pod.index_provision]:
            seen["store"] = sorted(p.name for p in pod.index_store.iterdir())
            seen["copy"] = sorted(p.name for p in pod.index_copy.iterdir())
            given = cmd[cmd.index("-c") + 2 :]  # after the script: the copy's path, the commit
            seen["synced"] = index_sync.sync(str(pod.index_copy), given[1])
            seen["config"] = (pod.index_copy / ".git" / "config").read_text()
            seen["head"] = git(pod.index_copy, "rev-parse", "HEAD")
        return real(cmd, timeout)

    for kept in ("calls", "find", "states", "networks"):
        setattr(runner, kept, getattr(real, kept))
    pod.runner = runner


@pytest.mark.parametrize("discarded", [True, False], ids=["product", "control-without-discard"])
def test_a_provisioning_run_never_meets_what_an_index_without_a_network_wrote(
    tmp_path, monkeypatch, discarded
):
    """The steady I, which has no network, keeps in its store what the planner asked it and
    leaves its copy's .git hostile: a filter git runs on a checkout. The provisioning command
    changes (a new configuration, a new vivibox), the pod starts again, and the run, which has
    the internet, starts on an empty store and an empty copy, and makes its copy from the host's
    view of the branch as it is now; the filter never runs. The control: the same run with the
    discard taken away meets every one of these, so the test sees them when they are there."""
    pod = provisioning_pod(tmp_path)
    pod.up()
    assert pod.index in starts(pod.runner.calls) and pod.index_failure == ""
    # What the steady I did: its copy made from the view (the real operation), then its traps.
    monkeypatch.setattr(index_sync, "SOURCE", str(pod.index_source))
    first = git(pod.index_source, "rev-parse", "HEAD")
    assert index_sync.sync(str(pod.index_copy), first) == 0
    (pod.index_store / "planner-asked").write_text("CANARY: find_symbol PaymentSecret\n")
    ran = tmp_path / "filter-ran"
    git(pod.index_copy, "config", "filter.i.smudge", f"sh -c 'echo ran >> {ran}; cat'")
    (pod.index_copy / ".git" / "info").mkdir(exist_ok=True)
    (pod.index_copy / ".git" / "info" / "attributes").write_text("* filter=i\n")
    (pod.index_copy / "README.md").unlink()
    # The trap is live: git on that copy runs I's command (the control of the filter).
    assert index_sync.sync(str(pod.index_copy), first) == 0 and ran.read_text() == "ran\n"
    ran.unlink()
    (pod.index_copy / "README.md").unlink()
    (pod.index_copy / "CANARY-untracked.txt").write_text("CANARY\n")
    if not discarded:
        monkeypatch.setattr(pod, "index_discard", lambda: None)
    now = commit(pod.repo, {"src/new.py": "new\n"})
    pod.down()
    pod.index_server = IndexServer(SERVER.mounts, SERVER.command, provision=(*PROVISIONING.provision, "--v2"))
    seen: dict = {}
    what_the_run_meets(pod, monkeypatch, seen)
    pod.up()
    assert seen, "the run started"
    if not discarded:
        assert "planner-asked" in seen["store"] and "CANARY-untracked.txt" in seen["copy"]
        assert ran.exists() and 'filter "i"' in seen["config"]
        return
    assert seen["store"] == [] and seen["copy"] == [], "the run starts on nothing I wrote"
    assert seen["synced"] == 0 and not ran.exists(), "I's filter never runs"
    assert "filter" not in seen["config"] and not (pod.index_copy / ".git" / "info" / "attributes").exists()
    assert seen["head"] == now == git(pod.index_source, "rev-parse", "HEAD"), "from the host's view as it is"
    assert not (pod.index_store / "planner-asked").exists()
    assert pod.index_failure == "" and pod.index_provisioned.read_text() == json.dumps(
        list(pod.index_server.provision)
    )


def test_the_pool_still_holds_eight_tasks_in_a_slash_24():
    """The index's range is cut from the planner's /29, which held one container: a task's
    block is still a /27, and the default pool holds as many tasks as before."""
    import ipaddress

    from vivibox.pod_network import ROLE_BITS, split_block

    block = ipaddress.IPv4Network("198.51.100.0/27")
    ranges = split_block(block)
    assert sum(r.num_addresses for r in ranges.values()) == 32 == block.num_addresses
    assert ROLE_BITS == {"w": 28, "r": 29, "p": 30, "i": 30}
    assert len(list(ipaddress.IPv4Network("198.51.100.0/24").subnets(new_prefix=27))) == 8


@pytest.mark.parametrize("trap", ["unreadable", "deep"])
def test_nothing_i_wrote_is_used_again_whatever_it_did_to_its_folders(tmp_path, trap):
    """I can make its folders hard to delete for the host, which runs as the same user: a folder
    with no permission at all, or one too deep to walk by recursion. The folder it wrote is set
    aside whole before it is deleted, so the next I starts on empty ones either way; and what can
    be deleted is."""
    pod = a_pod(tmp_path)
    pod.up()
    locked = pod.index_store / "locked"
    if trap == "unreadable":
        (locked / "inside").mkdir(parents=True)
        (locked / "inside" / "file").write_text("I's\n")
        locked.chmod(0)
    else:
        fd = os.open(pod.index_copy, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(5000):  # past both Python's recursion and PATH_MAX
            os.mkdir("d", dir_fd=fd)
            inner = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = inner
        os.close(fd)
    try:
        pod.index_discard()
    finally:
        if locked.exists():
            locked.chmod(0o700)
    assert list(pod.index_store.iterdir()) == [] and list(pod.index_copy.iterdir()) == []
    assert [p.name for p in pod.disk.iterdir() if "discarded" in p.name] == [], "deleted, once readable"


def test_nothing_is_deleted_while_a_container_of_is_is_still_there(tmp_path):
    pod = a_pod(tmp_path)
    pod.up()
    written_by_i(pod)
    real = pod.runner

    def refusing(cmd, timeout=None):
        if list(cmd)[:3] == ["docker", "rm", "-f"] and pod.index in cmd:
            real.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "the daemon said no")
        return real(cmd)

    refusing.calls, refusing.find, refusing.states = real.calls, real.find, real.states
    pod.runner = refusing
    with pytest.raises(PodError, match="could not be removed"):
        pod.index_discard()
    assert (pod.index_store / "cache" / "symbols").exists() and pod.index_provisioned.exists()


def test_a_folder_swapped_for_a_link_while_it_is_deleted_is_not_followed(tmp_path, monkeypatch):
    """Were I still writing (it is stopped first): an entry that was a folder when it was listed
    and is a link when it is entered is never changed through the link."""
    from vivibox import pod_index

    top = tmp_path / "aside"
    (top / "sub").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o750)
    real = pod_index._files_gone

    def swapping(fd):
        folders = real(fd)
        if "sub" in folders:
            os.rmdir("sub", dir_fd=fd)
            os.symlink(victim, "sub", dir_fd=fd)
        return folders

    monkeypatch.setattr(pod_index, "_files_gone", swapping)
    with pytest.raises(OSError):
        pod_index._delete(top)
    assert victim.stat().st_mode & 0o777 == 0o750 and victim.is_dir()


def test_what_could_not_be_deleted_is_set_aside_and_never_mounted_again(tmp_path, monkeypatch):
    from vivibox import pod_index

    pod = a_pod(tmp_path)
    pod.up()
    written_by_i(pod)

    def failing(top):
        raise OSError("an error of the disk's")

    monkeypatch.setattr(pod_index, "_delete", failing)
    with pytest.raises(PodError, match="could not all be deleted"):
        pod.index_discard()
    assert list(pod.index_store.iterdir()) == [] and list(pod.index_copy.iterdir()) == []
    [store] = pod.disk.glob(f"{podspec.INDEX_STORE}-discarded-*")
    [copy] = pod.disk.glob(f"{podspec.INDEX_COPY}-discarded-*")
    assert (store / "cache" / "symbols").exists() and (copy / ".git").exists(), "set aside whole"
    pod.runner.calls.clear()
    pod.up()
    [run] = [c for c in pod.runner.find("docker", "run", "-d", "--name") if c[4] == pod.index]
    assert not [b for b in sources(run) if "-discarded-" in b], "never mounted"
