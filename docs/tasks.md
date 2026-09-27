# Working with tasks

Run `vivibox` with no arguments:

```bash
vivibox
```

The interactive view lists your projects by name with their tasks under them, the cursor on the
task waiting for you, and refreshes on its own. A task is listed under what you typed until the agent has planned
it; from then on under the one-line summary of its plan. Enter on a project folds its tasks away
and unfolds them again; a folded project's row still says how many wait for you. A project's row
also says what would keep its tasks from starting, such as a variable it passes that is not set in
this shell. The footer shows only the keys that do something for the selected row, and `?` lists
them all:

| Key | Action |
|---|---|
| `?` | every key, and when it applies |
| `d` or Enter | show or hide the details of the selected task: its plan, its acceptance criteria as the agent ticks them off, the files it changed, the risky-file diff or the agent's question |
| `h` / `H` | show or hide the tasks you have accepted, or the ones you deleted, listed below the live ones; the deleted ones start hidden, the header counts what is out of sight, and both choices are kept for the next time |
| `i` | set up a project: browse to a repository vivibox does not know yet, or to a folder, new or empty, where one should start; its verification is a command you type, or left to the first task's writer, with what its build files and pipeline (GitHub Actions, GitLab, Jenkins, Bitbucket, Azure) run in its notes |
| `n` | new task: its kind (feature, bug, other; not asked for a project with no code in it yet), whether it has nothing to build (research, a ticket analysis: the verification then checks the criteria and the commits only), what the agent should do, from one line to a whole ticket, optionally `--auto` or `--draft`, and what each role runs on, config.toml's unless you pick another: a model of any provider you have a key for, or, for the planner, you in your own chat |
| `u` | usage: one task a row, how long its planner, writer and reviewer took (their turns added up), its verifications, and the whole task from its creation to done or to now; for a live task, what its pod uses now: CPU and memory of its containers, the disk of its volumes and its folder, measured every ten seconds while `u` is open and never otherwise; the finished tasks too while the list shows them (`h`). `vivibox usage [--json]` prints the same |
| `k` | settings: providers & MCP (add a provider from opencode's list with a key, import an opencode.json, manage what is on), what each role runs on by default, the reviewer and how it works, the editor `o` opens with, desktop notifications, the limits; the machine's own settings (`tasks_dir`, the address pool) are shown, and the last row opens `config.toml` in your editor |
| `a` | accept the plan; the writer's verification command, kept for the project (the field `e` shows, prefilled, Enter keeps it); or the finished work, which lands in your checkout; then commit it with the suggested message, on the branch the task started on or a new one named after it, or leave it uncommitted |
| `r` | reply: reject, ask for changes, or answer the agent's question; when the work has come back to you, also add acceptance criteria for what you found |
| `e` | edit the plan in `$EDITOR` before accepting it; with a manual planner, paste your chat's answer; at the command checkpoint, change the writer's command before it is kept |
| `c` / `C` | with a manual planner: copy the planning prompt for a chat in your browser, or for a CLI |
| `f` | the work as a diff, in git's pager, before you accept it |
| `o` | open the review copy in your editor: the project's, else `config.toml`'s, else the one the repository's own folders point at (`.idea`, `.vscode`) among those found here, else the first found; `?` says which, `k` or the project's row changes it |
| `p` | approve changes to risky files |
| `g` | a task blocked on a failed verification, or on a question the agent asked about it: run the verification again without the agent, once you have fixed what was outside the code (a token expired, Docker, a service) |
| `l` | read, in your pager: the task's timeline (what happened, one line each: what the task started with and what `m` or `e` changed since, states and why, turns with their cost and time, verifications with what failed, your decisions), a role's conversation (`planner.log`, `writer.log`, `reviewer.log`: each turn with its prompt in short, what the agent said, the tools it called with their arguments cut short, what the turn cost and how long it took; kept after the pod is gone), a verification log (newest first, each with what ran and how it went; a log ends with a summary naming the first trouble line, and the pager opens at its end), or the supervisor's log as diagnostics; on a finished task, what its archive kept of these and its reviews; with one entry it opens at once; while the verification runs its log is the cursor's and followed as it is written (Ctrl-C stops following). `vivibox timeline <id>` prints the timeline |
| `w` | watch or talk to the agent: the planner while it plans, the writer from implementation on, and once both have a conversation, the one you pick; while the verification runs, its log as it is written, and the agents' conversations a choice away; when it ends, the window says so and where the logs stay (`l`); Ctrl-q brings you back (Esc in the agent's window interrupts the agent). The window exists only while you look: it opens on `w` and closes when you leave, so nothing renders for nobody in between |
| `m` | what a role runs on for this task: another model, or planning it yourself; applies from the next start |
| `s` | stop a task, or start it again from where it was |
| `S` | stop it by force, whenever it is not done: for a stop or a start that hangs on the container or on Docker; the supervisor and the containers are killed, the turn under way is lost, the work on disk is kept, and the task says `stopped by force` until you start it again |
| `x` | delete a task without accepting it, after saying what goes and what stays; on a finished one, its line in the history |
| `b` | on a project's row: open a box, the project's pod for you to work in by hand, with your keys, the tools and opencode to run yourself, and no agent of its own; in a box, `w` is a shell and `a` closes it for review |

