"""host/vivibox-host, the only code vivibox runs as root: a closed list of operations, every
argument validated before anything privileged runs, nothing run through a shell, and the nft
rules it loads written by itself from structured input (ADR-0048). Tested without root: the
helper is loaded as a module with a fake runner, or run with --dry-run, which prints what it
would load and touches nothing."""

import copy
import ipaddress
import json
import os
import random
import re
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from vivibox import names

HOST = Path(__file__).parent.parent / "host"
HELPER = HOST / "vivibox-host"
GOLDEN = Path(__file__).parent / "fixtures" / "host-policy.nft"


def load():
    """The helper as a module, compiled from its file: no bytecode is left beside it in host/."""
    module = types.ModuleType("vivibox_host")
    exec(compile(HELPER.read_text(), str(HELPER), "exec"), module.__dict__)
    return module


helper = load()

POLICY = {
    "state": "present",
    "networks": [{"role": "w", "bridge": "vbx-shop-1-w", "subnet": "198.51.100.0/28", "policy": "open"}],
    "pool": "198.51.100.0/24",
    "mtu": 1500,
    "hostsvc": ["198.51.100.1:5432", "203.0.113.1:5055", "192.168.1.50:8080"],
    "dns": "203.0.113.1",
    "dns_port": 5053,
}
ENGINE = {"host_address": "203.0.113.1/32", "pool": "198.51.100.0/24"}


class Host:
    """The machine as the helper sees it: what `ip -j` says, and every command it is given."""

    def __init__(self, addr=None, route=None, nft_fails=False):
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.addr = addr if addr is not None else [LO, WLAN]
        self.route = route if route is not None else ROUTES
        self.nft_fails = nft_fails

    def __call__(self, cmd, input=None):
        self.calls.append(list(cmd))
        self.inputs.append(input)
        out, rc, err = "", 0, ""
        if cmd[:3] == ["ip", "-j", "-d"] and "addr" in cmd:
            out = json.dumps(self.addr)
        elif cmd[:2] == ["ip", "-j"] and "route" in cmd:
            out = json.dumps(self.route)
        elif cmd[:1] == ["nft"] and self.nft_fails:
            rc, err = 1, "Error: Could not process rule: No such file or directory"
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def privileged(self):
        """Every call that changes something: all of them but the two that read the machine."""
        return [c for c in self.calls if not (c[:2] == ["ip", "-j"] or c[:3] == ["ip", "-j", "-d"])]


LO = {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]}
WLAN = {"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.1.2", "prefixlen": 24}]}
ROUTES = [
    {"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0"},
    {"dst": "192.168.1.0/24", "dev": "wlan0"},
    {"dst": "0.0.0.0/1", "dev": "CloudflareWARP", "table": "65743"},
    {"type": "local", "dst": "192.168.1.2", "dev": "wlan0", "table": "local"},
]


def call(*argv, host=None, root=True):
    host = host or Host()
    code = helper.main(list(argv), run=host, euid=0 if root else 1000)
    return code, host


def dry(*argv) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HELPER), "--dry-run", *argv], capture_output=True, text=True)


def policy(**changes):
    p = copy.deepcopy(POLICY)
    p.update(changes)
    return json.dumps(p)


def rules(text: str, chain: str) -> list[str]:
    """The rules a file adds to one chain, in order, without the chain's prefix."""
    prefix = f"add rule inet vivibox {chain} "
    return [line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)]


# --- the policy it writes -------------------------------------------------------------------------


def test_the_dry_run_prints_the_rules_without_root():
    done = dry("apply-policy", "shop-1", json.dumps(POLICY))
    assert done.returncode == 0, done.stderr
    assert done.stdout == GOLDEN.read_text(), "the golden file is the reviewed policy"


def test_the_pods_traffic_out_is_policed_on_the_host_by_its_bridge():
    text = helper.policy_file("shop-1", helper.parse_policy("shop-1", json.dumps(POLICY)))
    out = rules(text, "out_vbx_shop_1_w")
    assert out[0] == "tcp flags & (syn | rst) == syn tcp option maxseg size set rt mtu"
    assert out[1] == "ct state established,related accept"
    assert "meta nfproto ipv6 reject with icmpx admin-prohibited" in out
    assert "ip saddr != 198.51.100.0/28 reject with icmpx admin-prohibited" in out, "its own subnet only"
    allowed = out.index("ip daddr 198.51.100.1 tcp dport 5432 accept")
    assert out.index("ip daddr 192.168.1.50 tcp dport 8080 accept") < out.index(
        next(r for r in out if "10.0.0.0/8" in r)
    )
    denied = next(i for i, r in enumerate(out) if "10.0.0.0/8" in r)
    assert allowed < denied, "a host service before the private ranges are refused"
    refused = out[denied]
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "198.51.100.0/24",
    ):
        assert net in refused, f"{net} refused"
    assert "meta l4proto tcp reject with tcp reset" in refused, "a refusal a client sees at once"
    assert out.index("meta l4proto udp reject with icmpx admin-prohibited") < len(out) - 1
    assert out[-1] == "accept", "the internet open: no allowlist yet"


def test_dns_from_the_pod_is_turned_to_the_forwarder_whatever_it_asks():
    text = helper.policy_file("shop-1", helper.parse_policy("shop-1", json.dumps(POLICY)))
    assert rules(text, "dns_vbx_shop_1_w") == [
        "udp dport 53 dnat ip to 203.0.113.1:5053",
        "tcp dport 53 dnat ip to 203.0.113.1:5053",
    ]
    host = rules(text, "host_vbx_shop_1_w")
    assert host.index("ip daddr 203.0.113.1 udp dport 5053 accept") < host.index(
        "reject with icmpx admin-prohibited"
    )
    assert "ip daddr 203.0.113.1 tcp dport 5053 accept" in host
    assert "ip daddr 198.51.100.1 tcp dport 5432 accept" in host, "a host service on the bridge's gateway"
    assert host[-2:] == ["meta l4proto tcp reject with tcp reset", "reject with icmpx admin-prohibited"]


