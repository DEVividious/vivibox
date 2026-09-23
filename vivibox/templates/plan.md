+++
# feature: new behaviour. bug: something works wrong. other: refactoring, tests, upkeep.
kind = "{{kind}}"
# One line naming what this task does, for your task list; the agent fills it in when it plans.
summary = ""
# How the gate builds and tests this project, when the project has no command yet (a new project);
# false when there is nothing to build or test. A CI definition (.github/workflows, .gitlab-ci.yml,
# Jenkinsfile) says how the project is built, when there is one.
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

## Context

## Approach

## Acceptance criteria

- [ ] Every test added was seen failing on its own assertion, not on a missing module, before
      the change that makes it pass, with the compared values recorded in red.md
- [ ] Replace with an observable outcome you can check

## Out of scope
