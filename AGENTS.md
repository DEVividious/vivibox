# Working on vivibox

- Before changing anything a person sees (the view: `vivibox/tui.py`, `vivibox/table.py`,
  `vivibox/keys_*.py`, `vivibox/panel.py`, `vivibox/markdown.py`, `vivibox/blocks.py`, `vivibox/ui.py`; the dialogs: `vivibox/dialogs.py`, `vivibox/newtask.py`,
  `vivibox/browse.py`, `vivibox/providers_ui.py`, `vivibox/settings.py`, `vivibox/logs.py`,
  `vivibox/widgets.py`, `vivibox/branches.py`, `vivibox/usage_view.py`; `vivibox/cli.py`
  and the commands apart from it in `vivibox/cli_*.py`, notification texts, the README), read `docs/ux-guidelines.md`.
  `tests/test_ux_rules.py` enforces its mechanical rules.
- Before changing anything an agent reads (the prompts in `vivibox/prompts.py`, `vivibox/manual.py`
  and `vivibox/demo.py`, `vivibox/templates/`, the gate's feedback in `vivibox/feedback.py`, the skill in
  `vivibox/skills/`), read
  `docs/prompt-guidelines.md`. `tests/test_prompt_rules.py` enforces its mechanical rules.
- Every change in behaviour gets a test that fails first for its own assertion.
- `vivibox/actions.py` is the facade the view and the command line call; what it does lives in
  `roles.py`, `projects.py`, `demo.py`, `box.py`, `review.py` and `window.py` (the agent's
  window under `w`), which reach the task primitives
  through `actions`. The view is the app in `tui.py` with its keys mixed in by group from `keys_*.py`
  and the list from `table.py`. The supervisor in `supervisor.py` drives a task through its states
  in the way `orchestration.py` says the task's mode shares them between the roles,
  on a tool that keeps the `harness.Harness` contract, through what `supervisor.Ports` hands it;
  `supervise.py` wires both for the command line. A module past 800 lines is split before a
  feature is added to it (`tests/test_structure.py` fails on one).
- A change under `vivibox/` gets a line under `## [Unreleased]` in `CHANGELOG.md` in the same
  commit (Keep a Changelog: Added, Changed, Fixed; a change a person cannot notice goes under
  `### Internal`). `tests/test_structure.py` fails on a branch that changes `vivibox/` and not
  `CHANGELOG.md`. The version is git's, never a file's: a tag `vX.Y.Z` is a release, every
  commit past it is `X.Y.(Z+1).devN+g<commit>`, and `vivibox --version` names it, so a bug
  report names the commit. A release is the `Unreleased` section given a number and a date, a
  commit, and an annotated tag pushed by the person.
- Tests and checks follow the testing policy below. The commands: `uv run pytest -n auto` (the
  unit suite, in parallel, a tenth of the time), `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run pytest -m docker` (the live suite: pods on Docker and
  Sysbox).
- A change to a prompt that no mechanical test covers gets a behavioural run:
  `uv run pytest -m model tests/behavioural -x`, or `-n auto` for its scenarios in parallel (the
  spending limit is the whole run's either way). It runs agents on real models for money (a
  limit of USD 2 per run, `VIVIBOX_BEHAVIOURAL_LIMIT`), so it is run only when the person asked
  for that run; the commit names its date, and the planning notes keep its cost and outcome.
- The repository is public: nothing from an employer or a private project goes into it.
- Code, comments, messages and documents are in English.

## Testing policy

Use the smallest test scope that gives meaningful confidence for the change.

Before each commit:
- run targeted tests for the code changed;
- run tests of direct consumers/importers when code is moved or interfaces change;
- run lint/format checks relevant to the changed files;
- do not run the entire unit or live suite mechanically before every commit.

Before completing an increment/branch:
- run the full unit suite;
- run ruff / formatting / static checks;
- run targeted live tests when the increment changes Docker, host integration, networking,
  mounts, resources, or another runtime boundary.

After review fixes:
- run targeted tests for the fix;
- rerun the full unit suite only when the fix has broad blast radius or the reviewer identifies
  a concrete reason.

Full live/Docker suite:
- run at major integration checkpoints, before final merge of a multi-increment stack, and at
  release gates;
- do not run it repeatedly for unrelated UI/docs/unit-only changes.

Stress/repetition:
- use repeated/stress runs only to investigate a concrete nondeterministic failure or when a
  release criterion explicitly requires them;
- do not use long probabilistic loops as routine validation.

Concurrency:
- only one actor should own host-level/live test execution at a time;
- do not run cleanup commands or competing live suites that can interfere with another test
  run.

A failing targeted or full test must still be investigated; reduced test scope is not permission
to ignore failures.