def test_the_base_chains_come_before_dockers_and_refuse_a_bridge_without_a_policy():
    text = helper.policy_file("shop-1", helper.parse_policy("shop-1", json.dumps(POLICY)))
    assert "add chain inet vivibox forward { type filter hook forward priority -10; policy accept; }" in text
    assert "add chain inet vivibox input { type filter hook input priority -10; policy accept; }" in text
    assert (
        "add chain inet vivibox prerouting { type nat hook prerouting priority -110; policy accept; }" in text
    )
    assert rules(text, "forward") == [
        "iifname vmap @out_by_bridge",
        "oifname vmap @back_by_bridge",
        'iifname "vbx*" reject with icmpx admin-prohibited',
    ]
    assert rules(text, "input") == [
        "iifname vmap @host_by_bridge",
        'iifname "vbx*" reject with icmpx admin-prohibited',
        # Only the pods and the host itself reach its address for the pods: not a LAN host that
        # routes it here. Every task bridge is decided above, so only lo is left to let through.
        'ip daddr 203.0.113.1 iifname != "lo" drop',
    ]
    assert 'add element inet vivibox out_by_bridge { "vbx-shop-1-w" : jump out_vbx_shop_1_w }' in text


def test_a_tunnel_smaller_than_ethernet_clamps_to_its_mtu_both_ways():
    text = helper.policy_file("shop-1", helper.parse_policy("shop-1", policy(mtu=1280)))
    assert (
        rules(text, "out_vbx_shop_1_w")[0] == "tcp flags & (syn | rst) == syn tcp option maxseg size set 1240"
    )
    assert rules(text, "back_vbx_shop_1_w") == [
        "tcp flags & (syn | rst) == syn tcp option maxseg size set 1240"
    ]


def test_every_role_network_of_a_task_refuses_every_other():
    networks = [
        {"role": "w", "bridge": "vbx-shop-1-w", "subnet": "198.51.100.0/28", "policy": "open"},
        {"role": "r", "bridge": "vbx-shop-1-r", "subnet": "198.51.100.16/28", "policy": "open"},
        {"role": "p", "bridge": "vbx-shop-1-p", "subnet": "198.51.100.32/28", "policy": "open"},
    ]
    text = helper.policy_file("shop-1", helper.parse_policy("shop-1", policy(networks=networks)))
    for me in networks:
        out = rules(text, f"out_{me['bridge'].replace('-', '_')}")
        for other in networks:
            if other is not me:
                assert f"ip daddr {other['subnet']} meta l4proto tcp reject with tcp reset" in out
                assert f"ip daddr {other['subnet']} reject with icmpx admin-prohibited" in out


def test_the_same_input_writes_the_same_file():
    networks = [
        {"role": "r", "bridge": "vbx-shop-1-r", "subnet": "198.51.100.16/28", "policy": "open"},
        {"role": "w", "bridge": "vbx-shop-1-w", "subnet": "198.51.100.0/28", "policy": "open"},
    ]
    first = dry("apply-policy", "shop-1", policy(networks=networks)).stdout
    assert first and first == dry("apply-policy", "shop-1", policy(networks=networks)).stdout
    assert first == dry("apply-policy", "shop-1", policy(networks=networks[::-1])).stdout, (
        "in the roles' order"
    )


def test_a_pool_that_overlaps_a_private_range_is_refused_in_one_piece():
    """nft refuses an anonymous set of overlapping ranges: a pool inside 10/8 is merged with it."""
    p = policy(
        pool="10.200.0.0/24",
        networks=[{"role": "w", "bridge": "vbx-shop-1-w", "subnet": "10.200.0.0/28", "policy": "open"}],
        hostsvc=[],
    )
    out = rules(helper.policy_file("shop-1", helper.parse_policy("shop-1", p)), "out_vbx_shop_1_w")
    refused = next(r for r in out if "10.0.0.0/8" in r)
    assert "10.200.0.0/24" not in refused


def test_absent_removes_that_tasks_chains_and_nothing_else():
    gone = json.dumps({"state": "absent", "networks": [{"role": "w", "bridge": "vbx-shop-1-w"}]})
    done = dry("apply-policy", "shop-1", gone)
    assert done.returncode == 0, done.stderr
    text = done.stdout
    for kind in ("out", "back", "host", "dns"):
        assert f"delete chain inet vivibox {kind}_vbx_shop_1_w" in text
        assert f'delete element inet vivibox {kind}_by_bridge {{ "vbx-shop-1-w" }}' in text
        # Added first, so that removing what is not there is no error.
        assert text.index(f"add chain inet vivibox {kind}_vbx_shop_1_w") < text.index(
            f"delete chain inet vivibox {kind}_vbx_shop_1_w"
        )
    rules_only = [line for line in text.splitlines() if not line.startswith("#")]
    touched = {w for line in rules_only for w in line.split() if "vbx" in w and "*" not in w}
    assert touched <= {
        *(f"{k}_vbx_shop_1_w" for k in ("out", "back", "host", "dns")),
        '"vbx-shop-1-w"',
    }, "only that task's bridge"
    assert "flush chain inet vivibox forward" not in text, "the base chains stay as they are"


# --- one transaction ------------------------------------------------------------------------------


def test_a_policy_is_loaded_in_one_transaction():
    code, host = call("apply-policy", "shop-1", json.dumps(POLICY))
    assert code == 0
    assert host.privileged() == [["nft", "-f", "-"]], "one nft -f: all of it or none of it"
    assert host.inputs[-1] == helper.policy_file("shop-1", helper.parse_policy("shop-1", json.dumps(POLICY)))


def test_a_policy_nft_refuses_is_an_error_and_nothing_else_runs(capsys):
    code, host = call("apply-policy", "shop-1", json.dumps(POLICY), host=Host(nft_fails=True))
    assert code == 1 and host.privileged() == [["nft", "-f", "-"]]
    assert "No such file or directory" in capsys.readouterr().err


def test_without_root_it_validates_and_stops(capsys):
    code, host = call("apply-policy", "shop-1", json.dumps(POLICY), root=False)
    assert code == 1 and host.calls == []
    assert "must run as root" in capsys.readouterr().err


