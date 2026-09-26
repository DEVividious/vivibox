"""Projects: what folder is one, what setting one up writes, and what keeps its tasks from
starting. Reached through actions, like everything the view and the command line call.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import init as project_init
from . import (
    repo,
)
from .config import (
    PROJECT_NAME,
    ConfigError,
    config_dir,
    load_project,
)


def git_root(path: Path) -> Path | None:
    top = (
        repo.git("rev-parse", "--show-toplevel", cwd=path, check=False).stdout.strip()
        if path.is_dir()
        else ""
    )
    return Path(top) if top else None


def project_files() -> list[Path]:
    folder = config_dir() / "projects"
    return sorted(folder.glob("*.toml")) if folder.is_dir() else []


def project_at(path: Path) -> str:
    """The name of the project already set up for this repository, if there is one."""
    for other in project_files():
        try:
            if load_project(other.stem).repo.expanduser().resolve() == path:
                return other.stem
        except ConfigError:
            continue  # a project file that cannot be read says nothing about this folder
    return ""


def broken_projects() -> dict[str, str]:
    """Projects that cannot be worked in, by name: the folder is gone, or the file does not parse.

    A repository you move or delete leaves its project file behind; that is a thing to mention, not a
    reason for vivibox to stop.
    """
    broken = {}
    for file in project_files():
        try:
            project = load_project(file.stem)
        except ConfigError as e:
            broken[file.stem] = str(e.args[0] if e.args else e)
            continue
        if not project.repo.expanduser().is_dir():
            broken[file.stem] = f"{project.repo} is gone"
    return broken


def project_problem(name: str) -> str:
    """Why no task of this project could start now, in a line; "" when they could. Shown on the
    project itself, so it is known before a task is created, not from a message that is gone
    in seconds after one fails to start."""
    try:
        project = load_project(name)
    except ConfigError as e:
        return str(e.args[0] if e.args else e)
    if not project.repo.expanduser().is_dir():
        return f"{project.repo} is gone"
    if missing := [v for v in project.pass_env if v not in os.environ]:
        return f"{', '.join(missing)} not set in this shell"
    return ""


def forget_project(name: str) -> Path:
    """Removes a project's file. The repository, wherever it is, is left alone."""
    path = config_dir() / "projects" / f"{name}.toml"
    if not path.exists():
        raise ConfigError(f"there is no project '{name}'")
    path.unlink()
    return path


# What a repository holds before anything has been written in it.
GROUNDWORK = {"README.md", "README", "readme.md", "LICENSE", "LICENSE.md", ".gitignore"}


def empty_project(name: str) -> bool:
    """True while a project has no code yet, so there is nothing in it that could work wrong."""
    try:
        tracked = repo.git("ls-files", cwd=load_project(name).repo, check=False).stdout.split()
    except (ConfigError, repo.RepoError):
        return False
    return all(path in GROUNDWORK for path in tracked)


def propose_project(path: Path) -> project_init.Detected:
    """What a project for this folder would look like: its name and how to build and test it."""
    return project_init.detect(git_root(path) or path)


def start_repository(path: Path) -> Path:
    """A new git repository for a project from scratch: an empty one with a first commit."""
    path.mkdir(parents=True, exist_ok=True)
    if any(p.name != ".git" for p in path.iterdir()):
        raise ConfigError(f"{path} is not empty and not a git repository; set up git there yourself")
    repo.git("init", "--quiet", "-b", "main", str(path))
    repo.identity(path)  # your name and e-mail, or this tells you to set them
    (path / "README.md").write_text(f"# {path.name}\n")
    repo.git("add", "README.md", cwd=path)
    repo.git("commit", "--quiet", "-m", "Initial commit", cwd=path)
    return path


def setup_project(
    path: Path,
    name: str,
    verify: list[str],
    java: str = "",
    create: bool = False,
    no_build: bool = False,
    prepare: list[str] | None = None,
) -> Path:
    """Writes ~/.config/vivibox/projects/<name>.toml for this repository. Returns the file.
    prepare: what a new task's clone runs first; None takes the build files' suggestion."""
    path = path.expanduser().resolve()
    top = git_root(path)
    if top is None:
        if not create:
            raise ConfigError(f"{path} is not a git repository; nothing set up")
        top = start_repository(path)
    if taken := project_at(top):
        raise ConfigError(f"this repository is already project {taken}")
    if not PROJECT_NAME.match(name):
        raise ConfigError(f"project name '{name}': lowercase letters, digits and '-', up to 31 characters")
    target = config_dir() / "projects" / f"{name}.toml"
    if target.exists():
        raise ConfigError(f"project {name} is already set up in {target}; pick another name")
    found = project_init.Detected(
        name,
        top,
        [c.strip() for c in verify if c.strip()],
        project_init.detect_demo(top),
        java,
        no_build=no_build,
        prepare=[c.strip() for c in prepare if c.strip()]
        if prepare is not None
        else project_init.prepare_suggestion(top),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(project_init.render(found))
    return target
