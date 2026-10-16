"""The planner's code index wrapper in front of an index that is fully compromised: nothing the
index says reaches the harness but a result's text, cleaned and marked as data; no request of
the index's reaches it at all; every call is bounded in bytes and in time; and a malformed
answer closes the connection."""

from __future__ import annotations

import json
import os
import socket
import time
import tracemalloc

import pytest
from hostile_index import MARK, RAW, HostileIndex
from index_harness import INITIALIZE, INITIALIZED, VALID, call, harness, listing, said

from vivibox.index import Limits, channel, server, tools
from vivibox.index.session import Failed, Failure, Session

FAST = Limits(call_seconds=2.0, connect_seconds=2.0)


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "socket"
    path.mkdir()
    return path


@pytest.fixture
def index(folder):
    found = HostileIndex(folder)
    yield found
    found.close()


def session(folder, limits: Limits = FAST) -> Session:
    return Session(channel.unix_entry(str(folder), "index.sock"), limits)


def one_call(folder, name="find_symbol", arguments=None, limits: Limits = FAST) -> tuple[str, bool]:
    responses, _ = harness(
        session(folder, limits), call(7, name, VALID[name] if arguments is None else arguments)
    )
    return said(responses[0])


def test_the_harness_learns_the_wrappers_server_and_tools_and_nothing_of_the_index(index, folder):
    index.mode = "mutate"
    responses, raw = harness(
        session(folder),
        INITIALIZE,
        INITIALIZED,
        listing(2),
        call(3, "find_symbol", VALID["find_symbol"]),
        listing(4),
        call(5, "get_symbols_overview", VALID["get_symbols_overview"]),
        listing(6),
    )
    assert MARK.encode() not in raw
    assert responses[0]["result"] == {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": server.SERVER,
    }
    lists = [r["result"] for r in responses if r["id"] in (2, 4, 6)]
    assert lists == [{"tools": tools.listed()}] * 3
    assert [t["name"] for t in lists[0]["tools"]] == [
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
        "find_implementations",
        "find_declaration",
        "search_for_pattern",
    ]
    # The index's handshake answer and notifications were sent; the wrapper never asked for its list.
    assert [m.get("method") for m in index.received[0]] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
    ]


def test_the_index_is_offered_no_capability_and_answered_nothing(index, folder):
    one_call(folder)
    hello = index.received[0][0]
    assert hello["params"]["capabilities"] == {}
    assert set(hello["params"]) == {"protocolVersion", "capabilities", "clientInfo"}
    # roots/list, sampling/createMessage, elicitation/create and ping: none got a reply.
    assert index.answered == []


def test_a_request_of_the_index_never_reaches_the_harness_nor_a_model(index, folder):
    index.mode = "sample"
    responses, raw = harness(
        session(folder), INITIALIZE, INITIALIZED, call(2, "find_symbol", VALID["find_symbol"])
    )
    assert [r["id"] for r in responses] == [1, 2]
    assert all("method" not in r for r in responses)
    assert said(responses[1]) == (f"Result of find_symbol {tools.DATA}:\nsampled", False)
    assert MARK.encode() not in raw
    assert index.answered == []


@pytest.mark.parametrize("name", list(VALID))
def test_a_valid_answer_of_every_tool_passes_through_under_the_wrappers_line(index, folder, name):
    text, failed = one_call(folder, name)
    tool, sent = tools.forwarded(name, VALID[name], FAST)
    assert not failed
    assert text == f"Result of {name} {tools.DATA}:\nanswer of {name}: " + json.dumps(sent, sort_keys=True)
    assert [c["params"] for c in index.calls()] == [{"name": name, "arguments": sent}]


def test_a_long_result_is_cut_with_the_wrappers_note_before_the_data_never_after(index, folder):
    index.mode = "wide"
    text, failed = one_call(folder)
    head, data = text.split("\n", 1)
    assert not failed
    assert head == (
        f"Result of find_symbol, cut at {FAST.text_chars} characters; "
        f"a narrower call shows the rest {tools.DATA}:"
    )
    assert data == "W" * FAST.text_chars
    assert MARK not in text


