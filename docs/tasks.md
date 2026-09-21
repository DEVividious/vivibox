# Working with tasks

Run `vivibox` with no arguments:

```bash
vivibox
```

The interactive view lists your projects with their tasks under them, the ones waiting for you
first, and refreshes on its own. A task is listed under what you typed until the agent has planned
it; from then on under the one-line summary of its plan. Enter on a project folds its tasks away
and unfolds them again; a folded project's row still says how many wait for you. A project's row
also says what would keep its tasks from starting, such as a variable it passes that is not set in
this shell. The footer shows only the keys that do something for the selected row, and `?` lists
them all:

| Key | Action |
|---|---|
| `?` | every key, and when it applies |
| `d` or Enter | show or hide the details of the selected task: its plan, its acceptance criteria as the agent ticks them off, the files it changed, the risky-file diff or the agent's question |
| `h` | show or hide the tasks you have accepted, listed below the live ones |
| `i` | set up a project: browse to a repository vivibox does not know yet, or to a folder, new or empty, where one should start |
| `n` | new task: its kind (feature, bug, other; not asked for a project with no code in it yet), what the agent should do, from one line to a whole ticket, optionally `--auto` or `--draft`, and what each role runs on, config.toml's unless you pick another: a model of any provider you have a key for, or, for the planner, you in your own chat |
| `k` | providers & MCP: add a provider from opencode's list with a key, import an opencode.json, or manage what is on and remove what is not wanted |
| `a` | accept the plan, or the finished work, which lands in your checkout; then commit it with the suggested message, or leave it uncommitted |
| `r` | reply: reject, ask for changes, or answer the agent's question; when the work has come back to you, also add acceptance criteria for what you found |
| `e` | edit the plan in `$EDITOR` before accepting it; with a manual planner, paste your chat's answer |
| `c` / `C` | with a manual planner: copy the planning prompt for a chat in your browser, or for a CLI |
| `o` | open the review copy in your editor; the first time, vivibox lists the editors it finds here and keeps your choice |
| `p` | approve changes to risky files |
| `g` | a task blocked on a failed verification: run the verification again without the agent, for when the cause was outside the code (a token expired, Docker, a service) |
| `l` | read the newest verification log, or the supervisor's, in your pager |
| `w` | watch or talk to the agent; Ctrl-q brings you back (Esc there interrupts the agent) |
| `m` | what a role runs on for this task: another model, or planning it yourself; applies from the next start |
| `s` | stop a task, or start or resume it |
| `x` | delete a task without accepting it, after saying what goes and what stays; on a finished one, its line in the history |

On a project's row: `n` starts a task in it, `e` edits its file (verify commands, `pass_env`, host
services), `o` opens its repository in your IDE, and `x` forgets it once it has no tasks; the
repository stays.

**Files as context.** *Attach…* in the new task dialog browses to a file or folder, or write `@~/tickets/PAY-123.md` (or `@/abs/path`, `@./relative`, a folder) in
the description; after `@` the view suggests paths as you type (arrows, then Tab or Enter; a folder
opens its contents), relative to where you started `vivibox`. vivibox copies the file into the task; the agent reads the copy, read-only, under
`/task/context/`, and never sees the rest of your disk. `@notes/x.md` without a leading `./` counts
only when the file exists, so `@john.doe` from a pasted ticket stays text. Files that look like
credentials (`.env`, keys, `settings.xml`, anything under `~/.ssh` or `~/.aws`) are refused, and
the files of one task may take up to 20 MB.

**Bugs** get a plan that starts by reproducing the bug in a failing test and finding its cause
before any fix; features and other tasks get the usual plan.

Desktop notifications tell you when a task waits for you, so you can leave the view closed. Their
buttons (**Show plan**, **Accept plan**, **Open in idea**) cover the common steps too.

Everything the view does is also a command, for scripts or when you prefer a shell:

```bash
vivibox new myproject "Add unit tests for OrderValidator"
vivibox new myproject - < ticket.md   # a longer description, from a file
vivibox new myproject --kind bug "Expired cards pass validation, see @~/tickets/PAY-123.md"
vivibox new myproject --model planner=deepseek/deepseek-v4-pro "Port the importer to streams"
vivibox accept myproject-1            # the work lands in your checkout; commit it with the suggested message?
vivibox accept myproject-1 --branch   # or put it on branch vivibox/myproject-1, e.g. for a pull request
vivibox status                        # all tasks, the ones waiting for you first
vivibox status myproject-1            # one task: its next step and recent events
vivibox attach myproject-1            # watch or talk to the agent (Ctrl-q leaves)
```

### Your decisions

| Command | When |
|---|---|
| `vivibox accept <id>` | accept the plan (edit `.task/plan.md` first if you like), or the finished work |
| `vivibox reply <id> "comment"` | reject, ask for changes, or answer the agent's question |
| `vivibox reply <id> "comment" --criterion "…"` | send finished or stuck work back with a new acceptance criterion (repeat for more): it joins the accepted plan, and the gate holds the work to it like the rest |
| `vivibox risky <id>` / `vivibox approve-risky <id>` | review and approve changes to files that run code on your host |
| `vivibox verify-again <id>` | a blocked task: run the verification once more, without the agent |

- `--auto` on `vivibox new` accepts the agent's plan without stopping, for small, well-described
  tasks. It still stops when the plan has no real acceptance criteria, when the agent asks a
  question, or when risky files changed.
