# Installing vivibox

Linux with Docker Engine and your user in the `docker` group; tested on Ubuntu 24.04, and other
recent distributions with a kernel Sysbox supports should work. A model: an API key for a provider
from opencode's list, or an `opencode.json` with your own providers, which vivibox imports.

```bash
git clone https://github.com/DEVividious/vivibox.git && cd vivibox
host/setup.sh            # once, asks before each change: Sysbox, tmux, /srv/vivibox, uv, vivibox
host/setup.sh --check    # confirms nothing is missing
vivibox                  # the first time: builds the agent image, then asks for a key and a model
```

```bash
host/setup.sh            # once, asks before each change: Sysbox, tmux, /srv/vivibox, uv, vivibox
host/setup.sh --check    # confirms nothing is missing
vivibox                  # the first time: builds the agent image, then asks for a key and a model
```

`host/setup.sh` moves Docker's default networks off `172.17.0.0/16` (Docker restarts) to the first
ranges nothing on your machine routes, a VPN included (`VIVIBOX_DOCKER_RANGES="<bridge> <pool>"`
chooses them instead), installs Sysbox, creates `/srv/vivibox` mounted `nosuid,nodev`, and allows
your user to run only the pod firewall helper through sudo. As you, without sudo, it installs
[uv](https://docs.astral.sh/uv/) in `~/.local/bin` when you have none, and the `vivibox` command
from this checkout, editable, so it follows the checkout as you update it. When an update changes
`pyproject.toml`, run `uv tool install --force --editable .` for the new dependencies.
Where Claude Code or Codex is installed (`~/.claude`, `~/.codex`), it also installs the vivibox
skill for them (`vivibox skill install`). The skill is a copy, so it does not follow the checkout:
after an update `--check` says when it is older, and running `host/setup.sh` again installs it anew.
`host/uninstall.sh` removes the copies vivibox installed.

Setup checks for Git and installs it when missing, just like tmux; `--check` only reports it.

Setup also checks that the range task networks are cut from (`network.pool` in `config.toml`,
`198.51.100.0/24` unless you set it) is one nothing on your machine routes. When something does,
it says what, and which Docker network it is when it is one, and goes on with the other checks;
remove that network or set another `network.pool`, and run it again.

A running view keeps the code it started with, and so does every task's supervisor. After an
update the view's title says *vivibox changed on disk: quit and start it again*, and the details of
a task whose supervisor is older say so too; stop and start that task (`s`) when it suits you.

## Removing vivibox

```
host/uninstall.sh            # lists what is there, removes it after confirmation
host/uninstall.sh --check    # only lists
```

It undoes `setup.sh` step by step: every task's pod (containers, networks, volumes, the agent
image), the `vivibox` command, the sudo rule and the firewall helper, the bind mount and its
`/etc/fstab` line, Sysbox, and the Docker network ranges (`/etc/docker/daemon.json` goes back to the
copy `setup.sh` kept, or loses only those two keys; Docker restarts). Then it asks, one by one,
before deleting anything of yours: the tasks directory, `~/.config/vivibox` and the stored API keys.
The packages and uv stay, as they are not vivibox's.

Next: [configuring providers and projects](configure.md), then [a task](tasks.md).

---
Back to the [README](../README.md).