On a project's row: `n` starts a task in it, `e` opens its settings, one row each: what a new task
runs first (`prepare`), how it is verified (a command, or left to the writer), how `v` runs it, its
JDK,
what its build needs from your shell (`pass_env`), the editor for its review copies, and last its
file for `host_services` and `risky_extra`; `o` opens its repository in your IDE, and `x` forgets
it once it has no tasks; the repository stays.

**Files as context.** *Attach…* in the new task dialog browses to a file or folder, or write
`@src/Order.java` or `@~/tickets/PAY-123.md` (also `@/abs/path`, `@./relative`, a folder) in the
description; after `@` the view suggests paths as you type (arrows, then Tab or Enter; a folder
opens its contents), from the project's repository first, then from where you started `vivibox`
(`./`, `../`, `~/` and `/` mean where you are). A piece
of a path is enough: `@OrderSer` or `@orders/OrderSer` finds `src/main/java/com/acme/orders/OrderService.java`
anywhere in the project, files git ignores left out. A file
of the project is the same file in the agent's clone, the one it edits, so the description points
there; if your copy has uncommitted changes the task says so, since the clone is of a commit, and a
file that is not committed at all is copied instead. Any other file vivibox copies into the task;
the agent reads the copy, read-only, under `/task/context/`, and never sees the rest of your disk. `@notes/x.md` without a leading `./` counts
only when the file exists, so `@john.doe` from a pasted ticket stays text. Files that look like
credentials (`.env`, keys, `settings.xml`, anything under `~/.ssh` or `~/.aws`) are refused, and
the files of one task may take up to 20 MB.

**Bugs** get a plan that starts by reproducing the bug in a failing test and finding its cause
before any fix; features and other tasks get the usual plan.

Desktop notifications tell you when a task waits for you, so you can leave the view closed, and
[ntfy](configure.md) carries the same to your phone. In the view itself a task that starts to wait rings the terminal's bell, and the window's title counts
those waiting; the header adds what today's tasks have cost, and says first when Docker is down or
a provider a role needs has no key, before you make a task that could not start. After a reboot the view offers to start the tasks that were running. Their
buttons (**Show plan**, **Accept plan**, **Open in idea**) cover the common steps too.

Everything the view does is also a command, for scripts or when you prefer a shell:

