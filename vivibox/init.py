"""vivibox init: sets up the repository you are in as a project, from what its build files tell.

Nothing happens behind your back: init shows the project file it would write and asks first.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_NAME

IMAGE_JAVA = 21
LTS = (21, 17, 11, 8)


@dataclass
class Detected:
    name: str
    repo: Path
    verify: list[str]
    demo: list[str] = field(default_factory=list)
    java: str = ""
    notes: list[str] = field(default_factory=list)
    # The build file the verify command comes from; "" when none was found.
    source: str = ""
    # Nothing to build or test here: verify = false in the project file.
    no_build: bool = False
    # What a new task's clone runs first, before the writer: a build without tests, suggested
    # by the build files; empty for nothing.
    prepare: list[str] = field(default_factory=list)
    # Toolchains the image does not have that the build files ask for, as mise versions.
    tools: list[str] = field(default_factory=list)


def project_name(repo: Path) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", repo.name.lower()).strip("-")[:31] or "project"
    return name if PROJECT_NAME.match(name) else f"p-{name}"[:31]


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def source_level(repo: Path) -> int | None:
    """The Java version the code is written for, from Gradle or Maven build files."""
    text = "".join(_read(repo / f) for f in ("build.gradle", "build.gradle.kts", "pom.xml"))
    patterns = (
        r"languageVersion\s*(?:=|\.set\()\s*JavaLanguageVersion\.of\((\d+)\)",
        r"(?:source|target)Compatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(?:1[._])?(\d+)",
        r"<maven\.compiler\.release>(\d+)<",
        r"<java\.version>(?:1\.)?(\d+)<",
        r"<maven\.compiler\.(?:source|target)>(?:1\.)?(\d+)<",
        r"<release>(\d+)</release>",
    )
    for pattern in patterns:
        if m := re.search(pattern, text):
            return int(m.group(1))
    return None


def gradle_version(repo: Path) -> tuple[int, int] | None:
    m = re.search(r"gradle-(\d+)\.(\d+)", _read(repo / "gradle" / "wrapper" / "gradle-wrapper.properties"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def newest_jdk_for_gradle(version: tuple[int, int]) -> int:
    """The newest LTS JDK a Gradle version runs on (docs.gradle.org compatibility matrix)."""
    if version >= (8, 5):
        return 21
    if version >= (7, 3):
        return 17
    return 11


# The project's own answer to "how do I run this", written by people who know it. Nothing we could
# work out from the build files beats it, so it is the only thing detection claims to know.
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def detect_demo(repo: Path) -> list[str]:
    return ["docker compose up"] if any((repo / name).exists() for name in COMPOSE_FILES) else []


NODE_VERIFY = {
    "npm": "npm ci && npm test",
    "yarn": "yarn install --immutable && yarn test",
    "yarn-classic": "yarn install --frozen-lockfile && yarn test",
    "pnpm": "pnpm install --frozen-lockfile && pnpm test",
}
# The install half of each: what a fresh clone needs before any of its scripts can run.
NODE_INSTALL = {manager: command.split(" && ")[0] for manager, command in NODE_VERIFY.items()}
# A command that installs the dependencies itself, in any of the package managers' words, also
# in a folder of its own (`yarn --cwd apps/web install`, `npm --prefix apps/web ci`, `pnpm -C …`).
INSTALLS = re.compile(
    r"\b(?:npm|yarn|pnpm)\b(?:\s+(?:--cwd|--prefix|--dir|-C)(?:=|\s+)\S+)?\s+(?:ci|install|i)\b"
    r"|\bcorepack\b|^\s*yarn\s*$"
)
# What `npm ci`, Yarn and pnpm install from; without one, an install on a fresh clone fails.
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")
# A command that begins by entering a folder: what follows runs there, and so does its install.
STARTS_IN = re.compile(r"^\s*cd\s+([\w./-]+)\s*(?:&&|;)")
# Where a monorepo keeps its packages when its package.json does not say (`workspaces`).
USUAL_WORKSPACES = ("apps/*", "packages/*")


def with_dependencies(repo: Path, commands: list[str]) -> list[str]:
    """The commands with the project's dependencies installed first, for a Node project whose
    commands do not do that themselves: on a fresh clone every tool they need is "not found". A
    command that starts in a folder (`cd apps/web && …`, a monorepo's app) is installed for in
    that folder, by that folder's lockfile; a package.json without a lockfile next to it gets
    nothing, because `npm ci` there fails every time."""
    if not commands or any(INSTALLS.search(command) for command in commands):
        return commands
    folder = STARTS_IN.match(commands[0])
    where = repo / folder.group(1) if folder else repo
    if not (where / "package.json").exists() or not has_lockfile(where):
        return commands
    install = NODE_INSTALL[package_manager(where)]
    return [f"cd {folder.group(1)} && {install}" if folder else install, *commands]


def has_lockfile(folder: Path) -> bool:
    return any((folder / name).exists() for name in LOCKFILES)


def _scripts(folder: Path) -> dict | None:
    """The scripts of a package.json, None when it has none at all (nothing said)."""
    try:
        return json.loads(_read(folder / "package.json")).get("scripts")
    except (ValueError, AttributeError):
        return None


def _workspaces(repo: Path) -> list[Path]:
    """The packages of a monorepo, each with a package.json: the globs `workspaces` names in the
    root package.json, else the usual folders."""
    try:
        named = json.loads(_read(repo / "package.json")).get("workspaces")
    except (ValueError, AttributeError):
        named = None
    if isinstance(named, dict):
        named = named.get("packages")
    globs = named if isinstance(named, list) else USUAL_WORKSPACES
    found = []
    for pattern in globs:
        found += sorted(p for p in repo.glob(str(pattern)) if (p / "package.json").is_file())
    return found


def node_candidates(repo: Path) -> list[tuple[str, str]]:
    """One per package of a monorepo with a test script, run from its folder; the root's own
    command when it has a test script, or when it says nothing and has no such packages."""
    packages = [p for p in _workspaces(repo) if "test" in (_scripts(p) or {})]
    scripts = _scripts(repo)
    found = []
    if (scripts is None and not packages) or "test" in (scripts or {}):
        found.append((NODE_VERIFY[package_manager(repo)], "package.json"))
    for package in packages:
        folder = package.relative_to(repo).as_posix()
        found.append((f"cd {folder} && {NODE_VERIFY[package_manager(package)]}", f"{folder}/package.json"))
    return found[:MAX_CI]


def package_manager(repo: Path) -> str:
    """The one the project itself names in package.json, then the one its lockfile belongs to;
    npm last. Yarn 1 is told apart: it knows --frozen-lockfile, not --immutable."""
    try:
        named = json.loads(_read(repo / "package.json")).get("packageManager", "")
    except (ValueError, AttributeError):
        named = ""
    name, _, version = str(named).partition("@")
    if name == "yarn":
        return "yarn-classic" if version.startswith("1.") else "yarn"
    if name in NODE_VERIFY:
        return name
    if (repo / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo / "yarn.lock").exists():
        return "yarn" if (repo / ".yarnrc.yml").exists() else "yarn-classic"
    return "npm"


# What a Python project's tests run with, and files that name more of what they need.
REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements/dev.txt")


PYTEST = re.compile(r"^\s*pytest\b(?![-_.\w])")


def _has_pytest(requirements) -> bool:
    return isinstance(requirements, list) and any(
        isinstance(r, str) and PYTEST.match(r) for r in requirements
    )


def pytest_source(pyproject: str) -> tuple[str, str]:
    """Where a pyproject.toml has pytest: ("group", name) for a dependency group, ("extra", name)
    for an optional dependency, ("", "") for neither, or when the file does not parse."""
    try:
        data = tomllib.loads(pyproject)
    except tomllib.TOMLDecodeError:
        return "", ""
    groups = data.get("dependency-groups") or {}
    extras = (data.get("project") or {}).get("optional-dependencies") or {}
    # dev is what `uv run` installs anyway; any other group or extra has to be asked for.
    if _has_pytest(groups.get("dev")):
        return "dev", ""
    for name, requirements in groups.items():
        if _has_pytest(requirements):
            return "group", name
    for name, requirements in extras.items():
        if _has_pytest(requirements):
            return "extra", name
    return "", ""


def python_candidates(repo: Path) -> list[tuple[str, str]]:
    """uv, in the image: the environment its lockfile pins, else a throwaway one from the
    project or its requirements, with the group or extra that has pytest. Nothing it writes
    shows in the clone (.venv ignores itself), so neither the writer's clone nor the gate's
    gains an uncommitted file."""
    pyproject = _read(repo / "pyproject.toml")
    kind, name = pytest_source(pyproject) if pyproject else ("", "")
    if (repo / "uv.lock").exists():
        with_ = {"dev": "", "group": f" --group {name}", "extra": f" --extra {name}"}.get(
            kind, " --with pytest"
        )
        return [(f"uv run --frozen{with_} pytest", "uv.lock")]
    if pyproject:
        # An extra is installed with the project; a group needs a project environment, so the
        # throwaway one gets pytest alone.
        project = f"'.[{name}]'" if kind == "extra" else ". --with pytest"
        return [(f"uv run --no-project --with-editable {project} pytest", "pyproject.toml")]
    if (repo / "requirements.txt").exists():
        named = " ".join(f"--with-requirements {name}" for name in REQUIREMENTS if (repo / name).exists())
        return [(f"uv run --no-project {named} --with pytest pytest", "requirements.txt")]
    return []


GO_VERSION = re.compile(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", re.MULTILINE)
GO_TOOLCHAIN = re.compile(r"^toolchain\s+go(\d+\.\d+(?:\.\d+)?)\s*$", re.MULTILINE)
RUST_CHANNEL = re.compile(r"""^\s*channel\s*=\s*["']([\w.+-]+)["']""", re.MULTILINE)


def tools_for(repo: Path) -> list[str]:
    """The Go or Rust a project's files ask for, as mise versions: go.mod's toolchain line, else
    its go line; the channel of rust-toolchain.toml, or the legacy rust-toolchain file, else stable."""
    found = []
    if (repo / "go.mod").exists():
        text = _read(repo / "go.mod")
        version = GO_TOOLCHAIN.search(text) or GO_VERSION.search(text)
        found.append(f"go@{version.group(1) if version else 'latest'}")
    if (repo / "Cargo.toml").exists():
        channel = RUST_CHANNEL.search(_read(repo / "rust-toolchain.toml"))
        legacy = _read(repo / "rust-toolchain").strip()
        name = channel.group(1) if channel else legacy if re.fullmatch(r"[\w.+-]+", legacy) else "stable"
        found.append(f"rust@{name}")
    return found


def prepare_suggestion(repo: Path) -> list[str]:
    """What to run once in a new task's clone before the writer: the build without its tests,
    so the writer starts on a built project and builds one module at a time. The build tool's
    own words for Maven and Gradle, the install by its lockfile for Node; nothing where the
    files say nothing, or name a tool the image does not have. The first build tool found, as
    the verification's candidates are ordered."""
    if (repo / "gradlew").exists():
        return ["bash gradlew assemble --no-daemon --console=plain"]
    if (repo / "build.gradle.kts").exists() or (repo / "build.gradle").exists():
        return ["gradle assemble --no-daemon --console=plain"]
    if (repo / "mvnw").exists():
        return ["bash mvnw -B install -DskipTests"]
    if (repo / "pom.xml").exists():
        return ["mvn -B install -DskipTests"]
    if (repo / "package.json").exists() and has_lockfile(repo):
        return [NODE_INSTALL[package_manager(repo)]]
    if (repo / "uv.lock").exists():
        return ["uv sync --frozen"]
    if (repo / "go.mod").exists():
        return ["go build ./..."]
    if (repo / "Cargo.toml").exists():
        return ["cargo test --no-run"]
    return []


def candidates(repo: Path) -> list[tuple[str, str]]:
    """Every command the project's build files and its pipeline call for, each with the file it
    comes from: notes for a new project, never its verification."""
    found = []
    if (repo / "gradlew").exists():
        # bash: the wrapper is often committed without its executable bit.
        found.append(("bash gradlew test --no-daemon --console=plain", "gradlew"))
    elif (repo / "build.gradle.kts").exists():
        found.append(("gradle test --no-daemon --console=plain", "build.gradle.kts"))
    elif (repo / "build.gradle").exists():
        found.append(("gradle test --no-daemon --console=plain", "build.gradle"))
    if (repo / "mvnw").exists():
        found.append(("bash mvnw -B verify", "mvnw"))
    elif (repo / "pom.xml").exists():
        found.append(("mvn -B verify", "pom.xml"))
    if (repo / "package.json").exists():
        found += node_candidates(repo)
    found += python_candidates(repo)
    if (repo / "go.mod").exists():
        found.append(("go test ./...", "go.mod"))
    if (repo / "Cargo.toml").exists():
        found.append(("cargo test", "Cargo.toml"))
    return found + [c for c in ci_commands(repo) if c[0] not in {command for command, _ in found}]


# Where a project says how its pipeline builds it, in the order they are read.
CI_FILES = (".gitlab-ci.yml", "Jenkinsfile", "bitbucket-pipelines.yml", "azure-pipelines.yml")
# A line of a pipeline step that is the build: it starts with a build tool. The rest (checkout,
# echo, docker, curl) is the pipeline's own business.
TOOLS = {
    "mvnw", "mvn", "gradlew", "gradle", "npm", "npx", "yarn", "pnpm", "corepack", "make",
    "uv", "pytest", "python", "python3", "go", "cargo", "dotnet",
}  # fmt: skip
# A step that ships the build is not one that checks it.
NOT_A_CHECK = re.compile(r"\b(deploy|publish|release|push|upload|sonar)\b")
YAML_STEP = re.compile(r"^(\s*)(?:-\s+)?(?:run|script|before_script):\s*(.*)$")
# sh 'cmd', sh "cmd", sh '''…''' and sh """…""" of a Jenkinsfile, one group per quoting.
SH_STEP = re.compile(
    r"\bsh\s*\(?\s*(?:'''(.*?)'''|" + '"""(.*?)"""' + r"|'([^'\n]*)'|" + r'"([^"\n]*)")', re.DOTALL
)
MAX_CI = 6
# A pipeline's own variables, so a note says what it runs: env: of a workflow, a job or a step,
# variables: of GitLab, Bitbucket or Azure. One map a file; a name set twice keeps the last.
ENV_BLOCK = re.compile(r"^(\s*)(?:env|variables):\s*$")
ENV_PAIR = re.compile(r"^\s*([A-Za-z_]\w*):\s*(.*?)\s*$")
VARIABLE = re.compile(r"\$\{\{\s*env\.(\w+)\s*\}\}|\$\{(\w+)\}|\$(\w+)")


def _yaml_env(text: str) -> dict[str, str]:
    lines, env, i = text.splitlines(), {}, 0
    while i < len(lines):
        m = ENV_BLOCK.match(lines[i])
        i += 1
        if not m:
            continue
        indent = len(m.group(1))
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            if pair := ENV_PAIR.match(lines[i]):
                env[pair.group(1)] = pair.group(2).strip("'\"")
            i += 1
    return env


def _expand(line: str, env: dict[str, str]) -> str:
    """The line with the variables the file sets filled in; the others left as they are."""
    filled = VARIABLE.sub(lambda m: env.get(next(filter(None, m.groups())), m.group(0)), line)
    return " ".join(filled.split())


def _yaml_steps(text: str) -> list[list[str]]:
    """The commands of each run:/script: step of a workflow, GitLab, Bitbucket or Azure file:
    one line, or the indented block or list below the key."""
    lines = text.splitlines()
    steps = []
    i = 0
    while i < len(lines):
        m = YAML_STEP.match(lines[i])
        i += 1
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2).strip()
        if rest and rest not in ("|", ">", "|-", ">-", "|+"):
            steps.append([rest])
            continue
        block = []
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            if lines[i].strip():
                block.append(lines[i].strip().removeprefix("- ").strip())
            i += 1
        steps.append(block)
    return steps


def _build_command(step: list[str]) -> str:
    """The build lines of a step joined as the gate runs them, or "" when the step is no build."""
    kept = []
    for line in step:
        line = line.strip().strip("'\"")
        words = line[2:].split() if line.startswith("./") else line.split()
        if not words or words[0] not in TOOLS or "${{" in line or NOT_A_CHECK.search(line):
            continue
        if line.startswith("./") and words[0] in ("mvnw", "gradlew"):
            line = "bash " + line[2:]
        kept.append(line)
    return " && ".join(kept)


def ci_commands(repo: Path) -> list[tuple[str, str]]:
    """What the project's pipeline runs to build and test it, with the file each comes from: the
    one place the project says how it is built, by people who know it. Read by lines, so an
    unusual file yields nothing rather than a wrong command."""
    workflows = repo / ".github" / "workflows"
    sources = sorted(workflows.glob("*.y*ml")) if workflows.is_dir() else []
    sources += [repo / name for name in CI_FILES if (repo / name).is_file()]
    found: list[tuple[str, str]] = []
    for path in sources:
        text = _read(path)
        if path.name == "Jenkinsfile":
            steps = [next(filter(None, groups)).splitlines() for groups in SH_STEP.findall(text)]
        else:
            env = _yaml_env(text)
            steps = [[_expand(line, env) for line in step] for step in _yaml_steps(text)]
        for step in steps:
            command = _build_command(step)
            if command and command not in {c for c, _ in found}:
                found.append((command, str(path.relative_to(repo))))
    return found[:MAX_CI]


def maven_notes(repo: Path) -> list[str]:
    """What Maven adds to every run by itself, from .mvn, where nobody looks while typing the
    command: its own -f or -pl, and the build cache."""
    notes = []
    if config := _read(repo / ".mvn" / "maven.config").split():
        notes.append(f".mvn/maven.config adds to every mvn run: {' '.join(config)}")
    if "maven-build-cache-extension" in _read(repo / ".mvn" / "extensions.xml"):
        notes.append(".mvn/extensions.xml turns the Maven Build Cache on")
    return notes


def detect(repo: Path) -> Detected:
    found = Detected(project_name(repo), repo, [])
    found.demo = detect_demo(repo)
    found.prepare = prepare_suggestion(repo)
    found.tools = tools_for(repo)
    found.notes += [f"{tool}: not in the image; the pod installs it with mise" for tool in found.tools]
    level = source_level(repo)
    newest = IMAGE_JAVA
    # What the build files name is a note, never the verification: a pipeline often builds with
    # more than they say (a profile, -f pom.xml), and a guess that builds the wrong thing passes.
    # An empty verification has the next task's writer propose the command it ran.
    if options := candidates(repo):
        found.source = options[0][1]
    if found.source == "gradlew":
        if version := gradle_version(repo):
            newest = newest_jdk_for_gradle(version)
            if newest < IMAGE_JAVA:
                found.notes.append(f"Gradle {version[0]}.{version[1]} does not run on Java {IMAGE_JAVA}.")
    elif found.source.startswith("build.gradle"):
        found.notes.append("No Gradle wrapper: the image's Gradle is used.")
    for command, source in options:
        found.notes.append(f"{source} runs: {command}")
    found.notes += maven_notes(repo)
    if level and level > newest:
        found.notes.append(f"The code targets Java {level}, newer than the build tool supports.")
    # The newest LTS that the build tool runs on and that compiles the code's level.
    jdk = next((v for v in LTS if v <= newest and (level is None or v >= level)), newest)
    if jdk != IMAGE_JAVA:
        found.java = str(jdk)
        found.notes.append(f"Java {jdk}" + (f" for code written for Java {level}." if level else "."))
    return found


def render(found: Detected) -> str:
    verify = "false" if found.no_build else "[" + ", ".join(f'"{c}"' for c in found.verify) + "]"
    home = Path.home()
    repo = f"~/{found.repo.relative_to(home)}" if found.repo.is_relative_to(home) else str(found.repo)
    demo = ", ".join(f'"{c}"' for c in found.demo)
    prepare = ", ".join(f'"{c}"' for c in found.prepare)
    tools = ", ".join(f'"{t}"' for t in found.tools)
    return (
        f'repo = "{repo}"\nverify = {verify}\nprepare = [{prepare}]\ndemo = [{demo}]\n'
        f'java = "{found.java}"\n'
        + (f"tools = [{tools}]\n" if found.tools else "")
        + "risky_extra = []\nhost_services = []\npass_env = []\n"
    )
