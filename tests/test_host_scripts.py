import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HOST = Path(__file__).parent.parent / "host"
HELPER = HOST / "vivibox-host"


def test_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "run 'uv sync' to install shellcheck-py"
    scripts = [HOST / "setup.sh", HOST / "uninstall.sh"]
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
    which is the pool in use. A task network's bridge is vbx… (br-<network id> before); those
    routes are left out. A Docker network of someone else's in the pool still counts."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ip").write_text(
        "#!/bin/sh\n"
        "echo 'default via 192.168.1.1 dev wlan0 proto dhcp metric 600'\n"
        "echo '192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.2'\n"
        "echo '198.51.100.0/28 dev br-bc2b47432840 proto kernel scope link src 198.51.100.1 linkdown'\n"
        "echo '198.51.100.16/28 dev br-0123456789ab proto kernel scope link src 198.51.100.17'\n"
        "echo '198.51.100.32/28 dev vbx-shop-1-w proto kernel scope link src 198.51.100.33'\n"
        "echo '198.51.100.48/28 dev vbx0123456789-w proto kernel scope link src 198.51.100.49'\n"
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


def run_setup_without_host_changes(
    tmp_path,
    *,
    check,
    installed,
    routes="",
    networks="",
    config="",
    home=None,
    pods="",
    engine="",
    path=None,
    fakes=None,
):
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "sudo-calls"
    scripts = {
        "docker": (
            'case "$*" in *--no-trunc*) printf "%s" "$TEST_NETWORKS" ;;\n'
            '  ps*) printf "%s" "$TEST_PODS" ;;\n'
            "  network*) ;;\n  *) echo sysbox-runc ;;\nesac"
        ),
        # `ip -4 route show table all`, and `docker network ls` for the name of a bridge's network.
        "ip": 'printf "%s" "$TEST_ROUTES"',
        "modinfo": "exit 0",
        "dpkg-query": (
            "for arg do\n"
            '  if [ "$arg" = git ] && [ "$TEST_GIT_INSTALLED" = no ]; then exit 1; fi\n'
            'done\necho "install ok installed"'
        ),
        "sudo": 'echo "$*" >> "$TEST_SUDO_CALLS"\n[ "$1" != apt-get ] || exit 31',
        "uv": 'echo "$TEST_TOOL_DIR"',
    }
    if engine:
        # The checkout's helper, run for its check of the host's address: what it says, else Python.
        scripts["python3"] = (
            f'case "$*" in *prepare-engine*) echo "{engine}" >&2; exit 1 ;; esac\nexec {sys.executable} "$@"'
        )
    scripts.update(fakes or {})
    for name, body in scripts.items():
        script = fake / name
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake}:{path if path is not None else os.environ['PATH']}",
        # Yours only when a test gives none: the skill's step reads the agent CLIs' folders.
        "HOME": str(home or os.environ["HOME"]),
        "VIVIBOX_TASKS_DIR": str(tmp_path / "tasks"),
        "TEST_GIT_INSTALLED": "yes" if installed else "no",
        "TEST_SUDO_CALLS": str(calls),
        "TEST_TOOL_DIR": str(tmp_path / "tools"),
        "TEST_ROUTES": routes,
        "TEST_NETWORKS": networks,
        "TEST_PODS": pods,
        "VIVIBOX_CONFIG_DIR": str(tmp_path / "config"),
    }
    if config:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "config.toml").write_text(config)
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
    assert stale() == ["missing", str(claude), "missing", str(codex)]
    source = (REPO / "vivibox" / "skills" / "vivibox" / "SKILL.md").read_text()
    for copy in (claude, codex):
        copy.mkdir(parents=True)
        (copy / "SKILL.md").write_text(source)
        (copy / ".vivibox-version").write_text("0.1.0\n")
    assert stale() == []
    (codex / "SKILL.md").write_text("an older skill")
    assert stale() == ["older", str(codex)], "there, but not this checkout's: an update"
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


