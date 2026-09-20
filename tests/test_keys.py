import stat

import pytest

from vivibox import keys


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_set_get_list_remove():
    keys.set_key("deepseek", "  sk-1234567890\n")
    assert keys.get_key("deepseek") == "sk-1234567890"
    assert keys.list_keys() == {"deepseek": "sk-… (13)"}
    assert keys.remove("deepseek") and not keys.remove("deepseek")
    assert keys.list_keys() == {}


def test_store_is_private():
    keys.set_key("deepseek", "sk-x")
    assert stat.S_IMODE(keys.store().stat().st_mode) == 0o700
    assert stat.S_IMODE((keys.store() / "deepseek").stat().st_mode) == 0o600


def test_key_readable_by_others_is_refused():
    keys.set_key("deepseek", "sk-x")
    (keys.store() / "deepseek").chmod(0o644)
    with pytest.raises(keys.KeyStoreError, match="chmod 600"):
        keys.get_key("deepseek")


def test_missing_key_says_how_to_add_it():
    with pytest.raises(keys.KeyStoreError, match="vivibox auth set deepseek"):
        keys.get_key("deepseek")


@pytest.mark.parametrize("provider", ["../etc/passwd", "Deep", "", "a/b", ".hidden"])
def test_provider_names_cannot_escape_the_store(provider):
    with pytest.raises(keys.KeyStoreError):
        keys.set_key(provider, "sk-x")


@pytest.mark.parametrize("value", ["", "   ", "sk 1"])
def test_keys_are_single_words(value):
    with pytest.raises(keys.KeyStoreError):
        keys.set_key("deepseek", value)


def test_cli_auth_reads_key_from_stdin(monkeypatch, capsys):
    import io

    from vivibox.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-from-pipe\n"))
    assert main(["auth", "set", "deepseek"]) == 0
    assert keys.get_key("deepseek") == "sk-from-pipe"
    assert "sk-from-pipe" not in capsys.readouterr().out, "the key itself is never printed"
    assert main(["auth", "list"]) == 0
    assert "deepseek" in capsys.readouterr().out