def test_it_says_its_version():
    done = subprocess.run([sys.executable, str(HELPER), "--version"], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.startswith("vivibox-host ")


# --- what it refuses --------------------------------------------------------------------------------


def bad_policies():
    def net(**changes):
        n = dict(POLICY["networks"][0])
        n.update(changes)
        return [n]

    yield "a newline", policy(dns="203.0.113.1\n")
    yield "a brace", policy(networks=net(bridge="vbx-shop-1-w}"))
    yield "a semicolon", policy(hostsvc=["198.51.100.1:5432;flush ruleset"])
    yield "a path", policy(networks=net(bridge="/etc/passwd"))
    yield "a 300-character name", policy(networks=net(bridge="v" * 300))
    yield "a bridge over 15 characters", policy(networks=net(bridge="vbx-shop-1234-w"))
    yield "another task's bridge", policy(networks=net(bridge="vbx-shop-2-w"))
    yield "a bridge of another role", policy(networks=net(bridge="vbx-shop-1-r"))
    yield "a subnet outside the pool", policy(networks=net(subnet="10.0.0.0/28"))
    yield "a subnet with host bits", policy(networks=net(subnet="198.51.100.1/28"))
    yield "an unknown role", policy(networks=net(role="x"))
    yield "an unknown policy", policy(networks=net(policy="unrestricted"))
    yield "an extra field", policy(extra=1)
    yield "an extra network field", policy(networks=net(gateway="198.51.100.1"))
    yield "a missing field", json.dumps({k: v for k, v in POLICY.items() if k != "dns"})
    yield "no network", policy(networks=[])
    yield "a role twice", policy(networks=net() * 2)
    yield "an unknown state", policy(state="gone")
    yield "an mtu that is text", policy(mtu="1500")
    yield "an mtu that is true", policy(mtu=True)
    yield "an mtu too small", policy(mtu=100)
    yield "a pool too small", policy(pool="198.51.100.0/29")
    yield "an IPv6 pool", policy(pool="fd00::/64")
    yield "a host service without a port", policy(hostsvc=["198.51.100.1"])
    yield "a host service by name", policy(hostsvc=["host.docker.internal:5432"])
    yield "a host service on port 0", policy(hostsvc=["198.51.100.1:0"])
    yield "a host service in another task's subnet", policy(hostsvc=["198.51.100.17:80"])
    yield "a host service twice", policy(hostsvc=["192.168.1.50:80", "192.168.1.50:80"])
    yield "a multicast host service", policy(hostsvc=["224.0.0.1:80"])
    yield "an address with a leading zero", policy(dns="203.0.113.01")
    yield "a forwarder in the pool", policy(dns="198.51.100.5")
    yield "a privileged forwarder port", policy(dns_port=53)
    yield "a list instead of an object", json.dumps([POLICY])
    yield "not JSON", "{state: present"
    yield "a key twice", json.dumps(POLICY)[:-1] + ', "mtu": 1500}'
    yield "NaN", json.dumps(POLICY).replace('"mtu": 1500', '"mtu": NaN')


@pytest.mark.parametrize(("why", "given"), list(bad_policies()), ids=[w for w, _ in bad_policies()])
def test_a_malformed_policy_is_refused_before_anything_runs(why, given, capsys):
    code, host = call("apply-policy", "shop-1", given)
    assert code == 1, why
    assert host.calls == [], f"{why}: refused before any command"
    assert capsys.readouterr().err.startswith("vivibox-host: ")


@pytest.mark.parametrize(
    "task_id", ["Shop-1", "shop", "shop-1\n", "shop-1;id", "-f", "../shop-1", "s" * 300 + "-1"]
)
def test_a_task_id_outside_the_pattern_is_refused(task_id):
    code, host = call("apply-policy", task_id, json.dumps(POLICY))
    assert code == 1 and host.calls == []


def test_an_absent_policy_takes_only_the_bridges():
    extra = json.dumps(
        {
            "state": "absent",
            "networks": [{"role": "w", "bridge": "vbx-shop-1-w", "subnet": "198.51.100.0/28"}],
        }
    )
    code, host = call("apply-policy", "shop-1", extra)
    assert code == 1 and host.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("apply-policy",),
        ("apply-policy", "shop-1"),
        ("drop-policy", "shop-1", "{}"),
        ("prepare-engine", "vm", "{}"),
        ("prepare-engine", "host"),
        ("apply-policy", "shop-1", "{}", "extra"),
        ("--dry-run",),
    ],
)
def test_anything_outside_the_vocabulary_is_a_usage_error(argv):
    code, host = call(*argv)
    assert code == 2 and host.calls == []


def test_an_argument_past_its_size_is_refused():
    code, host = call("apply-policy", "shop-1", json.dumps({**POLICY, "pad": "x" * 20000}))
    assert code == 1 and host.calls == []


def test_the_helper_names_a_bridge_as_vivibox_does():
    """A bridge must be the one vivibox's names.bridge gives the task and role: the helper works
    it out itself, so no task can be given another's rules."""
    rng = random.Random(7)
    ids = [
        "shop-1",
        "a-project-with-a-long-name-123",
        *(f"p{rng.randint(0, 10**6)}-{rng.randint(1, 10**5)}" for _ in range(300)),
    ]
    for task_id in ids:
        for role in names.ROLES:
            assert helper.bridge(task_id, role) == names.bridge(task_id, role)


def test_a_bridge_vivibox_names_is_taken():
    task_id = "a-project-with-a-long-name-123"
    p = policy(
        networks=[
            {"role": "w", "bridge": names.bridge(task_id, "w"), "subnet": "198.51.100.0/28", "policy": "open"}
        ]
    )
    code, _ = call("apply-policy", task_id, p)
    assert code == 0


def test_a_second_network_on_the_firsts_subnet_is_refused_for_the_overlap():
    """What the live test gives the helper to see the rules before it kept: a second role network
    on the first one's subnet. The refusal it must meet is the overlap, which it reaches only
    with the bridge vivibox names for that role; the readable shape of a long id's bridge
    (vbx-fwtest-1234-r, 17 characters) is refused as a name first, and the overlap is never checked."""
    task_id = "fwtest-1234"
    nets = [
        {"role": role, "bridge": names.bridge(task_id, role), "subnet": "198.51.100.0/28", "policy": "open"}
        for role in ("w", "r")
    ]
    done = dry("apply-policy", task_id, policy(networks=nets))
    assert done.returncode == 1 and "networks[1].subnet 198.51.100.0/28 overlaps" in done.stderr, done.stderr


