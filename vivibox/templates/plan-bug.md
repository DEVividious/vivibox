+++
# feature: new behaviour. bug: something works wrong. other: refactoring, tests, upkeep.
kind = "{{kind}}"
# One line naming what this task does, for your task list; the agent fills it in when it plans.
summary = ""
# false when this task has nothing to build or test, set when it is made. How the project is
# built is not the plan's to say: the writer proposes it.
verify = []
+++

# Goal

{{goal}}

## Reproduction

<!-- How the bug shows, and the test that fails because of it. Write that test first, before any fix. -->

## Root cause

<!-- Why it happens, found in the code, not guessed from the symptom. -->

## Approach

## Acceptance criteria

- [ ] A test reproduces the bug: it fails without the fix and passes with it, recorded in
      /task/handoff/red.md
- [ ] Every other test added was seen failing on its own assertion too, not on a missing
      module, with the compared values recorded the same way
- [ ] Replace with an observable outcome you can check

## Out of scope
