# vivibox

vivibox runs coding agents on tasks in isolated copies of your repositories. You approve a
plan, agents write the code, vivibox verifies it, and you decide what reaches your checkout.

For developers already using Claude Code, Codex or OpenCode who want to hand off implementation
and return for decisions. Plan with a strong model in your existing CLI subscription, then let
API models do the coding; choose who reviews each round.

![vivibox: compare four flows, approve a plan, verify and review changes, follow a CLI review, and inspect the result.](docs/img/flow.gif)

*44-second tour of the real view: four flows, CLI planning and review, and your decisions.
Fictional tasks, simulated progress and illustrative costs.*
[Still view](docs/img/view.svg) · [Quick start](#quick-start) · [Documentation](#documentation)

## Why vivibox

- **Choose where the strong model works.** Use it once to plan, or keep it involved in every
  review. Each task can use a different flow and models.
- **Check the work before accepting it.** Build and test committed changes in a fresh clone.
  Verification also checks the acceptance checklist, commits, disabled tests and recorded
  failing-test evidence. Failed checks and blocking review notes trigger bounded fix rounds.
- **Keep builds apart.** Each task's **pod** holds its clone and a Docker daemon for
  Testcontainers and Compose. The host's Docker socket is never shared.
- **Leave tasks running.** Work continues with the view closed. Track several projects, inspect
  a review copy and ask for changes. Enable desktop notifications (off by default) or configure
  ntfy to hear when a task needs you.

## How it works

The default flow separates planning, writing and review:

```text
Planner → You approve → Writer → Gate → Reviewer → You review and accept
                          ↑       │       │
                          └───────┴───────┘
                           failed checks / blocking notes
```

**Gate** means mechanical verification. Each correction passes through it again. The default
allows three fix rounds before you take over; your reply renews that allowance. Changes to
build files, hooks and IDE settings need separate approval before the review copy opens.

### Choose a flow

Set **Flow** per task (`n`) or as a default (`k`). P = planner, W = writer, R = reviewer;
`+` shares a session and model. Every flow includes verification, plan approval by default,
and your final decision.

| Flow | Sessions and verification | Best for |
|---|---|---|
| **Single agent** | P+W+R → Gate | Small, routine tasks |
| **Planner → Executor** | P → W+R → Gate | A strong plan with cheaper execution |
| **Planner → Writer → Reviewer** · default | P → W → Gate → R | Independent review with automatic fix rounds |
| **Supervisor ⇄ Worker** | P → W → Gate → P+R | Keeping the planning context through complex changes |

The first two review their own work before verification. The default uses a separate reviewer
with a fresh conversation each review. Supervisor ⇄ Worker reuses the planning conversation to
review every round, including when you plan in your CLI. Both send blocking notes back for
correction and verification. [Flow details](docs/tasks.md#orchestration-modes).

## Quick start

**Alpha · Linux only.** Setup targets Ubuntu on x86-64. Have Git, GitHub SSH access,
Docker Engine accessible without sudo, and a model API key or provider configuration to import.

1. Clone:
   ```bash
   git clone git@github.com:DEVividious/vivibox.git
   ```
2. Enter the checkout:
   ```bash
   cd vivibox
   ```
3. Install:
   ```bash
   host/setup.sh
   ```
4. Make the installed command available in this shell:
   ```bash
   export PATH="$HOME/.local/bin:$PATH"
   ```
5. Open the view:
   ```bash
   vivibox
   ```

Setup asks before changing Sysbox, Docker networking, host tools or task storage; it may restart
Docker. It also installs or updates the vivibox skill for installed Claude Code and Codex CLIs.
First launch builds the agent image. [Installation and uninstall](docs/install.md).

In the view:

1. **Configure:** `k` → **Providers & MCP** → add a provider or import `opencode.json`.
2. **Create:** `i` adds a repository; `n` describes a task and chooses its flow and models.
   Pick a **Planner** model for automatic planning; initially it is **you, in your own chat**.
3. **Approve:** `d` shows the plan; `a` accepts it. If verification has no command yet, its
   proposed command needs your approval at a separate checkpoint.
4. **Review:** `f` shows the diff, `o` opens the review copy, `r` asks for changes. `a` applies
   the work; a separate dialog offers a branch and commit message.

### Plan in Claude Code or Codex

After provider setup, open your agent's CLI in the project's repository and ask:

> Use the vivibox skill to add CSV export. Plan it with me, then hand implementation to vivibox.

The skill uses `vivibox info`, creates a task with `vivibox new --plan-in-cli`, brings your
approved plan in with `vivibox plan import`, and follows it with `vivibox wait`.
With **Supervisor ⇄ Worker**, that CLI also reviews every round using
`vivibox review <id> --prompt` and `--import`. You still approve the plan, risky changes and
final work. **Single agent** needs an automatic planner; CLI planning supports the other flows.

The list, cost bar and details show CLI usage separately as **$… sub**: an estimate at API list
prices, separate from API spending. [CLI workflow and skill setup](docs/tasks.md#driving-vivibox-from-an-agents-cli).

## Documentation

- [Providers, models and project configuration](docs/configure.md)
- [Tasks, flows, CLI commands and logs](docs/tasks.md)
- [Isolation, verification and security boundaries](docs/security.md)
- [Development and reproducing the tour](CONTRIBUTING.md)

[Changelog](CHANGELOG.md) · [MIT license](LICENSE)
