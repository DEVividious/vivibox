# What vivibox protects against

The agent is isolated in both directions: it cannot reach your machine, and nothing it writes runs
on your machine until you have approved it.

- **The agent is not trusted.** It runs as your UID without capabilities, on a read-only root
  filesystem, in a pod whose Docker daemon runs under Sysbox. A privileged container the agent
  starts cannot reach your host. The pod cannot reach your host or LAN, except services you list.
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
- **Keys stay out of images, volumes and `docker inspect`.** The agent pod gets the provider keys needed by its configured roles and enabled MCP secrets,
  mounted as read-only files on tmpfs. The separate review container gets the reviewer's provider
  key and enabled MCP secrets. These files are readable by the agents that use them.

## How the pod is built

Each task gets a pod: an unprivileged agent container and a Docker daemon in a sidecar that runs
under [Sysbox](https://github.com/nestybox/sysbox). The agent container runs as your UID with every
capability dropped, `no-new-privileges` and a read-only root filesystem; it writes only to the task's
clone, its home and `/tmp`, and `/tmp` is a tmpfs of half the container's memory limit (2 GiB by
default), so what an agent leaves there cannot take the host's memory. Your replies to the agent are mounted read-only from the task's own folder,
where they are read (`/task/handoff/comments.md`), in the agent's container and the reviewer's, so the agent
cannot change what you told it. The Docker socket lives in a volume the two share and never on your host.
Each container of the pod is bounded by its cgroup: memory with swap bounded to the same figure
(`--memory`, `--memory-swap`), processes (`--pids-limit`) and the pod's CPUs, all under the task's
slice (`--cgroup-parent`), never with the OOM killer turned off; `[limits]` in `config.toml` and
`vivibox limits` set them. The pod's Docker data, the agent's home, what a build installs and the
verification's and the reviewer's clones are directories of a disk image of the task's (an XFS file
in the task's folder, attached by the root helper on a loop device mounted `nosuid,nodev`, not
`noexec`: what a build installs there, and the agent's tools, are run from it),
whose size is the most the task writes there; the clone is outside it.
Testcontainers in the pod runs Ryuk 0.12.0, whatever the project's library would pick
(`TESTCONTAINERS_RYUK_CONTAINER_IMAGE`): the Ryuk of older libraries prunes images when it cleans up
after a test, and the pod's daemon then loses the image another test is still pulling, at random. A
project that needs another Ryuk passes the variable in `pass_env`.
Rules on your host, keyed by the bridge of the task's network and loaded before the pod's first
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

## Limits of the boundary

Sysbox containers share the host kernel; they are not virtual machines. The host's rules block
host and private-network access except explicitly allowed services, but public internet access
remains open, and the DNS forwarder applies no policy: any name resolves. The agent can read credentials passed to its build in `pass_env`.

The check of the task's clone guards the git vivibox runs, at the moment it runs it. Git you run
yourself in the clone, or an IDE you open on it, is not checked: open the review copy instead, which
holds only what was committed.

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
