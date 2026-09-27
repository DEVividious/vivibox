# Changelog

Notable changes to vivibox. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/) with a leading zero: the command
line, the configuration files and what `host/setup.sh` changes on your machine may still change
between minor versions. The version itself comes from git: a tag `vX.Y.Z` names a release, and
every commit past it is `X.Y.(Z+1).devN+g<commit>`, which `vivibox --version` prints.

## [Unreleased]

First public version.

### Added

- The agent runs in a pod: an unprivileged container with its own Docker daemon under Sysbox, cut
  off from the host, the LAN and the host's Docker socket. Scratch space in a pod is capped at 4 GB.
- A task goes plan, accept, implement, gate, review, accept. The agent works on a clone and its
  work comes back as a review copy; files that run code on IDE import, `AGENTS.md` among them,
  need approval. The repository's `AGENTS.md` is read as instruction.
- The gate builds and tests the commits on a fresh clone with a build cache of its own, and checks
  the acceptance criteria, the commit messages, switched-off tests and the agent's `red.md`. A
  criterion that is only the build passing is refused.
- Orchestration modes (`agent_orchestration_mode`, in `config.toml`, under `k` and per task under
  `n`): `single_agent` (P+W+R → Gate), `planner_executor` (P → W+R → Gate),
  `planner_maker_checker` (P → W → Gate → R ⇄ W, the default) and `supervisor_worker`
  (P → W → Gate → (P+R) ⇄ W). Roles joined in one agent share one conversation and the first
  role's model; a writer that reviews its own work gets a self-review turn before every
  verification; a supervisor, the planner, reviews the worker's work after a green
  verification in the pod, with the plan still in its conversation.
  One limit, `limits.max_rounds` (3), counts the fix turns the writer gets, from the
  verification or from a review, before the work comes to you; your reply gives them back.
  `max_iterations`, `max_reviews` and the reviewer's `mode` are read no more, and a file that
  still has them is told once. Without `[roles.reviewer]` the default mode reviews on the
  writer's model.
- A reviewer on a model of its own reads the work after a green gate; blocking notes go back to
  the writer by themselves. The reviewer reads the writer's reply in a later round. Its cost
  stands in a column of its own. What is reviewed, and what comes to you, is the commit the
  verification ran on: commits that changed since go through it again first.
- A project without a verification command asks the writer for one, and the proposal waits for
  your review before the first verification (`review the command`); a proposal narrowed to some
  tests is refused. The writer is told to allow its own whole build the verification's time
  limit, so its tool's default timeout does not read as a failure.
