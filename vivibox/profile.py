"""A task's resource profile: every figure of what its containers may use, as podspec.Limits has
them, each with where it came from (vivibox's default, the machine's config.toml, the task's own,
or a person's change, with who and when), and the agent's language server as a figure of its own,
unaccounted (None) until there are measurements of it.

A model only: nothing in the product reads or writes a profile yet. It is made from the limits a
task runs with today and gives back the very same Limits (to_limits(from_limits(m, own)) is
limits.for_task's Limits, to the byte, with the same flags), and it is checked as Limits checks
them: a value replaced goes through Limits.with_values. pod_memory is today's figure, a budget each
container's limit is checked against and not their sum (docs/configure.md); what it is to mean is
not decided here. Nothing adds the figures up: what a task costs is not this module's."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from .podspec import LIMIT_KEYS, ROLES, Limits, parse_size

# The containers a profile names: the ones of [limits] and the planner's code index, which has
# figures of its own in Limits but is no key of [limits] (it can be read here, not changed).
CONTAINERS = (*ROLES, "index")
# The part of the agent's memory its language server takes: counted apart, within the agent's
# container's limit, and enforced by nothing but that limit.
LANGUAGE_SERVER = "agent_language_server_memory"
KEYS = (*LIMIT_KEYS, "index_memory", "index_pids", LANGUAGE_SERVER)


class Source(StrEnum):
    DEFAULT = "default"  # vivibox's own figure
    MACHINE = "machine"  # the machine's config.toml, [limits]
    TASK = "task"  # the task's own, .task/limits.json
    PERSON = "person"  # a person's change, with who made it and when


@dataclass(frozen=True)
class Provenance:
    """Where a value came from; a person's change alone names who made it (actor: "you", or the
    host's agent as "cli-agent:<tool>") and when (at: ISO 8601 with its zone, as an event's ts)."""

    source: Source
    actor: str = ""
    at: str = ""

    def __post_init__(self):
        object.__setattr__(self, "source", Source(self.source))
        if self.source is not Source.PERSON:
            if self.actor or self.at:
                raise ValueError("only a person's change names an actor and a time")
            return
        if not isinstance(self.actor, str) or not self.actor or not _zoned(self.at):
            raise ValueError("a person's change names who made it and when, a time with its zone")

    @classmethod
    def person(cls, actor: str, at: str) -> Provenance:
        return cls(Source.PERSON, actor, at)


def _zoned(text) -> bool:
    try:
        return isinstance(text, str) and datetime.fromisoformat(text).tzinfo is not None
    except ValueError:
        return False


@dataclass(frozen=True)
class Value:
    value: int | float | None
    provenance: Provenance

    def __post_init__(self):
        if not isinstance(self.provenance, Provenance):
            raise ValueError(f"a value says where it came from, not {self.provenance!r}")


@dataclass(frozen=True)
class ResourceProfile:
    """Every key of KEYS with its value, read only; checked whole when made: each value of its
    kind, and the limits it gives passing what Limits checks across them (a container's memory at
    most the pod's, its processes at most the pod's)."""

    values: Mapping[str, Value]

    def __post_init__(self):
        if set(self.values) != set(KEYS):
            missing, extra = sorted(set(KEYS) - set(self.values)), sorted(set(self.values) - set(KEYS))
            raise ValueError(f"a resource profile has every key, no other: missing {missing}, extra {extra}")
        for key in KEYS:
            found = self.values[key]
            if not isinstance(found, Value):
                raise ValueError(f"{key}: not a value with where it came from: {found!r}")
            _check(key, found.value)
        object.__setattr__(self, "values", MappingProxyType({key: self.values[key] for key in KEYS}))
        to_limits(self).with_values({})

    def value(self, key: str) -> int | float | None:
        return self.values[key].value

    def provenance(self, key: str) -> Provenance:
        return self.values[key].provenance

    def memory(self, container: str) -> int:
        return self.values[f"{container}_memory"].value

    def pids(self, container: str) -> int:
        return self.values[f"{container}_pids"].value

    def with_value(self, key: str, value, provenance: Provenance) -> ResourceProfile:
        """This profile with one value replaced, given as [limits] takes it ("8g", 2048, 2.5) and
        checked by Limits.with_values as config.toml's and limits.json's are; the language
        server's a size or None. ValueError names the key."""
        if key not in KEYS:
            raise ValueError(f"{key} is not a key of a resource profile; they are {', '.join(KEYS)}")
        if key == LANGUAGE_SERVER:
            try:
                found = None if value is None else parse_size(value)
            except ValueError as e:
                raise ValueError(f"{key}: {e}") from None
        else:
            found = _of(to_limits(self).with_values({key: value}), key)
        return ResourceProfile({**self.values, key: Value(found, provenance)})

    def to_json(self) -> dict:
        """The profile as JSON keeps it: by key, the value (bytes, a count, CPUs, None) and where
        it came from, with the actor and the time of a person's change."""
        form = {}
        for key, found in self.values.items():
            origin = found.provenance
            entry = {"value": found.value, "source": str(origin.source)}
            if origin.source is Source.PERSON:
                entry |= {"actor": origin.actor, "at": origin.at}
            form[key] = entry
        return form

    @classmethod
    def from_json(cls, form) -> ResourceProfile:
        """A profile from its JSON form, checked whole as one made here; ValueError for anything else."""
        if not isinstance(form, dict):
            raise ValueError("a resource profile is an object of its keys")
        values = {}
        for key, entry in form.items():
            if not isinstance(entry, dict) or not {"value", "source"} <= set(entry) <= ENTRY:
                raise ValueError(f"{key}: a value and its source (and a person's actor and at), nothing else")
            origin = Provenance(entry["source"], entry.get("actor", ""), entry.get("at", ""))
            values[key] = Value(entry["value"], origin)
        return cls(values)


