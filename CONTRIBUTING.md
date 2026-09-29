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
git clone git@github.com:DEVividious/vivibox.git
cd vivibox
uv sync                      # Python 3.12, the dependencies, vivibox in editable mode
uv run pytest -n auto        # the unit tests, in parallel: under a minute
uv run ruff check . && uv run ruff format --check .
```

Tests that start containers need Docker and Sysbox: `uv run pytest -m docker`. Tests that run
agents on real models cost money and are run only on purpose: `uv run pytest -m model
tests/behavioural -x` (`-n auto` runs them in parallel, under one spending limit).

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

### Documentation checks

`uv run python docs/check_links.py --external` validates README links, local heading anchors,
images, and links in every Markdown document the README points to. External checks follow
redirects without credentials. Review the rendered README as well, including its flow table
and animation at a normal GitHub desktop width.

### README visuals

The hero is a 27-second GIF of the **real Textual UI at 100×32**, driven by Textual's Pilot.
The fixtures simulate task progress, model usage and reviews in disposable repositories; no
pods or model APIs run, and no real configuration or keys are read. The seven scenes show the
dashboard, role models, plan approval, verification, independent review, a correction round and
the final human checkpoint. The demo ends before acceptance: the result still belongs to you
to inspect.

Rebuild the GIF and the static SVG with optional recording tools (not application dependencies):

```bash
uv run --with playwright==1.63.0 playwright install chromium
uv run --with pillow==12.3.0 --with playwright==1.63.0 python docs/img/screenshot.py --gif
```

The renderer uses Chromium via Playwright, DejaVu Sans / DejaVu Sans Mono fonts and Pillow.
Install the fonts with `sudo apt install fonts-dejavu-core` on Ubuntu if missing. For an existing
Chrome/Chromium, set `VIVIBOX_DEMO_BROWSER` to its executable and skip the browser download.
Network requests are blocked while rendering. `VIVIBOX_DEMO_FRAMES=/tmp/vivibox-demo` also saves
individual PNG scenes for inspection. Without `--gif`, only `docs/img/view.svg` is rebuilt.

The GIF is 1000 pixels wide, uses one 128-colour palette, has 200 ms transitions and holds each
scene for 3–5 seconds. It loops without a blank frame. Check every scene and the loop at normal
README width after regenerating; the linked static SVG is the alternative for readers who do
not want animation (GIF does not honour reduced-motion preferences). The README explains the
same workflow in text.

## Releases

A release is a tag: the `Unreleased` section gets a version and a date, the commit is tagged
`vX.Y.Z`, the tag is pushed, and a GitHub release carries that section. There is no schedule; a
release comes when `main` has something new for you and it was tried on a real project.