# --- the host's own address -------------------------------------------------------------------------


def test_prepare_engine_makes_the_interface_and_loads_the_base_table():
    code, host = call("prepare-engine", "host", json.dumps(ENGINE))
    assert code == 0
    done = host.privileged()
    assert done[:3] == [
        ["ip", "link", "add", "vivibox0", "type", "dummy"],
        ["ip", "addr", "add", "203.0.113.1/32", "dev", "vivibox0"],
        ["ip", "link", "set", "vivibox0", "up"],
    ]
    assert done[3] == ["nft", "-f", "-"]
    assert "add chain inet vivibox forward" in host.inputs[-1]


def test_prepare_engine_again_changes_nothing_that_is_there():
    there = {
        "ifname": "vivibox0", "flags": ["BROADCAST", "NOARP", "UP", "LOWER_UP"],
        "linkinfo": {"info_kind": "dummy"},
        "addr_info": [{"family": "inet", "local": "203.0.113.1", "prefixlen": 32}],
    }  # fmt: skip
    route = [*ROUTES, {"type": "local", "dst": "203.0.113.1", "dev": "vivibox0", "table": "local"}]
    code, host = call(
        "prepare-engine", "host", json.dumps(ENGINE), host=Host(addr=[LO, WLAN, there], route=route)
    )
    assert code == 0
    assert host.privileged() == [["nft", "-f", "-"]], "only the base table, which loads the same"


def test_prepare_engine_moves_its_own_interface_to_a_new_address():
    there = {
        "ifname": "vivibox0", "flags": ["UP"], "linkinfo": {"info_kind": "dummy"},
        "addr_info": [{"family": "inet", "local": "203.0.113.9", "prefixlen": 32}],
    }  # fmt: skip
    code, host = call("prepare-engine", "host", json.dumps(ENGINE), host=Host(addr=[LO, there]))
    assert code == 0
    assert ["ip", "addr", "del", "203.0.113.9/32", "dev", "vivibox0"] in host.privileged()
    assert ["ip", "addr", "add", "203.0.113.1/32", "dev", "vivibox0"] in host.privileged()


@pytest.mark.parametrize(
    ("addr", "route", "said"),
    [
        (
            [
                LO,
                {
                    "ifname": "eth1",
                    "addr_info": [{"family": "inet", "local": "203.0.113.1", "prefixlen": 24}],
                },
            ],
            ROUTES,
            "network.host_address 203.0.113.1 is already an address of eth1",
        ),
        (
            [LO, WLAN],
            [*ROUTES, {"dst": "203.0.113.0/24", "dev": "tun0"}],
            "a route to 203.0.113.0/24 through tun0 covers network.host_address 203.0.113.1",
        ),
        (
            [LO, WLAN],
            [*ROUTES, {"dst": "203.0.113.1", "gateway": "192.168.1.1", "dev": "wlan0", "table": "200"}],
            "a route to 203.0.113.1 through wlan0 covers network.host_address 203.0.113.1",
        ),
        (
            [LO, {"ifname": "vivibox0", "linkinfo": {"info_kind": "bridge"}, "addr_info": []}],
            ROUTES,
            "an interface named vivibox0 that is not vivibox's",
        ),
    ],
)
def test_prepare_engine_refuses_a_conflict_and_changes_nothing(addr, route, said, capsys):
    code, host = call("prepare-engine", "host", json.dumps(ENGINE), host=Host(addr=addr, route=route))
    assert code == 1
    assert host.privileged() == [], "no ip addr add, no nft: nothing is overwritten"
    err = capsys.readouterr().err
    assert said in err and "config.toml" in err


def test_prepare_engine_refuses_an_address_in_the_task_pool(capsys):
    code, host = call("prepare-engine", "host", json.dumps({**ENGINE, "host_address": "198.51.100.200/32"}))
    assert code == 1 and host.calls == []
    err = capsys.readouterr().err
    assert "network.host_address" in err and "network.pool" in err


@pytest.mark.parametrize(
    "engine",
    [
        {**ENGINE, "host_address": "203.0.113.1/24"},
        {**ENGINE, "host_address": "203.0.113.1/32\nip link del eth0"},
        {**ENGINE, "host_address": "127.0.0.1/32"},
        {**ENGINE, "extra": True},
        {"host_address": "203.0.113.1/32"},
    ],
)
def test_prepare_engine_refuses_malformed_input(engine):
    code, host = call("prepare-engine", "host", json.dumps(engine))
    assert code == 1 and host.calls == []


def test_prepare_engine_dry_run_prints_what_it_would_do():
    done = dry("prepare-engine", "host", json.dumps(ENGINE))
    # The machine's own ip -j is read; on one where the address is taken the refusal is right too.
    assert done.returncode in (0, 1), done.stderr
    if done.returncode == 0:
        assert "add chain inet vivibox forward" in done.stdout


def test_the_engine_alone_keeps_the_lan_off_the_hosts_address():
    """The same base from prepare-engine, before any task: the forwarder and the mirror listen
    there from the first pod's start."""
    code, host = call("prepare-engine", "host", json.dumps(ENGINE))
    assert code == 0
    assert rules(host.inputs[-1], "input")[-1] == 'ip daddr 203.0.113.1 iifname != "lo" drop'


def test_dns_over_tls_is_refused_and_the_host_services_still_pass():
    """Port 53 goes to the forwarder; DNS over TLS (853, TCP and UDP) would go past it."""
    out = rules(
        helper.policy_file("shop-1", helper.parse_policy("shop-1", json.dumps(POLICY))), "out_vbx_shop_1_w"
    )
    tcp, udp = (
        out.index("tcp dport 853 reject with tcp reset"),
        out.index("udp dport 853 reject with icmpx admin-prohibited"),
    )
    assert out.index("ip daddr 192.168.1.50 tcp dport 8080 accept") < tcp < out.index("accept")
    assert udp < out.index("accept")


