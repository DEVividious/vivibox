# What vivibox protects against

The agent is isolated in both directions: it cannot reach your machine, and nothing it writes runs
on your machine until you have approved it.

- **The agent is not trusted.** It runs as your UID without capabilities, on a read-only root
  filesystem, in a pod whose Docker daemon runs under Sysbox. A privileged container the agent
  starts cannot reach your host. The pod cannot reach your host or LAN, except services you list.
- **Nothing the agent writes runs on your host without your approval.** The task clone's
  `.git/config` and hooks are read-only for the agent, so git on your host, run by you or by your
  IDE, cannot execute agent-written commands. Files that run code on IDE import or in your
  shell (`pom.xml`, `.mvn/`, Gradle files, `package.json`, `.idea/`, `.vscode/`, `.envrc`, git
  hooks, nested repositories…) need your approval whenever they change. So does test
  configuration (`vitest.config.*`, `jest.config.*`, `pytest.ini`, `pyproject.toml`, `conftest.py`…),
  which can leave tests out without any of the words the gate looks for.
- **The gate checks what the agent claims:** your verify commands, the acceptance criteria of the
  plan you accepted, one-line commit messages without co-author or AI signatures, tests switched
  off in added lines (`@Disabled`, `skipITs`, `it.skip` and the like), invisible Unicode
  characters in added lines, and that every test file added or changed is named in the agent's
  `red.md`. Tests the work removed are counted for you at review. When a verification fails, the task's details show the lines of the
  build log that say why, and where the whole log is.
- **Keys stay out of images, volumes and `docker inspect`.** A task gets only the key its model
  needs, as a read-only file on tmpfs.

## How the pod is built

Each task gets a pod: an unprivileged agent container and a Docker daemon in a sidecar that runs
under [Sysbox](https://github.com/nestybox/sysbox). The agent container runs as your UID with every
capability dropped, `no-new-privileges` and a read-only root filesystem; it writes only to the task's
clone, its home and `/tmp`. The Docker socket lives in a volume the two share and never on your host.
Testcontainers in the pod runs Ryuk 0.12.0, whatever the project's library would pick
(`TESTCONTAINERS_RYUK_CONTAINER_IMAGE`): the Ryuk of older libraries prunes images when it cleans up
after a test, and the pod's daemon then loses the image another test is still pulling, at random. A
project that needs another Ryuk passes the variable in `pass_env`.
A firewall in the pod's network namespace blocks your host and every private network, except the
services you list per project; DNS and the internet are open. When the host reaches the internet
through a tunnel smaller than Ethernet (a VPN such as Cloudflare WARP), the firewall clamps TCP MSS in
the pod to fit it, so large downloads do not stall. The pod trusts the certificate
authorities your host trusts: the host's CA bundle is mounted read-only over the sidecar's, the
agent's and the gate's, so a registry or a proxy behind a corporate authority works in the pod as
it does on your host. The sidecar remembers the bundle it started with: when the host's changes,
as it does when a VPN client installs a new corporate authority, the next start of the task makes
the sidecar again, because its Docker daemon reads the authorities once, at its start.

The task's clone is a real `git clone`, so your repository's `.git` never enters the pod. In the
clone, `.git/config` and `.git/hooks/` are read-only for the agent, and the project's own hooks reach
git through `core.hooksPath` in the agent's global config, not through files the agent can edit.

## Compared with Docker Sandboxes

[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) run an agent in a microVM with its own
Docker daemon, which is a stronger boundary than Sysbox: a microVM has its own kernel, a Sysbox
container shares yours. If you have KVM and want the strongest wall around the agent, it is a good
choice.

vivibox draws the second line, from the agent back to you. Docker Sandboxes mount your working tree
read-write, so the agent edits the files you see, git hooks and build files included, and the
documentation asks you to review changes before running any modified code. vivibox never lets the
agent into your checkout: it works on a clone, its work comes back as a review copy, files that run
code on import need your approval whenever they change, and a gate the agent cannot bypass checks the
work first. vivibox also runs without KVM, in a VM or on a machine without hardware virtualization.

---
Back to the [README](../README.md).