SPIKE = "198.51.100.240/28 dev br-d9c3d14812d4 proto kernel scope link src 198.51.100.241 linkdown\n"


def test_a_taken_task_pool_is_named_and_the_check_goes_on(tmp_path):
    """A range something else routes is for you to fix, not for setup to stop at: the rest of the
    checks still say what they found, and the network that holds the range is named."""
    result, _ = run_setup_without_host_changes(
        tmp_path, check=True, installed=True, routes=SPIKE, networks="d9c3d14812d4 spike-net\n"
    )
    assert result.returncode == 1
    line = next(line for line in result.stdout.splitlines() if "198.51.100.240/28" in line)
    assert line.startswith("  problem") and "Docker network spike-net" in line
    assert "network.pool" in line
    assert "vivibox command" in result.stdout, "the checks after it ran"


def test_setup_checks_the_task_pool_config_toml_sets(tmp_path):
    """The advice is network.pool in config.toml; a check that ignored it would refuse forever."""
    result, _ = run_setup_without_host_changes(
        tmp_path, check=True, installed=True, routes=SPIKE, config='[network]\npool = "10.123.0.0/24"\n'
    )
    assert "ok       task network pool 10.123.0.0/24 is free" in result.stdout
    assert "problem" not in result.stdout


@pytest.mark.parametrize(
    "config,pool",
    [
        ("", "198.51.100.0/24"),
        ('[network]\npool = "10.1.0.0/24"\n', "10.1.0.0/24"),
        ("[network]\n# a comment\npool = '10.2.0.0/24'  # mine\n", "10.2.0.0/24"),
        ('[review]\npool = "no"\n[network]\nmtu = 1280\n', "198.51.100.0/24"),
        ('network.pool = "10.3.0.0/24"\n', "10.3.0.0/24"),
    ],
)
def test_the_task_pool_is_read_as_vivibox_reads_it(tmp_path, config, pool):
    (tmp_path / "config.toml").write_text(config)
    assert setup_fn("task_pool", {"VIVIBOX_CONFIG_DIR": str(tmp_path)}).stdout.strip() == pool


@pytest.mark.parametrize(("there", "state"), [(None, "missing"), ("old", "older"), ("new", "same")])
def test_a_copy_is_missing_older_or_the_same(tmp_path, there, state):
    source, copy = tmp_path / "source", tmp_path / "copy"
    source.write_text("new")
    if there:
        copy.write_text(there)
    assert setup_fn(f'copy_state "{source}" "{copy}"').stdout.strip() == state


def test_what_is_there_but_older_is_an_update_not_missing(tmp_path):
    home = tmp_path / "home"
    copy = home / ".claude" / "skills" / "vivibox"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("an older skill")
    (copy / ".vivibox-version").write_text("0.1.0\n")
    result, _ = run_setup_without_host_changes(tmp_path, check=False, installed=True, home=home)
    assert f"  update   the vivibox skill in {copy}" in result.stdout
    assert f"  - update the vivibox skill in {copy}" in result.stdout, "the list of changes says so too"


def test_a_vivibox_command_from_another_checkout_is_an_update(tmp_path):
    receipt = tmp_path / "tools" / "vivibox" / "uv-receipt.toml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('requirements = [{ name = "vivibox", editable = "/elsewhere/vivibox" }]\n')
    result, _ = run_setup_without_host_changes(tmp_path, check=True, installed=True, home=tmp_path / "home")
    assert (
        "  update   vivibox command, installed from /elsewhere/vivibox, from this checkout" in result.stdout
    )


