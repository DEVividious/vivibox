"""A project's own JDK, installed with mise into the shared cache and used through JAVA_HOME and PATH.

The agent container gets JAVA_HOME=/config/jdk and that JDK first on PATH; /config/jdk is a link,
per task, to the version mise installed. Build tools, the agent's shell and the gate all see it.
"""

from __future__ import annotations

from .pod import Pod

JDK_LINK = "/config/jdk"


def mise_spec(java: str) -> str:
    return f"java@corretto-{java}" if java[0].isdigit() else f"java@{java}"


def agent_env(java: str, image_env: dict[str, str]) -> dict[str, str]:
    if not java:
        return {}
    return {"JAVA_HOME": JDK_LINK, "PATH": f"{JDK_LINK}/bin:{image_env.get('PATH', '/usr/bin:/bin')}"}


def install_declared(pod: Pod, gate: bool = False) -> None:
    """Install whatever the checkout's own mise.toml asks for, on top of the image's defaults.

    A shim exists only for a tool that is installed, so a project that chose Go or Ruby would
    otherwise fail with 'command not found' — which names the symptom and hides the cause. Failure
    is not raised: the verify command runs next and says what is actually missing.
    """
    (pod.gate_exec if gate else pod.exec)("bash", "-c", "mise install --yes", check=False)


def ensure(pod: Pod, java: str, gate: bool = False, tools: list[str] | tuple = ()) -> None:
    """In the agent container, or with gate=True in the gate container, which has its own /config.
    tools: the project's other toolchains, made global in the container's own mise configuration
    (under /config), so the repository gets no mise.toml; the installs go to the shared cache."""
    run = pod.gate_exec if gate else pod.exec
    if java:
        spec = mise_spec(java)
        run(
            "bash", "-c", f'mise install --yes {spec} >/dev/null && ln -sfn "$(mise where {spec})" {JDK_LINK}'
        )
    if tools:
        run("bash", "-c", f"mise use --global --yes {' '.join(tools)}")
