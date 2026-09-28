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
- The details panel (`d`) has one layout in every state: the header, a line with the project,
  the times and the cost so far, `Next:`, what the state asks of you, then the newest review,
  the acceptance criteria, a Roles list (a line per agent: what it runs on, its turns and its cost, or "not yet") and
  the plan. The review stays in the panel while the writer fixes its notes. Headings of a
  review or a plan no longer stand larger than the panel's own, and the accepted plan leaves its
  criteria to their section, where they are ticked.
- `vivibox new --flow <mode>` picks the task's orchestration mode, as the Flow row in `n`.
- A Docker Hub mirror, off until `hub_mirror = true` under `[network]` in `config.toml`: a
  pull-through cache in one container on your Docker, `vivibox-mirror`, that every pod's daemon
  pulls through, so an image one task pulled is not downloaded again for the next. Each pod
  keeps its own daemon and images; the mirror listens only where pods reach the host, takes no
  pushes, holds no credentials, and forgets a layer nobody pulled for two weeks. When it is
  down, a pod pulls from Docker Hub itself. `vivibox mirror` says what it holds, `vivibox
  mirror remove` removes it.

### Changed

- The view has one look: a theme of its own (`vivibox/look.py`) with neutral surfaces and one
  accent for what takes your keys. Focus, selection, status and the primary button are drawn
  apart: the focused field's label and band in the accent, the row under the cursor a lighter
  surface that keeps its colours, a status a mark and a word (● waits for you, ✕ failed, ‖
  stopped, ✓ done, a spinner at work), the primary button tinted and solid once focused.
- Every dialog is framed the same: its title in the top edge, its keys in the bottom one
  (`ctrl+s create  esc cancel`), buttons one line high. The pickers (logs, models, editors,
  sessions, roles) are one list dialog; `i`, the folder browser, Add a provider, Providers &
  MCP, the import of an opencode.json and Manage are forms like `n`, their helper buttons (New
  folder…, Import opencode.json…, Manage…, Remove…) in the row they act on, never among the
  closing ones, `ctrl+s` for the import and for Manage's Save; `k` and a project's `e` align
  their rows under upper-case sections, mute what is only shown and mark with › what opens a
  screen.
- The list: the project's name muted in a task's id, times as `now`, `12m`, `3h`, `2d`, numbers
  on the right, a dot for nothing, finished tasks muted; a medium terminal shows the cost as
  one figure (COST); DEMO is APP. Above it a bar says what waits and what works in their
  colours; the footer groups decisions, the row's keys and the keys that work anywhere.
- Words: the modes are "One agent", "Planner, then writer", "Planner, writer, reviewer" and
  "Planner supervises writer", their steps in words (`plan → write → verify → review ⇄ write`);
  the form calls the mode Flow; Build is a list of two answers; settings say "cost limit",
  "verification timeout", "tasks folder", "Review copy".

- Flow under `n`: the modes are Single agent, Planner → Executor, Planner → Writer → Reviewer
  and Supervisor ⇄ Worker, each with what tells it from the others (`3 sessions · independent
  review`). A model row per agent of the flow, named as the flow names it (Agent, Executor,
  Supervisor, Worker), not a planner, writer and reviewer where one agent plays two. Rounds says
  what one round is in the flow. The floating paragraph on hover is gone: under the fields a
  help says the highlighted flow in facts (sessions, the models picked, review, rounds, best
  for) and its diagram, `P → W → Gate → R ⇄ W`; `k` says the same. The Flow list opens upward
  so the help stays in view.
