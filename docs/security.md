# What vivibox protects against

The agent is isolated in both directions: it cannot reach your machine, and nothing it writes runs
on your machine until you have approved it.

- **The agent is not trusted.** It runs as your UID without capabilities, on a read-only root
  filesystem, in a pod whose Docker daemon runs under Sysbox. A privileged container the agent
  starts cannot reach your host. The pod cannot reach your host or LAN, except services you list.
  The reviewer reads the work from a network of its own, which the pod cannot reach and which
  cannot reach the pod.
- **The writer cannot change what the planner remembers.** Where a flow has a planner other than
  the writer, the planner (in the Supervisor ⇄ Worker flow, the same conversation reviews the
  work) runs in a container and a network of its own: its conversation store, its server, its
  password and its key are there and nowhere the writer's container, or a container the writer
  starts, mounts or reaches.
- **Nothing the agent writes runs on your host without your approval.** The task clone's
  `.git/config` and hooks are read-only for the agent, so git on your host, run by you or by your
  IDE, cannot execute agent-written commands. Files that run code on IDE import or in your
  shell (`pom.xml`, `.mvn/`, Gradle files, `package.json` and lockfiles, `Cargo.toml`, `build.rs`,
  `.cargo/`, `Cargo.lock`, `AGENTS.md`, `.idea/`, `.vscode/`, `.envrc`, git
  hooks, nested repositories…) need your approval whenever they change. So does test
  configuration (`vitest.config.*`, `jest.config.*`, `pytest.ini`, `pyproject.toml`, `conftest.py`…),
  which can leave tests out without any of the words the gate looks for.
- **The gate checks what the agent claims:** your verify commands, the acceptance criteria of the
  plan you accepted, commit subjects of at most 72 characters, without co-author or AI signatures, tests switched
  off in added lines (`@Disabled`, `skipITs`, `it.skip` and the like), invisible Unicode
  characters in added lines, and that every test file added or changed is named in the agent's
  `red.md`. Tests the work removed are counted for you at review. When a verification fails, the task's details show the lines of the
  build log that say why, and where the whole log is.
- **A task's use of your machine is bounded.** Every container of its pod runs with a memory
  limit (swap bounded to the same figure), a process limit and the pod's CPUs, under one cgroup
  slice of the task's that counts what the pod's own Docker runs as well; what the task writes
  outside its clone is on a disk image of its own, whose size bounds it, so a full disk is the
  task's and not your machine's. The pod's total memory and processes are a budget the
  containers' limits are checked against, not a limit of the slice itself.
- **Keys stay out of images, volumes and `docker inspect`.** Each container gets the keys of the
  role that runs in it, mounted as read-only files on tmpfs: the agent's container the writer's
  provider key and enabled MCP secrets, the planner's control container the planner's, the
  review container the reviewer's. A Single agent task has the planner's and the writer's in the
  agent's container. A provider two roles use is in both their containers. These files are
  readable by the agents that use them.

## How the pod is built