- `--draft` only creates the task, to write the plan yourself or check the baseline first with
  `vivibox verify <id>`; then `vivibox start <id>`.

### Planning in your own chat

With `harness = "manual"` for the planner, you plan in a chat of your own: claude.ai, Gemini, or a
CLI such as `claude` or `gemini` in your checkout, on whatever plan you have there. vivibox never
touches that login: it gives you the prompt and takes back the plan, and everything after it runs
on its own, on the writer's key.

1. When the task starts, the writer spends one short turn describing the repository for a chat that
   cannot see it (a new project skips this). The task then waits for you: *plan it yourself*.
2. `c` copies the prompt for your browser, `C` the one for a CLI, which reads your checkout itself.
   Discuss the plan there as long as you need.
3. `e` opens the answer file: paste the chat's final answer, words around it included, and save.
   A CLI writes the file itself, so `e` only shows you what it wrote. vivibox finds the plan, counts
   its criteria and offers to accept it. If the answer is not a plan, the message that asks the
   chat to fix it is in your clipboard.

The same from a shell: `vivibox plan prompt <id> [--cli]` and `vivibox plan import <id> [file|-]`.
A subscription is for your own use of the chat, which is why vivibox does not run a model on it;
for planning without you, give the planner an API key (`harness = "claude-code"`).

### Reviewing the work

Nothing the agent writes reaches your repository until its work passes the gate and risky changes
are approved. Then vivibox prepares a review copy in `/srv/vivibox/<id>/<your repository's name>`:
a worktree of your repository at the commit the task started from, with the agent's work as
uncommitted changes. Your IDE lists them like your own work (IntelliJ: the Commit tool window,
Alt+0), with a diff for each file. Your checkout and your branches stay as they are.

- **Run the tests or the application** in the review copy if you like. It lives in the task's
  directory, which the agent cannot see.
- **Ask for changes** with `vivibox reply <id> "…"`. The next round updates the copy.
An accepted task leaves a line in `~/.local/share/vivibox/history.jsonl`: what it was, what it
cost, and the commit it left. The view lists those under the live tasks; `x` forgets one.

- **Accept** with `vivibox accept <id>`: only now does the work reach your checkout, as
  uncommitted changes on your current branch. vivibox asks whether to commit them and suggests a
  message, the agent's own when it made one commit; answer `n` to look at them in your IDE first.
  It refuses while you have staged changes. Files that conflict with your branch are left for you
  to resolve, and the work is kept on branch `vivibox/<id>` too. `--branch` skips your checkout
  and only creates that branch.
- `vivibox review <id>` updates the copy by hand, for example to look at work in progress.

Changes your IDE makes to its own project files (`.idea/`, `*.iml`, `.vscode/`) in the copy are
ignored. Changes of yours are never overwritten: vivibox stops and names the files. Do not open
`/srv/vivibox/<id>/repo` in an IDE. It is the agent's working copy, and IDEs rewrite their project
files when they open it.

### Running the app

`vivibox demo <id>`, or `v` in the view, starts the project inside its pod and opens it:

```
$ vivibox demo myshop-1
From your project file:
  docker compose up -d db
  ./gradlew bootRun
Listening: http://198.51.100.3:8080
```

The port is not configured anywhere. vivibox asks the kernel what began listening after the
commands ran, so whatever the project is — Vite, uvicorn, Spring Boot, three services from a
compose file — the address it prints is the one that is actually open. That also makes the usual
mistake legible instead of silent:

```
Port 8080 is bound to localhost inside the pod, so nothing outside can reach it.
Start it on 0.0.0.0 instead.
```

The commands come from the task's own run instruction when it has one, then from `demo` in the
project file if you set one, then from the repository's compose file. When none of those says,
vivibox asks the agent: it reads the README and the build files, may start a database, and can ask
you back when the choice is yours. Answer with `--reply "…"`, or in the view, and the same
conversation carries on until the project comes up. That conversation is separate from the task's
own, so a question about how to run something can never stop the work itself.

What it works out is written to `.task/handoff/demo.md` inside the task, as short markdown whose
shell blocks are the commands. It stays there: nothing is copied into your repository. Accepting
the task keeps the instruction in the history, and the next task in that project is offered it
after you have read it, so the model works this out once rather than once per task.

`vivibox demo <id> --stop` stops it; so does stopping the pod. `vivibox pod shell <id>` is still
there when you would rather run it by hand.

Each task has its own address, so two tasks can serve on the same port without colliding, and the
port you use inside the pod is the port you use from outside. Addresses come from
`198.51.100.0/24`, a range RFC 5737 reserves for documentation so that nothing else may use it; a
task holds one until you accept or remove it, and `network.pool` in `config.toml` changes where they
come from.

A frontend that calls its backend by container name is a separate matter: that name is resolved by
the browser on your machine, which knows nothing about it. Let the frontend call a path on its own
origin and forward it server-side (`server.proxy` in Vite, rewrites in Next, or a backend that
serves the built frontend). That also settles CORS.

### Stopping and removing

`vivibox stop <id>` stops a task and keeps its work, and `vivibox resume <id>` continues it.
`vivibox rm <id>` removes a task you do not want, without accepting it. Task numbers are never
reused, so a removed task's branch is never overwritten.

---
Back to the [README](../README.md).
