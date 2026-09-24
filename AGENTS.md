# Working on vivibox

- Before changing anything a person sees (`vivibox/tui.py`, `vivibox/panel.py`, `vivibox/dialogs.py`,
  `vivibox/ui.py`, `vivibox/cli.py`, notification texts, the README), read `docs/ux-guidelines.md`.
  `tests/test_ux_rules.py` enforces its mechanical rules.
- Before changing anything an agent reads (the prompts in `vivibox/supervisor.py`, `vivibox/manual.py`
  and `vivibox/demo.py`, `vivibox/templates/`, the gate's feedback in `vivibox/gate.py`), read
  `docs/prompt-guidelines.md`. `tests/test_prompt_rules.py` enforces its mechanical rules.
- Every change in behaviour gets a test that fails first for its own assertion.
- `vivibox/actions.py` is the facade the view and the command line call; what it does lives in
  `roles.py`, `projects.py`, `demo.py`, `box.py` and `review.py`, which reach the task primitives
  through `actions`. A module past 800 lines is split before a feature is added to it.
- Before committing: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.
  Run `uv run pytest -m docker` only when the change touches the pod.
- A change to a prompt that no mechanical test covers gets a behavioural run:
  `uv run pytest -m model tests/behavioural -x`. It runs agents on real models for money (a
  limit of USD 2 per run, `VIVIBOX_BEHAVIOURAL_LIMIT`), so it is run only when the person asked
  for that run; the commit names its date, and the planning notes keep its cost and outcome.
- The repository is public: nothing from an employer or a private project goes into it.
- Code, comments, messages and documents are in English.