def test_an_answer_past_the_byte_limit_is_not_read_and_the_next_call_connects_again(index, folder):
    index.mode = "big"
    s = session(folder)
    responses, _ = harness(
        s, call(1, "find_symbol", VALID["find_symbol"]), call(2, "find_symbol", VALID["find_symbol"])
    )
    assert [said(r) for r in responses] == [(tools.failure("too_large", FAST), True)] * 2
    assert len(index.received) == 2
    assert [len([m for m in got if m.get("method") == "tools/call"]) for got in index.received] == [1, 1]


def test_an_endless_answer_stops_at_the_byte_limit_holding_no_more_than_it(index, folder):
    index.mode = "endless"
    s = session(folder)
    tracemalloc.start()
    try:
        with pytest.raises(Failed) as e:
            s.call("find_symbol", tools.forwarded("find_symbol", VALID["find_symbol"], FAST)[1])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert e.value.failure is Failure.TOO_LARGE
    assert peak < 2 * FAST.response_bytes


def test_a_silent_index_is_ended_at_the_time_limit_and_the_call_is_never_sent_again(index, folder):
    index.mode = "hang"
    limits = Limits(call_seconds=0.3, connect_seconds=2.0)
    started = time.monotonic()
    responses, _ = harness(
        session(folder, limits),
        call(1, "find_symbol", VALID["find_symbol"]),
        call(2, "search_for_pattern", VALID["search_for_pattern"]),
    )
    assert time.monotonic() - started < 3
    assert [said(r) for r in responses] == [(tools.failure("timeout", limits), True)] * 2
    assert [
        [c["params"]["name"] for c in got if c.get("method") == "tools/call"] for got in index.received
    ] == [
        ["find_symbol"],
        ["search_for_pattern"],
    ]


def test_a_stream_of_notifications_without_an_answer_ends_at_a_limit(index, folder):
    index.mode = "progress"
    limits = Limits(call_seconds=0.5, connect_seconds=2.0)
    started = time.monotonic()
    text, failed = one_call(folder, limits=limits)
    assert time.monotonic() - started < 3
    assert failed and text in (tools.failure("timeout", limits), tools.failure("too_large", limits))


def test_a_message_longer_than_the_wrapper_holds_ends_the_call(index, folder):
    index.mode = "slow_line"
    assert one_call(folder) == (tools.failure("too_large", FAST), True)


@pytest.mark.parametrize("mode", sorted(RAW))
def test_a_malformed_answer_fails_closed(index, folder, mode):
    index.mode = mode
    s = session(folder)
    responses, raw = harness(
        s, call(1, "find_symbol", VALID["find_symbol"]), call(2, "find_symbol", VALID["find_symbol"])
    )
    assert said(responses[0]) == (tools.failure("malformed", FAST), True)
    assert MARK.encode() not in raw
    assert len(index.received) == 2  # disconnected, and connected again for the next call


def test_a_connection_closed_inside_the_answer_gives_no_part_of_it(index, folder):
    index.mode = "close"
    responses, raw = harness(session(folder), call(1, "find_symbol", VALID["find_symbol"]))
    assert said(responses[0]) == (tools.failure("unavailable", FAST), True)
    assert MARK.encode() not in raw


def test_an_answer_to_another_id_is_never_the_result(index, folder):
    index.mode = "forged"
    assert one_call(folder) == (f"Result of find_symbol {tools.DATA}:\nthe real answer", False)


def test_what_the_index_sends_ahead_of_a_request_is_not_its_answer(index, folder):
    index.mode = "ahead"
    responses, raw = harness(
        session(folder),
        call(1, "find_symbol", VALID["find_symbol"]),
        call(2, "find_symbol", VALID["find_symbol"]),
    )
    assert [said(r) for r in responses] == [(f"Result of find_symbol {tools.DATA}:\nfirst answer", False)] * 2
    assert MARK.encode() not in raw
    assert len(index.received) == 1