Each task gets a pod: an unprivileged agent container and a Docker daemon in a sidecar that runs
under [Sysbox](https://github.com/nestybox/sysbox). The agent container runs as your UID with every
capability dropped, `no-new-privileges` and a read-only root filesystem; it writes only to the task's
clone, its home and `/tmp`, and `/tmp` is a tmpfs of half the container's memory limit (2 GiB by
default), so what an agent leaves there cannot take the host's memory. Your replies to the agent are mounted read-only from the task's own folder,
where they are read (`/task/handoff/comments.md`), in the agent's container, the planner's and the
reviewer's, so the agent cannot change what you told it. The Docker socket lives in a volume the two share and never on your host.
Each container of the pod is bounded by its cgroup: memory with swap bounded to the same figure
(`--memory`, `--memory-swap`), processes (`--pids-limit`) and the pod's CPUs, all under the task's
slice (`--cgroup-parent`), never with the OOM killer turned off; `[limits]` in `config.toml` and
`vivibox limits` set them. The pod's Docker data, the agent's home, what a build installs and the
verification's clone and the reviewer's copy of the commits are directories of a disk image of the task's (an XFS file
in the task's folder, attached by the root helper on a loop device mounted `nosuid,nodev` with
`discard`, not `noexec`: what a build installs there, and the agent's tools, are run from it),
whose size is the most the task writes there; the clone is outside it.
The dependency caches the tasks share (`/cache/m2`, `/cache/gradle`, `/cache/npm`, `/cache/yarn`,
`/cache/mise`, `/cache/corepack`, `/cache/uv`, `/cache/go`, `/cache/cargo`, `/cache/rustup`) are
writable for the agent and mounted read-only in the gate's container: the verification runs nothing
from them and writes nothing into them. Its `PATH` is the image's without mise's shims from the
shared cache (a program the agent put there would otherwise be its `git`, its `python`); its
`git`, `mvn`, `java`, `node`, `python` and `uv` are the image's, unless the checkout's own
`mise.toml` or the project's `tools` pin one: then the verification installs it into a directory of
its own and runs that one, the project's choice from a file you approve (the clone and its
submodules are made before anything is installed); its Gradle home, Maven repository, npm, Yarn,
corepack, uv, Go and Cargo caches are directories of its own under the gate's directory on the
task's disk, made fresh for every verification, so no init script, JVM option or configuration the
agent left in a shared cache is read. What a tool can read from the shared cache without writing it
reads: Maven asks a file repository of the shared downloads before the project's repositories,
Gradle reads the shared dependency cache through its read-only cache; the rest is fetched again.
A tool a project brings through mise (`tools`, its own `mise.toml`) is installed by the gate into a
mise data directory of its own. One residual risk stays, and is accepted for now: a
project JDK (`java` in the project's settings) is the one the agent's mise installed into the shared
cache, found there read-only by `mise where` and run by the gate through `JAVA_HOME`, so a writer
who replaced that JDK's binaries is run by the verifications of every task that pins the same JDK,
a risk shared between tasks of one trust level; a cache per trust level closes it later.
Testcontainers in the pod runs Ryuk 0.12.0, whatever the project's library would pick
(`TESTCONTAINERS_RYUK_CONTAINER_IMAGE`): the Ryuk of older libraries prunes images when it cleans up
after a test, and the pod's daemon then loses the image another test is still pulling, at random. A
project that needs another Ryuk passes the variable in `pass_env`.
Rules on your host, keyed by the bridges of the task's networks and loaded before the pod's first
packet, refuse your host, every private network, other tasks' ranges, IPv6 and UDP other than DNS,
except the services you list per project; the internet is open. Root in the pod cannot remove them:
they are not in its network namespace. Every operation of vivibox that starts a container of a
pod applies them first and does not start it when the root helper refuses: the pod at `vivibox
start` and `resume`, the gate's container before a verification and the reviewer's before a
review. The rules and `vivibox0` do not survive a restart of your machine, and no pod container
starts by itself after one. A pod container you start by hand with `docker start` after a restart
runs on a bridge Docker made again, with no rules of vivibox's until the task's next `vivibox
start`: do not start pod containers by hand. Every DNS query to port 53, UDP or TCP, whatever server it
names, goes to a forwarder on your host, which passes it to your host's resolver; while it is down,
the pod resolves nothing that way. DNS over TLS (port 853) is refused. DNS over HTTPS is HTTPS to
the rules, and is not prevented until names are allowed by policy. When the host
reaches the internet through a tunnel smaller than Ethernet (a VPN such as Cloudflare WARP), the
rules clamp TCP MSS to fit it, so large downloads do not stall. The pod trusts the certificate
authorities your host trusts: the host's CA bundle is mounted read-only over the sidecar's, the
agent's and the gate's, so a registry or a proxy behind a corporate authority works in the pod as
it does on your host. The sidecar remembers the bundle it started with: when the host's changes,
as it does when a VPN client installs a new corporate authority, the next start of the task makes
the sidecar again, because its Docker daemon reads the authorities once, at its start.

The reviewer's container is in a network of the task's own, apart from the writer's. The sidecar,
the agent, the verification's container and every container the pod's Docker runs are on the
writer's network, in the sidecar's network namespace; the reviewer is on its own bridge, at an
address allocated for it, with rules of its own on your host, loaded before it starts. Each
network's rules refuse the other's range, so nothing the writer runs, a nested container with
`NET_RAW` or `NET_ADMIN` included, sees a packet of the reviewer's or reaches its container, and the
reviewer reaches nothing of the writer's; vivibox talks to each through `docker exec`, never through
the task's networks. A project's `host_services` are let through for the writer's network alone:
the reviewer reaches none of your machine's services. Its DNS goes to the forwarder like the
pod's, `host.docker.internal` there is its own network's gateway, and its internet is open like
the writer's (it reaches its provider over TLS); nothing narrows it until names are allowed by
policy.

Where a flow has a planner other than the writer (Planner → Executor, Planner → Writer →
Reviewer, Supervisor ⇄ Worker), the planner runs in a control container of its own,
`vivibox-<id>-control`, for the whole task; in the Supervisor ⇄ Worker flow the same conversation
reviews the work there. It is made like the reviewer's: your UID, no capability, a read-only
root, no Docker socket, none of the build's caches and nothing a build installed, under the
task's slice with the `control_*` limits. What is its own: a home on the task's disk, where its
harness keeps the conversation from turn to turn; an opencode server on the localhost of its own
network namespace, with a password of its own; a directory of secrets with its provider's key and
its MCP servers' secrets; its harness files; and a third network of the task's, the planner's, on
a bridge of its own with rules of its own on your host, which refuse the writer's range and the
reviewer's as theirs refuse its, and let through none of your machine's services. The agent's
container mounts neither that home nor those secrets, and nothing on the writer's network (the
agent, the sidecar, the verification, a nested container with any capability) has a route to the
planner's container or sees its packets; vivibox talks to it through `docker exec`. So the writer,
a process it left running between turns included, cannot write a message into the planner's
conversation, call the planner's server, or read its password or, when the two use different
providers, its key. The planner reads the work in a copy of the task's commits that vivibox makes
on your host, from the clone once it is as recorded, into a directory of the task's disk mounted
read-only in the control container at the path the writer has its clone; the writer's clone itself
is not mounted there, so git in the planner's container never reads a `.git` the writer can
write, and uncommitted work is not what the planner reads. Git on your host works in that copy with none of your git configuration (no global or
system file, no submodules), so no filter or setting of yours runs on what the writer
committed; for a review the copy stands on the commit the verification passed, and a verified
commit that is not on the task's branch stops the task. The task's review folder, where the planner or the reviewer writes its review and vivibox
reads it, is read-only in the agent's container in the flows where another agent than the
writer's reviews. A Single agent task has no control container and no planner's network: one
agent plans and writes in one conversation.

