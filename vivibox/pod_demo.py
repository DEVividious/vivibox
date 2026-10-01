"""Running the project for the person to look at, and preparing it: what listens in the pod, what
the demo prints, what is left over. Mixed into Pod, whose _run, agent, sidecar and repo it uses."""

from __future__ import annotations

import shlex
import time
from collections.abc import Sequence

from .probe import (
    DEMO_ALIVE,
    DEMO_KILL,
    DEMO_LOG,
    DEMO_PID,
    LEFTOVERS,
    MAX_COMMAND,
    PROBE,
    DemoState,
    Leftover,
    Listener,
    PodProbe,
    parse_listening,
)

# The preparation's process, in the agent's /tmp: gone with the container, as the process is.
PREPARE_PID = "/tmp/vivibox-prepare.pid"


class PodDemo:
    def listening(self) -> list[Listener]:
        """What is listening in the pod, read from the kernel so no tool has to be in the image.
        Ephemeral ports are left out: they belong to the pod's own Docker daemon."""
        found = self._run(
            "docker", "exec", self.sidecar, "sh", "-c",
            "cat /proc/sys/net/ipv4/ip_local_port_range; cat /proc/net/tcp /proc/net/tcp6 2>/dev/null",
            check=False,
        )  # fmt: skip
        if found.returncode != 0:
            return []
        return parse_listening(found.stdout, from_table=True)

    def demo_running(self) -> bool:
        return self.demo_state().running

    def probe(self) -> PodProbe:
        """One question of the pod instead of two, so a list of tasks stays cheap to keep current."""
        found = self._run("docker", "exec", self.agent, "sh", "-c", PROBE, check=False)
        if found.returncode != 0:
            return PodProbe()
        head, _, log = found.stdout.partition("\n--\n")
        state, _, rows = head.partition("\n")
        return PodProbe(parse_listening(rows), state.strip() == "RUNNING", log.strip())

    def demo_state(self) -> DemoState:
        """Whether what you started is still alive, and its last output, in one question: a crash
        is only useful if it comes with the reason, and the reason is in the log."""
        script = f"({DEMO_ALIVE}) && echo RUNNING || echo STOPPED; tail -n 25 {DEMO_LOG} 2>/dev/null"
        found = self._run("docker", "exec", self.agent, "sh", "-c", script, check=False)
        if found.returncode != 0:
            return DemoState(False, "")
        first, _, rest = found.stdout.partition("\n")
        return DemoState(first.strip() == "RUNNING", rest.strip())

    def leftovers(self) -> list[Leftover]:
        """What the agent's container has listening, one entry per process. opencode's own server
        is left out: it is the agent, behind the window you watch it in."""
        found = self._run("docker", "exec", self.agent, "sh", "-c", LEFTOVERS, check=False)
        if found.returncode != 0:
            return []
        seen: dict[int, Leftover] = {}
        for line in found.stdout.splitlines():
            pid, _, rest = line.partition("\t")
            port, _, command = rest.partition("\t")
            try:
                pid, port = int(pid), int(port, 16)
            except ValueError:
                continue
            command = " ".join(command.split())
            if "opencode serve" in command or pid in seen:
                continue
            seen[pid] = Leftover(pid, port, command[:MAX_COMMAND])
        return list(seen.values())

    def stop_leftovers(self) -> list[str]:
        """Stops what the agent left listening in its container and says what went. The app is
        run once the agent rests, so a server still up in there is a leftover of its turn, and
        one that holds the port the app wants makes the app die on it."""
        left = self.leftovers()
        for process in left:
            self._run(
                "docker", "exec", self.agent, "sh", "-c", f"kill -TERM {process.pid} 2>/dev/null", check=False
            )
        return [str(process) for process in left]

    def demo_stop(self) -> None:
        """Kills the whole process group: a build tool starting a server leaves children behind."""
        self._run("docker", "exec", self.agent, "sh", "-c", DEMO_KILL, check=False)

    def prepare_start(self, commands: Sequence[str], log: str, exit_file: str) -> None:
        """Runs a project's preparation in the clone, in the background like the app: it outlives
        the exec that started it, and says how it ended in exit_file, which goes first."""
        script = f"({' && '.join(commands)}); echo $? > {exit_file}"
        started = (
            f"rm -f {exit_file}; setsid sh -c {shlex.quote(script)} > {log} 2>&1 < /dev/null &"
            f" echo $! > {PREPARE_PID}"
        )
        self._run("docker", "exec", "-d", "-w", str(self.repo), self.agent, "sh", "-c", started)

    def prepare_running(self) -> bool:
        alive = f'test -f {PREPARE_PID} && kill -0 "$(cat {PREPARE_PID})" 2>/dev/null'
        return self._run("docker", "exec", self.agent, "sh", "-c", alive, check=False).returncode == 0

    def demo_log(self, lines: int = 20) -> str:
        found = self._run("docker", "exec", self.agent, "tail", "-n", str(lines), DEMO_LOG, check=False)
        return found.stdout.strip() if found.returncode == 0 else ""

    def demo_start(self, commands: Sequence[str], workdir: str = "", wait: float = 40) -> list[Listener]:
        """Runs the project the way the project is run, in the agent's container, and waits for it
        to listen. Returns the ports that were not there before; empty means nothing came up.

        setsid: the commands outlive the exec that started them, so the app stays up while you look
        at it, and its whole process group can be stopped later in one go.
        """
        self.demo_stop()
        # A port the last run held is not free the moment its process is killed, and starting again
        # on the same port would then look like nothing came up. Wait for the picture to settle.
        before = {listener.port for listener in self.listening()}
        settle = time.monotonic() + 10
        while time.monotonic() < settle:
            time.sleep(0.5)
            now_ports = {listener.port for listener in self.listening()}
            if now_ports == before:
                break
            before = now_ports
        script = " && ".join(commands)
        started = f"setsid sh -c {shlex.quote(script)} > {DEMO_LOG} 2>&1 < /dev/null & echo $! > {DEMO_PID}"
        self._run("docker", "exec", "-d", "-w", workdir or str(self.repo), self.agent, "sh", "-c", started)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            fresh = [listener for listener in self.listening() if listener.port not in before]
            if fresh:
                return fresh
            if not self.demo_running():
                return []  # it exited; the log says why
            time.sleep(0.5)
        return []
