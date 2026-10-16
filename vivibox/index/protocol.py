"""The wire between the wrapper and the index, as bytes: MCP's stdio framing, one JSON object a
line, and what the wrapper takes from a line. No socket and no clock here: the session brings
the bytes and keeps the time, so a channel of another shape uses this parser unchanged.

Every byte from the index is taken to be the worst it could be. A line is UTF-8 and one JSON
object, with no key twice and no NaN or Infinity; a line longer than the limit is never held
whole. A message that is a request or a notification is dropped unread, whatever its method; a
response is the answer only when its id is the one outstanding request's, compared as the
string the wrapper made; anything else that is no well-formed JSON-RPC 2.0 message ends the
connection (ProtocolError)."""

from __future__ import annotations

import json
from dataclasses import dataclass

RESPONSE_KEYS = frozenset({"jsonrpc", "id", "result", "error"})


class ProtocolError(Exception):
    """The index sent what the wrapper does not take: the connection is closed."""


class TooLong(ProtocolError):
    """A message longer than the wrapper holds, or more bytes for one call than it reads."""


class LineBuffer:
    """Bytes in, lines out, never more than `limit` bytes held. The newline is looked for only
    in what came since the last look, so bytes that come one at a time cost no more to frame
    than bytes that come at once."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.peak = 0
        self._held = bytearray()
        self._looked = 0
        self._skipping = False

    def room(self) -> int:
        return self.limit - len(self._held)

    def feed(self, data: bytes) -> None:
        if self._skipping:
            at = data.find(b"\n")
            if at < 0:
                return
            self._skipping, data = False, data[at + 1 :]
        if len(data) > self.room():
            raise TooLong("a message longer than the wrapper holds")
        self._held += data
        self.peak = max(self.peak, len(self._held))

    def line(self) -> bytes | None:
        """The next whole line without its newline, or None until one has come."""
        at = self._held.find(b"\n", self._looked)
        if at < 0:
            self._looked = len(self._held)
            return None
        found = bytes(self._held[:at])
        del self._held[: at + 1]
        self._looked = 0
        return found

    def skip(self) -> None:
        """Drop what is held and the rest of its line, as it comes: for a sender trusted not to
        mean harm, whose line was too long all the same (the harness's)."""
        self._held.clear()
        self._looked = 0
        self._skipping = True


def _unique(pairs: list[tuple[str, object]]) -> dict:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key twice")
    return found


def _no_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def decoded(line: bytes) -> object:
    """One line as JSON, strictly: UTF-8 (no byte order mark), no key twice in an object, no
    NaN or Infinity, no nesting past what the parser recurses to."""
    try:
        return json.loads(line.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise ProtocolError("a line that is no JSON the wrapper takes") from None


@dataclass(frozen=True)
class Reply:
    """The answer to the outstanding request: its result, or None when it was an error."""

    result: dict | None


def classify(line: bytes, wanted: str) -> Reply | None:
    """The answer to the request with id `wanted`, or None for a message to drop: a request or a
    notification of the index's (sampling, roots, elicitation, a changed tool list, progress,
    logging, anything), or a response to another id. What is neither ends the connection."""
    message = decoded(line)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise ProtocolError("not a JSON-RPC 2.0 message")
    if "method" in message:
        if not isinstance(message["method"], str):
            raise ProtocolError("a method that is no string")
        return None
    if not message.keys() <= RESPONSE_KEYS or "id" not in message:
        raise ProtocolError("a response of another shape")
    if ("result" in message) == ("error" in message):
        raise ProtocolError("a response with both a result and an error, or neither")
    found = message["id"]
    if not (isinstance(found, str) and found == wanted):
        return None
    if "error" in message:
        if not isinstance(message["error"], dict):
            raise ProtocolError("an error that is no object")
        return Reply(None)
    if not isinstance(message["result"], dict):
        raise ProtocolError("a result that is no object")
    return Reply(message["result"])


def texts(result: dict) -> tuple[list[str], bool]:
    """A tool call's text parts and whether the index called it an error. Every other part
    (image, audio, resource, a link to one) is dropped, and so is every other field of the
    result (structured content, metadata); a part or a flag of the wrong type ends the
    connection rather than being guessed at."""
    content = result.get("content", [])
    failed = result.get("isError", False)
    if not isinstance(content, list) or not isinstance(failed, bool):
        raise ProtocolError("a tool result of another shape")
    found = []
    for part in content:
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            raise ProtocolError("a content part of another shape")
        if part["type"] == "text":
            if not isinstance(part.get("text"), str):
                raise ProtocolError("a text part without text")
            found.append(part["text"])
    return found, failed


def encoded(message: dict) -> bytes:
    """A message of the wrapper's as one line: ASCII JSON, so no character the model put in an
    argument can end the line early or be read another way by the other side."""
    return json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def request(ident: str, method: str, params: dict) -> bytes:
    return encoded({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})


def notification(method: str) -> bytes:
    return encoded({"jsonrpc": "2.0", "method": method})