- A box: a pod of your own in a project, with opencode, to work in by hand (`b`).
- Projects prepare once in a new task's clone (`prepare`), asked for under `i` and by `vivibox
  init` with the build files' suggestion of a build without tests; Node projects get their dependencies
  installed by their lockfile, in the folder a command starts in; Maven keeps what a task
  installs in a volume of its own; Testcontainers images and the host's certificate authorities
  reach the pod.
- Providers, models and MCP servers from an imported `opencode.json`; keys in vivibox's own
  store; a retired model is not offered, and a start on one says so.
- The view: tasks waiting for you first, projects by name, a task's row kept in place; `w`
  watches or talks to the agent, or the verification as it runs, asking which when there is a choice and saying when the verification is over, from inside tmux too; `l`
  reads the timeline, a verification log or the supervisor's; `f` pages the diff; `S` stops by
  force; `h` and `H` show accepted and deleted tasks; the header counts what waits and works,
  today's cost, and says in red when Docker is down or a provider has no key; `?` names every key and
  the build.
- Attachments: `@path` in a task's description copies a file or folder for the agent; a path
  in the repository is a reference. Secrets folders are refused.
- Notifications on your phone through ntfy; a cost warning and a cost limit per task;
  `vivibox stats`, `vivibox timeline`, `vivibox status`. The timeline records what a task started
  with (roles, review, limits, verification, preparation, base, the build of vivibox) and what
  `m` or `e` changed while it ran.
- `host/setup.sh` and `host/uninstall.sh`; Docker's default networks moved off ranges a VPN
  uses; the pod's TCP MSS clamped to the uplink's MTU.
- The commit suggested at acceptance tells the whole of the task's work, its subject from the
  task and every commit of the agent as a line.
- A finished task keeps its timeline, its verification logs, its reviews and the supervisor's
  log with its plan, and `l` on its row reads them.
- Each role's conversation is kept turn by turn in `log/planner.log`, `log/writer.log` and
  `log/reviewer.log`: the prompt in short, what the agent said, the tools it called with their
  arguments cut short, the cost and the time. `l` lists them after the timeline, with their
  turns and cost, on a live task and from a finished one's archive.

- `init` suggests how a Python project is tested and prepared: `uv run --frozen pytest` and
  `uv sync --frozen` with a `uv.lock`, else `pytest` in a throwaway environment from
  `pyproject.toml` or the requirements files, with the dependency group or extra that has
  pytest. uv's cache is shared between tasks and the gate.

- Go and Rust projects: `init` writes the toolchain their files ask for to the project file
  (`tools = ["go@1.25.3"]`, `rust@stable`), which the pod installs with mise for the agent and
  the gate, and suggests `go test ./...` or `cargo test` and a build to prepare with. Go's and
  Cargo's downloads and builds, and Rust itself, are kept in caches shared between tasks.
- `u` shows how long each task took, one a row: its planner's, writer's and reviewer's turns,
  its verifications and the whole task; `vivibox usage [--json]` prints the same. A
  verification's event says how many seconds it took.
- `u` and `vivibox usage` show what each live task's pod uses now: CPU and memory of its
  containers and the disk of its volumes and folder, measured in a thread only while `u` is
  open; `--json` has them per container and per volume.
- Accepted work is committed on the branch the task started on or on a new one named after the
  task (`feature/<title>`, `bugfix/<title>`, or `<title>` for other work, with a number when the
  name is taken); the commit dialog asks which, a new branch first when the task started on
  `main`, and the checkout stays on the branch committed on. `vivibox accept` asks the same.

### Fixed

- The pod has a C compiler (gcc): Rust's build scripts failed with "linker `cc` not found", and a
  Python package built from source had none either.
- `pip install` in a pod outside a virtual environment is refused: it installed into mise's
  Python, shared by every task and the gate, and put one task's clone on the others' import path.
- A Python environment in the clone (`uv sync`'s `.venv`, `.tox`) no longer stops a task for risky
  files: it is skipped while git tracks nothing in it.
- Python in a pod writes no `__pycache__` folders, which stopped the gate as uncommitted files.
- A task's row says what sent the writer on its fix turn (`implementing (round 1/3: 2 criteria
  not met)`), and the supervisor's window logs every change of state with its reason.
- A commit whose message is about the task's files ("Record test red evidence", "Tick criteria")
  is refused by the gate, and the commit `a` proposes leaves such lines out of its body.
- Task columns follow the current terminal width immediately after a resize.
- Settings name what a row is for: "Manual review" and "IDE / text editor (o)" (in the footer
  and `?` too), a "Notifications" group, and "verification gate timeout" shown in minutes and
  edited as `30m` or `1800`.
- New tasks require your Git name and email before opening the form, with commands to set them.
- Esc on an open list in a dialog closes the list; it closed the whole dialog, and a new task's
  description went with it.
- The plan's repair turn, and the review's, went on in a second session briefed as the role
  again, doing the first turn's work over: they continue the role's own conversation.

### Internal

- The gate's feedback text moves to `vivibox/feedback.py`.
- The agent's window (tmux, `w`) lives in `window.py`, reached through `actions`.
- `vivibox auth` and `vivibox models` live in `cli_providers.py`, under the size limit.
- `CHANGELOG.md` is written as the work is done: a change under `vivibox/` changes it in the
  same commit, and `tests/test_structure.py` fails on a branch that does not.
