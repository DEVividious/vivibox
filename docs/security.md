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
- **`--auto` takes no decision before its check.** A task made with `--auto` has its plan
  accepted without you only when its clone is as vivibox recorded it (the check every
  verification starts with, made on the clone's files, with no git) and no risky file changed:
  the clone is looked at, then the risky files, then the clone once more. The command its
  writer proposes, in a project that has none, is written into the project's settings only
  after a verification has passed with exactly that command, as vivibox read it once from the
  writer's file and recorded it where the pod cannot write, and only if the clone is then still
  as recorded, has no risky change, and is on the commit that was verified. A clone the check
  refuses, a verification that fails or cannot run, and a clone that changed by then leave the
  plan to you and the project's settings as they were. The last look at the clone and the
  write are two steps: a change made between them is not seen before the next verification.
  Whether the project still has no command is looked at as its file is written, one keep at a
  time, and the file is put in place whole: a command you set meanwhile is not written over. By hand, the
  command a task waits with at `review the command` is the one vivibox read once when the task
  got there, recorded where the pod cannot write: the notification, `vivibox status`, the panel
  and the field under `a` show that one, and a plain accept keeps and runs that one; what the
  writer's file names later is said and never kept. A kept command
  is written into the project's file as one string: nothing in it ends the list or the line
  it is on, so it sets no other setting of the project.
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
  readable by the agents that use them. The review container is made anew for every round, of
  one role read once when the round starts: the reviewer as the task has it then (`m`), or the
  writer's model where `config.toml` names no reviewer and the flow has one read. Its
  configuration names that role's provider alone, its secrets are that provider's key and the
  secrets of the MCP servers you brought, and its harness asks that provider for that role's
  model. A reviewer you put on another provider gets, from its next round, a container with
  the new provider's key and without the old one's; the round under way ends as it began,
  with the key and the model it was made with. The container of the round before is removed before the
  new round's key is written into the directory it mounted, and a container Docker does not
  remove stops the round before any key is written. A round whose container cannot be
  made (no key for the provider) is not started.

## What is enforced, and what holds it

Each line names what vivibox enforces, the mechanism, a test that fails when the claim breaks
(unit tests run on every change; `-m docker` tests on real pods), and what the claim does not
cover. A claim with no test is not in this table.

| vivibox enforces | by | held by | not covered |
|---|---|---|---|
| The host writes nothing through a link in `handoff/`, reads no FIFO, device or other special file there or in the clone, and does not block on one; what a link reaches outside the clone is not read | host code: `O_NOFOLLOW`, a guarded reader for the clone | `test_handoff.py::test_a_link_is_not_written_through`, `::test_a_fifo_is_refused_without_waiting_for_a_reader`; `test_clone_read.py::test_a_link_that_leads_out_of_the_clone_is_refused`, `::test_a_special_file_is_refused_without_being_opened` | git or an IDE you run in the clone yourself |
| Git the host runs in the clone is bounded in time; past the limit the operation ends with a reason, never as "clean", and no git is left running; it runs without the `GIT_*` of your shell | host code: a timeout on every call, one environment builder | `test_git_timeout.py::test_a_fifo_where_git_reads_ends_the_verification_in_time_and_not_green`, `::test_no_git_process_is_left_after_a_timeout`; `test_git_environment.py::test_git_on_the_host_gets_what_vivibox_sets_for_it_and_nothing_else_of_gits` | programs other than git that vivibox starts for you (a pager, an editor) |
| A clone whose `.git` files the task changed (`commondir`, alternates, hooks, `shallow` as a link or a FIFO, a nested repository, a worktree) gets no git of the host's; a nested repository or worktree blocks the task until it is out | host code: `check_protection` before every host git, from files alone | `test_blocked_clone.py::test_a_repository_the_task_tracks_blocks_it_until_it_is_out`; `test_blocked_start.py::test_the_gate_the_views_and_the_reply_run_no_git_in_a_clone_with_both_additions`; `test_control.py::test_git_in_the_planners_copy_runs_nothing_the_writer_planted`; `test_history.py::test_a_shallow_file_of_the_clone_that_is_no_plain_file_is_a_change` | the engine's own git boundary (a later phase) |
| The readers' copies of the commits hold the last `history_depth` commits; only the host makes the planner's deeper, for commit ids it names in the handoff and nothing else, along the task's branch from the clone as recorded and then along your base branch from the remote recorded when the task was made, by your way of reaching it only for a task on your own work and only when your checkout is shallow, bounded in fetches and time and what git prints (64 MiB), the copy as it was when the commit does not come; either copy always holds the task's base | host code: `history.ensure`, `history.answer` | `test_history.py::test_what_is_no_commit_id_is_refused_before_any_git_runs`, `::test_what_the_writer_sets_in_its_clone_is_neither_run_nor_followed`, `::test_the_clones_own_remote_is_never_fetched_from`, `::test_your_remote_is_not_reached_for_a_task_on_work_not_yet_yours`, `::test_the_steps_are_bounded_and_a_commit_past_them_is_not_found`, `::test_a_step_past_its_time_ends_the_request_with_the_copy_as_it_was`, `::test_every_fetch_of_a_request_has_its_input_closed_its_output_counted_and_a_time`, `::test_the_fourth_request_of_a_phase_is_refused_and_the_turn_goes_on`, `::test_a_commit_your_remote_has_past_the_base_is_not_found_nor_reachable`, `::test_a_step_from_your_remote_leaves_its_newer_commits_readable_in_the_copys_store`, `::test_a_commit_left_behind_by_a_request_that_failed_is_no_root`, `::test_a_shallow_file_the_writer_left_in_a_whole_clone_reaches_no_remote`, `::test_the_recorded_remote_is_fetched_whatever_the_clone_names`, `::test_the_fetch_from_your_remote_asks_nowhere_for_what_it_lacks`, `::test_a_recorded_remote_is_never_taken_for_an_option`, `::test_a_reviewers_round_reads_the_work_from_its_base_however_long_it_is`; `test_git_environment.py::test_the_planners_copy_is_made_deeper_from_the_tasks_clone_and_nowhere_else`; `test_control.py::test_the_planners_configuration_is_a_readers_no_language_server_and_no_serena` | a commit off the base branch's history; the objects of the base branch's commits newer than the base, which a remote step may leave readable in the copy's store (never available, never reachable from its HEAD); the size of a remote's pack, bounded only by the request's ten minutes and the task disk's quota, not in bytes; the internet the planner's container has (a clone into its `/tmp`) |
| Each agent's container holds the keys of its role alone: the writer none of the reviewer's or the planner's; a reviewer or planner moved to another provider gets a container without the old key | host: a secrets directory per container, tmpfs, read-only | `test_secrets.py::test_the_writer_has_no_key_of_the_reviewer_and_the_reviewer_none_of_the_writer`, `::test_every_mode_puts_each_roles_key_where_the_role_runs`, `::test_a_reviewer_moved_to_another_provider_leaves_no_old_key_at_its_next_round` | a key is in the container of the agent that uses it, readable by it; a provider two roles share is in both; keys outside the pod (a gateway, a later phase) |
| The writer cannot write your replies (`comments.md`) | host: a read-only mount over the file in every container that mounts `handoff/` | `test_pod.py::test_the_agent_reads_your_replies_from_a_read_only_mount_over_its_folder`; `test_control.py::test_every_container_that_mounts_the_handoff_mounts_your_replies_over_it` | the rest of `handoff/`, which the writer and the planner both write |
| The planner and the reviewer each run in a container and a network of their own, with a conversation store, server, password and secrets the writer's container does not mount and cannot reach; their harness works in a host-made directory and reads a host-made read-only copy of the commits; it loads no configuration, plugin, hook or MCP server the writer committed | host: a container and a bridge per agent, nft between them, host-made `/work` and copy, a reader's configuration written by vivibox | `test_control.py::test_the_control_container_is_a_readers_with_a_home_and_a_network_of_its_own`, `::test_no_container_but_the_planners_mounts_anything_of_the_planners`; `test_control.py::test_the_planners_harness_never_works_in_the_copy_of_the_writers_commits`; `test_control_live.py::test_harness_configuration_the_writer_committed_runs_nothing_for_the_planner`, `::test_the_planners_server_and_password_are_out_of_the_writers_reach`; `test_control.py::test_a_review_reads_the_commit_that_was_verified_not_the_branch_as_it_is_now`; `test_serena_project.py::test_a_readers_configuration_has_no_serena_whatever_its_mode` | `handoff/` shared with the writer until a checkpoint; a model misled by what the repository says; an MCP server you configured (below) |
| From the pod: your host, your LAN, every private range, other tasks, IPv6 and UDP other than DNS are refused; the networks W, R and P refuse one another; every DNS query goes through the host's forwarder; the rules are loaded before a workload starts and a workload does not start without them; root in the pod cannot remove them | host: nft on the task's bridges, through the root helper, applied by every path that starts a container | `test_netns_live.py::test_the_host_and_the_lan_are_refused_at_once`, `::test_root_in_the_pod_cannot_remove_the_rules`, `::test_dns_fails_while_the_forwarder_is_down_and_the_header_says_why`; `test_pod.py::test_up_applies_the_hosts_rules_before_the_first_container_starts`, `::test_a_helper_that_refuses_before_the_gate_or_the_review_starts_no_container`; `test_pod.py::test_your_hosts_services_are_the_writers_only` | the internet is open (`test_netns_live.py::test_the_internet_is_open`): no allow-list of names or addresses; DNS is not confidential and DNS over HTTPS is not stopped; a pod container you start by hand after a reboot |
| Every container of a task runs with a memory limit (swap bounded to it), a process limit and the pod's CPUs under the task's slice; what the task writes outside its clone is on a disk image of its own size | host: cgroup flags on every container, an XFS image attached by the root helper | `test_limits.py::test_an_oom_kill_is_attributed_to_the_container_whose_scope_saw_it`; `test_pod.py::test_up_attaches_the_tasks_disk_after_the_host_and_before_any_container`, `::test_a_disk_that_cannot_be_attached_stops_the_pod_before_any_container` | I/O and bandwidth; the sum of all pods against the machine (the slice itself is not limited yet) |
| What a container's own process prints, which anything run in a role can write to (`/proc/1/fd/1`), and every request to the Docker Hub mirror, is Docker's log on the host's disk, outside the task's: every container vivibox leaves running has a bounded log, one file of 1 MB (`json-file`, `max-size=1m`, `max-file=1`) for the writer, planner, reviewer, gate, code index and mirror, three of 10 MB for the sidecar's daemon; the containers the writer starts in the pod log to the task's disk | host: `podspec.BOUNDED_LOG` on every `docker run -d`, the mirror made again when it has none | `test_pod.py::test_every_role_keeps_one_megabyte_of_docker_log`, `::test_every_container_vivibox_keeps_running_has_a_bounded_docker_log`, `::test_the_sidecars_logs_rotate_and_say_why_its_daemon_did_not_start`; `test_mirror.py::test_the_mirrors_log_is_bounded_and_one_made_with_none_made_again`; `test_log_live.py::test_a_role_that_floods_its_log_keeps_one_megabyte_and_goes_on` | the daemon's CPU while a role floods its log, outside the role's cgroup; a container of a task started before this bound keeps Docker's default until the task's next start; the short-lived containers vivibox removes when they end (`--rm`: the image's checks, opencode's list of models), whose log goes with them |
| The verification runs nothing from the shared caches and writes nothing into them: its caches are fresh directories of its own, the shared ones read-only, mise's shims off its `PATH` | host: read-only mounts, the gate's own cache directories and `PATH` | `test_pod.py::test_the_gate_reads_the_shared_caches_and_writes_only_its_own`, `::test_the_gates_caches_are_made_fresh_and_the_shared_ones_are_left_alone`, `::test_gate_container_sees_only_committed_work` | a project JDK the agent's mise installed into the shared cache, run by the gate through `JAVA_HOME` (accepted, one trust level); the cost of a cold Gradle home |
| Accepting work is refused while a submodule commit it keeps — in the work, in a submodule's history, nested — is kept by no repository of yours; the check reads only your repositories, within a time limit and 64 MiB of output per git | host code: `submodules.check` before anything is accepted, a bounded reader | `test_submodule_commits.py::test_the_history_of_a_commit_the_history_points_at_is_checked_too`, `::test_submodules_more_than_eight_deep_are_refused_not_accepted`; `test_counted.py::test_an_answer_of_the_limit_is_whole_and_a_byte_more_is_none` | vivibox copies no commit for you; the address in a printed `git clone` is the work's `.gitmodules`', not checked |
| `p` approves only the risky files on the screen, as shown, and nothing when none is; a file that cannot be shown whole (special, too large, a link to a folder or to nothing) is covered by no approval; a command the writer proposes is kept for the project only after the gate passed with it, and by hand only the command that was shown | host code: one scan for the drawing and the identity, a re-scan at the approval, the command recorded by the host at the checkpoint | `test_risky_shown.py::test_an_approval_of_what_is_no_longer_shown_is_refused_whole`, `::test_a_file_that_could_not_be_shown_is_covered_by_no_identity`; `test_auto_decisions.py::test_a_plan_is_not_accepted_by_itself_on_a_clone_the_check_refuses`; `test_auto_decisions.py::test_the_command_is_kept_only_after_the_gate_has_passed_with_it`; `test_command_shown.py::test_a_plain_accept_keeps_the_command_that_was_shown_never_the_files` | whether you read what was drawn; keys typed ahead within one frame; permission bits other than the executable one; the `--auto` window named below |
| Text an agent wrote is shown as text: no link opens, no markup is read, its prose stands behind a rule, control and invisible characters are shown as code points, and no line of a code block lies past the right edge without a note | the view's own Markdown parser and escaping, structural tests over every sink | `test_untrusted_text.py::test_a_link_an_agent_wrote_is_text_with_its_address_in_full`; `test_agent_prose.py::test_no_heading_comes_from_what_an_agent_wrote`; `test_right_edge.py::test_a_long_line_of_a_risky_file_is_on_the_screen_whole`; `test_markup_rules.py::test_no_message_vivibox_did_not_write_is_read_as_markup` | a terminal narrower than 80 columns; what you approve is the file's bytes, not the drawing |
| vivibox's built-in Serena runs for the writer alone, on the writer's clone in the writer's container, keeps its files in the writer's home and changes nothing git tracks; the planner and the reviewer get neither it nor a language server | host-written configurations per role | `test_serena_project.py::test_a_start_of_serena_changes_nothing_git_tracks_and_leaves_nothing_git_would_take`, `::test_serena_is_started_with_no_report_of_its_use`, `::test_a_readers_configuration_has_no_serena_whatever_its_mode`; `test_serena_live.py::test_the_project_is_active_and_a_search_of_it_is_answered` | an MCP server you configured yourself (a trusted integration, below); code intelligence for the planner (none in this release) |

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

That copy, and the reviewer's of each round, holds the last `history_depth` commits of the
task's branch (100, set in `config.toml`), not the whole history; a refresh takes the new
commits and keeps it as deep as it is. Either copy always holds the task's base, which the
review's `git diff <base>..HEAD` needs: when the work is longer than that, the host makes the
copy deeper from the clone alone, with the bounds below and within the ten minutes the copy's
first fetch started, and refuses it, with the step, when the base is more than 20 ×
`history_depth` commits back, or when the branch no longer holds it. The task's clone keeps the whole
history of your checkout.
Only the host makes the planner's copy deeper, and only for a commit the planner names by its id
in `/task/handoff/history-request.md` at the end of its turn: hexadecimal and nothing else (no
ref, range, option or address, refused before any git runs), five a request, three requests a
planning or a review, the fourth refused with "ask the person". For each id vivibox fetches
`history_depth` more commits at a time along the task's branch from the clone, once it is as
recorded, with none of your git configuration and nothing fetched lazily, until the commit is
an ancestor of the copy's HEAD with its parent there (a root commit, whose raw object names no
parent, has none) or the clone has no more; then, when your checkout is shallow, along your
base branch from the remote it tracked when the task was made (recorded then from your
checkout, never read from the clone or the request), by your own way of reaching it
(`GIT_SSH_COMMAND`, your certificates, your credential helper), the same path as the submodules of a
new clone and only for a task that starts from your own work; that fetch asks nowhere for
credentials: with `GIT_TERMINAL_PROMPT=0`, an empty `GIT_ASKPASS` and
`SSH_ASKPASS_REQUIRE=never` it opens no prompt on a terminal and no askpass window on your
desktop, and fails at once when it needs a user name, a password or a passphrase your
credential helper or ssh agent does not give. At most 20 fetches in all, each
from the clone within a minute and all within ten minutes, what each prints read up to 64 MiB,
input closed, no retry; what a remote sends is not counted in bytes, only bounded by those ten
minutes and the task disk's quota; a commit that does not come leaves the copy as deep as it was, and
`/task/handoff/history.md` says why for each id. The reviewer's copy is never made deeper on request. The
remote step fetches the base branch of the remote recorded when the task was made, and nothing
of any other branch or remote: a commit of that branch newer than the base is never reported
available and never reachable from the copy's HEAD, but its objects may be in the copy's object
store, where the planner can read them by their id. Not guaranteed: that those newer commits
of the base branch stay out of the copy's store; and the planner's container keeps the open
internet it had, so a planner can still clone a public repository into its own `/tmp`; vivibox
gives it no command or tool that fetches history.

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
that each is as recorded; when one is not, it runs no git there. Nothing approves such a change. A
repository of its own that the task tracks (`git init` in a folder, then `git add`) or a worktree it
added (inside the clone too, where git lists its folder in the index once it is added) blocks the
task until it is gone: the verification does not run, the task waits for you, and your reply sends
the writer to take it out. What is told apart is only what is said: no git runs in either case.
Alternates are read-only in the pod.
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
with nothing fetched, within a time limit, and reading no more than 64 MiB of what one git
prints (a few objects of the agent's make a listing of gigabytes): past it git is ended with
the processes of its session (what it started that did not leave it), and the accepting
refused.

## Limits of the boundary: what vivibox does not guarantee

In one place, what the table above leaves out and what is accepted as a known limit of this
release:

- **Containers, not virtual machines.** Pods share the host's kernel under Sysbox. A kernel
  flaw reachable from a container is reachable from a pod.
- **The internet is open.** No allow-list of names or addresses; the DNS forwarder applies no
  policy, DNS is not confidential, and DNS over HTTPS is HTTPS to the rules. A task can send
  what it reads to anywhere on the internet.
- **Keys are in the pod.** Every provider key enabled for an agent is in that agent's container,
  readable by that agent; so are the secrets of the MCP servers you gave it. A provider two
  roles share is in both containers. Nothing stands between an agent and its key.
- **`handoff/` is shared.** The writer and the planner both write it; the writer can replace the
  planner's draft or question before vivibox reads it. The draft comes to you to read.
- **An agent reads the repository as data, by its brief, not by enforcement.** The planner and
  the reviewer can be misled by what a file says; what is enforced is where their harness works
  and what it is configured with.
- **An MCP server you configured is trusted.** See the next paragraph of this section.
- **The gate verifies commits while the writer keeps running.** A process the writer left
  running can change the clone between the gate's steps; the gate reads commits, not the
  working tree, and the protection check guards only the git vivibox runs, at the moment it
  runs it. Git or an IDE you run in the clone yourself is not checked.
- **The `--auto` window.** The last look at the clone and the write of a kept command are two
  steps; a change between them is seen at the next verification, not before.
- **Approvals are of what was drawn, not of what you read.** Keys typed ahead within one frame
  act on the new drawing; permission bits other than the executable one are not looked at.
- **Resources.** I/O and bandwidth are not limited; the sum of all pods against the machine is a
  budget, not a limit of the slice.
- **Shared toolchains.** A project JDK the agent's mise installed into the shared cache is the
  one the gate runs, for every task that pins it (one trust level).
- **A reboot.** The rules and `vivibox0` do not survive one; a pod container you start by hand
  with `docker start` afterwards runs without them until the task's next `vivibox start`.
- **No code intelligence for the planner.** In this release the planner has no language server
  and no Serena of vivibox's: on a large repository it reads files and searches with its
  harness's own tools.

An MCP server explicitly configured by the user is a trusted integration. Vivibox v0.2.0 does
not claim to sandbox a malicious MCP server from the planner. What vivibox enforces is the
isolation of the planner and the reviewer from the writer and from the repository's content;
what it trusts is a server you deliberately configured: it runs in the container of each agent
you gave it to, with the secrets you named for it, sees what that agent sees, and its answers
reach the agent's conversation as a tool's do. Give the planner only servers you would run in
your own shell.

Sysbox containers share the host kernel; they are not virtual machines. The host's rules block
host and private-network access except explicitly allowed services, but public internet access
remains open, and the DNS forwarder applies no policy: any name resolves. The agent can read credentials passed to its build in `pass_env`.

The check of the task's clone guards the git vivibox runs, at the moment it runs it. Git you run
yourself in the clone, or an IDE you open on it, is not checked: open the review copy instead, which
holds only what was committed.

An approval of risky files is of the state you were shown. A view of them (`vivibox risky`,
the details panel) has an identity: a SHA-256, whole, over the record of the last approval and
over every risky file as that view's scan found it: its path, what it is (a file, a link and
where it leads, a special file), its content's digest, and whether a plain file can be run,
all read as vivibox reads the clone, following no link out of it. `p` carries the identity of
the view the panel is drawn from at the moment of the key: the diffs on the screen and that
identity are one scan's, set together and cleared together (the details closed, another row,
the task no longer waiting for the approval), and with none on the screen `p` approves
nothing. A refresh you did not ask for does not replace what you are reading: the drawing
stays, a line above it says the files changed, and only your own key draws them anew. The
identity is bound when the text is handed to the panel, before the panel has painted it: until
its next frame the screen still shows the drawing before. Keys typed ahead in that moment (a
second `p` and Enter right after a refusal, say) act on the new drawing; they are your own
keys, on a drawing only your key can ask for, and nothing tells whether you read it. And `vivibox
approve-risky <id> <identity>` the one `vivibox risky` printed; the command line approves
nothing it printed itself in the same run, which nobody would have read. The approval scans
again, and when the clone or the record of the last approval no longer gives that identity it
approves nothing and says they changed since they were shown. A view in which a file could not
be shown whole has no identity, `vivibox risky` then ends with no command, and the approval
refuses such a file whatever identity it is given: one that is not text, one larger than
vivibox shows of one file (64 MiB: it is not drawn, and nothing is put in its place for you
to read, since a copy made for reading would follow the clone and not the view you look at),
one past the total vivibox reads in a pass, one it cannot open, a special file (a named pipe, a socket, a device),
a link that leads out of the repository, and a link to a folder, to nothing or to a special
file, of which nothing can be shown or checked. Each is said as what it is, with the reply
that has the writer take it out. A link your repository already had at the task's base that led
to no file vivibox shows when the task began (to a folder, say), and still names the same
target, is not the work's change: it stays recorded by its target alone, never followed. One
that led to a file is read through like any link inside the repository: when the work changes
or removes that file, the link is a change, shown as what it leads to now and refused when
that cannot be shown. What is recorded as approved is that
scan's digests, and the copy kept of each file, which the next view's diff is drawn against,
is the bytes its digest is of: the copy is made beside the ones kept before and says what it
wrote, and when that is not the scan's digest (the file was another for that moment) nothing
is approved and nothing kept before is replaced. A kept copy that is not what the record says
is never the side a diff is drawn against: the file is shown whole instead. Nothing reads the
clone again as the approved content: the verification compares a new scan's digests with the
record, and the review copy is made of the commits, not of these copies. A file changed
after an approval is a risky change again at the next check. Whether a
plain file can be run is part of what it is: the view says it after the change's kind
(`executable`, `now executable`, `no longer executable`), from the same read the identity
holds, and the approval records it in the one record that holds the digests, put in place by
one rename (an approval cut short is the one before, whole), so a file made executable after its approval is a risky change again at
the next check, shown as that. The other permission bits are not looked at: no commit carries
them. A task approved by an earlier vivibox has no record of which files could be run:
nothing is compared with it until the task's next approval, which records every risky file
that can be run then, also one whose content did not change and which the view does not list. Not covered:
nothing tells whether you read what was drawn.

