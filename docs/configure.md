# Configuring vivibox

## Providers and models

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

## Notifications

Desktop notifications (`notify-send`) when a task waits for you are off until you turn them on:
`desktop = true` under `[notifications]`, or Notifications under `k`. Without them the list
rings the terminal's bell when a task starts to wait.

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

## Flows and limits

`agent_orchestration_mode`, at the top of the file, says how a task is shared between the
planner, the writer and the reviewer, and where the gate runs ([flow details](tasks.md#orchestration-modes)): `single_agent`,
`planner_executor`, `planner_writer_reviewer` (the default; `planner_maker_checker`, its former
name, is read too) or `supervisor_worker`. Roles joined in one agent share one conversation and
the first role's model, so `single_agent` needs a planner on an opencode model and
`supervisor_worker` a planner on a model or in an agent's CLI (`--plan-in-cli`), not in a
browser; a task that cannot run in its mode says so before it starts. The same
setting is a row under `k` and a row under `n`, for one task.

A third role, `[roles.reviewer]`, is the reviewer of `planner_writer_reviewer`: it reads the work
after a green gate in a container of its own, through opencode, on a model of your choice;
another family than the writer's is what makes the review worth its cost. Without it, the mode
reviews on the writer's model, and the reviewer's row under `k` says so. The other modes have
the writer or the planner review, and read no `[roles.reviewer]`.

`config.toml` also holds the limits: `max_rounds`, the fix turns the writer gets on its own, from
the gate or from the review, before the work comes to you (3; the first implementation is none
of them, and your reply gives them back), and `verify_timeout`, the seconds one
verification command may take (1800) before it is stopped and the task waits for you as on any
failure outside the code. A project file may set its own `verify_timeout`. `cost_warning` and
`cost_limit` use the total reported task cost, including review, to decide when you are
told and when it stops for you: it waits with the figures on its row (*cost limit reached*),
and goes on once you raise the limit under `k` and press `s`. These checks run before planning
and implementation steps, not before every model call. A turn, its self-review or a separate
review can take the total past the limit; neither is set unless you set it. A file from
before orchestration modes may still have `limits.max_iterations`, `limits.max_reviews` or the
reviewer's `mode`. `max_reviews` and `mode` are ignored; `max_iterations` supplies `max_rounds`
only when the latter is absent. The supervisor reports these old fields once, in its window
and in the task's timeline.

## Project setup

Once per repository you want agents to work on: press `i` in the view, and browse to its folder
(or make a new one there, for a project from scratch), or from a shell inside it:

```bash
cd ~/projects/myproject
vivibox init
vivibox init ~/projects/new-idea --git   # an empty or new folder: starts the repository too
```

vivibox reads the build files and proposes preparation commands, toolchains and notes about
verification. It shows the project file and writes it to
`~/.config/vivibox/projects/<name>.toml` only when you confirm. A new or empty folder gets a Git
repository with an initial commit. An existing repository needs your Git identity configured
(`user.name` and `user.email`) before its first task. Merely launching `vivibox` does not register
the current folder.

### Choosing verification

Type a verification command during project setup, or leave it to the first task's writer.
The notes list commands found in build files and CI pipelines; these are evidence to inspect,
not commands vivibox automatically trusts. Large Maven reactors can get a scoped command;
see [scoped verification](#scoped-verification-and-preparation).

When the writer proposes a command, the task stops at **review the command** before its first
verification. `a` opens the command field; Enter keeps it for future tasks in the project.
`e` changes it and `r` asks the writer for another proposal. A missing proposal leaves the field
empty for you to fill. Commands selecting only some tests or modules are labelled as narrowed:
a passing subset can miss regressions elsewhere. `--auto` keeps a whole command without stopping;
a narrowed or missing command still waits for you.

`e` on a project's row changes its verification later. If the command refers to a file missing
from the commits (an uncommitted wrapper, for example), the task waits with **verification could
not run**, without spending a fix round. Correct the command and verify again.

Use `verify = false` in a task's plan for work that needs no build. In a project file it means
that repository has nothing to build or test. The other mechanical checks still run; a task
that gains build files without a verification command names them in its details.

### Dependencies and toolchains

Preparation (`prepare`) runs once in the task clone: for example, `./mvnw -B install -DskipTests`,
`./gradlew assemble`, or the Node install selected by the lockfile. It runs during planning,
and the writer waits for it before implementation. `--prepare` or **Change…** during setup
sets your own commands.

Verification uses a fresh clone. For Node projects, vivibox adds the dependency install
(`npm ci`, Yarn or pnpm according to the lockfile) when the command does not already do so.
A command such as `cd apps/web && yarn test` installs in that folder. Without a lockfile there,
no install is inferred: include it in the verification command. A missing tool such as `tsc`
is treated as an environment problem and waits for you.

Build-file notes also show Maven/Gradle wrappers and Python verification through uv:
`uv run --frozen pytest` with `uv.lock`, otherwise a temporary environment from the project's
metadata or requirements. A Maven wrapper without its executable bit is invoked as
`bash ./mvnw`. Choose a different JDK in the project settings when its build needs one.

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
verify = ["./gradlew test --no-daemon --console=plain"]
java = "17"                                     # empty for Java 21
host_services = ["host.docker.internal:5432"]   # optional, network access to services on your host
pass_env = ["REPO_TOKEN"]                       # optional, variables passed from your shell
prepare = ["./gradlew assemble"]                # optional, once per new task clone
# tools = ["go@1.25.3"]                        # for a Go project; installed by mise
```

## Scoped verification and preparation

`{modules}` in `verify` builds only the modules a task changes, for a project of several:
`verify = ["./mvnw -B -pl {modules} -am verify"]`. Build tools that name modules their own way take
`{modules:FORMAT}`, each module written by FORMAT (`%s` its folder, `%p` Gradle's project path)
and joined by spaces: `./gradlew {modules:%p:check}` runs `:core:check :web:api:check`,
`npm test {modules:--workspace=%s}` and `pnpm {modules:--filter=./%s} test` their workspaces; the
whole build is `./gradlew check`, `npm test --workspaces`, `pnpm -r test`. A task's plan names the directories it changes
(`modules = ["core"]` in its header; a plan from a chat or an agent's CLI gives a
`Modules: core, app` line), you see them when you accept the plan, and each verification runs the
command with them and with any other module its commits changed, comma-joined. A module is the
nearest folder above a file with a build file of its own (`pom.xml`, `build.gradle(.kts)`, a
workspace's `package.json`). Only a change in no module, the root's build file included, runs the
same command without them (`./mvnw -B verify`), the whole build; the task's timeline says which
modules came from the changes, or which files took it whole. `vivibox init` writes this command
for a Maven or Gradle project of three modules or more (an npm or pnpm one gets its test command
from the writer, which the box then narrows), and the verification dialog (`i`, `e`) offers it
as a box, "build only the modules a task changes", from two. The whole build otherwise runs once
before the work comes to you when `whole_build_before_review` is on (under `k`, "whole build
before review"; off by default, when the project's pipeline builds it whole after a push).

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

## Build credentials

`pass_env` is for what a build reads from its environment, such as a package registry token that a
settings file in the repository refers to (`${env.REPO_TOKEN}` in Maven's settings.xml). The agent
and the gate get the values from the shell you start vivibox in, so set them first, for example
with your login command: `my_login && vivibox`. A task will not start while one is missing. The
agent can read them, since it runs the build, so pass a token that only reads packages. Unlike
model keys, they are in the containers' environment, where `docker inspect` shows them. The gate's
log shows `***` in their place. A token that expires needs a fresh start: log in again, start
vivibox from that shell, and stop and start the task with `s`; a task that got blocked on it is
verified again with `g`.

## Docker image downloads

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
