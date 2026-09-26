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
- A reviewer on a model of its own reads the work after a green gate; blocking notes go back to
  the writer by themselves (`loop`) or every note comes to you (`supervised`); a task can go
  without a review. The reviewer reads the writer's reply in a later round. Its cost stands in a
  column of its own.
- A project without a verification command asks the writer for one, and the proposal waits for
  your review before the first verification (`review the command`); a proposal narrowed to some
  tests is refused.
- A box: a pod of your own in a project, with opencode, to work in by hand (`b`).
- Projects prepare once in a new task's clone (`prepare`); Node projects get their dependencies
  installed by their lockfile, in the folder a command starts in; Maven keeps what a task
  installs in a volume of its own; Testcontainers images and the host's certificate authorities
  reach the pod.
- Providers, models and MCP servers from an imported `opencode.json`; keys in vivibox's own
  store; a retired model is not offered, and a start on one says so.
- The view: tasks waiting for you first, projects by name, a task's row kept in place; `w`
  watches or talks to the agent, or the verification as it runs, from inside tmux too; `l`
  reads the timeline, a verification log or the supervisor's; `f` pages the diff; `S` stops by
  force; `h` and `H` show accepted and deleted tasks; the header counts what waits and works,
  today's cost, and says when Docker is down or a provider has no key; `?` names every key and
  the build.
- Attachments: `@path` in a task's description copies a file or folder for the agent; a path
  in the repository is a reference. Secrets folders are refused.
- Notifications on your phone through ntfy; a cost warning and a cost limit per task;
  `vivibox stats`, `vivibox timeline`, `vivibox status`.
- `host/setup.sh` and `host/uninstall.sh`; Docker's default networks moved off ranges a VPN
  uses; the pod's TCP MSS clamped to the uplink's MTU.
- The commit suggested at acceptance tells the whole of the task's work, its subject from the
  task and every commit of the agent as a line.

### Internal

- `CHANGELOG.md` is written as the work is done: a change under `vivibox/` changes it in the
  same commit, and `tests/test_structure.py` fails on a branch that does not.