A checkout that is a partial clone (`git clone --filter=…`) is not supported as a project: a
task's clone would lack the objects your checkout lacks, and the git vivibox runs in your
checkout is not handed how you reach its promisor remote.

The gate checks the accepted checklist and whether changed test files are named in `red.md`;
it does not independently prove that each criterion is satisfied or that the recorded test
actually failed before the fix. Code review and your final acceptance still matter. Once you
approve risky files and run the review copy in your IDE, that code runs with your permissions.

The agent chooses file names and what the files say. Wherever vivibox shows them to you (the
risky files' diffs, `vivibox risky`, the details panel, the messages that name a file), a
control or invisible character (ESC, a carriage return, a bidi override, a zero-width space, a
Hangul filler, a variation selector, the blank braille pattern) is
shown as its code point, `‹U+001B›`, and `‹` and `›` themselves the same way, so no line is
hidden from you and no text is drawn that the file does not hold. What you approve is still the
file's bytes, not what is shown. In prose shown whole (a plan, a goal) the selector of an emoji
stays where it follows a symbol; in a name or a line of a file it is shown like the rest. Of
these characters the verification refuses, in a line the work adds, those that make a name read
as another and that no ordinary text has: the zero-width and bidi characters, the tags, the
Hangul fillers and the combining grapheme joiner; not an emoji's selector.

What an agent writes for you to read (a plan, a question, a review's notes) keeps its headings,
lists and emphasis in the details panel, and makes no link there. A link in it, however it is
written, is shown as text: its label, then the address it leads to, whole, in parentheses
(`the docs (https://example.org/x)`), so a click opens nothing and no address hides behind other
words; a link's title follows its address in quotes; HTML in it is shown as the text it is. The only links a click opens in the panel are the
addresses of what the task is running, which vivibox writes itself.

---
Back to the [README](../README.md).
