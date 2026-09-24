import json

import pytest

from vivibox import actions, keys, opencode, providers, roles
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
    assert [(p.name, p.what) for p in found] == [
        ("acme", "3 models"),
        ("local", "1 model"),
        ("deepseek", "opencode's provider"),
    ]
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
    monkeypatch.setattr(roles, "provider_models", lambda p: asked.append(p) or [f"{p}/m"])
    monkeypatch.setattr(roles, "models_cache", lambda: source.parent / "cache.json")
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


def test_opencode_configurations_are_found_where_opencode_reads_them(env, tmp_path):
    xdg = tmp_path / "xdg" / "opencode"
    xdg.mkdir(parents=True)
    (xdg / "opencode.jsonc").write_text('{"provider": {"a": {}, "b": {}}} // global')
    repo = tmp_path / "repo"
    (repo / "opencode.json").write_text('{"provider": {"c": {}}}')
    named = tmp_path / "custom.json"
    named.write_text('{"provider": {"d": {}}}')
    (tmp_path / "no-providers.json").write_text('{"mcp": {}}')
    env_vars = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "OPENCODE_CONFIG": str(named)}
    found = providers.discover([repo], env=env_vars)
    assert found == [(named, 1), (xdg / "opencode.jsonc", 2), (repo / "opencode.json", 1)]
    env_vars["OPENCODE_CONFIG"] = str(tmp_path / "no-providers.json")
    assert (tmp_path / "no-providers.json", 0) not in providers.discover([], env=env_vars)


def test_opencode_own_provider_brings_only_its_key(source):
    providers.import_opencode(source, env={"ACME_KEY": "k"})
    assert "deepseek" not in providers.load() and keys.get_key("deepseek") == "sk-literal"
    found = {f.name: f.own for f in providers.read_opencode(source, env={}).found}
    assert found == {"acme": True, "local": True, "deepseek": False}


SERVERS = """{
  "mcp": {
    "company": {"type": "remote", "url": "https://mcp.acme.example/sse",
                "headers": {"Authorization": "Bearer mcp-secret"}},
    "tools": {"type": "local",
              "command": ["npx", "-y", "@acme/tools-mcp"],
              "environment": {"TOOLS_TOKEN": "{env:TOOLS_TOKEN}"}}
  },
  "agent": {}
}"""


@pytest.fixture
def servers(env, tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(SERVERS)
    return path


def test_mcp_servers_come_over_with_their_secrets_in_the_key_store(servers):
    reading = providers.read_opencode(servers, env={"TOOLS_TOKEN": "tools-secret"})
    assert [(f.kind, f.name, f.what) for f in reading.found] == [
        ("mcp", "company", "remote https://mcp.acme.example/sse"),
        ("mcp", "tools", "local npx -y @acme/tools-mcp"),
    ]
    assert reading.left == ["agent"]
    providers.bring_over(reading.found)
    stored = providers.mcp_path().read_text()
    assert "mcp-secret" not in stored and "tools-secret" not in stored
    assert keys.get_key("mcp.company.authorization") == "Bearer mcp-secret"
    assert keys.get_key("mcp.tools.tools_token") == "tools-secret"
    assert providers.mcp_secrets() == ["mcp.company.authorization", "mcp.tools.tools_token"]


def test_a_task_gets_the_mcp_servers_pointing_at_mounted_secrets(servers):
    providers.import_opencode(servers, env={"TOOLS_TOKEN": "t"})
    mcp = opencode.config("deepseek/deepseek-v4-flash")["mcp"]
    assert mcp["company"]["headers"] == {
        "Authorization": "{file:/run/vivibox-secrets/mcp.company.authorization}"
    }
    assert mcp["tools"]["environment"] == {"TOOLS_TOKEN": "{file:/run/vivibox-secrets/mcp.tools.tools_token}"}


def test_what_you_have_is_told_apart_from_what_differs(servers):
    env = {"TOOLS_TOKEN": "t"}
    assert {f.status for f in providers.read_opencode(servers, env=env).found} == {"new"}
    providers.import_opencode(servers, env=env)
    assert {f.status for f in providers.read_opencode(servers, env=env).found} == {"same"}
    status = {f.name: f.status for f in providers.read_opencode(servers, env={"TOOLS_TOKEN": "other"}).found}
    assert status == {"company": "same", "tools": "replaces"}, "a changed secret is a difference too"


def test_removing_an_mcp_server_removes_its_secrets(servers):
    providers.import_opencode(servers, env={"TOOLS_TOKEN": "t"})
    assert providers.forget("company", providers.MCP)
    assert "company" not in providers.load_mcp() and "mcp.company.authorization" not in keys.list_keys()
    assert "mcp.tools.tools_token" in keys.list_keys()


def test_a_file_with_neither_is_refused(env, tmp_path):
    path = tmp_path / "package.json"
    path.write_text('{"name": "x", "version": "1.0.0"}')
    with pytest.raises(ConfigError, match="defines no providers and no MCP servers"):
        providers.read_opencode(path, env={})


def test_a_fresh_download_is_offered_first(env, tmp_path):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads" / "opencode (1).json").write_text(SERVERS)
    (home / "Downloads" / "report.json").write_text("{}")
    found = providers.discover([], env={"HOME": str(home)})
    assert found == [(home / "Downloads" / "opencode (1).json", 2)]


def files_in(repo, n, suffix=".py", where=""):
    folder = repo / where if where else repo
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"m{i}{suffix}").write_text("x = 1\n")
    return repo