@pytest.mark.parametrize("bits", range(0, 33))
def test_the_pool_vivibox_takes_is_the_pool_the_helper_takes(tmp_path, bits):
    """config.toml and the helper agree on network.pool: a pool vivibox accepts never fails at
    every pod's start, and one it refuses is refused where you wrote it."""
    from vivibox.config import ConfigError, load_config

    pool = str(ipaddress.IPv4Network(f"10.0.0.0/{bits}", strict=False))
    (tmp_path / "config.toml").write_text(
        f'tasks_dir = "/t"\n[network]\npool = "{pool}"\nhost_address = "203.0.113.1"\n'
        '[roles.planner]\nharness = "opencode"\nmodel = "m"\n'
        '[roles.writer]\nharness = "opencode"\nmodel = "m"\n'
    )
    try:
        load_config(tmp_path)
        vivibox_takes = True
    except ConfigError as e:
        assert "network.pool" in str(e) or "network.host_address" in str(e)
        vivibox_takes = "network.pool" not in str(e)
    try:
        helper.network(pool, "network.pool", helper.POOL_SHORTEST, helper.POOL_LONGEST)
        helper_takes = True
    except helper.Refused:
        helper_takes = False
    assert vivibox_takes is helper_takes, pool


# --- a task's disk --------------------------------------------------------------------------------


class Disks(Host):
    """The machine's loop devices and mounts as the helper asks about them: findmnt, blkid and
    losetup -j answer from what the test set; the commands that change something are recorded."""

    def __init__(
        self, mounted=(), loops=None, fstype=None, grows_fail=False, mount_fails=0, trim_fails=False
    ):
        super().__init__()
        self.mounted = set(mounted)
        self.loops = dict(loops or {})
        self.fstype = dict(fstype or {})
        self.grows_fail = grows_fail
        self.mount_fails = mount_fails  # how many mounts fail before one works
        self.trim_fails = trim_fails

    def __call__(self, cmd, input=None, pass_fds=()):
        self.calls.append(list(cmd))
        self.inputs.append(input)
        out, rc, err = "", 0, ""
        if cmd[:1] == ["fstrim"] and self.trim_fails:
            rc, err = 1, "fstrim: /srv/vivibox/shop-1/disk: FITRIM ioctl failed: Operation not supported"
        elif cmd[:1] == ["mount"] and self.mount_fails:
            self.mount_fails -= 1
            rc, err = 1, "mount: /srv/vivibox/shop-1/disk: unknown filesystem type 'xfs'"
        elif cmd[:1] == ["findmnt"]:
            target = cmd[-1]
            out, rc = ("/dev/loop7\n", 0) if target in self.mounted else ("", 1)
        elif cmd[:1] == ["blkid"]:
            out, rc = (self.fstype[cmd[-1]] + "\n", 0) if self.fstype.get(cmd[-1]) else ("", 2)
        elif cmd[:2] == ["losetup", "-j"]:
            out = f"{self.loops[cmd[2]]}\n" if cmd[2] in self.loops else ""
        elif cmd[:2] == ["losetup", "--find"]:
            out = "/dev/loop7\n"
            self.loops[cmd[-1]] = "/dev/loop7"
        elif cmd[:1] == ["xfs_growfs"] and self.grows_fail:
            rc, err = 1, "xfs_growfs: /srv/vivibox/shop-1/disk is not a mounted XFS filesystem"
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def privileged(self):
        """Every call that changes something: not the questions (findmnt, blkid, losetup -j)."""
        asks = (["findmnt"], ["blkid"], ["losetup", "-j"])
        return [c for c in self.calls if not any(c[: len(a)] == a for a in asks)]


@pytest.fixture
def tasks(tmp_path, monkeypatch):
    """A tasks directory the helper takes as the root, with the task shop-1 in it."""
    monkeypatch.setattr(helper, "tasks_root", lambda: str(tmp_path))
    (tmp_path / "shop-1").mkdir()
    return tmp_path


def disk(task_id="shop-1", tasks=None, **changes):
    spec = {"dir": str(tasks / task_id), "size": "20g"}
    spec.update(changes)
    return json.dumps(spec)


def test_attach_makes_a_sparse_xfs_image_mounts_it_and_makes_the_pods_directories(tasks, capsys):
    host = Disks()
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 0, capsys.readouterr().err
    img = str(tasks / "shop-1" / "disk.img")
    made = os.stat(img)
    assert made.st_size == 20 * 2**30 and made.st_blocks == 0, "sparse: made by the helper, never written"
    assert host.privileged()[:2] == [["mkfs.xfs", "-q", img], ["losetup", "--find", "--show", img]]
    [mount] = [c for c in host.privileged() if c[:1] == ["mount"]]
    assert mount[:5] == ["mount", "--no-canonicalize", "-o", "nosuid,nodev", "/dev/loop7"], (
        "without --no-canonicalize mount(8) reads the link back to a path and mount(2) resolves it anew"
    )
    assert re.fullmatch(r"/proc/self/fd/\d+", mount[5]), "the mount point as a directory fd, not a path"
    assert len(host.privileged()) == 3
    for name in ("docker", "config", "installed", "gate-installed", "gate-build-cache", "gate", "review"):
        found = os.lstat(tasks / "shop-1" / "disk" / name)
        assert stat.S_ISDIR(found.st_mode) and stat.S_IMODE(found.st_mode) == 0o755, name
        assert found.st_uid == (tasks / "shop-1").stat().st_uid
    assert stat.S_IMODE(os.lstat(tasks / "shop-1" / "disk").st_mode) == 0o755


def test_the_directories_are_0755_whatever_the_umask(tasks, monkeypatch):
    """The helper runs under umask 077: a mkdir alone would give the pod 0700 directories."""
    was = os.umask(0o077)
    try:
        code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=Disks())
    finally:
        os.umask(was)
    assert code == 0
    for path in (tasks / "shop-1" / "disk", tasks / "shop-1" / "disk" / "docker"):
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o755, path


def test_attach_of_an_existing_image_only_mounts_it(tasks):
    img = tasks / "shop-1" / "disk.img"
    img.write_bytes(b"")
    host = Disks(fstype={str(img): "xfs"})
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 0
    assert [c[0] for c in host.privileged()] == ["losetup", "mount"], "no truncate, no mkfs: the data stays"


