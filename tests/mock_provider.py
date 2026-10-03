"""A stand-in for a model provider, for the live tests of who can reach an agent's conversation:
an HTTP server with the two endpoints the harnesses call, OpenAI-compatible chat completions
(answered as a stream when asked, which opencode requires) and Anthropic's messages. It answers
every request with the same short text, calls nothing, needs no key, and appends every request
body to a log, one JSON object a line: what a harness sent as the conversation is read from there.

A turn that reads files: while a file `<log>.reads` is there, a path a line, a chat completion
that offers a `read` tool is answered with a call of it for the next path the conversation has
no result for yet, and with the text once every path was read (opencode's read tool). A turn
that uses other tools: a file `<log>.calls`, a JSON object a line (`{"tool": …, "arguments":
{…}}`), names the calls to make after the reads, in order, each asked for once the one before
it has its result (a review's turn: read, `bash` for the diff, `write` for the review).

Standard library only: it runs inside the container whose harness talks to it
(`python3 mock_provider.py <port> <log>`), on that container's localhost.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ANSWER = "OK done."
# The provider a harness is pointed at this server by, and its one model.
PROVIDER, MODEL = "mock", "mock-model"


def opencode_config(port: int) -> dict:
    """An opencode.json whose only provider is this server on the container's localhost."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{PROVIDER}/{MODEL}",
        "autoupdate": False,
        "share": "disabled",
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow", "external_directory": "allow"},
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Mock",
                "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                "models": {MODEL: {"name": "Mock Model"}},
            }
        },
        "instructions": ["/task/harness/instructions.md"],
    }


def reads(log: Path | str) -> Path:
    """Where the paths a turn is to read are named, beside the log."""
    return Path(f"{log}.reads")


def calls(log: Path | str) -> Path:
    """Where the tool calls a turn is to make after its reads are named, beside the log."""
    return Path(f"{log}.calls")


def requests(log: str) -> list[dict]:
    """The model requests a log holds, oldest first: the bodies sent to either endpoint."""
    found = []
    for line in log.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry.get("body"), dict) and "messages" in entry["body"]:
            found.append(entry["body"])
    return found


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def said_by(body: dict, role: str) -> list[str]:
    """The text of every message of that role in a request, in order, whichever API's shape."""
    return [_text(m.get("content")) for m in body.get("messages", []) if m.get("role") == role]


class Handler(BaseHTTPRequestHandler):
    log: Path

    def _body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw)
        except ValueError:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _record(self, entry: dict) -> None:
        with self.log.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def _json(self, out: dict) -> None:
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _to_call(self, body: dict) -> tuple[int, str, dict]:
        """The next tool call to ask for, its number, and its arguments; "" when there is none
        (left), or the request does not offer that tool."""
        wanted: list[tuple[str, dict]] = []
        try:
            lines = reads(self.log).read_text().splitlines()
            wanted += [("read", {"filePath": line.strip()}) for line in lines if line.strip()]
        except OSError:
            pass
        try:
            for line in calls(self.log).read_text().splitlines():
                if line.strip():
                    call = json.loads(line)
                    wanted.append((str(call["tool"]), dict(call.get("arguments") or {})))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        offered = [tool.get("function", {}).get("name") for tool in body.get("tools") or []]
        done = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
        if done < len(wanted) and wanted[done][0] in offered:
            return done, *wanted[done]
        return done, "", {}

    def _stream(self, model: str, number: int = 0, tool: str = "", arguments: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "m", "object": "chat.completion.chunk", "created": 0, "model": model}
        first = {"index": 0, "delta": {"role": "assistant", "content": ANSWER}, "finish_reason": None}
        last = {"index": 0, "delta": {}, "finish_reason": "stop"}
        if tool:
            function = {"name": tool, "arguments": json.dumps(arguments or {})}
            call = {"index": 0, "id": f"call_{number}", "type": "function", "function": function}
            first = {"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}
            last = {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        for sent in ({**chunk, "choices": [first]}, {**chunk, "choices": [last], "usage": usage}):
            self.wfile.write(f"data: {json.dumps(sent)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_POST(self) -> None:
        body = self._body()
        self._record({"path": self.path, "body": body})
        model = body.get("model", MODEL) if isinstance(body, dict) else MODEL
        if "chat/completions" in self.path:
            if body.get("stream"):
                self._stream(model, *self._to_call(body))
                return
            message = {"role": "assistant", "content": ANSWER}
            self._json(
                {
                    "id": "m",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )
        elif "/v1/messages" in self.path:
            # Not a stream, whatever was asked: Claude Code then asks again without one.
            self._json(
                {
                    "id": "msg",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": ANSWER}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        self._record({"path": self.path, "method": "GET"})
        self._json({"data": []})

    def log_message(self, *args) -> None:
        pass


def server(port: int, log: Path, bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("Logged", (Handler,), {"log": log})
    return ThreadingHTTPServer((bind, port), handler)


if __name__ == "__main__":
    server(int(sys.argv[1]), Path(sys.argv[2])).serve_forever()