def test_an_error_of_the_index_is_told_in_the_wrappers_words_and_the_connection_stays(index, folder):
    index.mode = "error"
    responses, raw = harness(
        session(folder),
        call(1, "find_symbol", VALID["find_symbol"]),
        call(2, "find_symbol", VALID["find_symbol"]),
    )
    assert [said(r) for r in responses] == [(tools.failure("refused", FAST), True)] * 2
    assert MARK.encode() not in raw
    assert len(index.received) == 1


def test_a_tool_error_reaches_the_model_as_data_under_the_wrappers_line(index, folder):
    index.mode = "tool_error"
    assert one_call(folder) == (
        f"The index reported an error for find_symbol; its words follow {tools.DATA}:\n{MARK}-tool-error",
        True,
    )


@pytest.mark.parametrize("mode", ["parts", "structured"])
def test_only_text_parts_reach_the_model(index, folder, mode):
    index.mode = mode
    expected = "the text part" if mode == "parts" else ""
    assert one_call(folder) == (f"Result of find_symbol {tools.DATA}:\n{expected}", False)


def test_control_and_invisible_characters_reach_the_model_as_code_points(index, folder):
    index.mode = "binary"
    text, _ = one_call(folder)
    assert text.split("\n", 1)[1] == (
        "a‹U+0000›b‹U+0007›c‹U+001B›[2Jd‹U+001B›]0;t‹U+0007›e‹U+007F›f‹U+202E›g‹U+200B›h‹U+2028›i"
        "‹U+0085›j\tk\nl‹U+D800›m"
    )


def test_one_line_a_message_to_the_index_whatever_the_arguments(index, folder):
    tricky = {"name_path_pattern": 'A"\n}{ \r\x00', "relative_path": "src/‮/x"}
    one_call(folder, "find_symbol", tricky)
    sent = b"".join(index.raw)
    assert sent.count(b"\n") == 3
    assert all(b < 0x80 for b in sent)
    assert index.calls()[0]["params"]["arguments"] == {**tricky, "max_answer_chars": FAST.text_chars}


@pytest.mark.parametrize(
    "arguments",
    [
        {"name_path_pattern": "A"},  # no path: the whole project
        {"name_path_pattern": "A", "relative_path": "."},
        {"name_path_pattern": "A", "relative_path": "/etc"},
        {"name_path_pattern": "A", "relative_path": "src/../.."},
        {"name_path_pattern": "A", "relative_path": "src", "max_answer_chars": 10**9},
        {"name_path_pattern": "A", "relative_path": "src", "depth": True},
        {"name_path_pattern": "A", "relative_path": "src", "include_body": 1},
        {"name_path_pattern": "A" * 513, "relative_path": "src"},
        {"name_path_pattern": ["A"], "relative_path": "src"},
        "not an object",
    ],
)
def test_a_call_outside_the_tools_schema_never_reaches_the_index(index, folder, arguments):
    text, failed = one_call(folder, "find_symbol", arguments)
    assert failed and text.startswith("Not a call find_symbol takes: ")
    assert index.received == []