def test_attach_refuses_to_format_an_image_without_a_filesystem(tasks, capsys):
    """An image left by a failed mkfs, or one that is not an image: never formatted over, said."""
    img = tasks / "shop-1" / "disk.img"
    img.write_bytes(b"")
    host = Disks()
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 1 and host.privileged() == []
    assert "no XFS filesystem" in capsys.readouterr().err


def test_attach_of_a_mounted_disk_does_nothing_and_says_so(tasks, capsys):
    mnt = tasks / "shop-1" / "disk"
    host = Disks(mounted={str(mnt)})
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 0 and host.privileged() == []
    assert "already attached" in capsys.readouterr().out


def test_detach_unmounts_and_frees_the_loop_device_but_never_deletes_the_image(tasks):
    img, mnt = str(tasks / "shop-1" / "disk.img"), str(tasks / "shop-1" / "disk")
    (tasks / "shop-1" / "disk.img").write_bytes(b"")
    host = Disks(mounted={mnt}, loops={img: "/dev/loop3"})
    (tasks / "shop-1" / "disk").mkdir()
    unmounted = unmounting(host)
    code, _ = call("detach-disk", "shop-1", json.dumps({"dir": str(tasks / "shop-1")}), host=host)
    assert code == 0
    [(target, flags)] = unmounted
    assert re.fullmatch(rb"/proc/self/fd/\d+/disk", target) and flags == helper.UMOUNT_NOFOLLOW, (
        "the syscall itself, on the task's directory held open and the last component resolved in it: "
        "a descriptor of the mounted root would be a reference on the mount, and umount2 EBUSY"
    )
    trim, freed = host.privileged()
    assert trim[0] == "fstrim" and re.fullmatch(r"/proc/self/fd/\d+/disk", trim[1]), (
        "the room the task's files took goes back to the host here, not as files are deleted (no discard)"
    )
    assert host.calls.index(trim) < len(host.calls), "trimmed before the umount"
    assert freed == ["losetup", "-d", "/dev/loop3"], "no umount command"
    assert (tasks / "shop-1" / "disk.img").exists(), "the caller deletes it, after a detach that worked"


def unmounting(host, works=True):
    """Steps.umount2 replaced: records the call, and the fake forgets the mount when it works."""
    calls = []

    def umount2(self, target, flags):
        calls.append((target, flags))
        if works:
            host.mounted.clear()

    helper.Steps.umount2 = umount2
    return calls


@pytest.fixture(autouse=True)
def real_umount2(monkeypatch):
    monkeypatch.setattr(helper.Steps, "umount2", helper.Steps.umount2)


def test_a_detach_that_leaves_the_mount_is_refused_with_the_reason(tasks, capsys):
    img, mnt = str(tasks / "shop-1" / "disk.img"), str(tasks / "shop-1" / "disk")
    (tasks / "shop-1" / "disk.img").write_bytes(b"")
    (tasks / "shop-1" / "disk").mkdir()
    host = Disks(mounted={mnt}, loops={img: "/dev/loop3"})
    unmounting(host, works=False)
    code, _ = call("detach-disk", "shop-1", json.dumps({"dir": str(tasks / "shop-1")}), host=host)
    assert code == 1 and [c[0] for c in host.privileged()] == ["fstrim"], "the loop device stays"
    assert "still mounted" in capsys.readouterr().err


def test_the_real_umount2_is_the_kernels_with_nofollow(tasks):
    """Not a library that resolves the path again: the syscall, told not to follow a link."""
    assert helper.UMOUNT_NOFOLLOW == 8
    with pytest.raises(helper.Refused, match="umount2 .* failed: .*"):
        helper.Steps(None, False).umount2(b"/proc/self/fd/9999/disk", helper.UMOUNT_NOFOLLOW)


def test_the_dry_run_of_a_detach_says_the_syscall(tasks, capsys):
    img, mnt = str(tasks / "shop-1" / "disk.img"), str(tasks / "shop-1" / "disk")
    (tasks / "shop-1" / "disk.img").write_bytes(b"")
    (tasks / "shop-1" / "disk").mkdir()
    host = Disks(mounted={mnt}, loops={img: "/dev/loop3"})
    unmounted = unmounting(host)
    code = helper.main(["--dry-run", "detach-disk", "shop-1", disk_only(tasks)], run=host, euid=1000)
    assert code == 0 and unmounted == [] and host.privileged() == []
    out = capsys.readouterr().out
    assert "# would run: fstrim /proc/self/fd/N/disk" in out
    assert "# would run: umount2 /proc/self/fd/N/disk UMOUNT_NOFOLLOW" in out
    assert out.index("fstrim") < out.index("umount2") and "losetup -d /dev/loop3" in out


def test_detach_of_a_detached_disk_does_nothing_and_says_so(tasks, capsys):
    host = Disks()
    code, _ = call("detach-disk", "shop-1", json.dumps({"dir": str(tasks / "shop-1")}), host=host)
    assert code == 0 and host.privileged() == []
    assert "already detached" in capsys.readouterr().out


def test_grow_enlarges_the_image_the_loop_device_and_the_filesystem(tasks):
    img, mnt = str(tasks / "shop-1" / "disk.img"), str(tasks / "shop-1" / "disk")
    with open(img, "wb") as f:
        f.truncate(20 * 2**30)
    (tasks / "shop-1" / "disk").mkdir()
    host = Disks(mounted={mnt}, loops={img: "/dev/loop3"})
    code, _ = call("grow-disk", "shop-1", disk(tasks=tasks, size="40g"), host=host)
    assert code == 0
    assert os.stat(img).st_size == 40 * 2**30 and os.stat(img).st_blocks == 0, "grown by the helper, sparse"
    assert host.privileged() == [["losetup", "-c", "/dev/loop3"], ["xfs_growfs", mnt]]


@pytest.mark.parametrize("size", ["20g", "10g"])
def test_grow_refuses_to_shrink_or_stay(tasks, size, capsys):
    img, mnt = str(tasks / "shop-1" / "disk.img"), str(tasks / "shop-1" / "disk")
    with open(img, "wb") as f:
        f.truncate(20 * 2**30)
    host = Disks(mounted={mnt}, loops={img: "/dev/loop3"})
    code, _ = call("grow-disk", "shop-1", disk(tasks=tasks, size=size), host=host)
    assert code == 1 and host.privileged() == []
    assert "only grows" in capsys.readouterr().err


