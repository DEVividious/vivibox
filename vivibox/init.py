"""vivibox init: sets up the repository you are in as a project, from what its build files tell.

Nothing happens behind your back: init shows the project file it would write and asks first.
"""

from __future__ import annotations

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
    java: str = ""
    notes: list[str] = field(default_factory=list)


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


def detect(repo: Path) -> Detected:
    found = Detected(project_name(repo), repo, [])
    level = source_level(repo)
    newest = IMAGE_JAVA
    if (repo / "gradlew").exists():
        # bash: the wrapper is often committed without its executable bit.
        found.verify = ["bash gradlew test --no-daemon --console=plain"]
        if version := gradle_version(repo):
            newest = newest_jdk_for_gradle(version)
            if newest < IMAGE_JAVA:
                found.notes.append(f"Gradle {version[0]}.{version[1]} does not run on Java {IMAGE_JAVA}.")
    elif (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        found.verify = ["gradle test --no-daemon --console=plain"]
        found.notes.append("No Gradle wrapper: the image's Gradle is used.")
    elif (repo / "mvnw").exists():
        found.verify = ["bash mvnw -B verify"]
    elif (repo / "pom.xml").exists():
        found.verify = ["mvn -B verify"]
    elif (repo / "package.json").exists():
        found.verify = ["npm ci && npm test"]
    if level and level > newest:
        found.notes.append(f"The code targets Java {level}, newer than the build tool supports.")
    # The newest LTS that the build tool runs on and that compiles the code's level.
    jdk = next((v for v in LTS if v <= newest and (level is None or v >= level)), newest)
    if jdk != IMAGE_JAVA:
        found.java = str(jdk)
        found.notes.append(f"Java {jdk}" + (f" for code written for Java {level}." if level else "."))
    return found


def render(found: Detected) -> str:
    verify = ", ".join(f'"{c}"' for c in found.verify)
    home = Path.home()
    repo = f"~/{found.repo.relative_to(home)}" if found.repo.is_relative_to(home) else str(found.repo)
    return (
        f'repo = "{repo}"\nverify = [{verify}]\njava = "{found.java}"\nrisky_extra = []\nhost_services = []\n'
    )
