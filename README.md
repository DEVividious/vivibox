# vivibox

Run AI coding agents on your projects in isolated pods, with you deciding at checkpoints.

Each task gets its own clone of your repository and a pod: an unprivileged agent container plus a
Docker daemon in a [Sysbox](https://github.com/nestybox/sysbox) sidecar, so the agent can run
`docker compose`, Testcontainers and full builds without access to your host. The agent plans, you
accept the plan, the agent implements, and a verification gate the agent cannot bypass checks the
result before you accept it. Changes come back to your repository as a branch.

Status: phase 1. One agent per task through [opencode](https://opencode.ai), on Ubuntu with
Docker. Reviewer agents, Claude Code, GitHub and MCP access, parallel tasks and a web UI are
planned.

## What it protects against

- **The agent is not trusted.** It runs as your UID without capabilities, on a read-only root
  filesystem, in a pod whose Docker daemon runs under Sysbox. A privileged container the agent
  starts cannot reach your host. The pod cannot reach your host or LAN, except services you list.
- **Nothing the agent writes runs on your host without your approval.** The task clone's
  `.git/config` and hooks are read-only for the agent, so git on your host, run by you or by your
  IDE, cannot execute agent-written commands. Files that run code on IDE import or in your
  shell (`pom.xml`, `.mvn/`, Gradle files, `package.json`, `.idea/`, `.vscode/`, `.envrc`, git
  hooks, nested repositories…) need your approval whenever they change.
- **The gate checks what the agent claims:** your verify commands, the acceptance criteria of the
  plan you accepted, one-line commit messages without co-author or AI signatures, and invisible
  Unicode characters in added lines.
- **Keys stay out of images, volumes and `docker inspect`.** A task gets only the key its model
  needs, as a read-only file on tmpfs.

## Requirements

- Ubuntu 24.04 (other recent Linux distributions should work) with Docker Engine; your user in the
  `docker` group.
- [uv](https://docs.astral.sh/uv/) and Python 3.12+.
- An API key for a model provider supported by opencode.

## Install

```bash
host/setup.sh            # once, asks before each change: Sysbox, tmux, /srv/vivibox, firewall helper
host/setup.sh --check    # confirms nothing is missing
uv sync
uv run vivibox image build
uv run vivibox image check
```

`uv run vivibox` works inside this repository. To run `vivibox` from any directory, install it as a
tool; `--editable` picks up changes to this checkout:

```bash
uv tool install --editable .
```

Run it again after updating this checkout when `pyproject.toml` changed, so the tool gets new
dependencies.

`host/setup.sh` moves Docker's default networks off `172.17.0.0/16` (Docker restarts), installs
Sysbox, creates `/srv/vivibox` mounted `nosuid,nodev`, and allows your user to run only the pod
firewall helper through sudo.

## Configure

Once per machine, the settings and the API key of your model provider:

```bash
mkdir -p ~/.config/vivibox
cp templates/config.example.toml ~/.config/vivibox/config.toml   # tasks directory and model
vivibox auth set deepseek          # the provider of the model in config.toml
vivibox models deepseek            # model names to put in config.toml
```

Keys live in `~/.local/share/vivibox/keys/`, one file per provider, readable only by you.

Once per repository you want agents to work on: press `n` in the view and pick
*+ set up a project…*, or from a shell inside it:

```bash
cd ~/projects/myproject
vivibox init
vivibox init ~/projects/new-idea --git   # an empty or new folder: starts the repository too
```

Either way vivibox reads the build files and proposes a project. For a repository that has no code
yet, leave the command empty: the first plan you accept sets how the project is built and tested,
and vivibox keeps it for the next task. Otherwise: the command the gate runs (`bash gradlew
test`, `bash mvnw -B verify`, `mvn -B verify`, `npm ci && npm test`) and, when the build needs it,
a JDK other than the image's Java 21, for example Java 17 for Gradle 7. It shows the file and writes
it to `~/.config/vivibox/projects/<name>.toml` only when you confirm. For a folder that is empty or
does not exist yet, it starts a git repository there with a first commit, so an app can be built
from nothing. Nothing is set up by just running `vivibox` somewhere. Edit the file to add services on your host the agent may reach:

```toml
repo = "~/projects/myproject"
verify = ["bash gradlew test --no-daemon --console=plain"]
java = "17"                                     # empty for Java 21
host_services = ["host.docker.internal:5432"]   # optional, network access to services on your host
```

A repository you move or delete leaves its project file behind. vivibox does not offer a project it
cannot work in: it names those on start and offers to forget them, which removes the project file
and nothing else.

## A task

Install `vivibox` as a tool (see Install) and run it with no arguments:

```bash
vivibox
```

The interactive view lists your tasks, the ones waiting for you first, and refreshes on its own.
A task is listed under what you typed until the agent has planned it; from then on under the
one-line summary of its plan.
The footer shows only the keys that do something for the selected task:

| Key | Action |
|---|---|
| `d` or Enter | show or hide the details of the selected task: its plan, the files it changed, the risky-file diff or the agent's question |
| `h` | show or hide the tasks you have accepted, listed below the live ones |
| `i` | set up a project: a repository vivibox does not know yet, or an empty folder where one should start |
| `n` | new task: its kind (feature, bug, other; not asked for a project with no code in it yet), what the agent should do, from one line to a whole ticket (the first line is its title), and optionally `--auto` or `--draft` |
| `a` | accept the plan, or the finished work, which lands in your checkout; then commit it with the suggested message, or leave it uncommitted |
| `r` | reply: reject, ask for changes, or answer the agent's question |
| `e` | edit the plan in `$EDITOR` before accepting it |
| `o` | open the review copy in your editor; the first time, vivibox lists the editors it finds here and keeps your choice |
| `p` | approve changes to risky files |
| `w` | watch or talk to the agent; Ctrl-q brings you back (Esc there interrupts the agent) |
| `s` | stop a task, or start or resume it |
| `x` | remove a task without accepting it |

**Files as context.** Write `@~/tickets/PAY-123.md` (or `@/abs/path`, `@./relative`, a folder) in
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
vivibox new myproject - < ticket.md   # a longer description; its first line is the title
vivibox new myproject --kind bug "Expired cards pass validation, see @~/tickets/PAY-123.md"
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
| `vivibox risky <id>` / `vivibox approve-risky <id>` | review and approve changes to files that run code on your host |

- `--auto` on `vivibox new` accepts the agent's plan without stopping, for small, well-described
  tasks. It still stops when the plan has no real acceptance criteria, when the agent asks a
  question, or when risky files changed.
- `--draft` only creates the task, to write the plan yourself or check the baseline first with
  `vivibox verify <id>`; then `vivibox start <id>`.

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

### Stopping and removing

`vivibox stop <id>` stops a task and keeps its work, and `vivibox resume <id>` continues it.
`vivibox rm <id>` removes a task you do not want, without accepting it. Task numbers are never
reused, so a removed task's branch is never overwritten.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                  # unit tests, no Docker needed
uv run pytest -m docker        # isolation, pod and git protection checks on real containers
```

## License

MIT, see [LICENSE](LICENSE).