def test_a_fresh_local_bin_is_explained_with_the_command_for_this_terminal(tmp_path):
    """Ubuntu puts ~/.local/bin on PATH at the next login only: setup says so, and gives the line
    that makes vivibox work in the terminal it ran in."""
    home = tmp_path / "home"
    missing = setup_fn(f'HOME="{home}"; BIN="{home}/.local/bin"; PATH=/usr/bin:/bin; path_hint')
    lines = missing.stdout.splitlines()
    assert (
        lines[0]
        == f"{home}/.local/bin is not on your PATH yet: " + "log out and in, or open a new login shell."
    )
    assert lines[1] == 'To use vivibox in this terminal now, run: export PATH="$HOME/.local/bin:$PATH"'
    there = setup_fn(f'BIN="{home}/.local/bin"; PATH="{home}/.local/bin:/usr/bin:/bin"; path_hint')
    assert there.stdout == "" and there.returncode == 0


def fake_docker(tmp_path, where: str, context: str = "default", info_ok: bool = True):
    """A docker command in a folder of the test's own, as the snap or Docker Desktop would give."""
    bin_dir = tmp_path / where
    bin_dir.mkdir(parents=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'[ "$1 $2" = "context show" ] && echo {context} && exit 0\n'
        f'[ "$1" = "info" ] && exit {0 if info_ok else 1}\n'
        "exit 0\n"
    )
    docker.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize(
    "where, context, info_ok, said",
    [
        ("usr/bin", "default", True, ""),
        ("snap/bin", "default", True, "snap"),
        ("usr/bin", "desktop-linux", True, "Docker Desktop"),
        ("usr/bin", "default", False, "docker group"),
    ],
)
def test_setup_says_which_docker_cannot_run_sysbox(tmp_path, where, context, info_ok, said):
    """Ubuntu offers `snap install docker` where docker is missing, and Docker Desktop runs its
    engine in a VM of its own: Sysbox runs under neither, and setup said so only when it failed."""
    bin_dir = fake_docker(tmp_path, where, context, info_ok)
    found = setup_fn(f'SNAP_ROOT="{tmp_path / "snap"}"; PATH="{bin_dir}:/usr/bin:/bin"; docker_problem')
    if said:
        assert said in found.stdout and "https://docs.docker.com/engine/install/" in found.stdout
    else:
        assert found.stdout == ""


def test_setup_says_where_to_get_docker_when_there_is_none(tmp_path):
    found = setup_fn(f'PATH="{tmp_path}"; docker_problem')
    assert "Docker is not installed" in found.stdout
    assert "https://docs.docker.com/engine/install/ubuntu/" in found.stdout


# --- the root helper, the host's address and tasks from before ------------------------------------


def test_setup_refuses_without_nft_or_python3(tmp_path):
    """The root helper is Python, and loads the host's rules with nft: setup says so, and what to
    install, before anything else."""
    nowhere = setup_fn(f'PATH="{tmp_path}"; required_tools')
    assert nowhere.returncode == 1
    assert "nft" in nowhere.stdout and "python3" in nowhere.stdout and "apt-get install" in nowhere.stdout
    for tool in ("nft", "python3"):
        (tmp_path / tool).write_text("#!/bin/sh\n")
        (tmp_path / tool).chmod(0o755)
    there = setup_fn(f'PATH="{tmp_path}"; required_tools')
    assert there.returncode == 0 and there.stdout == ""


def test_setup_check_stops_where_nft_is_missing(tmp_path):
    # /usr/bin and /bin have the shell's tools; nft lives in /usr/sbin, which is left out.
    result, calls = run_setup_without_host_changes(tmp_path, check=True, installed=True, path="/usr/bin:/bin")
    assert result.returncode == 1
    assert "nft" in result.stderr and "nftables" in result.stderr, result.stderr
    assert "Checking host" not in result.stdout and calls == ""


def test_setup_installs_the_new_helper_and_its_rule_and_drops_the_old(tmp_path):
    result, calls = run_setup_without_host_changes(tmp_path, check=True, installed=True)
    assert "/usr/local/libexec/vivibox-host" in result.stdout
    setup = (HOST / "setup.sh").read_text()
    assert "HELPER=/usr/local/libexec/vivibox-host" in setup
    assert '"$HERE/vivibox-host" "$HELPER"' in setup, "the checkout's helper is what is installed"
    assert "vivibox-netns" in setup and "do_old_helper" in setup, "the old one goes"
    assert not (HOST / "vivibox-netns").exists()


