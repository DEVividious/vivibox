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


def test_gate_env_has_the_images_path_without_the_shared_shims_and_its_own_mise():
    """The shims under /cache/mise are the writer's to write: the gate never looks there. What a
    project brings through mise, the gate installs into a data directory of its own; java never
    through mise in the gate (the image's JDK, or the project's through JAVA_HOME)."""
    image = {"PATH": "/cache/mise/shims:/usr/local/bin:/usr/bin", "JAVA_HOME": "/usr/lib/jvm/x"}
    env = toolchain.gate_env("", image, "/srv/t/disk/gate")
    assert env == {
        "PATH": "/srv/t/disk/gate/caches/mise/shims:/usr/local/bin:/usr/bin",
        "MISE_DATA_DIR": "/srv/t/disk/gate/caches/mise",
        "MISE_DISABLE_TOOLS": "java",
    }
    env = toolchain.gate_env("17", image, "/srv/t/disk/gate")
    assert env["JAVA_HOME"] == "/config/jdk" and env["PATH"].startswith("/config/jdk/bin:/srv/t/disk/gate/")
    # Built from the agent's environment by mistake: the agent's link and the shims come out.
    agents = {"PATH": "/config/jdk/bin:/cache/mise/shims:/usr/bin"}
    assert toolchain.gate_env("", agents, "/g")["PATH"] == "/g/caches/mise/shims:/usr/bin"
    # An image whose environment nobody asked for: the usual directories, never the shims.
    assert toolchain.gate_env("", {}, "/g")["PATH"] == "/g/caches/mise/shims:" + toolchain.USUAL_PATH


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
    assert pod.agent == ["mise install --yes", "mise use --global --yes go@1.25.3"]
    assert pod.gate == ["mise use --global --yes go@1.25.3 rust@stable"]
    toolchain.ensure(pod, "")
    assert pod.agent[2:] == ["mise install --yes"], "no tools: the defaults, which the gate no longer fills"


def test_the_agents_mise_fills_the_shared_cache_with_the_defaults_the_gate_once_did():
    """The gate's `mise install` used to install the image's default Python and uv into the shared
    cache, where the agent's shims found them. The gate installs into its own directory now, so the
    agent's start does it; it is a no-op once they are there."""
    pod = Recording()
    toolchain.ensure(pod, "")
    assert pod.agent == ["mise install --yes"]
    toolchain.ensure(pod, "", gate=True)
    assert pod.gate == [], "the gate's defaults come with install_declared, into its own directory"


def test_the_gate_finds_a_project_jdk_in_the_shared_cache_without_installing_anything():
    """The gate's mise data directory is its own and cold; the JDK the writer's mise installed is
    read from the shared cache, read-only, by `mise where` with that data directory named for
    the one command, and linked under the gate's home: the named residual risk, nothing more."""
    pod = Recording()
    toolchain.ensure(pod, "17", gate=True)
    where = "MISE_DATA_DIR=/cache/mise MISE_DISABLE_TOOLS= mise where java@corretto-17"
    assert pod.gate == [f'jdk="$({where})" && ln -sfn "$jdk" /config/jdk']
    toolchain.ensure(pod, "17")
    assert pod.agent[1].startswith("mise install --yes java@corretto-17 >/dev/null && ln -sfn"), "installs"