```bash
vivibox new myproject "Add unit tests for OrderValidator"
vivibox new myproject - < ticket.md   # a longer description, from a file
vivibox new myproject --kind bug "Expired cards pass validation, see @~/tickets/PAY-123.md"
vivibox new myproject --model planner=deepseek/deepseek-v4-pro "Port the importer to streams"
vivibox new myproject --no-build "Find out why PAY-123 happens; answer in the handoff"
vivibox accept myproject-1            # the work lands in your checkout; commit it with the suggested message?
vivibox accept myproject-1 --branch   # or put it on branch vivibox/myproject-1, e.g. for a pull request
vivibox status                        # all tasks, the ones waiting for you first
vivibox status myproject-1            # one task: its next step and recent events
vivibox attach myproject-1            # watch or talk to the agent, or the verification (Ctrl-q leaves)
```

### Your decisions

| Command | When |
|---|---|
| `vivibox accept <id>` | accept the plan (edit `.task/plan.md` first if you like), the writer's verification command (`--verify "…"` keeps yours instead), or the finished work |
| `vivibox reply <id> "comment"` | reject, ask for changes, or answer the agent's question |
| `vivibox reply <id> "comment" --criterion "…"` | send finished or stuck work back with a new acceptance criterion (repeat for more): it joins the accepted plan, and the gate holds the work to it like the rest |
| `vivibox risky <id>` / `vivibox approve-risky <id>` | review and approve changes to files that run code on your host |
| `vivibox verify-again <id>` | a blocked task: run the verification once more, without the agent |

When a verification fails on something outside the code (no Docker, no network, a credential, a
full disk, or a command past `verify_timeout`), the task shows `verification could not run` and
waits for you without spending one of the agent's attempts: fix it and press `g`. When the
verification does not recognise such a cause but the agent does and asks about it, `g` is the
answer too: the build runs again, without a turn of the agent. When it fails again, the agent's
question stands and the task waits for you once more, no turn and no attempt spent: a feedback
turn would have the agent work around what it asked about instead of waiting for your answer.

- `--auto` on `vivibox new` accepts the agent's plan without stopping, for small, well-described
  tasks. It still stops when the plan has no real acceptance criteria, when the agent asks a
  question, or when risky files changed.
- `--draft` only creates the task, to write the plan yourself or check the baseline first with
  `vivibox verify <id>`; then `vivibox start <id>`. A plan you finished (the placeholder criterion
  replaced) goes straight to your review when the task starts, whoever the planner is; one with
  the placeholder still in it goes to the planner as its starting point.

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
cost, and the commit it left; and its record in `~/.local/share/vivibox/archive/<id>/`: the plan it
was held to, its events and its criteria, a few dozen kilobytes, without the clone or the logs. A
deleted task leaves the same. The view lists those under their projects with the plan in the
details; `x` forgets one, archive included.

- **Accept** with `vivibox accept <id>`: only now does the work reach your checkout, as
  uncommitted changes on your current branch. vivibox asks whether to commit them and suggests a
  message: a subject from the plan's summary (else the goal's first line, when it fits), then every
  commit the agent made, one line each in order, so the commit tells the whole of the work;
  answer `n` to look at them in your IDE first. Then it asks where: on the branch the task started
  on, or on a new branch named after the task (`feature/<title>`, `bugfix/<title>`, or `<title>`
  for other work; `-1`, `-2`, … when the name is taken), which a task started on `main` or
  `master` offers first. Your checkout stays on the branch the commit went to.
  It refuses while you have staged changes. Files that conflict with your branch are left for you
  to resolve, and the work is kept on branch `vivibox/<id>` too. `--branch` skips your checkout
  and only creates that branch.
- `vivibox review <id>` updates the copy by hand, for example to look at work in progress.

Changes your IDE makes to its own project files (`.idea/`, `*.iml`, `.vscode/`) in the copy are
ignored. Changes of yours are never overwritten: vivibox stops and names the files. Do not open
`/srv/vivibox/<id>/repo` in an IDE. It is the agent's working copy, and IDEs rewrite their project
files when they open it.

### Orchestration modes

How a task is shared between the planner (P), the writer (W) and the reviewer (R), and where the
verification runs, is its orchestration mode: config.toml's `agent_orchestration_mode`, the Flow
row under `k`, or the Flow row under `n` for one task. Steps joined with `+` are one agent in one
conversation, on the first role's model; `→` is then; `⇄` is rounds of fixes.

