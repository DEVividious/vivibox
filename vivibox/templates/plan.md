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

## Context

## Approach

## Acceptance criteria

- [ ] Every test added was seen failing on its own assertion, not on a missing module, before
      the change that makes it pass, with the compared values recorded in red.md
- [ ] Replace with an observable outcome you can check

## Out of scope
