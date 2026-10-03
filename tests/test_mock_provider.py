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
