"""host/vivibox-host, the only code vivibox runs as root: a closed list of operations, every
argument validated before anything privileged runs, nothing run through a shell, and the nft
rules it loads written by itself from structured input (ADR-0048). Tested without root: the
helper is loaded as a module with a fake runner, or run with --dry-run, which prints what it
would load and touches nothing."""

import copy
import ipaddress
import json
import random
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
