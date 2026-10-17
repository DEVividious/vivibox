"""Serena in the code index's container, I: where it and its language servers keep their state,
the configuration vivibox gives it, and the environment it runs in. Both of I's runs use this:
the steady one (relay) and the provisioning run (provision), so that what the second fills is
what the first finds.

All of Serena's state is in I's store, mounted at STORE, and none of it in the project's tree,
which is I's copy of the commits at the planner's path: that copy is reset and cleaned to the
host's commit at every start and every refresh, so what is written in it does not stay, and a
project's own `.serena/` is never Serena's. Serena reads `<project>/.serena/project.yml` when
the folder its configuration names for the project's data is missing, so that folder is made
before Serena starts, at every start.

The configuration is vivibox's, written here at every start over what is there: Serena's global
one and the project's, with no setting that runs a command (no activation command, no language
server of the project's choosing, no project trusted for its own settings), the languages the
host chose, and the Java language server's heap from the memory I is given. The Java language
server's workspace is removed at every start: it holds the tree of its last start, which the
server trusts over the copy as it is now (what it downloaded stays: the servers, the shared
index of the JDK's and the jars', the Maven and Gradle caches). I may have changed any of it
since: it is I's own, and I is taken to be compromised; what is written again here is
only so that an honest start is the same start every time."""

from __future__ import annotations

import json
import os
import shutil

STORE = "/config"
SERENA_HOME = f"{STORE}/serena"
SERENA_CONFIG = f"{SERENA_HOME}/serena_config.yml"
# The project's data folder (its project.yml, Serena's symbol caches): in the store, not the tree.
PROJECT_DATA = f"{SERENA_HOME}/project"
PROJECT_YML = f"{PROJECT_DATA}/project.yml"
# A file Serena reads over project.yml, never vivibox's.
PROJECT_LOCAL_YML = f"{PROJECT_DATA}/project.local.yml"
# The Java language server's workspaces (its project model, its index of the project's sources):
# Serena's layout for vscode-java, one per project and settings.
JDT_WORKSPACES = f"{SERENA_HOME}/language_servers/static/EclipseJDTLS/workspaces"
SERENA = "/usr/local/bin/serena"
# Serena's own interpreter, which the provisioning run imports Serena in.
SERENA_PYTHON = "/opt/uv-tools/serena-agent/bin/python"
# The language servers vivibox gives Serena, by Serena's name for each.
LANGUAGES = ("java", "typescript", "python")
# What the image's environment names under /cache, which I has not: each tool's own in the store.
FOLDERS = ("gradle", "npm", "uv", ".cache", ".config", ".local/share")
# A tool call longer than this is ended by Serena itself, before the wrapper's own limit.
TOOL_SECONDS = 110
# The Java language server's heap, as a share of I's memory: the rest is its own overhead, the
# other language servers, Serena and the scratch tmpfs.
HEAP_SHARE = 5 / 8
HEAP_LEAST = 512 * 2**20
HEAP_WITHOUT_LIMIT = 3 * 2**30
CGROUP_MEMORY = "/sys/fs/cgroup/memory.max"


def heap(memory_max: str = CGROUP_MEMORY) -> str:
    """The Java language server's -Xmx, from the memory limit of I's own cgroup."""
    try:
        with open(memory_max) as f:
            word = f.read().strip()
    except OSError:
        word = "max"
    total = int(word) if word.isdigit() else 0
    size = max(HEAP_LEAST, int(total * HEAP_SHARE)) if total else HEAP_WITHOUT_LIMIT
    return f"{size // 2**20}m"


