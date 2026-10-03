"""A project's toolchains: its own JDK and the tools mise brings, for the agent and for the gate.

The agent: mise installs into the shared cache (/cache/mise, writable), its shims are on PATH,
and a project JDK is JAVA_HOME=/config/jdk, a link per task to the version mise installed, first
on PATH. The gate: the shared caches are read-only for it (podspec, its container), its PATH is the
image's without the shims, mise has a data directory of the gate's own for what a project brings,
and a project JDK is the same link, made by the gate from where the writer's mise installed it
in the shared cache: the one program of the writer's cache a verification runs, named in
docs/security.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import podspec

if TYPE_CHECKING:
    from .pod import Pod

JDK_LINK = "/config/jdk"
# The files of a checkout that pin a tool for mise (mise's configuration file names).
MISE_FILES = frozenset(
    {
        "mise.toml", ".mise.toml", "mise.local.toml", ".mise.local.toml", ".tool-versions",
        ".mise/config.toml", "mise/config.toml", ".config/mise.toml", ".config/mise/config.toml",
    }
)  # fmt: skip
# The shims of the shared cache, on the agent's PATH and never on the gate's.
SHIMS = f"{podspec.CACHES['mise']}/shims"
# The image's PATH without the shims, for a pod built without the image's environment.
USUAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def mise_spec(java: str) -> str:
    return f"java@corretto-{java}" if java[0].isdigit() else f"java@{java}"


def agent_env(java: str, image_env: dict[str, str]) -> dict[str, str]:
    if not java:
        return {}
    return {"JAVA_HOME": JDK_LINK, "PATH": f"{JDK_LINK}/bin:{image_env.get('PATH', '/usr/bin:/bin')}"}


def gate_env(java: str, image_env: dict[str, str], gate_dir) -> dict[str, str]:
    """The gate's PATH: a project JDK first, then the shims of the gate's own mise, then the image's
    directories without the shims of the shared cache (a writer can put a program there). mise
    installs into the gate's directory, and never java: the gate's java is the image's, or the
    project's from the shared cache through JAVA_HOME (ensure)."""
    mise = f"{podspec.gate_caches(gate_dir)}/mise"
    left_out = (SHIMS, f"{JDK_LINK}/bin")
    dirs = [d for d in image_env.get("PATH", USUAL_PATH).split(":") if d and d not in left_out]
    dirs = [f"{JDK_LINK}/bin"] * bool(java) + [f"{mise}/shims"] + dirs
    env = {"JAVA_HOME": JDK_LINK} if java else {}
    return {**env, "PATH": ":".join(dirs), "MISE_DATA_DIR": mise, "MISE_DISABLE_TOOLS": "java"}


def install_declared(pod: Pod, gate: bool = False) -> None:
    """Install whatever the checkout's own mise.toml asks for, on top of the image's own toolchains.

    A shim exists only for a tool that is installed, so a project that chose Go or Ruby would
    otherwise fail with 'command not found' — which names the symptom and hides the cause. Failure
    is not raised: the verify command runs next and says what is actually missing. Only when the
    checkout pins a tool (MISE_FILES): the gate's mise data directory is its own and cold, and
    mise would otherwise download for every verification; the image's Python, uv and JDK are in
    its root filesystem and need no mise.
    """
    root = Path(pod.gate_src if gate else pod.repo)
    if not any((root / name).is_file() for name in MISE_FILES):
        return
    (pod.gate_exec if gate else pod.exec)("bash", "-c", "mise install --yes", check=False)


def ensure(pod: Pod, java: str, gate: bool = False, tools: list[str] | tuple = ()) -> None:
    """In the agent container, or with gate=True in the gate container, which has its own /config.
    The agent installs a project JDK into the shared cache; the gate only finds that JDK there, by
    `mise where` with the shared data directory named for that one command, which reads it.
    tools: the project's other toolchains, made global in the container's own mise configuration
    (under /config), so the repository gets no mise.toml; the installs go to the shared cache for
    the agent and to the gate's own directory for the gate."""
    run = pod.gate_exec if gate else pod.exec
    if java:
        spec = mise_spec(java)
        if gate:
            shared = f"MISE_DATA_DIR={podspec.CACHES['mise']} MISE_DISABLE_TOOLS= mise where {spec}"
            run("bash", "-c", f'jdk="$({shared})" && ln -sfn "$jdk" {JDK_LINK}')
        else:
            run(
                "bash", "-c",
                f'mise install --yes {spec} >/dev/null && ln -sfn "$(mise where {spec})" {JDK_LINK}',
            )  # fmt: skip
    if tools:
        run("bash", "-c", f"mise use --global --yes {' '.join(tools)}")
