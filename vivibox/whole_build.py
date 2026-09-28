"""Whether the command a writer proposes is a whole build: not a selection of tests nor of one
part of the project, and without the build tool's debug output. The gate refuses one that is not,
and names what is wrong."""

from __future__ import annotations

import re

# What picks some tests out of a build: a test class or pattern given to Maven, Gradle, pytest,
# Jest or Vitest, Go, or a test file named as the argument. A whole build names none of these.
SELECTS_TESTS = re.compile(
    r"(?<![\w-])(?:-Dtest=\S+|-Dit\.test=\S+|--tests(?:=|\s+)\S+|-k\s+\S+|-t\s+\S+"
    r"|--testNamePattern(?:=|\s+)\S+|--testPathPattern(?:=|\s+)\S+|-run\s+\S+"
    r"|\S*\.(?:test|spec)\.[cm]?[jt]sx?\b|\S*(?:/|^)test_\w+\.py\b|\S*_test\.py\b"
    r"|unittest\s+(?:-v\s+)?\S*test_\w+)"
)


# Maven's and Gradle's debug output: megabytes of log, where the writer and you look for the
# failure. A pipeline turns it on to debug the pipeline (jsoup's runs mvn -X); a proposal copies it.
BUILD_TOOL = re.compile(r"(?<![\w-])(?:mvnw?|gradlew?)(?![\w-])")
DEBUG_OUTPUT = re.compile(r"(?<![\w-])(?:-X|--debug)(?![\w=-])")


# What picks one part of the project, per build tool: a Maven module or its pom, a Gradle
# project's task, a Go package, a Cargo package, a workspace. The writer of the first task
# proposes the command every task is verified with, and its part is the one that task was about.
# {modules} is not a part: the plan of each task names its own (ADR-0033). A directory the command
# changes into is not one either: in a repository of separate apps it is the app's whole build.
SELECTS_PART = [
    (
        re.compile(r"(?<![\w-])mvnw?(?![\w-])"),
        re.compile(
            r"(?<![\w-])(?:(?:-pl|--projects)(?:=|\s+)(?!\S*\{modules\})\S+|(?:-f|--file)(?:=|\s+)\S+/pom\.xml)"
        ),
    ),
    (
        re.compile(r"(?<![\w-])gradlew?(?![\w-])"),
        re.compile(r"(?<!\S)(?::[\w.-]+)+:\w+(?!\S)|(?<![\w-])-p\s+\S+"),
    ),
    (
        re.compile(r"(?<![\w-])go\s+test(?![\w-])"),
        re.compile(r"(?<!\S)\./(?!\.\.\.)[\w.-]+(?:/[\w.-]+)*(?:/\.\.\.)?"),
    ),
    (re.compile(r"(?<![\w-])cargo(?![\w-])"), re.compile(r"(?<![\w-])(?:-p|--package)(?:=|\s+)\S+")),
    (
        re.compile(r"(?<![\w-])(?:npm|pnpm|yarn)(?![\w-])"),
        re.compile(r"(?<![\w-])(?:--workspace(?:=|\s+)\S+|--filter(?:=|\s+)\S+|workspace\s+\S+)"),
    ),
]


def narrowed_proposal(command: str) -> str:
    """What is wrong with a proposed command, as its part that says so: the selection of tests
    or of one part of the project (a writer verified by a part alone would pass whatever it broke
    elsewhere), or the build tool's debug output; "" for a whole build that reads."""
    if found := SELECTS_TESTS.search(command):
        return found.group(0)
    for tool, part in SELECTS_PART:
        if tool.search(command) and (found := part.search(command)):
            return found.group(0)
    found = DEBUG_OUTPUT.search(command) if BUILD_TOOL.search(command) else None
    return found.group(0) if found else ""


def proposal_fault(part: str) -> str:
    """How the part narrowed_proposal found is named to you: a selection, or debug output."""
    return f"with debug output on ({part})" if DEBUG_OUTPUT.fullmatch(part) else f"narrowed to {part}"
