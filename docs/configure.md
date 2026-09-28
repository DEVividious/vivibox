# Configuring vivibox

Run `vivibox`. The first time, it builds the agent image (a few minutes), writes a commented
`~/.config/vivibox/config.toml` and opens the view. An update that changes the image builds it again
on the next start and removes the older versions, about 2 GB each, except one a task's pod still runs
on. Planning starts in your own chat; the writer has no model until you add a provider under `k`
and pick one of its models in your first task. `k` offers two ways in:

- a provider from opencode's own list, searched as you type, and your key, or
- what an opencode configuration defines: providers, such as your employer's endpoint with its
  models, and MCP servers. vivibox offers the configurations it finds (one just downloaded to
  `~/Downloads`, `$OPENCODE_CONFIG`, `~/.config/opencode/`, your projects' own `opencode.json`), or
  lets you browse your folders for one, showing only JSON files and saying of each whether it is an
  opencode configuration. You then see everything in it: new entries ticked, one that differs from
  yours unticked until you choose to overwrite it, one you already have shown as the same. Keys,
  and every value of an MCP server's headers and environment, go to vivibox's key store, never to
  its config files.

Every task gets the MCP servers that are on. A remote one is reached from the task's pod; a local
one runs its command inside the pod, so it must be something the pod has (`uvx` and `npx` are at
hand; a program installed only on your machine is not). vivibox brings Serena itself, installed in
the agent image and set up on the task's repository. Its mode is auto unless you set it on or off:
a task gets it when its repository has 100 or more source files in a language Serena reads, where
finding symbols beats reading files; below that it would only add a language server's start and its
tools' descriptions to every turn. The task's panel says whether it got Serena, and why.

`k` in the view opens the settings, one row each, Enter on a row changes it and the file keeps its
comments: *Providers & MCP* first, then what each role runs on by default, the editor `o` opens
with, desktop notifications, and the limits; `tasks_dir` and the address pool are shown, and the
last row opens `config.toml` in your editor for the rest. *Providers & MCP* lists what you have.
From there: *Add provider…*, *Import opencode.json…*, and *Manage…*, where you tick what is on,
set Serena's mode, and remove what you no longer want. A provider turned off offers no models; an
MCP server turned off is given to no task; both keep their keys for when you turn them on again.
The first model you pick for a role becomes its default in config.toml. The same, from a shell:

```bash
vivibox auth set anthropic                                 # asks for the key
vivibox auth import ~/.config/opencode/opencode.json
vivibox models anthropic
```

Keys live in `~/.local/share/vivibox/keys/`, one file per provider, readable only by you.

The messages a task's supervisor prints, on your phone too: `ntfy` under `[notifications]` names
a topic on [ntfy](https://ntfy.sh), the one the app on your phone subscribes to, and every
message goes there as well, high priority when the task waits for you or has stopped. Pick a name
nobody guesses, since anyone who knows it reads the topic; for that reason a message says what
happened, never what the agent or the build said (the whole of it stays in the window and the
view). `ntfy_server` is ntfy.sh unless you run your own; a token, when the topic needs one, is
`vivibox auth set ntfy`. The first message is a word when the supervisor takes the task up
(*started: planning*), so a topic just set up shows it works; `ntfy_events = "all"` adds every
stage after that (implementing, verifying) to the decisions. The three rows are under `k` as well, and a change there
reaches a running task from its next message.

`agent_orchestration_mode`, at the top of the file, says how a task is shared between the
planner, the writer and the reviewer, and where the gate runs (docs/tasks.md, *Orchestration
modes*): `single_agent`, `planner_executor`, `planner_maker_checker` (the default) or
`supervisor_worker`. Roles joined in one agent share one conversation and the first role's model,
so `single_agent` needs a planner on an opencode model and `supervisor_worker` a planner on a
model, not `manual`; a task that cannot run in its mode says so before it starts. The same
setting is a row under `k` and a row under `n`, for one task.

A third role, `[roles.reviewer]`, is the reviewer of `planner_maker_checker`: it reads the work
after a green gate in a container of its own, through opencode, on a model of your choice;
another family than the writer's is what makes the review worth its cost. Without it, the mode
reviews on the writer's model, and the reviewer's row under `k` says so. The other modes have
the writer or the planner review, and read no `[roles.reviewer]`.

`config.toml` also holds the limits: `max_rounds`, the fix turns the writer gets on its own, from
the gate or from the review, before the work comes to you (3; the first implementation is none
of them, and your reply gives them back), and `verify_timeout`, the seconds one
verification command may take (1800) before it is stopped and the task waits for you as on any
failure outside the code. A project file may set its own `verify_timeout`. `cost_warning` and
`cost_limit` are dollars a task may cost, planning and implementation together, before you are
told and before it stops for you: it waits with the figures on its row (*cost limit reached*),
and goes on once you raise the limit under `k` and press `s`. Both are checked before a turn, so
a turn may run past the limit by its own cost; neither is set unless you set it. A file from
before orchestration modes may still have `limits.max_iterations`, `limits.max_reviews` or the
reviewer's `mode`: they are not read, `max_iterations` carries over as `max_rounds` when that is
not set, and the supervisor says so once, in its window and in the task's timeline.

Once per repository you want agents to work on: press `i` in the view, and browse to its folder
(or make a new one there, for a project from scratch), or from a shell inside it:

```bash
cd ~/projects/myproject
vivibox init
vivibox init ~/projects/new-idea --git   # an empty or new folder: starts the repository too
```

Either way vivibox reads the build files and proposes a project: how it is verified, and what a
new task's clone runs first (`prepare`, a build without tests, so the writer starts on a built
project: `bash mvnw -B install -DskipTests`, `bash gradlew assemble`, or the Node install by its
lockfile; empty where the files say nothing, `--prepare` and Change… set it). The verification
is a command you type, or left empty: the next task's writer, who builds the project while it works, then writes
the command it ran to `/task/handoff/verify-proposal.md` (told to allow that build the
verification's own time limit, `verify_timeout`, so its tool's default of two minutes does not
cut a suite short), and before the first verification the
task waits for you as `review the command`: `a` opens the same field as under `e` with the
command in it, Enter keeps it for the project and the gate runs with it; `e` changes it first,
`r` sends the writer back for another. A writer that proposed none leaves the field empty, for
you to type the command or to ask for one; a command that picks some tests (`-Dtest=`, `--tests`,
`-k`, a test file named) is shown with its selection, because a writer verified by its own tests
alone would pass whatever it broke elsewhere. With `--auto` a whole command is kept without
stopping, and a narrowed or missing one still waits for you. The project's next task has the
command and skips this. A command that runs a file the repository's commits do not have (`./mvnw`
after a move to Gradle, a wrapper never committed) leaves the task waiting for you with
"verification could not run", no attempt spent: change it under `e`. A
plan with `verify = false` runs no build in its task and settles nothing: when the task leaves
build files behind, its panel names the command they call for. A repository
with nothing to build or test ever (documents, configuration) says so with `verify = false` in the
project file: the verification then checks the criteria and the commits only. In the view, `e` on
the project's row shows the command as it is, one line that Enter saves, and a box that leaves it
to the writer; `i` asks the same before the project exists. What the build files and the pipeline
run (read from `.github/workflows`, `.gitlab-ci.yml`, `Jenkinsfile`, `bitbucket-pipelines.yml` and
`azure-pipelines.yml`: the steps that start with a build tool, deploys left out) is listed in the
notes of `i` and `vivibox init`, never taken for the verification by itself: a pipeline often
builds with more than its build files say, and a command that builds the wrong thing passes. For a Node project the gate installs the dependencies on the fresh clone first (`npm ci`, or
Yarn or pnpm by the lockfile) when the command does not do that itself, as a command of its own
in the log; a command that starts in a folder (`cd apps/web && yarn test`, a monorepo's app) is
installed for in that folder, by its lockfile; a `package.json` without a lockfile next to it, a
monorepo's root, gets no install, and the notes name each package with a test script instead
(`cd apps/web && yarn install --frozen-lockfile && yarn test`). A tool the fresh clone lacks
(`tsc: not found`) is a failure of the environment, not of the code: the task waits for you to
put the install in front of the command. Otherwise: the command the gate runs (`bash gradlew
test`, `bash mvnw -B verify`, `mvn -B verify`, `npm ci && npm test`, or the same with Yarn or pnpm
when `packageManager` in `package.json` or the lockfile names them; for Python, `pytest` through
uv: `uv run --frozen pytest` with a `uv.lock`, else in a throwaway environment from
`pyproject.toml` or the requirements files) and, when the build needs it,
a JDK other than the image's Java 21, for example Java 17 for Gradle 7. It shows the file and writes
it to `~/.config/vivibox/projects/<name>.toml` only when you confirm. For a folder that is empty or
does not exist yet, it starts a git repository there with a first commit, so an app can be built
from nothing. Nothing is set up by just running `vivibox` somewhere.

The pod comes with Node, npm, Python, uv and Java 21 ready to run (uv's downloads are shared
between tasks, and Python writes no `__pycache__` into the clone), and `mise` installs any other
toolchain a task needs. For Go and Rust, `init` reads the version from `go.mod` (its `toolchain`
line, else its `go` line from Go 1.21 on; an older `go` line was only a floor, so the latest Go)
or `rust-toolchain.toml`, and for bun from `packageManager` in `package.json`, else the latest,
and writes it to the project file as `tools = ["go@1.25.3"]`: the pod installs it for the agent
and the gate, into caches shared between tasks, with `go test ./...`, `cargo test` or
`bun run test` as the suggested verification. Nothing is
written into your repository. A project that wants different versions of what the image has, or
another language, can also commit its own `mise.toml`: the agent proposes it, the gate reads it,
and you approve it as a build file before it reaches your checkout.

`e` on the project's row opens its settings: what a new task runs first, the verification, how `v`
runs it, its JDK, the variables its build needs from your shell, and the editor for its review
copies, each written to the file alone with the comments kept. The last row opens the file itself, for services on your
host the agent may reach and for extra risky patterns:

```toml
repo = "~/projects/myproject"
verify = ["bash gradlew test --no-daemon --console=plain"]
java = "17"                                     # empty for Java 21
host_services = ["host.docker.internal:5432"]   # optional, network access to services on your host
pass_env = ["REPO_TOKEN"]                       # optional, variables passed from your shell
prepare = ["bash mvnw -B install -DskipTests"]  # optional, run once in a new task's clone
tools = ["go@1.25.3"]                           # optional, toolchains the image does not have
```

`prepare` is for a project whose whole build takes long: the commands run once in a new task's
clone while the plan is made, and the writer's first turn waits for them, so the writer builds the
module it changes instead of everything. Their output is under `l`; a failure is on the task's
timeline and the task goes on without it. A stop and a start do not run them again, unless the stop
cut them short. Maven keeps what a task installs apart from other tasks: modules one task installs
never reach another task's build, while what Maven downloads is shared. With Maven's build cache
extension, the verification keeps a cache of its own: a later attempt reuses what an earlier one
built from the committed work, never what the agent built. The agent itself builds without that
cache (`maven.build.cache.enabled=false` in its `MAVEN_OPTS`): a cache hit would replay only the
phases after the one an earlier command reached, skipping what `initialize` sets for the tests,
such as Mockito's agent path, and the test JVM would not start.

`pass_env` is for what a build reads from its environment, such as a package registry token that a
settings file in the repository refers to (`${env.REPO_TOKEN}` in Maven's settings.xml). The agent
and the gate get the values from the shell you start vivibox in, so set them first, for example
with your login command: `my_login && vivibox`. A task will not start while one is missing. The
agent can read them, since it runs the build, so pass a token that only reads packages. Unlike
model keys, they are in the containers' environment, where `docker inspect` shows them. The gate's
log shows `***` in their place. A token that expires needs a fresh start: log in again, start
vivibox from that shell, and stop and start the task with `s`; a task that got blocked on it is
verified again with `g`.

Every pod has a Docker of its own, so two tasks that run the same database in their tests download
its image twice. With `hub_mirror = true` under `[network]` in `config.toml`, vivibox starts one
pull-through cache of Docker Hub on your Docker, `vivibox-mirror`, and every pod's daemon pulls through
it: the first task downloads the image, the next ones take it from your machine. A pod started before
you turned it on uses it from its next start. It saves the download, not the unpacking, so it pays
off on a slow or metered connection (a VPN) more than on a fast one. It serves Docker Hub only;
quay.io, ghcr.io and other registries are pulled as before. `vivibox mirror` says whether it runs
and how much it holds; it forgets a layer nobody pulled for two weeks, and `vivibox mirror remove`
removes it with everything it holds. If it is down, a pod pulls from Docker Hub itself. It listens on
port 5055 of the address the pods reach your machine by; `hub_mirror_port` changes it.

A repository you move or delete leaves its project file behind. vivibox does not offer a project it
cannot work in: it names those on start and offers to forget them, which removes the project file
and nothing else.

## Yarn downloads

The agent and verification share downloaded Yarn packages in `/cache/yarn`; deleting a task
keeps this cache. `node_modules` and other installed project files stay in the task's clone,
and verification installs them again in a fresh clone.

Yarn Classic defaults to `/cache/yarn/classic` through `/usr/local/etc/yarnrc` in the image. Your own
`cache-folder`, CLI flag or `YARN_CACHE_FOLDER` can override it. Yarn 2 and later use
`YARN_GLOBAL_FOLDER=/cache/yarn/berry` for the global cache or the download mirror of a local
cache. vivibox leaves `enableGlobalCache`, `cacheFolder` and `enableMirror` to the project,
so checked-in caches and zero-installs keep working. A project that chooses a local cache and
disables its mirror will still download again unless those archives are committed.

After updating vivibox, run `vivibox image build`, then stop and start existing tasks to use
the new image and mount. `u` or `vivibox usage` shows the shared caches separately from tasks.

---
Back to the [README](../README.md).