What the planner and the writer still share is the handoff folder (`/task/handoff`), which both
write: the writer, or a process it left running, can replace the planner's `plan-draft.md` or
`question.md` before vivibox reads it. That is not the planner's history, and the draft comes to
you to read at the plan's checkpoint; it is a known limit. The planner also reads the
repository, which is the writer's work: what a file there says is data to it, by its brief, not
by enforcement. What is enforced is where the planner's harness works and what it is configured
with. It starts in a directory vivibox made, `/work`: a git repository with one empty commit
and nothing else, on the task's disk, read-only in the container. The copy of the commits is
beside it and is read as data, by its path; it is never the harness's working directory, and
Claude Code there is given no directory besides its own. A planner on opencode runs on the
configuration vivibox writes for it: its language servers are off and none is downloaded, and
it carries the MCP servers you brought and never vivibox's own Serena, whose language servers
would work on the writer's commits beside the planner's key and conversation. A planner on
Claude Code gets no configuration and no MCP server from vivibox: its brief, its key and a
home of its own. Git in the planner's container answers for the copy from wherever it is run
(`GIT_DIR` and `GIT_WORK_TREE` in its environment); the copy's `.git` is vivibox's and
read-only there.

The reviewer reads the same work, and the same is enforced for it. Its harness starts in a
directory vivibox made for the round, `/work`: a git repository with one empty commit and
nothing else, on the task's disk, read-only in the review container. The work is beside it and
is read as data, by its path, never as the harness's working directory: a copy of the task's
commits that vivibox makes on your host for every round, from the clone once it is as recorded,
with none of your git configuration, on the commit the verification passed, mounted read-only
at the path the writer has its clone. The writer's clone itself is not mounted in the review
container and no git runs there against it; git there answers for the copy from wherever it is
run (`GIT_DIR` and `GIT_WORK_TREE` in its environment), and the copy's `.git` is vivibox's and
read-only there. The reviewer runs on opencode, on the configuration vivibox writes for it: its
language servers are off and none is downloaded, and it carries the MCP servers you brought and
never vivibox's own Serena. A copy that cannot be made (a clone that is not as recorded, a
verified commit that is not on the task's branch) stops the review with the reason, and no
review container is started: the reviewer is never run in the writer's tree instead. Its home
and its scratch folder are the container's own memory, new every round, and its secrets are
mounted in that container alone.

Both copies, the planner's and the reviewer's, are made the same way. Their `.git` is made
without git's templates and holds no hook. A symbolic link the writer committed is checked out
there as a plain file holding the path it names (`core.symlinks=false` in the copy's own
configuration), so a link to a file of the reader's container, its secrets among them, is not
followed when the planner or the reviewer reads it. After every refresh a copy holds the files
of its commit and nothing else: what is untracked or ignored there is removed. Git on your host
works on a copy with that folder named as its repository, whatever `GIT_DIR` your shell
carries, and fetches nothing lazily for it (`GIT_NO_LAZY_FETCH=1`), whatever remote the
writer's clone names. And git in both
containers takes a directory for a repository only when it is pointed at it
(`safe.bareRepository=explicit`, set in their environment): a folder committed to look like a
bare repository is not one to a git command there that was not given `--git-dir`. The
reviewer's round is made of the commit the verification passed and of nothing else: for a task
that records none, no reviewer is started, and the task is verified first.

