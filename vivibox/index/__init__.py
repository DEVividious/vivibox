"""The planner's code index, as the planner reaches it: a wrapper of vivibox's own, run inside
the planner's container as a stdio MCP server of its harness, in front of an index that runs
elsewhere and is taken to be compromised by the repository it reads.

The wrapper owns everything the planner's harness learns of the index: the server's name, the
tool list, each tool's name, description and schema, and every sentence of a result but the
data itself. Of the index it asks one thing, `tools/call` of a tool on its own list, one call
at a time; of what the index sends it reads the answer to that call and nothing else, bounded in
bytes and in time, and hands on only its text, cleaned, under a line that says it is data
derived from the repository. Nothing the index sends is ever a request to the harness.

`protocol` is that parser, bytes in and messages out, with no socket and no clock; `session`
holds one connection and one call; `channel` is how the bytes travel; `tools` is the text the
planner reads; `server` is the harness's side."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """What one call may cost the planner's container. The numbers are tuning, not the boundary:
    the boundary is that each is enforced, whatever the index sends."""

    # The request the wrapper writes for one call, as encoded: the arguments the model gave.
    request_bytes: int = 16 * 1024
    # Bytes read from the index for one call, every message it sent meanwhile included; also the
    # longest message the wrapper holds, so it never holds more than this of the index's.
    response_bytes: int = 512 * 1024
    # Characters of a result's text the model is given.
    text_chars: int = 60_000
    # Seconds a call may take from its request to its answer, and a connection with its handshake.
    call_seconds: float = 120.0
    connect_seconds: float = 60.0
    # The longest line the wrapper takes from its own harness.
    harness_bytes: int = 1024 * 1024