- Focus never resizes a dialog: `k`'s help has a fixed number of lines for the terminal, and
  `n`'s too; moving between rows changes the words, not the frame. The keys that close a dialog
  stand in its last row, inside the frame. In `n`, Flow's details show only while Flow has
  focus; every field has a line of help instead. The workflow reads Plan review, Flow, the
  agents' models, Fix rounds (the number alone). Branch shows the whole name and a › (`Current
  (main)  ›`), Files says `or add @path in Goal`. `k` lists the flow first, then planner,
  writer, reviewer. The list's goal column reaches the screen's edge; live task ids are in the
  foreground; the command bar's words are the secondary tone.
- Contrast: three surfaces a step apart, off-white text for what you read, labels and headings
  a step quieter (secondary), metadata quieter still, disabled controls dim; placeholders
  readable; the focused field has an edge of the accent and selection is a blue-grey tint.

### Fixed

- A command the writer proposes with Maven's or Gradle's debug output on (`mvn -X`, `--debug`,
  copied from jsoup's pipeline) is refused like one that picks some tests: every verification
  log would have been megabytes of it. The proposal prompt says so, and the checkpoint names it.
- `vivibox --version`, the timeline and the history name the commit that runs when vivibox runs
  from a checkout, as an editable install does: they named the version of the day it was
  installed, and every pull since went unnamed in a bug report.
- "Started … (model)" and the timeline's "started on" name the model the writing runs on in the
  task's mode: the planner's in `single_agent`, where they named the writer's, which never ran.
- `vivibox init` on a bun project (`bun.lock`): bun from mise in `tools`, `bun install
  --frozen-lockfile` as the preparation, `bun run test` as the suggestion. The agent had
  installed bun in its own home, which the gate does not have. `bun.lock`, `bun.lockb` and
  `bunfig.toml` are risky files.
- `vivibox init` on a Go module whose `go` line is older than 1.21 names the latest Go: that
  line was a floor nothing enforced, and cobra's `go 1.15` got a Go its tests do not build on.
- A build's output that git ignores (`dist/package.json`) no longer stops a task for approval
  as a risky file: the review copy is made from the commits, and they never carry it.
- A tool the gate's fresh home has no version of (`mise ERROR No version is set for shim: bun`,
  after the agent installed bun in its own) is a failure of the environment: the task waits
  for you instead of spending the writer's attempt.
- A turn that committed nothing builds again when the project's toolchain changed since (a tool
  or a JDK given to it): the reused failure had spent the writer's rounds on what no commit of
  its could change.
- The gate's fresh clone has the project's git submodules, as the task's clone has them: the
  Angular RealWorld app's build read its theme from one. A submodule that cannot be fetched is
  a failure of the environment.

- A Rust project's build files are risky files, their changes waiting for your approval like
  `pom.xml`'s: `Cargo.toml`, `Cargo.lock`, `build.rs`, `.cargo/`, `rust-toolchain(.toml)`.
  rust-analyzer runs build scripts and proc macros as your IDE opens the project.

- `vivibox new` without the agent image says so before it creates a task, not after, as a task
  that stands as "could not start" (a draft still needs no image). `vivibox delete` run with no
  terminal to answer on says to add `--yes` instead of a traceback.
- A writer asked for a project's verification command is told what the build files and the
  pipeline name (`go test ./...` (go.mod), the pipeline's steps), as `vivibox init` shows them.
- The planners' briefs say what the gate refuses: a criterion never asks any build or test
  command to pass, not only the verification's (the planner wrote `mvn -Dtest=… test passes` and
  took a repair turn in most tasks). A goal the code already meets is a question for you, not a
  plan. The repair turn, when it happens, says why in the timeline (`planner (plan repair: …)`).

- The commit dialog: Message beside the first line of its text, not above it, as Goal under `n`;
  Branch a row apart from the files above it. The README's pictures show the view as it is now,
  a frame of Flow's help under `n` included.

- `?` showed what `o` opens with on a line of its own that a long command (JetBrains Toolbox's
  `~/.local/share/.../idea`) ran off the dialog's edge; it is the `o` line of an "On this machine"
  section, your home as `~`, and every line of the help wraps under its own words, not under
  the keys.

- The orchestration mode under `k` reads in lines: its name, its flow on a line of its own, when
  it fits, what it costs, and the symbols last.
- The new task form fits 80×24 with a reviewer too: Orchestration and Rounds were a scroll away.
- Under `k` and a project's `e`, a line under the list says what the highlighted setting does
  (what a round is, what `cost_warning` does, the orchestration mode it is on with its legend);
  switching the orchestration no longer puts its description in a notification, and a change
  shows on its row without a notification repeating it; when it applies is on that line.
- In `n`, Kind, Branch and Build start in one column, each on a band of its own; Rounds has a
  band like the lists, and Attach… looks like a button.
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

- Counts ("1 turn", "2 turns") are said by one function, `ui.count`.
- The details panel's sections live in `sections.py`; `panel.py` keeps the header and the states.
- The gate's feedback text moves to `vivibox/feedback.py`.
- The agent's window (tmux, `w`) lives in `window.py`, reached through `actions`.
- `vivibox auth` and `vivibox models` live in `cli_providers.py`, under the size limit.
- `vivibox image` and `vivibox mirror` live in `cli_host.py`, under the size limit.
- What a proposed command is refused for lives in `whole_build.py`; `gate` re-exports it.
- `CHANGELOG.md` is written as the work is done: a change under `vivibox/` changes it in the
  same commit, and `tests/test_structure.py` fails on a branch that does not.
