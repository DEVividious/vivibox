"""What the index's tests share: the harness's side driven by lines, a call for every tool, and a
channel whose index is a function, on a clock of the test's."""

from __future__ import annotations

import io
import json

from vivibox.index import server
from vivibox.index.session import Session

VALID = {
    "get_symbols_overview": {"relative_path": "src/A.java"},
    "find_symbol": {"name_path_pattern": "A/run", "relative_path": "src"},
    "find_referencing_symbols": {"name_path": "A/run", "relative_path": "src/A.java"},
    "find_implementations": {"name_path": "Shape", "relative_path": "src/Shape.java"},
    "find_declaration": {"relative_path": "src/A.java", "regex": "b\\.(run)\\("},
    "search_for_pattern": {"substring_pattern": "run\\(", "relative_path": "src"},
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "harness"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def listing(ident) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/list"}


def call(ident, name: str, arguments=None) -> dict:
    params = {"name": name} if arguments is None else {"name": name, "arguments": arguments}
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": params}


def harness(session: Session, *messages: dict | bytes) -> tuple[list[dict], bytes]:
    """Every response the wrapper wrote to its harness for these lines, and the bytes as written."""
    data = b"".join(m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages)
    out: list[bytes] = []
    server.serve(io.BytesIO(data).read, out.append, session)
    raw = b"".join(out)
    return [json.loads(one) for one in raw.splitlines()], raw


def said(response: dict) -> tuple[str, bool]:
    """The text a tool call's response gives the model, and whether it is an error."""
    result = response["result"]
    assert [part["type"] for part in result["content"]] == ["text"]
    assert set(result) == {"content", "isError"}
    return result["content"][0]["text"], result["isError"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Scripted:
    """A channel to an index that is a function: given each message the wrapper sends, the bytes
    it answers with, handed out `piece` bytes a receive, each receive taking `step` seconds; a
    receive with nothing to hand out waits out its whole timeout."""

    def __init__(self, clock: Clock, answer, step: float = 0.0, piece: int | None = None) -> None:
        self.clock, self.answer, self.step, self.piece = clock, answer, step, piece
        self.sent: list[dict] = []
        self.pending = bytearray()
        self.closed = False
        self.held_most = 0

    def send(self, data: bytes, timeout: float) -> None:
        if timeout <= 0:
            raise TimeoutError
        assert data.endswith(b"\n") and data.count(b"\n") == 1
        message = json.loads(data)
        self.sent.append(message)
        self.pending += self.answer(message) or b""

    def recv(self, limit: int, timeout: float) -> bytes:
        if timeout <= 0:
            raise TimeoutError
        if not self.pending:
            self.clock.now += timeout
            raise TimeoutError
        self.clock.now += self.step
        n = min(limit, self.piece or limit, len(self.pending))
        out = bytes(self.pending[:n])
        del self.pending[:n]
        return out

    def close(self) -> None:
        self.closed = True


def greeted(answer):
    """An index's answer function that answers the handshake as an index should, and every call
    as `answer` says."""

    def whole(message: dict) -> bytes:
        if message.get("method") == "initialize":
            return json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}).encode() + b"\n"
        if message.get("method") == "tools/call":
            return answer(message)
        return b""

    return whole
