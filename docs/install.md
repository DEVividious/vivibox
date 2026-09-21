# Installing vivibox

Ubuntu 24.04 (other recent Linux distributions should work) with Docker Engine, your user in the
`docker` group, and an API key for a model provider that opencode supports.

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

A running view keeps the code it started with, and so does every task's supervisor. After an
update the view's title says *vivibox changed on disk: quit and start it again*, and the details of
a task whose supervisor is older say so too; stop and start that task (`s`) when it suits you.

Next: [configuring providers and projects](configure.md), then [a task](tasks.md).

---
Back to the [README](../README.md).