| Mode | In the view | Flow | When |
|---|---|---|---|
| `single_agent` | Single agent | P+W+R → Gate | small, routine, cheap tasks; no independent review |
| `planner_executor` | Planner → Executor | P → W+R → Gate | a good plan matters and the implementation is routine |
| `planner_maker_checker` (default) | Planner → Writer → Reviewer | P → W → Gate → R ⇄ W | an independent review at every round |
| `supervisor_worker` | Supervisor ⇄ Worker | P → W → Gate → (P+R) ⇄ W | hard, multi-step changes under a strong model's constant supervision |

Every mode plans first and stops for your acceptance of the plan (`--auto` does not). Then:

- **W+R**, the writer reviewing its own work (`single_agent`, `planner_executor`): after each of
  the writer's turns, the first implementation, a fix after a red gate, or what it did with your
  reply, it gets one more turn in the same conversation to read the diff against the plan and
  the criteria, for what the gate cannot see (a test that cannot fail, a criterion ticked on
  faith, behaviour the plan did not ask for), and to fix it. Then the gate runs. The timeline
  says `writer turn (self-review)`.
- **The reviewer** of `planner_maker_checker` works in a container of its own on a fresh clone of
  the commits, with the writer's tree read-only and only its own key, after a green gate, and
  writes `handoff/review-N.md`: notes under **Blocking** and **Not blocking**, each with a place
  (`path:line`), what is wrong and what would make it right: what keeps the work from being what
  the plan says, and, besides the plan, what a senior reviewer sends back (code the repository
  already has, an abstraction with one caller, behaviour nobody asked for, a comment that
  restates the code, a test of how the code is written, a name that says the type). It runs no
  build, asks nothing and adds no criteria. Blocking notes go back to the writer, then the gate
  runs again and the reviewer reads again, with what the writer answered where it disagreed
  (`review-N-reply.md`) read first. No blocking notes, and the work comes to you, the notes with
  it. Without `[roles.reviewer]` the reviewer runs on the writer's model.
- **The supervisor** of `supervisor_worker` is the planner: after a green gate it reads the
  worker's work in the same pod, on the same clone, with the plan still in its conversation, and
  writes the same `review-N.md`; it changes nothing and commits nothing. The rounds go as the
  reviewer's do: blocking notes back to the worker, the gate again, the supervisor again, until it
  accepts or the rounds are out. Nothing the gate would catch reaches it: a red gate goes back to
  the worker by itself. What differs from `planner_maker_checker` is who reviews and where, not
  where the gate stands.

