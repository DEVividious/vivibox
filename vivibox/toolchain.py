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


def ensure(pod: Pod, java: str, gate: bool = False) -> None:
    """In the agent container, or with gate=True in the gate container, which has its own /config."""
    if not java:
        return
    spec = mise_spec(java)
    script = f'mise install --yes {spec} >/dev/null && ln -sfn "$(mise where {spec})" {JDK_LINK}'
    (pod.gate_exec if gate else pod.exec)("bash", "-c", script)
