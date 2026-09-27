# vivibox

**Give coding agents a task. Review the result. Keep control of your code.**

vivibox runs coding agents in isolated environments, each with a clone of your repository and
its own Docker daemon. They plan, implement, test and review; you approve the plan and decide
what lands in your checkout.

Built for when access to a strong model is limited and cheaper models are plentiful: put the
strong model on planning, let cheaper models write and review, and verify their work with your
project's build and tests.

![A task in vivibox: describe it, approve the plan, follow implementation and verification, then review and accept the work.](docs/img/flow.svg)

*An example task, with time compressed and illustrative costs.* [Open the still view](docs/img/view.svg).

## Why vivibox?

- **Several tasks, one terminal.** Work across repositories at once. See progress, costs and
  what needs your attention; get desktop or ntfy notifications when a task waits for you.
- **Models you choose, roles you assign.** Use different models for planning, writing and
  review. Pick a flow for each task, from one agent to independent review and fix rounds.
- **A full build environment per task.** Run builds, Testcontainers and `docker compose` in
  the task's pod, without sharing your host's Docker socket. Preview the app there too.
- **Verification before handoff.** Builds and tests run on a fresh clone. Checks cover the
  accepted plan's checklist, commit messages, disabled tests and recorded failing-test evidence.
  Failures go back to the writer, within your round and cost limits.
- **Your checkout stays yours.** Inspect a review copy in your IDE, ask for changes, then
  accept. Changes to build files, hooks and IDE settings need separate approval before review.

A **pod** is the task's isolated environment: an agent container and its own Docker daemon,
using Sysbox. Host and private-network access is blocked except for services you allow.
[How isolation and verification work →](docs/security.md)

## Get started

**Requirements:** Linux with Docker Engine (tested on Ubuntu 24.04), and a model API key or an
existing `opencode.json` to import. macOS and Windows are not supported.

```bash
git clone https://github.com/DEVividious/vivibox.git
cd vivibox
host/setup.sh
vivibox
```

Setup asks before changing your machine, including installing Sysbox and restarting Docker.
On first launch, vivibox builds the agent image and helps you choose a provider and model.
[Installation details and uninstall →](docs/install.md)

## Your first task

1. **Describe the change.** Press `i` to add a repository, then `n` for a task. Paste a ticket
   or a short goal; attach context with `@path`.
2. **Agree on the plan.** Read the approach and acceptance criteria. `a` accepts, `r` asks for
   changes, `e` edits. Coding starts after your approval by default.
3. **Let the agents work.** The writer implements, verification runs, and review follows your
   chosen flow. Fixes repeat automatically within the task's limits. If the project has no
   verification command, the writer proposes one for you to approve separately.
4. **Try the result.** `o` opens the review copy in your IDE, `f` shows the diff, `v` runs the
   app in the pod. Use `r` to ask for changes.
5. **Accept when ready.** `a` applies the work to your checkout. A separate dialog lets you
   choose the branch and edit the proposed commit message.

Prefer a shell? The same workflow is available as commands:

```bash
vivibox new myproject "Add a /health endpoint with a database check"
vivibox status myproject-1
vivibox accept myproject-1
vivibox reply myproject-1 "Also cover the database timeout"
```

[All keys and commands →](docs/tasks.md)

## Choose a flow

Choose **Flow** when creating a task (`n`), or set the default in settings (`k`).

| Flow | How the work is shared | Best for |
|---|---|---|
| **Single agent** | One agent plans, writes and reviews its own work | Small, routine tasks |
| **Planner → Executor** | A planner hands off to an agent that writes and reviews its own work | A strong plan with cheaper execution |
| **Planner → Writer → Reviewer** · default | Separate agents plan, write and review; review follows verification | Independent review with automatic fix rounds |
| **Supervisor ⇄ Worker** | The agent that planned also reviews each verified revision | Keeping the planning context through complex changes |

Every flow includes verification. The first two use self-review; the other two send review
feedback back to the writer. You choose the models and the limit on fix rounds.
[Flow details →](docs/tasks.md#orchestration-modes)

## Bring your tools

Use providers supported by opencode, a custom API endpoint or a local model. Import providers
and MCP servers from your `opencode.json`; API keys stay in vivibox's key store.
You can also plan in your own chat and bring the plan back to vivibox.
[Providers, models and projects →](docs/configure.md)

Want to work directly in the isolated environment? Press `b` on a project to open a **box** with
opencode and a shell. Its changes return through the same review-copy and approval steps.
[Working in a box →](docs/tasks.md#a-box-the-pod-without-an-agent)

## Project status & contributing

**Alpha.** Local task workflows, four flows, parallel tasks and app previews are available today.
GitHub pull request integration is planned.

[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) ·
[Security](docs/security.md) · [MIT license](LICENSE)