def test_grow_refuses_a_disk_that_is_not_attached(tasks, capsys):
    img = str(tasks / "shop-1" / "disk.img")
    with open(img, "wb") as f:
        f.truncate(20 * 2**30)
    code, host = call("grow-disk", "shop-1", disk(tasks=tasks, size="40g"), host=Disks())
    assert code == 1 and host.privileged() == []
    assert "not attached" in capsys.readouterr().err


def bad_disks(tasks):
    yield "a size below a gigabyte", disk(tasks=tasks, size="500m")
    yield "a size past two terabytes", disk(tasks=tasks, size="3t")
    yield "a size without a unit", disk(tasks=tasks, size="20")
    yield "a size that is a number", json.dumps({"dir": str(tasks / "shop-1"), "size": 20})
    yield "a size with a fraction", disk(tasks=tasks, size="1.5g")
    yield "a dir of another task", disk(tasks=tasks, dir=str(tasks / "shop-2"))
    yield "a dir outside the root", disk(tasks=tasks, dir="/tmp/shop-1")
    yield "a dir two levels deep", disk(tasks=tasks, dir=str(tasks / "nested" / "shop-1"))
    yield "a relative dir", disk(tasks=tasks, dir="shop-1")
    yield "a dir with a trailing slash", disk(tasks=tasks, dir=str(tasks / "shop-1") + "/")
    yield "a dir with a dot component", disk(tasks=tasks, dir=f"{tasks}/./shop-1")
    yield "an extra field", disk(tasks=tasks, extra=1)
    yield "a missing size", json.dumps({"dir": str(tasks / "shop-1")})


def test_a_malformed_disk_request_is_refused_before_anything_runs(tasks, capsys):
    (tasks / "nested" / "shop-1").mkdir(parents=True)
    (tasks / "shop-2").mkdir()
    for why, given in bad_disks(tasks):
        host = Disks()
        code, _ = call("attach-disk", "shop-1", given, host=host)
        assert code == 1, why
        assert host.calls == [], f"{why}: refused before any command"
        assert capsys.readouterr().err.startswith("vivibox-host: "), why


def test_a_dir_with_a_symlink_in_its_path_is_refused(tmp_path, monkeypatch, capsys):
    """A link anywhere in the path could point the mount, or the chown of the pod's directories,
    at something else of the person's; every component has to be a plain directory."""
    real = tmp_path / "real"
    (real / "shop-1").mkdir(parents=True)
    linked = tmp_path / "tasks"
    linked.symlink_to(real)
    monkeypatch.setattr(helper, "tasks_root", lambda: str(linked))
    host = Disks()
    code, _ = call("attach-disk", "shop-1", disk(tasks=linked), host=host)
    assert code == 1 and host.calls == []
    assert "symbolic link" in capsys.readouterr().err
    (real / "shop-2").symlink_to(real / "shop-1")
    monkeypatch.setattr(helper, "tasks_root", lambda: str(real))
    code, _ = call("attach-disk", "shop-2", disk("shop-2", tasks=real), host=host)
    assert code == 1 and host.calls == []


def test_a_task_id_outside_the_pattern_is_refused_for_a_disk_too(tasks):
    for task_id in ("Shop-1", "shop", "../shop-1", "shop-1;id"):
        code, host = call("attach-disk", task_id, disk(tasks=tasks), host=Disks())
        assert code == 1 and host.calls == [], task_id


def test_the_tasks_root_comes_from_a_root_owned_file_or_the_default(tmp_path):
    """Where a task's dir may be is decided by the host's file, never by the request: a file
    that is not root's (or that others may write) is refused, and without one the default holds."""
    assert helper.tasks_root(str(tmp_path / "missing.json")) == "/srv/vivibox"
    mine = tmp_path / "host.json"
    mine.write_text('{"tasks_dir": "/data/tasks"}')
    with pytest.raises(helper.Refused, match="root"):
        helper.tasks_root(str(mine))


def test_the_disk_dry_run_prints_the_steps_and_runs_nothing(tasks, capsys):
    host = Disks()
    code = helper.main(["--dry-run", "attach-disk", "shop-1", disk(tasks=tasks)], run=host, euid=1000)
    assert code == 0 and host.privileged() == []
    out = capsys.readouterr().out
    assert "# would make" in out and "sparse" in out and "# would run: mkfs.xfs" in out
    assert "mount --no-canonicalize -o nosuid,nodev" in out
    assert not (tasks / "shop-1" / "disk").exists(), "nothing made"
    assert not (tasks / "shop-1" / "disk.img").exists(), "no image either"


def test_the_disks_size_range_is_the_one_config_takes():
    from vivibox.podspec import DISK_LARGEST, DISK_SMALLEST

    assert (helper.DISK_SMALLEST, helper.DISK_LARGEST) == (DISK_SMALLEST, DISK_LARGEST)


# --- what a link in the task's directory could do (the reviewer's probe) ----------------------------


def mounted_image(tasks, host=None):
    """An image with a filesystem, as after a restart of the machine: attach mounts it again."""
    img = tasks / "shop-1" / "disk.img"
    img.write_bytes(b"")
    host = host or Disks()
    host.fstype[str(img)] = "xfs"
    return host