@pytest.mark.parametrize(
    ("config", "address"),
    [
        ("", "203.0.113.1/32"),
        ('[network]\nhost_address = "10.250.0.1/32"\n', "10.250.0.1/32"),
        ('[network]\nhost_address = "10.250.0.2"\n', "10.250.0.2/32"),
        ('network.host_address = "10.250.0.3"\n', "10.250.0.3/32"),
    ],
)
def test_the_host_address_is_read_as_vivibox_reads_it(tmp_path, config, address):
    (tmp_path / "config.toml").write_text(config)
    assert setup_fn("host_address", {"VIVIBOX_CONFIG_DIR": str(tmp_path)}).stdout.strip() == address


def test_a_host_address_another_interface_holds_is_named_and_nothing_changes(tmp_path):
    said = "vivibox-host: network.host_address 203.0.113.1 is already an address of eth1. Nothing was changed"
    result, calls = run_setup_without_host_changes(tmp_path, check=False, installed=True, engine=said)
    line = next(line for line in result.stdout.splitlines() if "already an address of eth1" in line)
    assert line.startswith("  problem") and "network.host_address" in line
    assert "prepare-engine" not in calls, "nothing run as root for it"


def test_setup_lists_the_tasks_from_before_whose_pods_still_run(tmp_path):
    tasks = tmp_path / "tasks"
    for task_id, version in (("shop-1", 2), ("shop-2", None), ("shop-3", 999)):
        meta = tasks / task_id / ".task"
        meta.mkdir(parents=True)
        state = {} if version is None else {"task_schema_version": version}
        (meta / "state.json").write_text(json.dumps(state))
    current = re.search(r"^TASK_SCHEMA_VERSION = (\d+)$", (REPO / "vivibox" / "task.py").read_text(), re.M)
    (tasks / "shop-3" / ".task" / "state.json").write_text(
        json.dumps({"task_schema_version": int(current[1])})
    )
    result, _ = run_setup_without_host_changes(
        tmp_path, check=True, installed=True, pods="shop-1\nshop-2\nshop-3\nshop-1\ngone-9\n"
    )
    problems = [line for line in result.stdout.splitlines() if line.startswith("  problem")]
    for task_id in ("shop-1", "shop-2"):
        line = next(p for p in problems if f" {task_id} " in p or p.endswith(task_id))
        assert "earlier vivibox" in line and f"vivibox delete {task_id}" in line
    assert not any("shop-3" in p for p in problems), "a task of today's layout is fine"
    gone = next(p for p in problems if "gone-9" in p)
    assert "docker rm -f" in gone
    assert sum("shop-1" in p for p in problems) == 1, "each task once"


def test_uninstall_removes_the_hosts_rules_and_address():
    uninstall = (HOST / "uninstall.sh").read_text()
    body = uninstall[uninstall.index("do_engine() {") :]
    body = body[: body.index("\n}\n")]
    assert "sudo nft delete table inet vivibox" in body and "sudo ip link del vivibox0" in body
    assert "pid=$(hostd_pid)" in body, "the forwarder stops too, and only the forwarder"
    assert "HELPER=/usr/local/libexec/vivibox-host" in uninstall and "vivibox-netns" in uninstall