ENTRY = {"value", "source", "actor", "at"}


def _check(key: str, value) -> None:
    """A value of its key's kind: a whole number above zero (bytes, a count), CPUs above zero, or
    for the language server also None; the ranges across keys are Limits'."""
    if key == LANGUAGE_SERVER and value is None:
        return
    whole = isinstance(value, int) and not isinstance(value, bool)
    if key == "pod_cpus":
        whole = whole or isinstance(value, float)
    if not whole or not value > 0:
        raise ValueError(f"{key}: not a figure above zero: {value!r}")


def _of(found: Limits, key: str) -> int | float | None:
    if key == LANGUAGE_SERVER:
        return None
    if key in ("pod_memory", "pod_cpus", "pod_pids", "disk"):
        return getattr(found, key)
    container, _, kind = key.rpartition("_")
    return (found.memory if kind == "memory" else found.pids)[container]


def from_limits(machine: Limits, overrides: dict) -> ResourceProfile:
    """The profile of a task run with the machine's limits and its own over them (limits.json's
    keys, as limits.for_task reads them, checked so): a key of the task's is the task's, one the
    machine's differs in is the machine's, the rest vivibox's. A machine's value equal to
    vivibox's is the default: Limits keeps no word of which keys config.toml said."""
    found, default = machine.with_values(overrides), Limits()
    values = {}
    for key in KEYS:
        if key in overrides:
            source = Source.TASK
        elif _of(machine, key) != _of(default, key):
            source = Source.MACHINE
        else:
            source = Source.DEFAULT
        values[key] = Value(_of(found, key), Provenance(source))
    return ResourceProfile(values)


def to_limits(p: ResourceProfile) -> Limits:
    """The Limits the profile's containers run with, as Limits.flags makes their arguments; the
    language server's figure is in none of them."""
    return Limits(
        pod_memory=p.value("pod_memory"),
        pod_cpus=float(p.value("pod_cpus")),
        pod_pids=p.value("pod_pids"),
        memory={container: p.memory(container) for container in CONTAINERS},
        pids={container: p.pids(container) for container in CONTAINERS},
        disk=p.value("disk"),
    )