@pytest.mark.parametrize("op", ["attach-disk", "detach-disk", "grow-disk"])
def test_a_mount_point_that_is_a_link_is_refused_before_anything_runs(tasks, tmp_path, op, capsys):
    """A link at <dir>/disk to /etc, by the person's own uid: the mount would land in /etc and the
    chown would hand it to the person. Refused before a question is even asked."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tasks / "shop-1" / "disk").symlink_to(elsewhere)
    host = mounted_image(tasks, Disks(mounted={str(tasks / "shop-1" / "disk")}))
    host.loops[str(tasks / "shop-1" / "disk.img")] = "/dev/loop3"
    with open(tasks / "shop-1" / "disk.img", "wb") as f:
        f.truncate(2**30)
    given = disk(tasks=tasks, size="40g") if op != "detach-disk" else disk_only(tasks)
    unmounted = unmounting(host)
    code, _ = call(op, "shop-1", given, host=host)
    assert code == 1 and host.calls == [] and unmounted == [], op
    assert "symbolic link" in capsys.readouterr().err
    assert elsewhere.stat().st_mode == elsewhere.stat().st_mode and not (elsewhere / "docker").exists()


def disk_only(tasks):
    return json.dumps({"dir": str(tasks / "shop-1")})


def test_a_directory_of_the_disk_that_is_a_link_is_refused_on_a_second_attach(tasks, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tasks / "shop-1" / "disk").mkdir()
    (tasks / "shop-1" / "disk" / "docker").symlink_to(elsewhere)
    host = mounted_image(tasks)
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 1
    err = capsys.readouterr().err
    assert "docker" in err and "symbolic link" in err
    assert elsewhere.stat().st_uid == os.getuid() and not (elsewhere / "config").exists()


def test_a_mount_point_owned_by_someone_else_is_refused(tasks, capsys, monkeypatch):
    """A directory there that is not the task owner's (root's from a half-done attach is the
    helper's own, so uid 0 passes): refused rather than handed over."""
    (tasks / "shop-1" / "disk").mkdir()
    real = os.lstat

    def other_owner(path, *a, **k):
        st = real(path, *a, **k)
        if str(path) == str(tasks / "shop-1" / "disk"):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 4242, 4242, st.st_size,
                                   st.st_atime, st.st_mtime, st.st_ctime))  # fmt: skip
        return st

    monkeypatch.setattr(helper.os, "lstat", other_owner)
    code, host = call("attach-disk", "shop-1", disk(tasks=tasks), host=mounted_image(tasks))
    assert code == 1 and host.calls == []
    assert "owner" in capsys.readouterr().err


@pytest.mark.parametrize("what", ["a link to a file", "a fifo"])
def test_an_image_that_is_not_a_plain_file_is_refused(tasks, tmp_path, what, capsys):
    img = tasks / "shop-1" / "disk.img"
    if what == "a fifo":
        os.mkfifo(img)
    else:
        target = tmp_path / "target.img"
        target.write_bytes(b"")
        img.symlink_to(target)
    host = Disks(fstype={str(img): "xfs"})
    for op in ("attach-disk", "grow-disk"):
        code, _ = call(op, "shop-1", disk(tasks=tasks, size="40g"), host=host)
        assert code == 1 and host.calls == [], (what, op)
        assert "not a plain file" in capsys.readouterr().err


def test_a_mount_that_failed_leaves_one_loop_device_that_the_next_attach_uses(tasks):
    """The first mount failed (no xfs module): the loop device stays on the image; the next
    attach must not pin a second one, which detach would never free."""
    host = Disks(mount_fails=1)
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 1
    assert [c for c in host.privileged() if c[:2] == ["losetup", "--find"]] == [
        ["losetup", "--find", "--show", str(tasks / "shop-1" / "disk.img")]
    ]
    host.fstype[str(tasks / "shop-1" / "disk.img")] = "xfs"
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 0
    assert len([c for c in host.calls if c[:2] == ["losetup", "--find"]]) == 1, "the device it has is used"
    assert [c for c in host.privileged() if c[:1] == ["mount"]][-1][4] == "/dev/loop7"


class Swapping(Disks):
    """The person's process swapping the image, or the mount point, for another between the
    helper's check and the tool it then runs: the fake does it while answering a question."""

    def __init__(self, swap_on, swap, **kw):
        super().__init__(**kw)
        self.swap_on, self.swap = swap_on, swap

    def __call__(self, cmd, input=None, pass_fds=()):
        done = super().__call__(cmd, input, pass_fds)
        if cmd[: len(self.swap_on)] == list(self.swap_on):
            self.swap()
        return done


def test_an_image_swapped_after_the_check_is_not_given_to_a_tool(tasks, capsys):
    """The inode checked is the inode the tool gets, or nothing runs: here the image is replaced
    while blkid is asked (after lstat, before losetup)."""
    img = tasks / "shop-1" / "disk.img"
    img.write_bytes(b"")
    other = tasks / "shop-1" / "other.img"
    other.write_bytes(b"")

    def swap():
        os.replace(other, img)

    host = Swapping(("blkid",), swap, fstype={str(img): "xfs"})
    code, _ = call("attach-disk", "shop-1", disk(tasks=tasks), host=host)
    assert code == 1 and host.privileged() == [], "no losetup, no mount"
    assert "changed while the helper worked" in capsys.readouterr().err


def test_a_mount_point_swapped_after_the_check_is_not_grown(tasks, capsys):
    img, mnt = str(tasks / "shop-1" / "disk.img"), tasks / "shop-1" / "disk"
    with open(img, "wb") as f:
        f.truncate(20 * 2**30)
    mnt.mkdir()

    def swap():
        mnt.rename(mnt.with_name("was-disk"))
        mnt.mkdir()

    host = Swapping(("losetup", "-c"), swap, mounted={str(mnt)}, loops={img: "/dev/loop3"})
    code, _ = call("grow-disk", "shop-1", disk(tasks=tasks, size="40g"), host=host)
    assert code == 1
    assert [c[0] for c in host.privileged()] == ["losetup"], "the loop grew, xfs_growfs did not run"
    assert "changed while the helper worked" in capsys.readouterr().err


def test_a_trim_that_fails_is_said_and_the_disk_is_detached_all_the_same(tasks, capsys):
    """Room not returned to the host is not a security matter: the detach goes on, and says it."""
    img, mnt = str(tasks / "shop-1" / "disk.img"), str(tasks / "shop-1" / "disk")
    (tasks / "shop-1" / "disk.img").write_bytes(b"")
    (tasks / "shop-1" / "disk").mkdir()
    host = Disks(mounted={mnt}, loops={img: "/dev/loop3"}, trim_fails=True)
    unmounted = unmounting(host)
    code, _ = call("detach-disk", "shop-1", json.dumps({"dir": str(tasks / "shop-1")}), host=host)
    assert code == 0 and len(unmounted) == 1
    assert [c[0] for c in host.privileged()] == ["fstrim", "losetup"]
    said = capsys.readouterr()
    assert "not returned" in said.out + said.err and "FITRIM" in said.out + said.err
