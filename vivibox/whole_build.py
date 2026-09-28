"""Whether the command a writer proposes is a whole build: not a selection of tests, and without
the build tool's debug output. The gate refuses one that is not, and names what is wrong."""

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


def narrowed_proposal(command: str) -> str:
    """What is wrong with a proposed command, as its part that says so: the selection of tests
    (a writer verified by the tests it wrote alone would pass whatever it broke elsewhere), or
    the build tool's debug output; "" for a whole build that reads."""
    if found := SELECTS_TESTS.search(command):
        return found.group(0)
    found = DEBUG_OUTPUT.search(command) if BUILD_TOOL.search(command) else None
    return found.group(0) if found else ""


def proposal_fault(part: str) -> str:
    """How the part narrowed_proposal found is named to you: a selection, or debug output."""
    return f"with debug output on ({part})" if DEBUG_OUTPUT.fullmatch(part) else f"narrowed to {part}"