The writer can read, and not write, what vivibox keeps of the planner's work under `/task`: the
planner's session id in the task's state and the transcripts of its turns in the task's log.
Knowing them gives no way to the conversation: the store and the server are in the planner's
container.

With `hub_mirror = true` in `config.toml`, the pods' daemons pull Docker Hub's images through one
pull-through cache on your Docker, `vivibox-mirror`. What the tasks share there is downloads, not
daemons: each pod keeps its own images, containers and volumes. The cache listens only on the address
the pods know your host by (`network.host_address`), and the rules let through that one port, as for
a host service.
A task can pull from it and nothing else: a registry that proxies refuses pushes, and deletes are off.
It has no credentials of its own, so what it keeps is what anyone may pull from Docker Hub.
Deleting a task never touches it.

The task's clone is a real `git clone`, so your repository's `.git` never enters the pod. In the
clone, `.git/config` and `.git/hooks/` are read-only for the agent, and the project's own hooks reach
git through `core.hooksPath` in the agent's global config, not through files the agent can edit.
Git follows more of a `.git` than those two to other configs, hooks and object stores (`commondir`,
`gitdir`, alternates, `config.worktree`, `HEAD` as a link, a submodule's `.git`, a repository listed
in the index). vivibox records them when it makes the clone, and before every git command it runs
on your machine in the clone, and before the verification's and the review's copies of it, checks
that each is as recorded; when one is not, it runs no git there. Alternates are read-only in the pod.
Every git command vivibox runs on your machine, in the clone, in your repository and in the
copies alike, runs without the git variables of your shell (every `GIT_*` but
`GIT_TERMINAL_PROMPT`), so a `GIT_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY` or
`GIT_CONFIG_COUNT` exported there neither aims it at another repository of yours nor hands it
configuration; your git configuration is read from the files where git keeps it, never from
one `GIT_CONFIG_GLOBAL` names, and it alone says who a commit is by. The one git vivibox sends
to your remotes, the fetch of a new clone's submodules from the URLs named by the
`.gitmodules` of the commit the task starts from, is also handed how you reach them
(`GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_SSH_VARIANT`, `GIT_ASKPASS`, `GIT_SSL_CAINFO`,
`GIT_SSL_CAPATH`, `GIT_SSL_CERT`, `GIT_SSL_KEY`, `GIT_PROXY_COMMAND`), and never
`GIT_ALLOW_PROTOCOL` or `GIT_SSL_NO_VERIFY`; and only when that commit is your own, on the
branch you are on or reachable from a ref of yours other than `refs/vivibox/`, where a task's
work waits until you accept it. No later git on what the agent wrote is handed them. Bringing
a task's commits to your repository fetches from the task's clone alone, without going into
your submodules, and accepting work is refused while a submodule commit it keeps is kept by
no repository of yours, since accepting deletes the task's clone. That check reads the
`.gitmodules` of the work, which the agent may have written, as data: without its includes,
with nothing fetched, within a time limit.

## Limits of the boundary

Sysbox containers share the host kernel; they are not virtual machines. The host's rules block
host and private-network access except explicitly allowed services, but public internet access
remains open, and the DNS forwarder applies no policy: any name resolves. The agent can read credentials passed to its build in `pass_env`.

The check of the task's clone guards the git vivibox runs, at the moment it runs it. Git you run
yourself in the clone, or an IDE you open on it, is not checked: open the review copy instead, which
holds only what was committed.

A checkout that is a partial clone (`git clone --filter=…`) is not supported as a project: a
task's clone would lack the objects your checkout lacks, and the git vivibox runs in your
checkout is not handed how you reach its promisor remote.

The gate checks the accepted checklist and whether changed test files are named in `red.md`;
it does not independently prove that each criterion is satisfied or that the recorded test
actually failed before the fix. Code review and your final acceptance still matter. Once you
approve risky files and run the review copy in your IDE, that code runs with your permissions.

The agent chooses file names and what the files say. Wherever vivibox shows them to you (the
risky files' diffs, `vivibox risky`, the details panel, the messages that name a file), a
control or invisible character (ESC, a carriage return, a bidi override, a zero-width space) is
shown as its code point, `‹U+001B›`, and `‹` and `›` themselves the same way, so no line is
hidden from you and no text is drawn that the file does not hold. What you approve is still the
file's bytes, not what is shown.

---
Back to the [README](../README.md).
