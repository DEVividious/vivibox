"""The index wrapper's parser and its one call at a time, on a channel and a clock of the test's:
what the bounds are when the bytes come slowly, not at all, or shaped to cost the most."""

from __future__ import annotations

import json
import re
import time
import tracemalloc

import pytest
from index_harness import INITIALIZE, Clock, Scripted, call, greeted, harness

from vivibox.index import Limits, protocol
from vivibox.index.session import DEFAULT, Failed, Failure, Session


def answer(words: str):
    return greeted(
        lambda m: (
            json.dumps(
                {"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": words}]}}
            ).encode()
            + b"\n"
        )
    )


def scripted(answer_with, limits: Limits = DEFAULT, **how) -> tuple[Session, Scripted, Clock]:
    clock = Clock()
    channel = Scripted(clock, answer_with, **how)
    return Session(lambda timeout: channel, limits, clock=clock), channel, clock


def test_bytes_that_come_one_at_a_time_end_at_the_deadline_not_at_each_receive():
    limits = Limits(call_seconds=50, connect_seconds=10)
    s, channel, clock = scripted(answer("x" * 1000), limits, step=0.1, piece=1)
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {})
    greeting = len(json.dumps({"jsonrpc": "2.0", "id": "0" * 32, "result": {}})) + 1
    assert e.value.failure is Failure.TIMEOUT
    # The handshake's answer at a byte a receive, then the call's deadline, and not a step more.
    assert clock.now == pytest.approx(greeting * 0.1 + 50, abs=0.11)
    assert channel.closed


def test_an_index_that_never_answers_is_ended_at_the_call_s_time_limit():
    limits = Limits(call_seconds=120, connect_seconds=60)
    s, channel, clock = scripted(greeted(lambda m: b""), limits)
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {})
    assert e.value.failure is Failure.TIMEOUT
    assert clock.now == 120
    assert [m.get("method") for m in channel.sent] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


def test_an_index_that_never_greets_is_ended_at_the_connection_s_time_limit():
    limits = Limits(call_seconds=120, connect_seconds=60)
    s, channel, clock = scripted(lambda m: b"", limits)
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {})
    assert e.value.failure is Failure.CONNECT_TIMEOUT
    assert clock.now == 60
    assert [m.get("method") for m in channel.sent] == ["initialize"]


def test_a_handshake_refused_is_no_connection():
    def refusing(m):
        return (
            json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": 1, "message": "no"}}).encode()
            + b"\n"
        )

    s, channel, _ = scripted(refusing)
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {})
    assert e.value.failure is Failure.UNAVAILABLE
    assert channel.closed


def test_every_request_has_an_id_the_index_cannot_guess():
    s, channel, _ = scripted(answer("ok"))
    for _ in range(3):
        s.call("find_symbol", {})
    ids = [m["id"] for m in channel.sent if "id" in m]
    assert len(set(ids)) == 4
    assert all(isinstance(i, str) and re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)


def test_an_answer_whose_id_matches_only_loosely_is_not_the_answer():
    """1 == 1.0 == True in Python: the id is compared as the string the wrapper made."""

    def loose(m):
        lines = [
            {"jsonrpc": "2.0", "id": [m["id"]], "result": {"content": [{"type": "text", "text": "list"}]}},
            {
                "jsonrpc": "2.0",
                "id": {"id": m["id"]},
                "result": {"content": [{"type": "text", "text": "dict"}]},
            },
            {
                "jsonrpc": "2.0",
                "id": m["id"] + " ",
                "result": {"content": [{"type": "text", "text": "space"}]},
            },
        ]
        return b"".join(json.dumps(one).encode() + b"\n" for one in lines)

    s, _, _ = scripted(greeted(loose), Limits(call_seconds=5))
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {})
    assert e.value.failure is Failure.TIMEOUT


def test_a_request_longer_than_the_limit_is_never_sent():
    s, channel, _ = scripted(answer("ok"), Limits(request_bytes=1024))
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {"name_path_pattern": "x" * 2000})
    assert e.value.failure is Failure.REQUEST_TOO_LARGE
    assert [m.get("method") for m in channel.sent] == ["initialize", "notifications/initialized"]
    assert not channel.closed


