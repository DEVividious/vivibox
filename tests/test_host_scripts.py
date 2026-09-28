import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOST = Path(__file__).parent.parent / "host"
HELPER = HOST / "vivibox-netns"


def helper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(HELPER), *args], capture_output=True, text=True)


def test_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "run 'uv sync' to install shellcheck-py"
    scripts = [HOST / "setup.sh", HOST / "uninstall.sh", HELPER]
    result = subprocess.run([shellcheck, "-x", *map(str, scripts)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("script", ["setup.sh", "uninstall.sh"])
def test_host_scripts_take_only_check(script):
    result = subprocess.run(["bash", str(HOST / script), "--undo"], capture_output=True, text=True)
    assert result.returncode == 2 and "usage" in result.stderr


def test_uninstall_undoes_every_step_setup_makes():
    """setup.sh's steps and uninstall.sh's removals are two lists kept by hand; a step added to
    one without the other would leave something behind. Packages and uv stay on purpose."""
    setup, uninstall = (HOST / "setup.sh").read_text(), (HOST / "uninstall.sh").read_text()
    steps = set(re.findall(r"^do_(\w+)\(\)", setup, re.MULTILINE)) - {"packages", "uv"}
    undone = set(re.findall(r"^do_(\w+)\(\)", uninstall, re.MULTILINE))
    assert steps <= undone, f"setup steps without a removal: {sorted(steps - undone)}"


@pytest.mark.parametrize(
    "args",
    [(), ("apply",), ("drop", "vivibox-shop-1-dind"), ("clear", "vivibox-shop-1-dind", "extra")],
)
def test_helper_rejects_bad_usage(args):
    assert helper(*args).returncode == 2


@pytest.mark.parametrize(
    "name",
    ["other", "vivibox-shop-dind", "vivibox-shop-1", "vivibox-Shop-1-dind", "vivibox-shop-1-dind;id", "-f"],
)
def test_helper_rejects_non_sidecar_names(name):
    result = helper("show", name)
    assert result.returncode == 1 and "not an vivibox sidecar" in result.stderr


@pytest.mark.parametrize(
    "target",
    [
        "10.0.0.1",
        "10.0.0.1:0",
        "10.0.0.1:65536",
        "256.0.0.1:80",
        "10.0.0:80",
        "host:80",
        "10.0.0.1:80;id",
        "::1:80",
    ],
)
def test_helper_rejects_bad_targets(target):
    result = helper("apply", "vivibox-shop-1-dind", "10.0.0.2:5432", target)
    assert result.returncode == 1 and "not <ipv4>:<port>" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root path")
def test_helper_validates_before_requiring_root():
    result = helper("apply", "vivibox-shop-1-dind", "172.20.0.1:5432", "192.168.1.10:8080")
    assert result.returncode == 1 and "must run as root" in result.stderr


@pytest.mark.parametrize(
    "pool",
    ["198.51.100.0", "198.51.100.0/", "198.51.100.0/31", "198.51.100.0/7", "256.0.0.0/24", "x/24"],
)
def test_helper_rejects_a_bad_pool(pool):
    result = helper("apply", "vivibox-shop-1-dind", "--pool", pool)
    assert result.returncode == 1 and "not an ipv4 network" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root path")
def test_helper_takes_a_pool_before_the_targets():
    result = helper("apply", "vivibox-shop-1-dind", "--pool", "198.51.100.0/24", "172.20.0.1:5432")
    assert result.returncode == 1 and "must run as root" in result.stderr, "validation got that far"


@pytest.mark.parametrize("mtu", ["1280x", "12", "70000", "-1280", ""])
def test_helper_rejects_a_bad_mtu(mtu):
    result = helper("apply", "vivibox-shop-1-dind", "--mtu", mtu)
    assert result.returncode == 1 and "not an mtu" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="checks the non-root path")
def test_helper_takes_an_mtu_with_the_pool_before_the_targets():
    result = helper(
        "apply", "vivibox-shop-1-dind", "--pool", "198.51.100.0/24", "--mtu", "1280", "172.20.0.1:5432"
    )
    assert result.returncode == 1 and "must run as root" in result.stderr, "validation got that far"


def setup_fn(call: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Calls one of setup.sh's functions: sourced, the script defines them and stops."""
    script = f'source "{HOST / "setup.sh"}"; {call}'
    run_env = {k: v for k, v in os.environ.items() if k != "VIVIBOX_DOCKER_RANGES"}
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env={**run_env, **(env or {})}
    )


@pytest.mark.parametrize(
    ("a", "b", "shared"),
    [
        ("172.20.0.0/16", "172.20.5.0/24", True),
        ("172.20.5.0/24", "172.20.0.0/16", True),
        ("172.16.0.0/12", "172.25.0.0/16", True),
        ("172.20.0.0/16", "172.21.0.0/16", False),
        ("172.20.0.0/16", "172.200.0.0/16", False),
        ("10.0.0.0/8", "10.203.0.0/16", True),
        ("172.20.0.0/16", "172.20.9.9", True),
        ("198.51.100.0/24", "198.51.101.0/24", False),
    ],
)
def test_ranges_overlap_by_their_addresses_not_their_prefix(a, b, shared):
    assert (setup_fn(f"overlaps {a} {b}").returncode == 0) is shared


def test_the_task_networks_in_use_are_not_routes_the_pool_collides_with(tmp_path):
    """setup.sh on a machine with a task: the pool overlapped the route to the task's own network,
    which is the pool in use. A task network's bridge is br-<network id>; those routes are left out.
    A Docker network of someone else's in the pool still counts."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ip").write_text(
        "#!/bin/sh\n"
        "echo 'default via 192.168.1.1 dev wlan0 proto dhcp metric 600'\n"
        "echo '192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.2'\n"
        "echo '198.51.100.0/28 dev br-bc2b47432840 proto kernel scope link src 198.51.100.1 linkdown'\n"
        "echo '198.51.100.16/28 dev br-0123456789ab proto kernel scope link src 198.51.100.17'\n"
    )
    (fake / "docker").write_text("#!/bin/sh\necho bc2b47432840\n")
    for script in ("ip", "docker"):
        (fake / script).chmod(0o755)
    result = setup_fn("routed", env={"PATH": f"{fake}:{os.environ['PATH']}"})
    assert result.stdout.split() == ["192.168.1.0/24", "198.51.100.16/28"], result.stderr


def test_routes_of_a_vpn_in_a_table_of_its_own_count_too(tmp_path):
    """The work laptop's case: Cloudflare WARP routes a corporate host in a table of its own, and setup.sh,
    reading the main table, gave Docker a pool that contains it. The local and broadcast entries
    of the local table are not destinations, and a half of the internet is a full tunnel's claim
    on everything, not a range in use."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ip").write_text(
        "#!/bin/sh\n"
        "echo 'default via 192.168.50.1 dev wlp0s20f3 proto dhcp metric 600'\n"
        "echo '192.168.50.0/24 dev wlp0s20f3 proto kernel scope link src 192.168.50.219'\n"
        "echo '0.0.0.0/1 dev CloudflareWARP table 65743 scope link'\n"
        "echo '128.0.0.0/1 dev CloudflareWARP table 65743 scope link'\n"
        "echo '172.25.0.10 dev CloudflareWARP table 65743 scope link'\n"
        "echo 'local 192.168.50.219 dev wlp0s20f3 table local proto kernel scope host src 192.168.50.219'\n"
        "echo 'broadcast 192.168.50.255 dev wlp0s20f3 table local proto kernel scope link"
        " src 192.168.50.219'\n"
    )
    (fake / "docker").write_text("#!/bin/sh\n")
    for script in ("ip", "docker"):
        (fake / script).chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}"}
    result = setup_fn("routed", env=env)
    assert result.stdout.split() == ["192.168.50.0/24", "172.25.0.10"], result.stderr
    ranges = setup_fn("pick_docker_ranges $(routed)", env=env)
    assert "172.25.0.0/16" not in ranges.stdout.split(), "the pool with the VPN's host is not free"


