# vivibox

Coding agents in a box: they work on a clone, in a pod of their own, and nothing they write runs
on your machine until you have looked at it.

vivibox is built for a situation many teams are in: access to a strong model is rationed, and
cheap models are plentiful. It gets the most out of that combination by putting the strong model
where a decision has the most leverage, the plan and the supervision, and the cheap ones in the
loop of implementing and reviewing, behind a gate none of them can bypass.

You describe a task. The agent plans; you accept the plan. The agent implements in its clone; a
gate builds and tests the commits on a fresh clone and checks the plan's criteria; a reviewer on
another model reads the work. Then it comes to you as a review copy, and only your `a` puts it in
your checkout. Everything in between runs on its own.

![One task, from the description to the commit](docs/img/flow.svg)

## Why a box

- **The agent cannot reach your machine.** It runs as an unprivileged container with a Docker
  daemon of its own (Sysbox), so builds, Testcontainers and `docker compose` work without your
  host's socket, your network or your files.
- **Nothing it writes runs on your machine unseen.** It works on a clone; its work comes back as a
  review copy your IDE shows as uncommitted changes; files that run code on import (`pom.xml`,
  `package.json`, git hooks, IDE settings, `AGENTS.md`) wait for your approval whenever they change.
- **A gate it cannot bypass.** The commits are built and tested on a fresh clone with the
  project's own command; every acceptance criterion has to be ticked, every test the agent
  touched has to be seen failing first, no test may be switched off, and the commit messages are
  checked. A red gate sends the agent back, up to a limit; then the task waits for you.

More on what it protects against, and a comparison with Docker Sandboxes: [docs/security.md](docs/security.md).

## Install

Linux with Docker Engine (tested on Ubuntu 24.04), and a model: an API key for any provider in
[opencode](https://opencode.ai)'s list, or an `opencode.json` you already have.

```bash
git clone https://github.com/DEVividious/vivibox.git && cd vivibox
host/setup.sh      # asks before each change: Sysbox, /srv/vivibox, uv, the vivibox command
vivibox            # builds the agent image, asks for a key and a model, opens the view
```

`host/uninstall.sh` is the way back. What `setup.sh` changes on your machine, step by step:
[docs/install.md](docs/install.md).

## A task, key by key

1. `i` points vivibox at a repository. `n` describes a task: a line, or a whole ticket, with
   files attached as `@path`.
2. The agent explores the repository and writes a plan with acceptance criteria. `a` accepts it,
   `r` sends it back with a comment, `e` edits it.
3. The agent implements and commits in its clone, ticking the criteria as it goes. The first
   task of a project also proposes the command that verifies it; you keep it with `a`, once.
4. The gate runs. Green, and the reviewer reads the work; red, and the agent gets the log back.
   Which agents there are, and in what order, is the task's orchestration mode (below).
5. The work waits for you as a review copy: `o` opens it in your IDE, `f` shows the diff, `v`
   runs the app in the pod, `r` asks for changes, `a` accepts it into your checkout and offers a
   commit message written from the plan and the agent's commits.

Desktop notifications, or [ntfy](docs/configure.md) on your phone, say when a task waits for you.
Several tasks run at once, each in its own pod. Everything the view does is also a command
(`vivibox new`, `accept`, `reply`, `status`…). All of it: [docs/tasks.md](docs/tasks.md).

## Orchestration modes

How a task is shared between the planner, the writer and the reviewer, and where the
verification runs, is one setting, `agent_orchestration_mode`: in `config.toml`, the Flow row
under `k`, and per task the Flow row under `n`.

| Mode | In the view | Flow | When |
|---|---|---|---|
| `single_agent` | One agent | plan+write+review → verify | small, routine, cheap tasks |
| `planner_executor` | Planner, then writer | plan → write+review → verify | a good plan matters and the implementation is routine |
| `planner_maker_checker` (default) | Planner, writer, reviewer | plan → write → verify → review ⇄ write | an independent review at every round |
| `supervisor_worker` | Planner supervises writer | plan → write → verify → planner reviews ⇄ write | hard, multi-step changes under a strong model's constant supervision |

Legend: `a+b` one agent does both, in one conversation, on the first role's model; `→` then; `⇄`
rounds of fixes, up to `max_rounds`; verify: the verification (build, tests, criteria, commits).
Below, P, W and R are the planner, the writer and the reviewer.

- A writer that reviews its own work (W+R) gets a turn after each of its turns to read the diff
  as a reviewer would, before the gate; nothing else reviews it.
- The reviewer of `planner_maker_checker` reads in a container of its own, after a green gate;
  its blocking notes go back to the writer, through the gate again, until it has none. Without
  `[roles.reviewer]` it runs on the writer's model.
- The supervisor of `supervisor_worker` is the planner: it reviews the worker's work after a
  green gate in the same pod, with the plan still in its conversation, and sends back what to
  change until it accepts. The flow is `planner_maker_checker`'s; what differs is who reviews and
  where: one strong session that planned, on the worker's clone, instead of a reviewer of its
  own in a container of its own.
- One limit, `max_rounds` (3), counts the fix turns the writer gets, from the gate or from a
  review, before the work comes to you; your reply gives them back.

Models: a strong planner and a cheaper writer; the reviewer cheaper than or a little stronger
than the writer, best of another family; in `supervisor_worker`, a strong supervisor. With little
of a strong model and plenty of a cheap one: `planner_maker_checker`, the strong one planning,
the cheap one writing and reviewing. The trade-offs: `single_agent` is the cheapest and has no
independent review; `planner_maker_checker` costs the most turns; `supervisor_worker` spends the
strong model on every round.

## Models

Any provider opencode knows, on an API key, or an `opencode.json` you import: your employer's
endpoint with its custom models, a local model, the MCP servers you already use. Keys go to
vivibox's own key store, never into config files. The planner, the writer and the reviewer can
each run on a different model; the planner can also be Claude Code on your subscription, or you in
your own chat. [docs/configure.md](docs/configure.md).

## More

- [What it protects against, and how](docs/security.md)
- [Working with tasks](docs/tasks.md): every key, every command, the orchestration modes and the
  reviewer, running the app
- [Providers, MCP servers and projects](docs/configure.md)
- A box without an agent: `b` on a project opens its pod for you, with your keys, opencode and a
  shell; closing it brings your work back through the same review as a task's
- [UX guidelines](docs/ux-guidelines.md) and [prompt guidelines](docs/prompt-guidelines.md), for
  anyone changing what the view says or what the agents read
- [Contributing](CONTRIBUTING.md): reporting a problem (`vivibox --version` first), working on the
  code, releases; [changelog](CHANGELOG.md)

Status: one writer per task, a planner and a reviewer on models of your choice, four ways of
sharing a task between them, several tasks at once. GitHub pull requests are next.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                  # unit tests, no Docker needed
uv run pytest -m docker        # isolation, pod and git protection checks on real containers
uv run python docs/img/screenshot.py   # redraws the pictures from made-up tasks
```

## License

MIT, see [LICENSE](LICENSE).
