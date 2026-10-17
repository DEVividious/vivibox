"""The steady code index's program: one Serena for the life of I, and the socket the planner's
wrapper connects to, in front of it.

Serena is started when I starts, so that its language servers are up before the planner's first
call, and kept: a connection does not start one (each would start the language servers again,
and two would build the same workspace at once). The relay greets Serena once, as its one MCP
client; a wrapper that connects is greeted by the relay, and its calls of the index's six tools
are handed to Serena under ids of the relay's own and their answers handed back under the
wrapper's. One connection at a time: a new one takes the place of the one before, whose answers
still to come are dropped. Nothing else of the wrapper's is forwarded, and nothing of Serena's but
the answers: its requests are refused, its notifications dropped.

When Serena ends, the relay ends, and with it I: the host makes I again at the next turn. The
wrapper bounds whatever comes out of here; this bounds only what the relay itself holds.

python3 -I -c <launch> --project <path> --languages <a,b> <socket's folder> [<socket's name>]"""

from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time

from vivibox.index_program import serena

# The index's tools the wrapper may call (vivibox.index.tools.TOOLS, by name).
TOOLS = frozenset(
    {
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
        "find_implementations",
        "find_declaration",
        "search_for_pattern",
    }
)
PROTOCOL_VERSION = "2025-06-18"
GREETING = {"name": "vivibox-index", "version": "1"}
# The longest line held from Serena or from a connection; a longer one is dropped to its end.
LONGEST = 16 * 2**20
# How long Serena may take to answer the relay's greeting.
GREET_SECONDS = 120.0
CHUNK = 64 * 1024
NOT_FOUND = {"code": -32601, "message": "Method not found"}


class Lines:
    """Bytes in, whole lines out, never more than LONGEST held."""

    def __init__(self) -> None:
        self.held = bytearray()
        self.skipping = False

    def feed(self, data: bytes) -> list[bytes]:
        found = []
        while data:
            at = data.find(b"\n")
            if at < 0:
                if not self.skipping:
                    self.held += data
                    if len(self.held) > LONGEST:
                        self.held.clear()
                        self.skipping = True
                return found
            if not self.skipping:
                found.append(bytes(self.held + data[:at]))
            self.held.clear()
            self.skipping = False
            data = data[at + 1 :]
        return found


