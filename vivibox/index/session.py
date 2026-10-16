"""One connection to the index and one call on it at a time. The connection is made at the
first call, with the wrapper's own handshake (no capability offered: no roots, no sampling, no
elicitation), and made again at the next call after any failure; a call that failed is never
sent again. Every request has an id the index cannot guess, so no message it finished before
the request can be taken for its answer.

What a call may cost is bounded whatever the index does: the bytes read for it (every message
meanwhile counted), the longest message held, the seconds from the request to the answer
(however slowly the bytes come), and the request written."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from enum import Enum

from vivibox.index import Limits, protocol
from vivibox.index.channel import Channel

PROTOCOL_VERSION = "2025-06-18"
CLIENT = {"name": "vivibox", "version": "1"}
CHUNK = 64 * 1024
DEFAULT = Limits()


class Failure(Enum):
    UNAVAILABLE = "unavailable"  # no connection, or the index closed it
    TIMEOUT = "timeout"
    CONNECT_TIMEOUT = "connect_timeout"  # the handshake's: the call was never sent
    TOO_LARGE = "too_large"
    MALFORMED = "malformed"
    REFUSED = "refused"  # a JSON-RPC error: the connection stays
    REQUEST_TOO_LARGE = "request_too_large"  # never sent: the connection stays


class Failed(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class Session:
    def __init__(
        self,
        connect: Callable[[float], Channel],
        limits: Limits = DEFAULT,
        clock: Callable[[], float] = time.monotonic,
        ids: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._connect, self.limits, self._clock, self._ids = connect, limits, clock, ids
        self._channel: Channel | None = None
        self._lines = protocol.LineBuffer(limits.response_bytes)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
        self._channel = None
        self._lines = protocol.LineBuffer(self.limits.response_bytes)

    def call(self, tool: str, arguments: dict) -> tuple[list[str], bool]:
        """The text parts of the index's answer and whether it called it an error; Failed when
        there is no answer the wrapper takes."""
        try:
            if self._channel is None:
                try:
                    self._open()
                except TimeoutError:
                    raise Failed(Failure.CONNECT_TIMEOUT) from None
            deadline = self._clock() + self.limits.call_seconds
            reply = self._exchange("tools/call", {"name": tool, "arguments": arguments}, deadline)
            if reply.result is None:
                raise Failed(Failure.REFUSED)
            return protocol.texts(reply.result)
        except Failed as e:
            if e.failure not in (Failure.REFUSED, Failure.REQUEST_TOO_LARGE):
                self.close()
            raise
        except protocol.TooLong:
            self.close()
            raise Failed(Failure.TOO_LARGE) from None
        except protocol.ProtocolError:
            self.close()
            raise Failed(Failure.MALFORMED) from None
        except TimeoutError:
            self.close()
            raise Failed(Failure.TIMEOUT) from None
        except OSError:
            self.close()
            raise Failed(Failure.UNAVAILABLE) from None

    def _open(self) -> None:
        deadline = self._clock() + self.limits.connect_seconds
        self._channel = self._connect(self.limits.connect_seconds)
        hello = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT}
        # Whatever the index answers (instructions, capabilities, a server's name, its version
        # of the protocol) is not read: an answer at all is all the handshake asks of it.
        if self._exchange("initialize", hello, deadline).result is None:
            raise Failed(Failure.UNAVAILABLE)
        self._channel.send(protocol.notification("notifications/initialized"), deadline - self._clock())

    def _exchange(self, method: str, params: dict, deadline: float) -> protocol.Reply:
        assert self._channel is not None
        wanted = self._ids()
        data = protocol.request(wanted, method, params)
        if len(data) > self.limits.request_bytes:
            raise Failed(Failure.REQUEST_TOO_LARGE)
        self._channel.send(data, deadline - self._clock())
        taken = 0
        while True:
            while (line := self._lines.line()) is not None:
                if (reply := protocol.classify(line, wanted)) is not None:
                    return reply
            left = self.limits.response_bytes - taken
            room = self._lines.room()
            if left <= 0 or room <= 0:
                raise protocol.TooLong("more than the wrapper reads for one call")
            chunk = self._channel.recv(min(CHUNK, left, room), deadline - self._clock())
            if not chunk:
                raise Failed(Failure.UNAVAILABLE)
            taken += len(chunk)
            self._lines.feed(chunk)
