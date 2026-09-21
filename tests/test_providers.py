import json

import pytest

from vivibox import actions, keys, opencode, providers
from vivibox.config import ConfigError

COMPANY = """{
  // the company's endpoint
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "acme": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Acme AI",
      "options": {"baseURL": "https://ai.acme.example/v1", "apiKey": "{env:ACME_KEY}"},
      "models": {"coder-large": {"name": "Coder L"}, "coder-small": {}, "chat": {},},
    },
    /* a local one, no key */
    "local": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://localhost:8080/v1"},
              "models": {"qwen": {}}},
    "deepseek": {"options": {"apiKey": "sk-literal"}}
  }
}"""


@pytest.fixture
def source(env, tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text(COMPANY)
    return path


def test_every_provider_comes_over_with_its_key(source):
    found = providers.import_opencode(source, env={"ACME_KEY": "acme-secret"})
    assert [(p.name, p.models) for p in found] == [("acme", 3), ("local", 1), ("deepseek", 0)]
    assert keys.get_key("acme") == "acme-secret" and keys.get_key("deepseek") == "sk-literal"
    stored = providers.path().read_text()
    assert "apiKey" not in stored and "sk-literal" not in stored, "keys stay in the key store"
    assert providers.models()["acme"] == ["acme/coder-large", "acme/coder-small", "acme/chat"]


def test_a_key_that_cannot_be_found_is_said(source):
    found = {p.name: p.key for p in providers.import_opencode(source, env={})}
    assert found == {"acme": "$ACME_KEY is not set here", "local": "none needed", "deepseek": "from the file"}


def test_a_key_from_a_file_is_read(source, tmp_path):
    (tmp_path / "acme.key").write_text("from-file\n")
    source.write_text(COMPANY.replace("{env:ACME_KEY}", "{file:acme.key}"))
    providers.import_opencode(source, env={})
    assert keys.get_key("acme") == "from-file"


def test_the_task_config_carries_your_provider_and_points_at_the_mounted_key(source):
    providers.import_opencode(source, env={"ACME_KEY": "acme-secret"})
    config = opencode.config("acme/coder-large", ["local"])
    acme = config["provider"]["acme"]
    assert acme["options"] == {
        "baseURL": "https://ai.acme.example/v1",
        "apiKey": "{file:/run/vivibox-secrets/acme}",
    }
    assert acme["models"]["coder-large"] == {"name": "Coder L"}
    assert (
        "apiKey" not in config["provider"]["local"]["options"]
        and "keyless" not in config["provider"]["local"]
    )
    assert "acme-secret" not in json.dumps(config)


def test_a_keyless_provider_needs_no_key_to_start(source, monkeypatch):
    from vivibox.config import load_config

    providers.import_opencode(source, env={})
    task = actions.create(
        "demo", "x", roles={"writer": ("opencode", "local/qwen"), "planner": ("manual", "")}
    )
    assert actions.provider_keys(load_config(), task) == []


def test_your_providers_are_listed_without_asking_opencode(source, monkeypatch):
    providers.import_opencode(source, env={"ACME_KEY": "k"})
    asked = []
    monkeypatch.setattr(actions, "provider_models", lambda p: asked.append(p) or [f"{p}/m"])
    monkeypatch.setattr(actions, "models_cache", lambda: source.parent / "cache.json")
    found = actions.available_models(refresh=True)
    assert found["acme"][0] == "acme/coder-large" and found["local"] == ["local/qwen"]
    assert asked == ["deepseek"], "only providers opencode knows by itself are asked"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("{", "is not JSON"),
        ('{"model": "x"}', "defines no providers"),
        ('{"provider": {"Big Co": {}}}', "cannot be named"),
    ],
)
def test_a_file_that_is_no_use_is_refused(env, tmp_path, text, error):
    path = tmp_path / "opencode.json"
    path.write_text(text)
    with pytest.raises(ConfigError, match=error):
        providers.import_opencode(path, env={})


def test_a_missing_file_is_refused(env, tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        providers.import_opencode(tmp_path / "nope.json", env={})


def test_comments_inside_strings_are_kept():
    text = '{"url": "http://x//y", /* c */ "a": [1, 2,], // end\n}'
    assert json.loads(providers.without_comments(text)) == {"url": "http://x//y", "a": [1, 2]}


def test_auth_import_says_what_came(source, capsys, monkeypatch):
    from vivibox.cli import main

    monkeypatch.setenv("ACME_KEY", "acme-secret")
    assert main(["auth", "import", str(source)]) == 0
    out = capsys.readouterr().out
    assert "acme" in out and "3 models, key from $ACME_KEY" in out and "acme-secret" not in out