def test_docker_ranges_are_the_usual_ones_when_nothing_routes_them():
    result = setup_fn("pick_docker_ranges 192.168.1.0/24 198.51.100.0/24")
    assert result.stdout.split() == ["172.20.0.0/16", "172.25.0.0/16"]


def test_docker_ranges_step_around_a_vpn_and_docker_networks():
    routes = "172.20.0.0/16 172.16.0.0/14 172.24.0.0/13 10.0.0.0/8"
    result = setup_fn(f"pick_docker_ranges {routes}")
    assert result.stdout.split() == ["172.21.0.0/16", "172.22.0.0/16"], result.stderr


def test_docker_ranges_are_refused_when_none_is_free():
    result = setup_fn("pick_docker_ranges 172.16.0.0/12 10.0.0.0/8")
    assert result.returncode == 1 and "set VIVIBOX_DOCKER_RANGES" in result.stderr


def test_docker_ranges_can_be_chosen_and_are_still_checked():
    chosen = {"VIVIBOX_DOCKER_RANGES": "10.77.0.0/16 10.78.0.0/16"}
    assert setup_fn("pick_docker_ranges 172.20.0.0/16", chosen).stdout.split() == [
        "10.77.0.0/16",
        "10.78.0.0/16",
    ]
    taken = setup_fn("pick_docker_ranges 10.0.0.0/8", chosen)
    assert taken.returncode == 1 and "10.77.0.0/16 overlaps the route to 10.0.0.0/8" in taken.stderr


