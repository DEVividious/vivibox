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

`k` in the view, *Providers & MCP*, lists what you have. From there: *Add provider…*, *Import
opencode.json…*, and *Manage…*, where you tick what is on, set Serena's mode, and remove what you
no longer want. A
provider turned off offers no models; an MCP server turned off is given to no task; both keep their
keys for when you turn them on again. The first model you pick for a role becomes its default in
config.toml. The same, from a shell:

```bash
vivibox auth set anthropic                                 # asks for the key
vivibox auth import ~/.config/opencode/opencode.json
vivibox models anthropic
```

Keys live in `~/.local/share/vivibox/keys/`, one file per provider, readable only by you.

`config.toml` also holds the limits: `max_iterations`, how many verification failures the
writer may fix on its own before the task stops for you (3), and `verify_timeout`, the seconds one
verification command may take (1800) before it is stopped and the task waits for you as on any
failure outside the code. A project file may set its own `verify_timeout`.

Once per repository you want agents to work on: press `i` in the view, and browse to its folder
(or make a new one there, for a project from scratch), or from a shell inside it:

```bash
cd ~/projects/myproject
vivibox init
vivibox init ~/projects/new-idea --git   # an empty or new folder: starts the repository too
```

Either way vivibox reads the build files and proposes a project. For a repository that has no code
yet, leave the command empty: the first plan you accept that names a command sets how the project
is built and tested, and vivibox keeps it for the next task; accepting that plan shows you the
command first. A plan with `verify = false` runs no build in its task and settles nothing: a new
product's first task has nothing to build until the writer makes it, and when the task leaves
build files behind, its panel names the command they call for and where to pick it. A repository
with nothing to build or test ever (documents, configuration) says so with `verify = false` in the
project file: the verification then checks the criteria and the commits only. In the view, `e` on the project's row picks between the commands the build files name, what
the pipeline runs (read from `.github/workflows`, `.gitlab-ci.yml`, `Jenkinsfile`,
`bitbucket-pipelines.yml` and `azure-pipelines.yml`: the steps that start with a build tool,
deploys left out), no build, and one of your own; `i` offers the same before the project exists,
and `vivibox init` lists what the pipeline runs in its notes. Otherwise: the command the gate runs (`bash gradlew
test`, `bash mvnw -B verify`, `mvn -B verify`, `npm ci && npm test`, or the same with Yarn or pnpm
when `packageManager` in `package.json` or the lockfile names them) and, when the build needs it,
a JDK other than the image's Java 21, for example Java 17 for Gradle 7. It shows the file and writes
it to `~/.config/vivibox/projects/<name>.toml` only when you confirm. For a folder that is empty or
does not exist yet, it starts a git repository there with a first commit, so an app can be built
from nothing. Nothing is set up by just running `vivibox` somewhere.

The pod comes with Node, npm, Python, uv and Java 21 ready to run, and `mise` installs any other
toolchain a task needs. A project that wants different versions, or another language, commits its
own `mise.toml`: the agent proposes it, the gate reads it, and you approve it as a build file
before it reaches your checkout.

Edit the file to add services on your host the agent may reach, or variables the build needs:

```toml
repo = "~/projects/myproject"
verify = ["bash gradlew test --no-daemon --console=plain"]
java = "17"                                     # empty for Java 21
host_services = ["host.docker.internal:5432"]   # optional, network access to services on your host
pass_env = ["REPO_TOKEN"]                       # optional, variables passed from your shell
```

`pass_env` is for what a build reads from its environment, such as a package registry token that a
settings file in the repository refers to (`${env.REPO_TOKEN}` in Maven's settings.xml). The agent
and the gate get the values from the shell you start vivibox in, so set them first, for example
with your login command: `my_login && vivibox`. A task will not start while one is missing. The
agent can read them, since it runs the build, so pass a token that only reads packages. Unlike
model keys, they are in the containers' environment, where `docker inspect` shows them. The gate's
log shows `***` in their place. A token that expires needs a fresh start: log in again, start
vivibox from that shell, and stop and start the task with `s`; a task that got blocked on it is
verified again with `g`.

A repository you move or delete leaves its project file behind. vivibox does not offer a project it
cannot work in: it names those on start and offers to forget them, which removes the project file
and nothing else.

---
Back to the [README](../README.md).