def environment(steady: bool) -> dict[str, str]:
    """Serena's environment, whole: nothing of the image's that names a folder I has not, every
    tool's cache in the store; offline for uv in the steady run, which has no network."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": STORE,
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        "SERENA_HOME": SERENA_HOME,
        "SERENA_USAGE_REPORTING": "false",
        "GRADLE_USER_HOME": f"{STORE}/gradle",
        "npm_config_cache": f"{STORE}/npm",
        "npm_config_update_notifier": "false",
        "UV_CACHE_DIR": f"{STORE}/uv",
        "UV_PYTHON_INSTALL_DIR": f"{STORE}/uv/python",
        "UV_LINK_MODE": "copy",
        "XDG_CACHE_HOME": f"{STORE}/.cache",
        "XDG_CONFIG_HOME": f"{STORE}/.config",
        "XDG_DATA_HOME": f"{STORE}/.local/share",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if steady:
        env["UV_OFFLINE"] = "1"
    return env


def global_config(java_heap: str) -> str:
    """Serena's own configuration: no dashboard, no window, no project trusted for its own
    settings, the project's data in the store, the tools' time limit under the wrapper's."""
    settings = {
        "language_backend": "LSP",
        "gui_log_window": False,
        "web_dashboard": False,
        "web_dashboard_open_on_launch": False,
        "log_level": 20,
        "trace_lsp_communication": False,
        "ls_specific_settings": {"java": {"jdtls_xmx": java_heap}},
        "ignored_paths": [],
        "tool_timeout": TOOL_SECONDS,
        "excluded_tools": [],
        "included_optional_tools": [],
        "fixed_tools": [],
        "base_modes": [],
        "default_modes": ["planning", "no-memories"],
        "default_max_tool_answer_chars": 150000,
        "project_serena_folder_location": PROJECT_DATA,
        "trusted_project_path_patterns": [],
        "projects": [],
    }
    # JSON is YAML: each value as it is, whatever it holds.
    return "".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items())


def project_config(languages: list[str]) -> str:
    """The project's configuration: the languages, read-only, and nothing that runs a command."""
    settings = {
        "project_name": "project",
        "language_servers": languages,
        "encoding": "utf-8",
        "ignore_all_files_in_gitignore": True,
        "ls_specific_settings": {},
        "ls_workspace_folders": ["."],
        "ls_additional_workspace_folders": [],
        "ignored_paths": [],
        "read_only": True,
        "excluded_tools": [],
        "included_optional_tools": [],
        "fixed_tools": [],
        "initial_prompt": "",
    }
    return "".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items())


def _replace(path: str, text: str) -> None:
    """path made a file holding text, whatever was in its place (a link is removed, not followed)."""
    if os.path.lexists(path) and not os.path.isdir(path):
        os.unlink(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def prepare(languages: list[str], java_heap: str) -> None:
    """The store made ready for Serena: its folders, and vivibox's configuration over whatever was
    there, the project's data folder among them so that the project's own `.serena/` is not read."""
    if not languages or any(language not in LANGUAGES for language in languages):
        raise ValueError(f"not languages vivibox gives Serena: {languages!r}")
    for folder in (SERENA_HOME, PROJECT_DATA, *(f"{STORE}/{name}" for name in FOLDERS)):
        if os.path.islink(folder):
            os.unlink(folder)
        os.makedirs(folder, mode=0o700, exist_ok=True)
    if os.path.lexists(PROJECT_LOCAL_YML):
        os.unlink(PROJECT_LOCAL_YML)
    # The Java language server's workspace holds the tree as it was at its last start, and it
    # trusts that over the copy as it is now: it starts on a workspace of its own every time.
    if os.path.islink(JDT_WORKSPACES):
        os.unlink(JDT_WORKSPACES)
    elif os.path.isdir(JDT_WORKSPACES):
        shutil.rmtree(JDT_WORKSPACES)
    _replace(SERENA_CONFIG, global_config(java_heap))
    _replace(PROJECT_YML, project_config(languages))


def command(project: str) -> list[str]:
    """Serena's MCP server on stdio, for the project at project."""
    return [
        SERENA, "start-mcp-server", "--transport", "stdio", "--project", project,
        "--context", "ide", "--mode", "planning", "--mode", "no-memories",
        "--enable-web-dashboard", "false", "--enable-gui-log-window", "false",
        "--open-web-dashboard", "false",
    ]  # fmt: skip


def arguments(argv: list[str]) -> tuple[str, list[str], list[str]]:
    """--project <path> --languages <a,b> [rest]: the project's path, its languages, the rest."""
    found: dict[str, str] = {}
    rest = list(argv)
    for key in ("--project", "--languages"):
        if key not in rest or rest.index(key) + 1 >= len(rest):
            raise ValueError(f"{key} is required")
        at = rest.index(key)
        found[key] = rest[at + 1]
        del rest[at : at + 2]
    project = found["--project"]
    if not os.path.isabs(project):
        raise ValueError("--project is an absolute path")
    return project, found["--languages"].split(","), rest
