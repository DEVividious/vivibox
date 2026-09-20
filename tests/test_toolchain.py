import pytest

from vivibox import toolchain
from vivibox.config import ConfigError, load_project


def test_default_vendor_is_corretto():
    assert toolchain.mise_spec("17") == "java@corretto-17"
    assert toolchain.mise_spec("temurin-11") == "java@temurin-11"


def test_agent_env_puts_project_jdk_first():
    env = toolchain.agent_env("17", {"PATH": "/cache/mise/shims:/usr/bin"})
    assert env == {"JAVA_HOME": "/config/jdk", "PATH": "/config/jdk/bin:/cache/mise/shims:/usr/bin"}
    assert toolchain.agent_env("", {"PATH": "/usr/bin"}) == {}


@pytest.mark.parametrize("value", ["17; rm -rf /", "$(id)", "17 21", "Corretto-17"])
def test_java_setting_is_validated(tmp_path, value):
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "p.toml").write_text(f'repo = "/r"\nverify = ["x"]\njava = "{value}"\n')
    with pytest.raises(ConfigError):
        load_project("p", tmp_path)
