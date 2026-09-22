# Working on vivibox

- Before changing anything a person sees (`vivibox/tui.py`, `vivibox/ui.py`, `vivibox/cli.py`,
  notification texts, the README), read `docs/ux-guidelines.md`. `tests/test_ux_rules.py` enforces
  its mechanical rules.
- Before changing anything an agent reads (the prompts in `vivibox/supervisor.py`, `vivibox/manual.py`
  and `vivibox/actions.py`, `vivibox/templates/`, the gate's feedback in `vivibox/gate.py`), read
  `docs/prompt-guidelines.md`. `tests/test_prompt_rules.py` enforces its mechanical rules.
- Every change in behaviour gets a test that fails first for its own assertion.
- Before committing: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.
  Run `uv run pytest -m docker` only when the change touches the pod.
- The repository is public: nothing from an employer or a private project goes into it.
- Code, comments, messages and documents are in English.
