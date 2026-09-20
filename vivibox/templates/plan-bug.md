+++
# feature: new behaviour. bug: something works wrong. other: refactoring, tests, upkeep.
kind = "{{kind}}"
# One line naming what this task does, for your task list; the agent fills it in when it plans.
summary = ""
# How the gate builds and tests this project, when the project has no command yet (a new project).
verify = []
# code-only: code, unit and integration tests, Testcontainers.
# full-system: also needs the local system described by the skills below.
mode = "code-only"
requires_skills = []
# supervised: stop at checkpoints. loop: writer and reviewer iterate until done.
collab = "supervised"
+++

# Goal

{{goal}}

## Reproduction

<!-- How the bug shows, and the test that fails because of it. Write that test first, before any fix. -->

## Root cause

<!-- Why it happens, found in the code, not guessed from the symptom. -->

## Approach

## Acceptance criteria

- [ ] A test reproduces the bug: it fails without the fix and passes with it, recorded in red.md
- [ ] Every other test added was seen failing on its own assertion too, not on a missing
      module, with the compared values recorded the same way
- [ ] Replace with an observable outcome you can check

## Out of scope
