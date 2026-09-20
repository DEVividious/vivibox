"""Opening the review copy in an editor: what is installed here, and how to start it.

A command with {path} in it can start anything; the list below is only what vivibox offers when you
have not chosen yet. Your choice is written to config.toml, so it asks once.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import config_dir

# Editors that open a folder from the command line, in the order they are offered.
KNOWN = (
    ("idea", "IntelliJ IDEA"),
    ("pycharm", "PyCharm"),
    ("webstorm", "WebStorm"),
    ("goland", "GoLand"),
    ("rider", "Rider"),
    ("clion", "CLion"),
    ("code", "VS Code"),
    ("code-insiders", "VS Code Insiders"),
    ("codium", "VSCodium"),
    ("zed", "Zed"),
    ("subl", "Sublime Text"),
    ("nvim", "Neovim"),
    ("vim", "Vim"),
    ("emacs", "Emacs"),
    ("hx", "Helix"),
)
TERMINAL = ("nvim", "vim", "hx", "emacs", "nano", "vi", "kak")
TOOLBOX = Path.home() / ".local" / "share" / "JetBrains" / "Toolbox" / "scripts"
DESKTOP_NAME = re.compile(r"^Name=(.+)$", re.MULTILINE)
DESKTOP_MIME = re.compile(r"^MimeType=(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class Editor:
    label: str
    command: str
    # Terminal editors take over the screen, so the view has to step aside while they run.
    terminal: bool = False


def _editor(program: str, label: str) -> Editor:
    return Editor(label, f"{shlex.quote(program)} {{path}}", Path(program).name in TERMINAL)


def from_path() -> list[Editor]:
    return [_editor(name, label) for name, label in KNOWN if shutil.which(name)]


def from_toolbox() -> list[Editor]:
    """JetBrains Toolbox installs its launchers in a folder that is often not on PATH."""
    if not TOOLBOX.is_dir():
        return []
    scripts = sorted(p for p in TOOLBOX.iterdir() if p.is_file() and os.access(p, os.X_OK))
    return [_editor(str(p), f"{p.name} (Toolbox)") for p in scripts if not shutil.which(p.name)]


def from_desktop() -> list[Editor]:
    """Applications your desktop offers for a folder, started through gio."""
    if not shutil.which("gio"):
        return []
    dirs = [Path.home() / ".local" / "share"] + [
        Path(d) for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/share").split(":") if d
    ]
    found = []
    for folder in dirs:
        for entry in sorted((folder / "applications").glob("*.desktop")):
            try:
                text = entry.read_text(errors="replace")
            except OSError:
                continue
            mime = DESKTOP_MIME.search(text)
            name = DESKTOP_NAME.search(text)
            if mime and name and "inode/directory" in mime.group(1):
                found.append(
                    Editor(f"{name.group(1)} (desktop)", f"gio launch {shlex.quote(str(entry))} {{path}}")
                )
    return found


def from_environment() -> list[Editor]:
    program = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    return [_editor(program, f"{program} (from $EDITOR)")] if program else []


def candidates() -> list[Editor]:
    """What this machine can open a folder with; the same editor is offered only once."""
    found, seen = [], set()
    for editor in from_path() + from_toolbox() + from_desktop() + from_environment():
        key = editor.command.split()[0]
        if key not in seen:
            seen.add(key)
            found.append(editor)
    return found


def command_for(path: Path, command: str) -> list[str]:
    parts = shlex.split(command)
    if any("{path}" in part for part in parts):
        return [part.replace("{path}", str(path)) for part in parts]
    return [*parts, str(path)]


def is_terminal(command: str) -> bool:
    return Path(shlex.split(command)[0]).name in TERMINAL


def open_folder(path: Path, command: str) -> None:
    """Starts the editor and leaves it alone; it outlives vivibox."""
    subprocess.Popen(command_for(path, command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)  # fmt: skip


def remember(command: str) -> Path:
    """Writes your choice to config.toml, under [review] ide."""
    path = config_dir() / "config.toml"
    text = path.read_text() if path.exists() else ""
    line = f'ide = "{command}"'
    if re.search(r"^\s*#?\s*ide\s*=", text, re.MULTILINE):
        text = re.sub(r"^\s*#?\s*ide\s*=.*$", line, text, count=1, flags=re.MULTILINE)
    elif re.search(r"^\[review\]", text, re.MULTILINE):
        text = re.sub(r"^\[review\]$", f"[review]\n{line}", text, count=1, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n\n[review]\n{line}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