One limit, `max_rounds` (3 in config.toml, or the task's own from `n`), counts the fix turns the
writer gets before the work comes to you: a red gate is one, a review with blocking notes is
one, the first implementation and a verification alone are none. Your reply gives them back.
The list says `implementing (round n/N)` from the first fix turn on, `reviewing` while a
reviewer or the supervisor reads, and the reviewing's cost stands in a column of its own where
the mode has one. From the first review on, the panel shows the newest one with its counts;
`l` opens every round.

What the reviewer reads, and what comes to you, is the commit the gate verified: commits that
changed since (you talked to the agent under `w`, an agent committed after its turn) go through
the gate again first, for no round.

### Running the app

`vivibox demo <id>`, or `v` in the view, starts the project inside its pod and opens it. A server
the agent left running in the background during its turn would hold the port, so it is stopped
first, and the view says what went:

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

The app runs once the work is back with you (*review the work*, *verification failed*) and in a
box. While the agent implements or the gate verifies, the pod and its working tree are theirs: a
second build on the same tree, or a server on a port their tests want, would get in their way.

Each task has its own address, so two tasks can serve on the same port without colliding, and the
port you use inside the pod is the port you use from outside. Addresses come from
`198.51.100.0/24`, a range RFC 5737 reserves for documentation so that nothing else may use it; a
task holds one until you accept or remove it, and `network.pool` in `config.toml` changes where they
come from.

A frontend that calls its backend by container name is a separate matter: that name is resolved by
the browser on your machine, which knows nothing about it. Let the frontend call a path on its own
origin and forward it server-side (`server.proxy` in Vite, rewrites in Next, or a backend that
serves the built frontend). That also settles CORS.

### A box: the pod without an agent

`b` on a project's row, or `vivibox box <project>`, opens a box: the project's clone in a pod of
its own, with the keys of every provider you have on, opencode's configuration with your MCP
servers, and the pod's tools (Java, Node, Python, Docker), and no agent. It is for working by hand
where a mode without permission prompts is safe and Docker works: `w` (or `vivibox attach <id>`)
gives you a shell in the clone, and there you run `opencode`, or `claude` after logging in with
its own client, or anything else; Ctrl-q leaves the shell and the box stays. `v` runs the app in
it like in a task. `vivibox box --new ~/projects/idea` starts a repository and a project there
first, for a project from scratch.

The box holds a clone of a commit, so uncommitted changes in your checkout are not in it. `a`
closes the box: whatever you left uncommitted is committed, and the work comes to review the way
a task's does, with the diff, the review copy, and your approval of risky files; `a` again accepts
it into your checkout. There is no verification unless you ask for one: `vivibox verify <id>` runs
the project's commands on the box's commits. `s` takes the pod down and up again; `x` deletes the
box without bringing anything back.

A login you make inside the box, such as `claude`'s, lives in the box's own volume, which vivibox
never reads or copies, and goes with the box.

### What the tasks cost, and what the gate refused

The list shows what a task cost in three columns, one figure each: `PLAN`, `IMPL` and, when a
reviewer is configured, `REVIEW`; a box shows one figure under `IMPL`. The history keeps the same
split; the panel (`d`) lists each agent of the task with what it runs on, its turns and its cost,
roles one agent plays together in one row.

`vivibox stats` adds up the events of every task, live and finished: how many verification runs
a task took to pass (median and maximum), what a turn costs per role and per state, how many
turns failed or were retried, and how often the gate refused work for each reason (a command
failed, criteria not met, commit problems, tests switched off, uncommitted files, tests without
red evidence, hidden characters, something outside the code, a build not run). `--project` and
`--since YYYY-MM-DD` narrow it, `--json` is for a script. These are the numbers to look at before
changing a word of a prompt.

`vivibox usage` prints how long each task took, one a row: `PLAN`, `WRITE` and `REVIEW` add up
the role's turns, `GATE` the verifications, `TOTAL` runs from the task's creation to done, or to
now while it lives; `CPU`, `RAM` and `DISK` are what a live task's pod uses now (one `docker stats
--no-stream` for every container, `docker system df -v` for the volumes, `du` for the task's
folder). `--live` leaves the finished ones out, `--project` narrows it, `--json` gives seconds,
and bytes per container and per volume, for a note or a script.

### When a task costs too much

`cost_warning` and `cost_limit` in `config.toml` (under `k`, limits) are dollars per task. Past
the warning you are told once and the task goes on; at the limit it stops before its next turn
and waits with the figures on its row. Raise the limit and press `s` to take it on.

### When a turn fails

An error that passes with time (the provider busy or rate limiting, the network gone for a
moment) is waited out: the turn runs again after half a minute, a minute, then two minutes, and
the timeline says so. The fourth failure in a row, and any other error, stops the task with the
reason on its row; `s` starts it again from where it was.

### Stopping and removing

`vivibox stop <id>` stops a task and keeps its work, and `vivibox start <id>` continues it from
where it was (`resume` is the same command). `vivibox stop <id> --force` (`S` in the view) kills the
supervisor and the containers instead of waiting for them, for a stop that hangs; the turn under
way is lost, the work on disk is kept. `vivibox delete <id>` (or `rm`) deletes a task you do
not want, without accepting it; a line in the history and its archive stay. Task numbers are never
reused, so a removed task's branch is never overwritten.

---
Back to the [README](../README.md).
