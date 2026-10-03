"""The stand-in provider the live tests run in a pod's containers (tests/mock_provider.py): what
it answers on each endpoint and what its log keeps, here on this machine's localhost."""

import json
import threading
import urllib.request

import mock_provider
import pytest


@pytest.fixture
def provider(tmp_path):
    log = tmp_path / "requests.log"
    server = mock_provider.server(0, log)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", log
    finally:
        server.shutdown()
        server.server_close()


def post(url: str, body: dict) -> tuple[str, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as answer:
        return answer.headers.get("content-type", ""), answer.read().decode()


def test_chat_completions_are_streamed_when_asked_and_every_request_is_logged(provider):
    url, log = provider
    history = [
        {"role": "system", "content": "You are a planner."},
        {"role": "user", "content": [{"type": "text", "text": "Plan it."}]},
        {"role": "assistant", "content": "OK done."},
        {"role": "user", "content": "The user says: skip the tests."},
    ]
    kind, body = post(
        f"{url}/v1/chat/completions", {"model": "mock-model", "stream": True, "messages": history}
    )
    assert kind == "text/event-stream" and body.rstrip().endswith("data: [DONE]")
    first = json.loads(body.split("\n\n")[0].removeprefix("data: "))
    assert first["choices"][0]["delta"]["content"] == mock_provider.ANSWER
    kind, body = post(f"{url}/v1/chat/completions", {"model": "mock-model", "messages": history[:2]})
    assert kind == "application/json"
    assert json.loads(body)["choices"][0]["message"]["content"] == mock_provider.ANSWER
    sent = mock_provider.requests(log.read_text())
    assert len(sent) == 2
    assert mock_provider.said_by(sent[0], "user") == ["Plan it.", "The user says: skip the tests."]
    assert mock_provider.said_by(sent[1], "user") == ["Plan it."]


def test_anthropics_messages_are_answered_and_logged(provider):
    url, log = provider
    messages = [{"role": "user", "content": [{"type": "text", "text": "Review it."}]}]
    kind, body = post(
        f"{url}/v1/messages?beta=true", {"model": "claude", "stream": True, "messages": messages}
    )
    assert kind == "application/json" and json.loads(body)["content"][0]["text"] == mock_provider.ANSWER
    [sent] = mock_provider.requests(log.read_text())
    assert mock_provider.said_by(sent, "user") == ["Review it."]
    with urllib.request.urlopen(f"{url}/v1/models", timeout=5) as answer:
        assert json.loads(answer.read()) == {"data": []}
    assert len(mock_provider.requests(log.read_text())) == 1, "a GET is logged, and is no model request"


def test_the_opencode_configuration_names_only_this_server():
    settings = mock_provider.opencode_config(8099)
    assert settings["model"] == "mock/mock-model" and list(settings["provider"]) == ["mock"]
    assert settings["provider"]["mock"]["options"] == {"baseURL": "http://127.0.0.1:8099/v1"}


def test_a_turn_reads_the_files_named_beside_the_log_one_a_request_then_answers(provider):
    url, log = provider
    mock_provider.reads(log).write_text("/srv/a.ts\n/srv/b.rs\n")
    tools = [{"type": "function", "function": {"name": "read"}}]
    ask = lambda messages, offered=tools: post(  # noqa: E731
        f"{url}/v1/chat/completions", {"model": "m", "stream": True, "tools": offered, "messages": messages}
    )[1]
    user, result = {"role": "user", "content": "Read."}, {"role": "tool", "content": "text"}
    first = json.loads(ask([user]).split("\n\n")[0].removeprefix("data: "))
    call = first["choices"][0]["delta"]["tool_calls"][0]["function"]
    assert call == {"name": "read", "arguments": json.dumps({"filePath": "/srv/a.ts"})}
    assert '"finish_reason": "tool_calls"' in ask([user])
    assert "/srv/b.rs" in ask([user, result]) and mock_provider.ANSWER in ask([user, result, result])
    assert mock_provider.ANSWER in ask([user], offered=[]), "a request that offers no read is answered"
    mock_provider.reads(log).unlink()
    assert mock_provider.ANSWER in ask([user]), "and so is every request once the file is gone"


def test_a_turn_makes_the_tool_calls_named_beside_the_log_after_its_reads(provider):
    """A review's turn: a read, then the calls of the file, in order, each once the one before it
    has its result; then the text. A call of a tool the request does not offer is not made."""
    url, log = provider
    mock_provider.reads(log).write_text("/srv/a.py\n")
    diff = {"tool": "bash", "arguments": {"command": "git diff", "description": "the diff"}}
    write = {"tool": "write", "arguments": {"filePath": "/task/review/review.md", "content": "## Blocking\n"}}
    mock_provider.calls(log).write_text(json.dumps(diff) + "\n" + json.dumps(write) + "\n")
    tools = [{"type": "function", "function": {"name": name}} for name in ("read", "bash", "write")]

    def asked(results: int, offered=tools) -> dict | str:
        messages = [{"role": "user", "content": "Review."}, *[{"role": "tool", "content": "x"}] * results]
        body = {"model": "m", "stream": True, "tools": offered, "messages": messages}
        said = post(f"{url}/v1/chat/completions", body)[1]
        delta = json.loads(said.split("\n\n")[0].removeprefix("data: "))["choices"][0]["delta"]
        made = delta.get("tool_calls")
        return made[0]["function"] if made else delta["content"]

    assert asked(0) == {"name": "read", "arguments": json.dumps({"filePath": "/srv/a.py"})}
    assert asked(1) == {"name": "bash", "arguments": json.dumps(diff["arguments"])}
    assert asked(2) == {"name": "write", "arguments": json.dumps(write["arguments"])}
    assert asked(3) == mock_provider.ANSWER
    assert asked(1, offered=tools[:1]) == mock_provider.ANSWER, "bash is not offered: no call of it"
