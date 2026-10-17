"""The planner's code index with real Serena on Keycloak, bounded: provisioning (the language
servers and the Maven import's dependencies, with the internet), Serena on I's private copy, the
symbol, definition, reference and implementation tools through the planner's wrapper, a refresh
after a verified commit, a warm restart, no network in the steady I. Not a whole-project index,
not a planner's turn on a real model, not the tuning of I's limits: the figures it prints
(-s) are one run's.

Opt-in: VIVIBOX_KEYCLOAK names a local clone of github.com/keycloak/keycloak (at
VIVIBOX_KEYCLOAK_COMMIT, else its HEAD); VIVIBOX_KEYCLOAK_MEMORY is I's memory (default 8g).
Needs what tests/test_index_serena_live.py needs, about 4 GB of the task's disk and half an hour.
Run with: VIVIBOX_KEYCLOAK=~/src/keycloak uv run pytest -m docker tests/test_index_keycloak_live.py -s
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_control_live import a_pod, a_task, gone, sh
from test_index_serena_live import (
    answered,
    ask,
    cgroup,
    commit,
    index_side,
    report,
    sizes,
    state,
    wait_for_socket,
)
from test_pod_live import TASKS_ROOT

from vivibox import control, image, serena_index
from vivibox.pod_egress import helper_missing
from vivibox.podspec import parse_size

pytestmark = pytest.mark.docker

SPI = "server-spi/src/main/java/org/keycloak/models"
SESSION = f"{SPI}/KeycloakSession.java"
ROLES = "model/storage-private/src/main/java/org/keycloak/storage/RoleStorageManager.java"
NARROW = {"name_path": "KeycloakSession/enlistForClose", "relative_path": SESSION}


@pytest.fixture(scope="module")
def keycloak():
    source = os.environ.get("VIVIBOX_KEYCLOAK", "")
    if not source:
        pytest.skip("set VIVIBOX_KEYCLOAK to a local clone of Keycloak")
    if not TASKS_ROOT.is_dir():
        pytest.skip("run host/setup.sh first")
    if why := helper_missing():
        pytest.skip(why)
    wanted = os.environ.get("VIVIBOX_KEYCLOAK_COMMIT", "HEAD")
    ref, _ = image.build()
    task = a_task("kcindex")
    sh("git", "fetch", "-q", "--depth", "1", f"file://{Path(source).expanduser()}", wanted, cwd=task.repo)
    sh("git", "reset", "-q", "--hard", "FETCH_HEAD", cwd=task.repo)
    head = sh("git", "rev-parse", "HEAD", cwd=task.repo).stdout.strip()
    pod = a_pod(task, ref)
    pod.index_server = serena_index.server(task.repo)
    pod.limits.memory["index"] = parse_size(os.environ.get("VIVIBOX_KEYCLOAK_MEMORY", "8g"))
    try:
        started = time.monotonic()
        pod.up()
        provisioned = time.monotonic() - started
        assert pod.index_failure == "", pod.index_failure
        control.refresh(task, pod)
        listening = wait_for_socket(pod, seconds=600)
        overview, first = answered(
            pod,
            "get_symbols_overview",
            {"relative_path": SESSION},
            lambda t: "enlistForClose" in t,
            seconds=900,
        )
        report(
            "keycloak cold",
            commit=head,
            languages=pod.index_server.command[pod.index_server.command.index("--languages") + 1],
            memory_max=cgroup(pod, "memory.max"),
            up_with_provisioning=round(provisioned, 1),
            socket=round(listening, 1),
            first_answer=round(first, 1),
            peak=cgroup(pod, "memory.peak"),
            pids_peak=cgroup(pod, "pids.peak"),
            store=sizes(pod),
        )
        yield SimpleNamespace(task=task, pod=pod, head=head)
    finally:
        gone(task, pod)


def test_serena_answers_symbols_definitions_references_and_implementations_on_keycloak(keycloak):
    pod, timed = keycloak.pod, {}

    def timed_ask(label: str, name: str, arguments: dict) -> str:
        started = time.monotonic()
        text, failed = ask(pod, name, arguments)
        timed[label] = round(time.monotonic() - started, 2)
        assert not failed, text[:2000]
        return text.split("\n", 1)[1]

    symbol = timed_ask(
        "find_symbol in a folder",
        "find_symbol",
        {"name_path_pattern": "KeycloakSession/enlistForClose", "relative_path": SPI},
    )
    assert '"name_path": "KeycloakSession/enlistForClose"' in symbol
    declared = timed_ask(
        "find_declaration from a usage in another module",
        "find_declaration",
        {
            "relative_path": "model/storage/src/main/java/org/keycloak/storage/AbstractStorageManager.java",
            "regex": "session\\.(enlistForClose)\\(instance\\)",
        },
    )
    assert '"name_path": "KeycloakSession/enlistForClose"' in declared and SESSION in declared
    implementations = timed_ask(
        "find_implementations of the interface",
        "find_implementations",
        {"name_path": "KeycloakSession", "relative_path": SESSION},
    )
    assert "DefaultKeycloakSession" in implementations
    method = timed_ask("find_implementations of a method", "find_implementations", NARROW)
    assert "DefaultKeycloakSession" in method
    references, waited = answered(
        pod, "find_referencing_symbols", NARROW, lambda t: "AbstractStorageManager.java" in t, seconds=600
    )
    files = sorted(json.loads(references.split("\n", 1)[1]))
    assert {"model/storage/src/main/java/org/keycloak/storage/AbstractStorageManager.java", ROLES} <= set(
        files
    ), files
    wide, _ = ask(
        pod,
        "find_referencing_symbols",
        {"name_path": "KeycloakSession/getKeycloakSessionFactory", "relative_path": SESSION},
    )
    report(
        "keycloak queries",
        times=timed,
        narrow_reference_files=files,
        narrow_references_waited=round(waited, 1),
        wide_references=wide[:400],
        peak=cgroup(pod, "memory.peak"),
    )


def test_a_refresh_on_keycloak_answers_for_the_verified_commit(keycloak):
    task, pod = keycloak.task, keycloak.pod
    caller = (
        "package org.keycloak.models;\n\npublic class IndexRefreshCaller {\n"
        "    public void close(KeycloakSession session, org.keycloak.provider.Provider p) {\n"
        "        session.enlistForClose(p);\n    }\n}\n"
    )
    roles = (
        (task.repo / ROLES)
        .read_text()
        .replace("session.enlistForClose(instance);", "// removed by the refresh test")
    )
    later = commit(task, {f"{SPI}/IndexRefreshCaller.java": caller, ROLES: roles}, "the verified commit")
    started = time.monotonic()
    control.refresh(task, pod, later)
    synced = time.monotonic() - started
    text, waited = answered(
        pod, "find_referencing_symbols", NARROW, lambda t: "IndexRefreshCaller.java" in t, seconds=600
    )
    assert ROLES not in text, "the call the verified commit removed"
    report(
        "keycloak refresh",
        sync=round(synced, 1),
        correct_answer_after=round(waited, 1),
        peak=cgroup(pod, "memory.peak"),
    )
    keycloak.head = later


def test_a_warm_restart_on_keycloak_and_no_network_in_the_steady_index(keycloak):
    pod = keycloak.pod
    pod.down()
    started = time.monotonic()
    pod.up()
    assert pod.index_failure == "" and state(pod.index_provision) == ""
    control.refresh(keycloak.task, pod, keycloak.head)
    listening = wait_for_socket(pod, seconds=600)
    _, first = answered(
        pod, "get_symbols_overview", {"relative_path": SESSION}, lambda t: "enlistForClose" in t, seconds=600
    )
    text, waited = answered(
        pod, "find_referencing_symbols", NARROW, lambda t: "IndexRefreshCaller.java" in t, seconds=600
    )
    assert ROLES not in text
    attached = subprocess.run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", pod.index],
        capture_output=True,
        text=True,
    )
    assert list(json.loads(attached.stdout)) == ["none"]
    interfaces = index_side(pod).exec("sh", "-c", "tail -n +3 /proc/net/dev | cut -d: -f1").stdout.split()
    assert interfaces == ["lo"]
    report(
        "keycloak warm restart",
        socket=round(listening, 1),
        first_answer=round(time.monotonic() - started, 1),
        overview=round(first, 1),
        references=round(waited, 1),
        peak=cgroup(pod, "memory.peak"),
        store=sizes(pod),
    )
