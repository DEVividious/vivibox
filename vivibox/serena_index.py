"""The planner's code index with Serena as its program (podspec.IndexServer): which languages it
reads, chosen by the host from its own view of the commits, and how the index container I and
its provisioning run start it. The program itself is vivibox/index_program, mounted read-only in
I and run there; nothing here runs in I, and nothing I writes is read here.

What Serena gets is decided here and by the configuration vivibox/index_program writes in I's
store at every start: the project at the planner's path (I's copy), its languages, no setting
of the project's own. The languages are those of the host's view at I's start: each of Java,
TypeScript (with JavaScript) and Python with enough files, the most first (Serena's default
language server). A language added to the project later is read from the next start of I,
after a provisioning run for it."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from . import index_program, podspec
from .pod import Mount
from .podspec import IndexServer
from .providers import NOT_SOURCES

PROGRAM = Path(index_program.__file__).parent
PROGRAM_MOUNT = "/opt/vivibox-program"
SERENA_PYTHON = "/opt/uv-tools/serena-agent/bin/python"
# Each program is a module of the package, imported in isolated mode from its read-only mount.
# The first line names what the provisioning run fills the store for: a new one is another
# command, and the host provisions again (pod_index._provisioned), on a store it discarded.
LAUNCH = (
    "# vivibox's code index: Serena 1.7.0, store layout 1\n"
    "import sys; sys.path.insert(0, {root!r}); from vivibox.index_program.{module} import main; "
    "sys.exit(main(sys.argv[1:]))"
)
# Serena's name for each language it reads here, by the files of it.
SUFFIXES = {
    "java": (".java",),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "python": (".py",),
}
# Fewer files than this of a language, and Serena starts no language server for it: each costs
# memory and a start that may fail (Serena then answers nothing). A judgement, not a measurement.
LEAST_FILES = 10


def languages(view: Path, least: int = LEAST_FILES) -> list[str]:
    """The languages of the project in view (the host's own view of the commits, which holds no
    link), most files first: a walk of its files, no git."""
    counts = dict.fromkeys(SUFFIXES, 0)
    by_suffix = {suffix: name for name, suffixes in SUFFIXES.items() for suffix in suffixes}
    for _, dirs, names in os.walk(view):
        dirs[:] = [d for d in dirs if d not in NOT_SOURCES]
        for name in names:
            if (language := by_suffix.get(os.path.splitext(name)[1])) is not None:
                counts[language] += 1
    found = [name for name, n in counts.items() if n >= least]
    return sorted(found, key=lambda name: -counts[name])


def _launch(module: str) -> str:
    return LAUNCH.format(root=PROGRAM_MOUNT, module=module)


def for_project(repo: Path, chosen: list[str]) -> IndexServer:
    """Serena on the project at repo (I's copy at the planner's path), for these languages: in
    their order for Serena (its first is its default), and as a set for what the provisioning
    run fills the store for, so that a commit that changes only which language has the most files
    provisions nothing again (the host provisions again on another command, on a store it
    discarded: pod_index._provisioned)."""
    chosen = list(dict.fromkeys(chosen))
    if not chosen:
        return replace(server(repo), command=(), provision=())
    project = ("--project", str(repo))
    in_order = ("--languages", ",".join(chosen))
    as_set = ("--languages", ",".join(sorted(chosen)))
    return IndexServer(
        mounts=(Mount(str(PROGRAM), f"{PROGRAM_MOUNT}/vivibox/index_program", read_only=True),),
        command=(
            "python3",
            "-I",
            "-c",
            _launch("relay"),
            *project,
            *in_order,
            podspec.INDEX_SOCKET_MOUNT,
        ),
        provision=(SERENA_PYTHON, "-I", "-c", _launch("provision"), *project, *as_set),
        for_view=lambda view: for_project(repo, languages(view)),
    )


def server(repo: Path) -> IndexServer:
    """Serena as a pod's index, before its languages are known: the pod chooses them from its
    view at I's start (IndexServer.for_view)."""
    return IndexServer(
        mounts=(Mount(str(PROGRAM), f"{PROGRAM_MOUNT}/vivibox/index_program", read_only=True),),
        command=(),
        for_view=lambda view: for_project(repo, languages(view)),
    )
