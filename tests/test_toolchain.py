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


def test_tools_are_mise_versions_and_nothing_else(tmp_path):
    (tmp_path / "projects").mkdir()
    path = tmp_path / "projects" / "p.toml"
    path.write_text('repo = "/r"\nverify = ["x"]\ntools = ["go@1.25.3", "rust@stable"]\n')
    assert load_project("p", tmp_path).tools == ["go@1.25.3", "rust@stable"]
    for bad in ('["go@1.25; rm -rf /"]', '["go"]', '"go@1.25"', '["$(id)@1"]'):
        path.write_text(f'repo = "/r"\nverify = ["x"]\ntools = {bad}\n')
        with pytest.raises(ConfigError):
            load_project("p", tmp_path)


class Recording:
    def __init__(self):
        self.agent, self.gate = [], []

    def exec(self, *cmd, check=True):
        self.agent.append(cmd[-1])

    def gate_exec(self, *cmd, check=True):
        self.gate.append(cmd[-1])


def test_a_projects_tools_are_installed_for_the_agent_and_the_gate():
    pod = Recording()
    toolchain.ensure(pod, "", tools=["go@1.25.3"])
    toolchain.ensure(pod, "", gate=True, tools=["go@1.25.3", "rust@stable"])
    assert pod.agent == ["mise use --global --yes go@1.25.3"]
    assert pod.gate == ["mise use --global --yes go@1.25.3 rust@stable"]
    toolchain.ensure(pod, "")
    assert len(pod.agent) == 1, "no tools, nothing to install"