@pytest.mark.parametrize(
    ("release", "shiftfs", "runs"),
    [
        ("7.0.0-31-generic", "no", True),
        ("6.8.0-45-generic", "no", True),
        ("5.15.0-122-generic", "no", True),
        ("5.12.0", "no", True),
        ("5.11.0-27-generic", "no", False),
        ("5.11.0-27-generic", "yes", True),
        ("5.4.0-200-generic", "yes", False),
        ("4.18.0-553.el8_10.x86_64", "no", False),
    ],
)
def test_the_kernel_says_whether_vivibox_can_run(release, shiftfs, runs):
    result = setup_fn(f"kernel_verdict {release} {shiftfs}")
    assert (result.returncode == 0) is runs
    said = result.stdout if runs else result.stderr
    assert f"kernel {release}: vivibox {'can' if runs else 'cannot'} run" in said


@pytest.mark.parametrize("installed", [False, True])
def test_setup_checks_git_like_other_packages(tmp_path, installed):
    result, calls = run_setup_without_host_changes(tmp_path, check=True, installed=installed)
    assert result.returncode == 1, "the fake host has other missing setup steps"
    assert ("missing  packages: git" in result.stdout) is (not installed)
    if installed:
        assert "ok       packages: git tmux" in result.stdout
    assert "apt-get" not in calls, "--check never installs packages"


def test_setup_installs_missing_git_after_confirmation(tmp_path):
    result, calls = run_setup_without_host_changes(tmp_path, check=False, installed=False)
    assert result.returncode == 31, "the fake sudo stops after recording the package installation"
    assert "apt-get install -y -q git" in calls


def run_setup_without_host_changes(tmp_path, *, check, installed):
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "sudo-calls"
    scripts = {
        "docker": "echo sysbox-runc",
        "ip": "exit 0",
        "modinfo": "exit 0",
        "dpkg-query": (
            "for arg do\n"
            '  if [ "$arg" = git ] && [ "$TEST_GIT_INSTALLED" = no ]; then exit 1; fi\n'
            'done\necho "install ok installed"'
        ),
        "sudo": 'echo "$*" >> "$TEST_SUDO_CALLS"\n[ "$1" != apt-get ] || exit 31',
        "uv": 'echo "$TEST_TOOL_DIR"',
    }
    for name, body in scripts.items():
        path = fake / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake}:{os.environ['PATH']}",
        "VIVIBOX_TASKS_DIR": str(tmp_path / "tasks"),
        "TEST_GIT_INSTALLED": "yes" if installed else "no",
        "TEST_SUDO_CALLS": str(calls),
        "TEST_TOOL_DIR": str(tmp_path / "tools"),
    }
    result = subprocess.run(
        ["bash", str(HOST / "setup.sh"), *(["--check"] if check else [])],
        input="y\n",
        capture_output=True,
        text=True,
        env=env,
    )
    return result, calls.read_text() if calls.exists() else ""


def test_setup_installs_the_skill_where_an_agent_cli_is_and_leaves_one_not_its_own(tmp_path):
    """A copy, so a checkout that moved on leaves it behind: --check says so, and the step
    installs it again. A skill of that name vivibox did not install is someone else's."""
    home = tmp_path / "home"
    home.mkdir()
    stale = lambda: setup_fn("skill_to_install", {"HOME": str(home)}).stdout.split()  # noqa: E731
    assert stale() == [], "no agent CLI here: nothing to install"
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    claude, codex = home / ".claude" / "skills" / "vivibox", home / ".agents" / "skills" / "vivibox"
    assert stale() == [str(claude), str(codex)]
    source = (REPO / "vivibox" / "skills" / "vivibox" / "SKILL.md").read_text()
    for copy in (claude, codex):
        copy.mkdir(parents=True)
        (copy / "SKILL.md").write_text(source)
        (copy / ".vivibox-version").write_text("0.1.0\n")
    assert stale() == []
    (codex / "SKILL.md").write_text("an older skill")
    assert stale() == [str(codex)]
    (codex / ".vivibox-version").unlink()
    assert stale() == [], "not vivibox's copy: left alone"


def test_uninstall_removes_only_the_skill_copies_vivibox_installed(tmp_path):
    home = tmp_path / "home"
    ours, theirs = home / ".claude" / "skills" / "vivibox", home / ".agents" / "skills" / "vivibox"
    for copy in (ours, theirs):
        copy.mkdir(parents=True)
        (copy / "SKILL.md").write_text("skill")
    (ours / ".vivibox-version").write_text("0.1.0\n")
    script = f'source "{HOST / "uninstall.sh"}"; skill_copies'
    found = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "HOME": str(home)}
    )
    assert found.stdout.split() == [str(ours)]


def test_the_scripts_install_and_remove_the_skill_where_vivibox_does():
    from vivibox import skill

    wanted = [f"{place.home} {place.skills}" for place in skill.PLACES]
    setup = re.search(r"^SKILL_PLACES=\((.*)\)$", (HOST / "setup.sh").read_text(), re.MULTILINE)
    assert setup and re.findall(r'"([^"]+)"', setup.group(1)) == wanted
    uninstall = re.search(r"^SKILL_PLACES=\((.*)\)$", (HOST / "uninstall.sh").read_text(), re.MULTILINE)
    assert uninstall and re.findall(r'"([^"]+)"', uninstall.group(1)) == wanted