def test_serena_on_auto_comes_to_a_big_repository_and_not_to_a_small_one(env, tmp_path):
    assert providers.serena_mode() == "auto"
    small = files_in(tmp_path / "small", 20)
    big = files_in(tmp_path / "big", providers.SERENA_MIN_FILES)
    assert providers.serena_for(small) == (False, "off, 20 source files (fewer than 100)")
    assert providers.serena_for(big) == (True, "on, 100+ source files")
    serena = opencode.config("deepseek/deepseek-v4-flash", repo=big)["mcp"]["serena"]
    assert serena["command"][:2] == ["serena", "start-mcp-server"] and "/task/repo" in serena["command"]
    assert "mcp" not in opencode.config("deepseek/deepseek-v4-flash", repo=small)


def test_what_a_build_or_a_package_manager_made_is_not_counted(env, tmp_path):
    repo = files_in(tmp_path / "app", 5, ".ts")
    files_in(repo, 300, ".js", where="node_modules/lib")
    files_in(repo, 300, ".js", where="dist")
    (repo / "README.md").write_text("docs are not source\n")
    assert providers.source_files(repo) == 5


def test_serena_on_or_off_is_what_you_set_whatever_the_size(env, tmp_path):
    small = files_in(tmp_path / "small", 3)
    providers.set_serena_mode("on")
    assert providers.serena_for(small) == (True, "on, as you set it")
    providers.set_serena_mode("off")
    assert providers.serena_for(files_in(tmp_path / "big", 500)) == (False, "off, as you set it")
    assert not providers.enabled(providers.MCP, "serena")
    with pytest.raises(ConfigError):
        providers.set_serena_mode("sometimes")


def test_a_server_turned_off_is_kept_but_not_given_to_tasks(servers):
    providers.import_opencode(servers, env={"TOOLS_TOKEN": "t"})
    providers.set_enabled(providers.MCP, "company", False)
    assert "company" in providers.load_mcp() and "company" not in providers.task_mcp()
    assert providers.mcp_secrets() == ["mcp.tools.tools_token"], "its secret is not mounted either"


def test_a_serena_from_your_machine_is_not_imported_over_vivibox_own(env, tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text('{"mcp": {"serena": {"type": "local", "command": ["serena", "start-mcp-server"]}}}')
    (found,) = providers.read_opencode(path, env={}).found
    assert found.status == "builtin"


def test_a_provider_turned_off_offers_no_models(env, monkeypatch, tmp_path):
    keys.set_key("deepseek", "k")
    monkeypatch.setattr(roles, "provider_models", lambda p: [f"{p}/m"])
    monkeypatch.setattr(roles, "models_cache", lambda: tmp_path / "cache.json")
    assert "deepseek" in actions.available_models(refresh=True)
    providers.set_enabled(providers.PROVIDER, "deepseek", False)
    assert "deepseek" not in actions.available_models(refresh=True)
    assert keys.get_key("deepseek") == "k", "its key is kept for when it is on again"


def test_a_model_the_catalog_retired_is_not_offered(monkeypatch):
    """opencode's list still names a deprecated model, and its server then refuses it with
    "Model not found": the list vivibox offers leaves it out, so nobody picks it."""
    import subprocess

    from vivibox import roles

    out = (
        "deepseek/deepseek-flash\ndeepseek/deepseek-v4-flash\ndeepseek/deepseek-v4-pro\n"
        "---\ndeepseek-v4-flash\n"
    )
    monkeypatch.setattr(roles.image, "image_ref", lambda: "img")
    monkeypatch.setattr(
        roles.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")
    )
    assert roles.provider_models("deepseek") == ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]


def test_a_start_on_a_retired_model_says_so_and_what_to_pick(env, monkeypatch):
    from vivibox import actions, roles
    from vivibox.config import load_config

    config = load_config()  # the writer runs on deepseek/m in the test config? no: "m" has no provider
    available = {"deepseek": ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]}
    monkeypatch.setattr(roles, "load_config", lambda base=None: config)
    from vivibox.config import Role

    retired = type(config)(
        config.tasks_dir, config.max_iterations,
        {"planner": Role("manual", ""), "writer": Role("opencode", "deepseek/deepseek-v4-flash")},
    )  # fmt: skip
    why = actions.model_missing(retired, None, available)
    assert why.startswith(
        "the writer's model deepseek/deepseek-v4-flash is not one its provider offers now ("
    )
    assert "deepseek-flash" in why and "pick another under m, or k" in why
    fine = type(config)(
        config.tasks_dir,
        3,
        {"planner": Role("manual", ""), "writer": Role("opencode", "deepseek/deepseek-flash")},
    )
    assert actions.model_missing(fine, None, available) == ""
    assert actions.model_missing(retired, None, {"deepseek": []}) == "", (
        "a list that could not be read blocks nothing"
    )
