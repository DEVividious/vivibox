# Contributing

Thank you for looking. vivibox is one person's tool, in the open; issues and pull requests are
welcome, and small ones land fastest.

## Reporting a problem

Open an issue from the bug template. It starts with `vivibox --version`: the version comes from
git, so it names the exact commit your build came from. Add the log the view shows under `l`
(the timeline, or the verification log) and the steps that got you there. Anything about a
vulnerability goes through [SECURITY.md](SECURITY.md), not an issue.

vivibox runs on Linux with Sysbox. macOS and Windows are not supported, and there is no plan to
change that: the second line of isolation needs a Docker daemon of the pod's own
([docs/security.md](docs/security.md)).

## Working on the code

```bash
git clone https://github.com/DEVividious/vivibox
cd vivibox
uv sync                      # Python 3.12, the dependencies, vivibox in editable mode
uv run pytest                # the unit tests, a few minutes
uv run ruff check . && uv run ruff format --check .
```

Tests that start containers need Docker and Sysbox: `uv run pytest -m docker`. Tests that run
agents on real models cost money and are run only on purpose: `uv run pytest -m model
tests/behavioural -x`.

The rules the code follows are in [AGENTS.md](AGENTS.md); the ones that can be checked are
checked by tests. In short:

- Every change in behaviour gets a test that fails first for its own assertion.
- A change a person can see follows [docs/ux-guidelines.md](docs/ux-guidelines.md); a change to
  what an agent reads follows [docs/prompt-guidelines.md](docs/prompt-guidelines.md).
- A change under `vivibox/` gets a line under `Unreleased` in [CHANGELOG.md](CHANGELOG.md) in
  the same commit; one a person cannot notice goes under `Internal`.
- A module past 800 lines is split before a feature is added to it.
- A commit's subject is in English and at most 72 characters; the body, if any, is a short list.

Open the pull request against `main`. CI runs the linters, the tests and a build, and checks
the changelog line.

### README visuals

`uv run python docs/img/screenshot.py` rebuilds the animated tour and still view from fictional
projects, without starting pods or using model APIs. The tour uses the real interface, with
captions and transitions composed by `docs/img/animation.py`. Check it at README width and with
reduced motion enabled after regenerating it.

## Releases

A release is a tag: the `Unreleased` section gets a version and a date, the commit is tagged
`vX.Y.Z`, the tag is pushed, and a GitHub release carries that section. There is no schedule; a
release comes when `main` has something new for you and it was tried on a real project.
