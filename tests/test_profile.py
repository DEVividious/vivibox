"""A task's resource profile (vivibox/profile.py): every value of today's limits with where it came
from, made from the machine's limits and the task's own and back to the very same Limits, one
value replaced with its origin, and a form for JSON; nothing written, nothing that reads it yet."""

import json

import pytest

from vivibox import limits, profile
from vivibox.podspec import DEFAULT_MEMORY, DEFAULT_PIDS, GIB, LIMIT_KEYS, ROLES, Limits, PastThePod
from vivibox.profile import Provenance, ResourceProfile, Source, Value

AT = "2026-10-20T12:00:00.000+00:00"
CONTAINERS = (*ROLES, "index")
# A machine whose config.toml says other figures than vivibox's, one of them a size that is no
# whole number of kiB (1.3g), and a task with its own over them.
MACHINE = {
    "pod_memory": "12g", "pod_cpus": 1.5, "pod_pids": 9000, "agent_memory": "8g", "gate_pids": 2000,
    "review_memory": "1.3g", "disk": "40g",
}  # fmt: skip
OWN = {"agent_memory": "10g", "control_pids": 512, "sidecar_memory": "3g"}


def every_flag(found: Limits) -> dict[str, list[str]]:
    return {role: found.flags(role, "shop-1") for role in CONTAINERS}


def test_the_default_profile_is_today_s_figures_each_from_vivibox():
    made = profile.from_limits(Limits(), {})
    gib = {role: made.memory(role) / GIB for role in CONTAINERS}
    assert gib == {"agent": 4, "gate": 4, "sidecar": 2, "review": 1.5, "control": 1.5, "index": 4}
    assert {role: made.memory(role) for role in CONTAINERS} == DEFAULT_MEMORY
    assert {role: made.pids(role) for role in CONTAINERS} == DEFAULT_PIDS
    assert (made.value("pod_memory"), made.value("pod_pids"), made.value("disk")) == (6 * GIB, 8192, 20 * GIB)
    assert made.value("pod_cpus") == Limits().pod_cpus
    assert made.value("agent_language_server_memory") is None, "unaccounted until there are figures"
    assert {made.provenance(key) for key in profile.KEYS} == {Provenance(Source.DEFAULT)}


def test_every_key_of_the_limits_is_in_the_profile_with_the_index_and_the_language_server():
    assert set(profile.KEYS) == {
        *LIMIT_KEYS, "index_memory", "index_pids", "agent_language_server_memory"
    }  # fmt: skip
    assert len(profile.KEYS) == len(set(profile.KEYS))


def test_the_default_limits_come_back_the_same_with_the_same_flags():
    back = profile.to_limits(profile.from_limits(Limits(), {}))
    assert back == Limits()
    assert every_flag(back) == every_flag(Limits())
    assert back.digest() == Limits().digest()


def test_other_limits_come_back_the_same_as_the_task_s_are_read_today(tmp_path):
    machine = Limits().with_values(MACHINE)
    (tmp_path / limits.OVERRIDES).write_text(json.dumps(OWN) + "\n")
    today = limits.for_task(machine, tmp_path)
    back = profile.to_limits(profile.from_limits(machine, OWN))
    assert back == today
    assert every_flag(back) == every_flag(today)
    assert back.digest() == today.digest()
    assert back.memory["review"] == int(1.3 * GIB), "a size of no whole kiB kept to the byte"


def test_each_value_says_where_it_came_from():
    made = profile.from_limits(Limits().with_values(MACHINE), OWN)
    assert made.provenance("gate_memory") == Provenance(Source.DEFAULT)
    assert made.provenance("pod_memory") == Provenance(Source.MACHINE)
    assert made.provenance("review_memory") == Provenance(Source.MACHINE)
    assert made.provenance("agent_memory") == Provenance(Source.TASK), "the task's own over the machine's"
    assert made.provenance("control_pids") == Provenance(Source.TASK)
    assert made.provenance("index_memory") == Provenance(Source.DEFAULT)
    changed = made.with_value("gate_memory", "6g", Provenance.person("you", AT))
    assert changed.provenance("gate_memory") == Provenance(Source.PERSON, "you", AT)
    assert {changed.provenance(key).source for key in profile.KEYS} == set(Source)


def test_a_machine_s_value_that_is_vivibox_s_own_is_the_default():
    made = profile.from_limits(Limits().with_values({"agent_memory": "4g"}), {})
    assert made.provenance("agent_memory") == Provenance(Source.DEFAULT)


def test_a_task_s_value_is_the_task_s_even_where_it_repeats_the_machine_s():
    made = profile.from_limits(Limits(), {"agent_memory": "4g"})
    assert made.provenance("agent_memory") == Provenance(Source.TASK)


def test_the_task_s_own_are_checked_as_they_are_today():
    with pytest.raises(PastThePod):
        profile.from_limits(Limits(), {"agent_memory": "8g"})
    with pytest.raises(ValueError, match="limits.agent_memory"):
        profile.from_limits(Limits(), {"agent_memory": "lots"})
    with pytest.raises(ValueError, match="not a limit"):
        profile.from_limits(Limits(), {"index_memory": "2g"})


def test_a_person_s_change_names_who_and_when():
    assert Provenance.person("cli-agent:claude-code", AT).actor == "cli-agent:claude-code"
    for actor, at in (("", AT), ("you", ""), ("you", "yesterday"), ("you", "2026-10-20T12:00:00")):
        with pytest.raises(ValueError, match="person"):
            Provenance(Source.PERSON, actor, at)
    for source in (Source.DEFAULT, Source.MACHINE, Source.TASK):
        with pytest.raises(ValueError, match="only a person's"):
            Provenance(source, "you", AT)
    with pytest.raises(ValueError):
        Provenance("planner")


