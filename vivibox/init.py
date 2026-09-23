"""vivibox init: sets up the repository you are in as a project, from what its build files tell.

Nothing happens behind your back: init shows the project file it would write and asks first.
"""

from __future__ import annotations

import json
import re
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


def candidates(repo: Path) -> list[tuple[str, str]]:
    """Every command the project's build files call for, each with the file it comes from, the
    one detect() picks first. What a picker offers; detection takes the first."""
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
        found.append((NODE_VERIFY[package_manager(repo)], "package.json"))
    return found


def detect(repo: Path) -> Detected:
    found = Detected(project_name(repo), repo, [])
    found.demo = detect_demo(repo)
    level = source_level(repo)
    newest = IMAGE_JAVA
    if options := candidates(repo):
        found.verify, found.source = [options[0][0]], options[0][1]
    if found.source == "gradlew":
        if version := gradle_version(repo):
            newest = newest_jdk_for_gradle(version)
            if newest < IMAGE_JAVA:
                found.notes.append(f"Gradle {version[0]}.{version[1]} does not run on Java {IMAGE_JAVA}.")
    elif found.source.startswith("build.gradle"):
        found.notes.append("No Gradle wrapper: the image's Gradle is used.")
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
    return (
        f'repo = "{repo}"\nverify = {verify}\ndemo = [{demo}]\njava = "{found.java}"\n'
        "risky_extra = []\nhost_services = []\npass_env = []\n"
    )