@pytest.mark.parametrize(
    "name", ["replace_in_files", "initial_instructions", "bash", "index_find_symbol", None, 3]
)
def test_a_tool_not_on_the_list_never_reaches_the_index(index, folder, name):
    responses, _ = harness(
        session(folder), {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
    )
    assert said(responses[0]) == ("Not a tool of the index.", True)
    assert index.received == []


@pytest.mark.parametrize("kind", ["link", "file", "fifo", "directory"])
def test_something_else_in_the_sockets_place_is_never_connected_to(tmp_path, folder, kind):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = HostileIndex(elsewhere)
    try:
        entry = folder / "index.sock"
        {
            "link": lambda: entry.symlink_to(elsewhere / "index.sock"),
            "file": lambda: entry.write_text("x"),
            "fifo": lambda: os.mkfifo(entry),
            "directory": lambda: entry.mkdir(),
        }[kind]()
        with pytest.raises(channel.NotASocket):
            channel.unix_entry(str(folder), "index.sock")(1.0)
        assert one_call(folder) == (tools.failure("unavailable", FAST), True)
        time.sleep(0.05)
        assert real.received == []
    finally:
        real.close()


def test_a_socket_directory_that_is_a_link_is_never_followed(tmp_path, index, folder):
    link = tmp_path / "link"
    link.symlink_to(folder)
    with pytest.raises(OSError):
        channel.unix_entry(str(link), "index.sock")(1.0)
    assert index.received == []


def test_no_index_at_all_is_unavailable(folder):
    assert one_call(folder) == (tools.failure("unavailable", FAST), True)


def test_a_socket_with_no_one_listening_is_unavailable(folder):
    dead = socket.socket(socket.AF_UNIX)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    dead.bind(f"/proc/self/fd/{fd}/index.sock")
    os.close(fd)
    dead.close()
    assert one_call(folder) == (tools.failure("unavailable", FAST), True)


class Victim:
    """A socket of the planner's own side, listening where the index cannot reach it but by a
    link: it records whatever connects to it."""

    def __init__(self, directory) -> None:
        self.path = directory / "victim.sock"
        self.got: list[bytes] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(4)
        self._sock.settimeout(0.2)

    def connected(self) -> bool:
        try:
            conn, _ = self._sock.accept()
        except TimeoutError:
            return False
        conn.settimeout(0.2)
        try:
            self.got.append(conn.recv(4096))
        except TimeoutError:
            self.got.append(b"")
        conn.close()
        return True

    def close(self) -> None:
        self._sock.close()


@pytest.mark.parametrize("swap", ["link", "gone", "file"])
def test_an_entry_swapped_between_its_check_and_the_connect_never_redirects_it(
    tmp_path, monkeypatch, index, folder, swap
):
    """The index changes its socket's name after the wrapper checked it and before the connect:
    to a link to a socket of the planner's side, to nothing, to a file. The connect still
    reaches the socket that was checked, and nothing reaches the planner's side."""
    elsewhere = tmp_path / "planner-side"
    elsewhere.mkdir()
    victim = Victim(elsewhere)
    entry = folder / "index.sock"
    aside = folder / "index.sock.aside"

    def swapped() -> None:
        os.rename(entry, aside)  # the listener keeps its socket; only its name moves
        if swap == "link":
            entry.symlink_to(victim.path)
        elif swap == "file":
            entry.write_text("x")

    class Swapping(socket.socket):
        def connect(self, address):
            if self.family == socket.AF_UNIX and entry.is_socket() and not entry.is_symlink():
                swapped()
            return super().connect(address)

    monkeypatch.setattr(channel.socket, "socket", Swapping)
    try:
        text, failed = one_call(folder)
        assert not failed and "answer of find_symbol" in text
        assert len(index.calls()) == 1
        assert not entry.is_socket() or entry.is_symlink(), "the swap happened before the connect"
        assert not victim.connected() and victim.got == []
    finally:
        victim.close()


def test_a_descriptor_the_index_passes_over_the_socket_is_never_taken(tmp_path, index, folder):
    """A unix socket carries descriptors: one of the index's, sent with its answer, is not
    received by the wrapper, which reads bytes and no ancillary data."""
    index.mode, index.passed = "pass_fd", str(tmp_path / "the-index-s-own")
    (tmp_path / "the-index-s-own").write_text("x")

    def held() -> int:
        return sum(
            os.readlink(f"/proc/self/fd/{fd}") == index.passed
            for fd in os.listdir("/proc/self/fd")
            if os.path.exists(f"/proc/self/fd/{fd}")
        )

    assert one_call(folder) == (tools.result(tools.TOOLS[1], ["with a descriptor"], False, FAST), False)
    assert held() == 0
