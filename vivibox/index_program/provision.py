"""The code index's provisioning run: what the steady I needs and cannot fetch, with no network,
fetched once into I's store while I has one. That is each language server Serena starts (it
downloads them, checked against the digests Serena pins for the versions it names) and, for Java,
the project's dependencies, which the Java language server's own import of the project downloads
into the store's Maven and Gradle folders: the steady I then builds the project from them offline.

It is Serena's own start of the project's language servers, in Serena's interpreter, with
vivibox's configuration (serena.prepare); then a wait until the Java import is done: the
language server says so (its project status), and what it downloads has stopped growing. Then
the language servers are stopped and the run ends: 0 once every one of them started, else 1. The
host knows only that status and gives the run a time limit of its own.

The run is taken to be compromised by the repository like the steady I (the import runs the
project's build scripts), with the internet besides: the host gives it nothing of the planner's
and discards everything I wrote before it starts.

<serena's python> -I -c <launch> --project <path> --languages <a,b>"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time

from vivibox.index_program import serena

# Where the Java import downloads: Maven's repository (the JVM's user.home is the store) and
# Gradle's caches and distributions; not the rest of Gradle's home, where its daemon's log grows
# as long as it runs.
DOWNLOADS = (f"{serena.STORE}/.m2", f"{serena.STORE}/gradle/caches", f"{serena.STORE}/gradle/wrapper")
# How long the downloads stay the same size before the import is taken to be done: with its
# status, and without it (a language server that never says).
SETTLED_SECONDS, SETTLED_WITHOUT_STATUS = 20.0, 120.0
POLL_SECONDS = 5.0
# The most the run waits for the import, under the host's own limit for the whole run.
IMPORT_SECONDS = 1500.0


def size(folders: tuple[str, ...] = DOWNLOADS) -> int:
    total = 0
    for top in folders:
        for folder, _, files in os.walk(top):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(folder, name)).st_size
    return total


def settled(
    status: threading.Event,
    measure=size,
    clock=time.monotonic,
    sleep=time.sleep,
    longest: float = IMPORT_SECONDS,
) -> bool:
    """Whether the downloads stopped growing within longest seconds: for SETTLED_SECONDS once
    the language server gave its project status, for SETTLED_WITHOUT_STATUS else."""
    start = clock()
    last, since = measure(), clock()
    while clock() - start < longest:
        sleep(POLL_SECONDS)
        now = measure()
        if now != last:
            last, since = now, clock()
        quiet = clock() - since
        if quiet >= (SETTLED_SECONDS if status.is_set() else SETTLED_WITHOUT_STATUS):
            return True
    return False


def watch_java_status(status: threading.Event) -> None:
    """status set when the Java language server first reports its project's status, whatever it
    is: the import is over, done or not (Serena itself waits only 20 seconds for it)."""
    from solidlsp.language_servers.eclipse_jdtls import EclipseJDTLS

    started = EclipseJDTLS._start_server

    def observed(self) -> None:
        def seen(method: str, params) -> None:
            if (
                method == "language/status"
                and isinstance(params, dict)
                and params.get("type") == "ProjectStatus"
            ):
                status.set()

        self.server.on_any_notification(seen)
        started(self)

    EclipseJDTLS._start_server = observed


def main(argv: list[str]) -> int:
    project, languages, rest = serena.arguments(argv)
    if rest:
        print("usage: provision --project <path> --languages <a,b>", file=sys.stderr)
        return 2
    serena.prepare(languages, serena.heap())
    os.environ.clear()
    os.environ.update(serena.environment(steady=False))
    os.chdir(serena.STORE)
    from serena.config.serena_config import SerenaConfig
    from serena.project import Project

    status = threading.Event()
    watch_java_status(status)
    loaded = Project.load(project, SerenaConfig.from_config_file(generate_if_missing=False))
    try:
        manager = loaded.create_language_server_manager()
    except Exception:
        return 1
    try:
        if "java" in languages and not settled(status):
            return 1
        return 0
    finally:
        manager.stop_all(save_cache=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