def decoded(line: bytes) -> dict | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def encoded(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


class Relay:
    def __init__(self, serena_in, serena_out: int, listening: socket.socket) -> None:
        self.serena_in, self.serena_out, self.listening = serena_in, serena_out, listening
        self.client: socket.socket | None = None
        self.client_lines = Lines()
        self.serena_lines = Lines()
        self.generation = 0
        # The relay's id of each call handed to Serena: the connection's generation and its id.
        self.pending: dict[str, tuple[int, object]] = {}
        self.sent = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(serena_out, selectors.EVENT_READ, "serena")
        self.selector.register(listening, selectors.EVENT_READ, "listening")

    def to_serena(self, message: dict) -> None:
        self.serena_in.write(encoded(message))
        self.serena_in.flush()

    def to_client(self, message: dict) -> None:
        if self.client is None:
            return
        try:
            self.client.sendall(encoded(message))
        except OSError:
            self.drop_client()

    def drop_client(self) -> None:
        if self.client is not None:
            self.selector.unregister(self.client)
            self.client.close()
        self.client = None
        self.client_lines = Lines()

    def accept(self) -> None:
        conn, _ = self.listening.accept()
        self.drop_client()
        conn.settimeout(10)
        self.generation += 1
        self.client = conn
        self.selector.register(conn, selectors.EVENT_READ, "client")

    def from_client(self, line: bytes) -> None:
        message = decoded(line)
        if message is None or "id" not in message or not isinstance(message.get("method"), str):
            return  # a notification, a response, or no message: nothing to answer
        ident, method = message["id"], message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": GREETING,
            }
            self.to_client({"jsonrpc": "2.0", "id": ident, "result": result})
        elif method == "ping":
            self.to_client({"jsonrpc": "2.0", "id": ident, "result": {}})
        elif method == "tools/call" and params.get("name") in TOOLS:
            self.sent += 1
            mine = f"relay-{self.sent}"
            self.pending[mine] = (self.generation, ident)
            call = {"name": params["name"], "arguments": params.get("arguments") or {}}
            self.to_serena({"jsonrpc": "2.0", "id": mine, "method": "tools/call", "params": call})
        else:
            self.to_client({"jsonrpc": "2.0", "id": ident, "error": NOT_FOUND})

    def from_serena(self, line: bytes) -> None:
        message = decoded(line)
        if message is None:
            return
        if "method" in message:
            if "id" in message:  # a request of Serena's to its client: none is answered but a ping
                answer = {"result": {}} if message["method"] == "ping" else {"error": NOT_FOUND}
                self.to_serena({"jsonrpc": "2.0", "id": message["id"], **answer})
            return
        found = self.pending.pop(message.get("id"), None) if isinstance(message.get("id"), str) else None
        if found is None:
            return
        generation, ident = found
        if generation == self.generation:
            self.to_client({**message, "id": ident})

    def serve(self) -> int:
        while True:
            for key, _ in self.selector.select():
                if key.data == "listening":
                    self.accept()
                elif key.data == "serena":
                    data = os.read(self.serena_out, CHUNK)
                    if not data:
                        return 1  # Serena ended: so does I
                    for line in self.serena_lines.feed(data):
                        self.from_serena(line)
                else:
                    try:
                        data = self.client.recv(CHUNK)
                    except OSError:
                        data = b""
                    if not data:
                        self.drop_client()
                        continue
                    for line in self.client_lines.feed(data):
                        self.from_client(line)


def greet(serena_in, serena_out: int, seconds: float = GREET_SECONDS) -> None:
    """The relay's handshake with Serena, as its one client: no capability offered."""
    hello = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": GREETING}
    serena_in.write(
        encoded({"jsonrpc": "2.0", "id": "relay-greeting", "method": "initialize", "params": hello})
    )
    serena_in.flush()
    lines, deadline = Lines(), time.monotonic() + seconds
    selector = selectors.DefaultSelector()
    selector.register(serena_out, selectors.EVENT_READ)
    while True:
        left = deadline - time.monotonic()
        if left <= 0 or not selector.select(left):
            raise TimeoutError("Serena did not answer its greeting")
        data = os.read(serena_out, CHUNK)
        if not data:
            raise EOFError("Serena ended before its greeting")
        for line in lines.feed(data):
            message = decoded(line)
            if message and message.get("id") == "relay-greeting" and "method" not in message:
                if "result" not in message:
                    raise EOFError("Serena refused its greeting")
                serena_in.write(encoded({"jsonrpc": "2.0", "method": "notifications/initialized"}))
                serena_in.flush()
                return


def listen(folder: str, name: str) -> socket.socket:
    path = os.path.join(folder, name)
    if os.path.lexists(path):
        os.unlink(path)
    listening = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listening.bind(path)
    listening.listen(4)
    return listening


def main(argv: list[str]) -> int:
    project, languages, rest = serena.arguments(argv)
    if len(rest) not in (1, 2):
        print("usage: relay --project <path> --languages <a,b> <folder> [<name>]", file=sys.stderr)
        return 2
    serena.prepare(languages, serena.heap())
    child = subprocess.Popen(
        serena.command(project), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=serena.environment(steady=True), cwd=serena.STORE,
    )  # fmt: skip
    signal.signal(signal.SIGTERM, lambda *_: (child.terminate(), sys.exit(0)))
    try:
        greet(child.stdin, child.stdout.fileno())
        listening = listen(rest[0], rest[1] if len(rest) == 2 else "index.sock")
        return Relay(child.stdin, child.stdout.fileno(), listening).serve()
    except (OSError, TimeoutError, EOFError):
        return 1
    finally:
        child.terminate()
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
