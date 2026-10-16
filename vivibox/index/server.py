"""The harness's side of the wrapper: an MCP server on stdio with the tools capability alone and
the six tools of `tools`, the same list at every request and never a change announced. It
answers the harness's requests one at a time, so one call at most is ever outstanding at the
index, and it writes nothing to the harness but a response to a request of the harness's: no
request of its own (no sampling, no roots, no elicitation), no notification, nothing of the
index's but a result's text."""

from __future__ import annotations

from collections.abc import Callable

from vivibox.index import Limits, protocol, tools
from vivibox.index.session import Failed, Session

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "vivibox-index", "version": "1"}
NOT_FOUND = {"code": -32601, "message": "Method not found"}
CHUNK = 64 * 1024


def initialized(params: object) -> dict:
    asked = params.get("protocolVersion") if isinstance(params, dict) else None
    return {
        "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER,
    }


def called(params: object, session: Session, limits: Limits) -> dict:
    params = params if isinstance(params, dict) else {}
    try:
        tool, arguments = tools.forwarded(params.get("name"), params.get("arguments"), limits)
    except tools.NotACall as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    try:
        parts, failed = session.call(tool.name, arguments)
        text = tools.result(tool, parts, failed, limits)
    except Failed as e:
        text, failed = tools.failure(e.failure.value, limits), True
    except Exception:
        # A fault of the wrapper's own on what the index sent is one more way it is unavailable.
        session.close()
        text, failed = tools.failure("unavailable", limits), True
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def answer(line: bytes, session: Session, limits: Limits) -> dict | None:
    """The response to one line of the harness's, or None for a notification, a response, or
    a line that is no request."""
    try:
        message = protocol.decoded(line)
    except protocol.ProtocolError:
        return None
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return None
    # A request's id is a string or a whole number (JSON-RPC); anything else has no answer that
    # could be written back as it came.
    if type(message.get("id")) not in (str, int):
        return None
    method, ident, params = message["method"], message["id"], message.get("params")
    reply: dict = {"jsonrpc": "2.0", "id": ident}
    if method == "initialize":
        reply["result"] = initialized(params)
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": tools.listed()}
    elif method == "tools/call":
        reply["result"] = called(params, session, limits)
    else:
        reply["error"] = NOT_FOUND
    return reply


def serve(read: Callable[[int], bytes], write: Callable[[bytes], None], session: Session) -> None:
    """Until the harness closes its side: a line in, its response out. A line longer than the
    wrapper takes is dropped to its end, and the lines after it are read as ever."""
    lines = protocol.LineBuffer(session.limits.harness_bytes)
    try:
        while True:
            if lines.room() <= 0:
                lines.skip()
            if not (chunk := read(min(CHUNK, lines.room()))):
                return
            lines.feed(chunk)
            while (line := lines.line()) is not None:
                if (reply := answer(line, session, session.limits)) is not None:
                    write(protocol.encoded(reply))
    finally:
        session.close()