def test_one_value_replaced_leaves_the_rest_and_the_profile_it_came_from():
    made = profile.from_limits(Limits(), {})
    by = Provenance.person("you", AT)
    changed = made.with_value("pod_memory", "8g", by).with_value("agent_memory", "8g", by)
    assert (changed.value("agent_memory"), changed.value("pod_memory")) == (8 * GIB, 8 * GIB)
    assert made.value("agent_memory") == 4 * GIB, "the profile it came from unchanged"
    assert {k: changed.values[k] for k in profile.KEYS if k not in ("pod_memory", "agent_memory")} == {
        k: made.values[k] for k in profile.KEYS if k not in ("pod_memory", "agent_memory")
    }
    assert profile.to_limits(changed) == Limits().with_values({"pod_memory": "8g", "agent_memory": "8g"})
    with pytest.raises(AttributeError):
        changed.values = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        changed.values["agent_memory"] = Value(1, by)  # type: ignore[index]


def test_a_value_replaced_is_checked_as_vivibox_limits_checks_it():
    made = profile.from_limits(Limits(), {})
    by = Provenance.person("you", AT)
    with pytest.raises(PastThePod):
        made.with_value("agent_memory", "8g", by)
    with pytest.raises(PastThePod):
        made.with_value("pod_memory", "1g", by)
    with pytest.raises(ValueError, match="limits.gate_pids"):
        made.with_value("gate_pids", 10_000, by)
    with pytest.raises(ValueError, match="not a limit"):
        made.with_value("index_memory", "2g", by)
    with pytest.raises(ValueError, match="not a key"):
        made.with_value("swap", "2g", by)


def test_the_language_server_s_memory_is_counted_apart_and_changes_no_limit():
    made = profile.from_limits(Limits(), {})
    counted = made.with_value("agent_language_server_memory", "3g", Provenance.person("you", AT))
    assert counted.value("agent_language_server_memory") == 3 * GIB
    assert counted.memory("agent") == 4 * GIB
    assert profile.to_limits(counted) == Limits()
    assert counted.with_value("agent_language_server_memory", None, Provenance(Source.DEFAULT)) == made
    with pytest.raises(ValueError, match="agent_language_server_memory"):
        made.with_value("agent_language_server_memory", "much", Provenance(Source.TASK))


def test_the_pod_s_memory_is_today_s_budget_and_never_the_containers_sum():
    """pod_memory kept as it is (a figure each container's limit is checked against), whatever its
    containers add up to: what it is to mean waits for gate G4."""
    made = profile.from_limits(Limits(), {"pod_memory": "5g"})
    assert made.value("pod_memory") == 5 * GIB
    assert sum(made.memory(role) for role in ROLES) > made.value("pod_memory")
    assert profile.to_limits(made).pod_memory == 5 * GIB
    raised = made.with_value("pod_memory", "7g", Provenance.person("you", AT))
    assert {role: raised.memory(role) for role in CONTAINERS} == DEFAULT_MEMORY


def test_the_json_form_comes_back_the_same_profile():
    made = profile.from_limits(Limits().with_values(MACHINE), OWN).with_value(
        "gate_memory", "6g", Provenance.person("cli-agent:codex", AT)
    )
    form = json.loads(json.dumps(made.to_json()))
    assert form["gate_memory"] == {"value": 6 * GIB, "source": "person", "actor": "cli-agent:codex", "at": AT}
    assert form["agent_memory"] == {"value": 10 * GIB, "source": "task"}
    assert form["agent_language_server_memory"] == {"value": None, "source": "default"}
    back = ResourceProfile.from_json(form)
    assert back == made
    assert profile.to_limits(back) == profile.to_limits(made)
    default = profile.from_limits(Limits(), {})
    assert ResourceProfile.from_json(json.loads(json.dumps(default.to_json()))) == default


def test_a_json_form_that_is_not_a_whole_profile_is_refused():
    good = profile.from_limits(Limits(), {}).to_json()
    for broken in (
        {k: v for k, v in good.items() if k != "disk"},
        {**good, "swap_memory": {"value": GIB, "source": "default"}},
        {**good, "disk": {"value": 20 * GIB}},
        {**good, "disk": {"value": 20 * GIB, "source": "planner"}},
        {**good, "disk": {"value": "20g", "source": "default"}},
        {**good, "pod_pids": {"value": True, "source": "default"}},
        {**good, "pod_pids": {"value": 0, "source": "default"}},
        {**good, "pod_cpus": {"value": -1, "source": "default"}},
        {**good, "agent_memory": {"value": None, "source": "default"}},
        {**good, "agent_memory": {"value": GIB, "source": "person"}},
        {**good, "agent_memory": {"value": GIB, "source": "task", "extra": 1}},
        {**good, "agent_memory": {"value": 8 * GIB, "source": "task"}},
        {**good, "agent_pids": {"value": 9000, "source": "task"}},
        {**good, "agent_language_server_memory": {"value": "3g", "source": "default"}},
        [],
    ):
        with pytest.raises(ValueError):
            ResourceProfile.from_json(broken)


def test_nothing_is_without_where_it_came_from():
    with pytest.raises(TypeError):
        Value(GIB)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="where it came from"):
        Value(GIB, "default")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="every key"):
        ResourceProfile({"agent_memory": Value(GIB, Provenance(Source.DEFAULT))})