def engine_fakes(tmp_path, *, has_address: bool, refused: str = "") -> dict:
    """ip and python3 as setup.sh asks them about the host's address for the pods."""
    fake = tmp_path / "fakes"
    fake.mkdir(exist_ok=True)
    listing = "5: vivibox0    inet 203.0.113.1/32 scope global vivibox0" if has_address else ""
    (fake / "ip").write_text(f"#!/bin/sh\necho '{listing}'\n")
    if refused:
        (fake / "python3").write_text(f'#!/bin/sh\necho "{refused}" >&2\nexit 1\n')
    else:
        (fake / "python3").write_text("#!/bin/sh\nexit 0\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    return {"PATH": f"{fake}:{os.environ['PATH']}"}


def report_engine(tmp_path, ready: str, **fakes) -> subprocess.CompletedProcess:
    env = engine_fakes(tmp_path, **fakes)
    return setup_fn(f'report_engine 203.0.113.1/32 198.51.100.0/24 {ready}; echo "todo=${{#todo[@]}}"', env)


def test_after_a_reboot_the_missing_interface_is_said_and_is_nothing_to_do(tmp_path):
    """vivibox0 and the table are gone after a reboot; the next pod start makes them again with
    the installed helper. --check says so and still finds nothing to do."""
    done = report_engine(tmp_path, "yes", has_address=False)
    assert "  info     " in done.stdout and "next pod start" in done.stdout
    assert done.stdout.strip().endswith("todo=0"), done.stdout + done.stderr


def test_without_the_helper_the_interface_is_a_step_to_take(tmp_path):
    done = report_engine(tmp_path, "no", has_address=False)
    assert "  missing  host address for the pods 203.0.113.1/32" in done.stdout
    assert done.stdout.strip().endswith("todo=1"), done.stdout + done.stderr


def test_the_interface_with_its_address_is_ok(tmp_path):
    done = report_engine(tmp_path, "no", has_address=True)
    assert "  ok       host address for the pods 203.0.113.1/32 on vivibox0" in done.stdout
    assert done.stdout.strip().endswith("todo=0")


def test_an_address_the_helper_refuses_is_a_problem_however_ready(tmp_path):
    said = "vivibox-host: network.host_address 203.0.113.1 is already an address of eth1"
    done = report_engine(tmp_path, "yes", has_address=False, refused=said)
    assert "  problem  network.host_address 203.0.113.1 is already an address of eth1" in done.stdout
    assert done.stdout.strip().endswith("todo=0")


def uninstall_fn(call: str, env: dict) -> subprocess.CompletedProcess:
    script = f'source "{HOST / "uninstall.sh"}"; {call}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, **env})


@pytest.mark.parametrize(
    ("argv", "ours"), [(["vivibox", "hostd", "run"], True), (["something", "else"], False)]
)
def test_uninstall_stops_only_a_process_that_is_the_forwarder(tmp_path, argv, ours):
    """A pid file left from before names a pid another process may have taken since."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *argv])
    try:
        (tmp_path / "vivibox").mkdir()
        (tmp_path / "vivibox" / "hostd.pid").write_text(str(process.pid))
        found = uninstall_fn("hostd_pid", {"XDG_RUNTIME_DIR": str(tmp_path)}).stdout.strip()
        assert found == (str(process.pid) if ours else "")
    finally:
        process.kill()
        process.wait()


def test_setup_check_asks_for_the_interface_while_the_helper_is_not_ready(tmp_path):
    """The whole script: with the helper not installed as it must be (here, not root's 755), no
    vivibox0 is a step to take, not a line that a pod's start will make it."""
    result, calls = run_setup_without_host_changes(
        tmp_path, check=True, installed=True, routes="default via 192.168.1.1 dev wlan0\n",
        fakes={
            "python3": f'case "$*" in *prepare-engine*) exit 0 ;; esac\nexec {sys.executable} "$@"',
            "stat": 'case "$*" in *libexec/vivibox-host*) echo "root 644" ;;\n'
            '  *) exec /usr/bin/stat "$@" ;;\nesac',
        },
    )  # fmt: skip
    assert "  missing  host address for the pods 203.0.113.1/32 on vivibox0" in result.stdout, result.stdout
    assert "  info     host address" not in result.stdout
    assert result.returncode == 1 and "prepare-engine" not in calls, "--check changes nothing"