@pytest.mark.parametrize(
    "shape",
    [
        lambda n: "[" + ",".join(["[]"] * n) + "]",
        lambda n: "[" + ",".join(["{}"] * n) + "]",
        lambda n: "{" + ",".join(f'"{k}":0' for k in range(n)) + "}",
        lambda n: "[" + ",".join(["[[[]]]"] * n) + "]",
        lambda n: "[" + ",".join(["[[[[]]]]"] * n) + "]",
        lambda n: "[" + ",".join(['{"":[[]]}'] * n) + "]",
    ],
)
def test_the_costliest_message_within_the_byte_limit_costs_a_bounded_amount_to_parse(shape):
    """A message is held whole only up to the byte limit; what parsing it costs is a multiple of
    that, measured here (about 46 times for lists of lists nested three deep, some 23 MiB) and
    pinned below 64."""
    limits = Limits()
    envelope = '{"jsonrpc":"2.0","id":"%s","result":{"content":[],"x":%s}}\n'
    n = 1
    while len(envelope % ("0" * 32, shape(n * 2))) < limits.response_bytes - 64:
        n *= 2
    lo, hi = n, n * 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (
            (mid, hi) if len(envelope % ("0" * 32, shape(mid))) < limits.response_bytes - 64 else (lo, mid)
        )
    s, _, _ = scripted(greeted(lambda m: (envelope % (m["id"], shape(lo))).encode()), limits)
    tracemalloc.start()
    try:
        assert s.call("find_symbol", {}) == ([], False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * limits.response_bytes


def test_framing_bytes_that_come_one_at_a_time_costs_no_more_than_at_once():
    lines = protocol.LineBuffer(1024 * 1024)
    started = time.monotonic()
    for _ in range(500_000):
        lines.feed(b"x")
        assert lines.line() is None
    lines.feed(b"\n")
    assert lines.line() == b"x" * 500_000
    assert time.monotonic() - started < 5
    assert lines.peak == 500_001


def test_a_line_never_held_past_the_limit():
    lines = protocol.LineBuffer(10)
    lines.feed(b"0123456789")
    with pytest.raises(protocol.TooLong):
        lines.feed(b"a")
    assert lines.peak == 10


@pytest.mark.parametrize(
    "line",
    [
        b'{"jsonrpc":"2.0","method":"sampling/createMessage","id":"w","params":{}}',
        b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}',
        b'{"jsonrpc":"2.0","method":"roots/list","id":"w","result":{"content":[]}}',
        b'{"jsonrpc":"2.0","id":"v","result":{"content":[]}}',
        b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}',
    ],
)
def test_a_request_a_notification_or_another_ids_response_is_dropped(line):
    assert protocol.classify(line, "w") is None


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"[]",
        b'"text"',
        b'{"jsonrpc":"1.0","id":"w","result":{}}',
        b'{"jsonrpc":"2.0","method":7}',
        b'{"jsonrpc":"2.0","id":"w"}',
        b'{"jsonrpc":"2.0","result":{}}',
        b'{"jsonrpc":"2.0","id":"w","result":{},"extra":1}',
        b'{"jsonrpc":"2.0","id":"w","error":"no"}',
        b'{"jsonrpc":"2.0","id":"w","result":"text"}',
        b'{"jsonrpc":"2.0","id":"w","result":{},"result":{}}',
        b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":"w","result":{}}',
        b'{"jsonrpc":"2.0","id":"w","result":{"x":Infinity}}',
        b'{"jsonrpc":"2.0","id":"w","result":{"x":-Infinity}}',
        b'\xef\xbb\xbf{"jsonrpc":"2.0","id":"w","result":{}}',
        b'{"jsonrpc":"2.0","id":"w","result":{"x":"\xff"}}',
        b"[" * 100_000,
    ],
)
def test_what_is_no_well_formed_message_ends_the_connection(line):
    with pytest.raises(protocol.ProtocolError):
        protocol.classify(line, "w")


@pytest.mark.parametrize(
    "result",
    [
        {"content": "text"},
        {"content": [None]},
        {"content": ["text"]},
        {"content": [{"text": "no type"}]},
        {"content": [{"type": "text"}]},
        {"content": [{"type": "text", "text": ["a"]}]},
        {"content": [], "isError": "false"},
        {"content": [], "isError": 0},
    ],
)
def test_a_tool_result_of_another_shape_is_not_guessed_at(result):
    with pytest.raises(protocol.ProtocolError):
        protocol.texts(result)


def test_a_harness_line_longer_than_the_wrapper_takes_is_dropped_and_the_next_answered():
    s, _, _ = scripted(answer("ok"), Limits(harness_bytes=4096))
    long_line = json.dumps(call(1, "find_symbol", {"name_path_pattern": "x" * 10_000})).encode() + b"\n"
    responses, _ = harness(s, long_line, call(2, "nothing"))
    assert [r["id"] for r in responses] == [2]


def test_the_harness_gets_no_answer_to_a_notification_and_an_error_for_a_method_not_offered():
    s, channel, _ = scripted(answer("ok"))
    responses, _ = harness(
        s,
        INITIALIZE,
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {"name": "x"}},
        {"jsonrpc": "2.0", "id": 4, "method": "logging/setLevel", "params": {"level": "debug"}},
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 6, "result": {}},
        b"not json\n",
    )
    assert [(r["id"], r.get("error", {}).get("code")) for r in responses] == [
        (1, None),
        (2, -32601),
        (3, -32601),
        (4, -32601),
        (5, None),
    ]
    assert channel.sent == []


def test_small_messages_without_an_answer_end_at_the_byte_limit_for_the_call():
    """Each notification is dropped as it comes, so none fills the line held: what ends them is
    the bytes counted for the call."""
    note = b'{"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":1}}\n'
    limits = Limits(response_bytes=64 * 1024, call_seconds=10**6)
    s, channel, _ = scripted(greeted(lambda m: note * 10_000), limits)
    with pytest.raises(Failed) as e:
        s.call("find_symbol", {})
    assert e.value.failure is Failure.TOO_LARGE
    assert len(note) * 10_000 - len(channel.pending) <= limits.response_bytes


def test_a_handshake_past_its_time_limit_is_told_with_the_connections_seconds():
    limits = Limits(call_seconds=120, connect_seconds=60)
    s, _, _ = scripted(lambda m: b"", limits)
    responses, _ = harness(s, call(1, "find_symbol", {"name_path_pattern": "A", "relative_path": "src"}))
    text = responses[0]["result"]["content"][0]["text"]
    assert "60 seconds" in text and "120" not in text


@pytest.mark.parametrize("ident", ["NaN", "Infinity", "-Infinity", "1e999", "true", "[1]", "{}"])
def test_a_harness_request_with_an_id_of_no_json_rpc_kind_is_dropped_and_the_next_answered(ident):
    s, _, _ = scripted(answer("ok"))
    bad = f'{{"jsonrpc":"2.0","id":{ident},"method":"ping"}}\n'.encode()
    responses, _ = harness(s, bad, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert responses == [{"jsonrpc": "2.0", "id": 2, "result": {}}]
