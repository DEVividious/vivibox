# Changelog

Notable changes to vivibox. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/) with a leading zero: the command
line, the configuration files and what `host/setup.sh` changes on your machine may still change
between minor versions. The version itself comes from git: a tag `vX.Y.Z` names a release, and
every commit past it is `X.Y.(Z+1).devN+g<commit>`, which `vivibox --version` prints.

## [Unreleased]

### Added

- Every container of a task's pod runs with limits of its own, under one cgroup slice of the
  task's (`vivibox-<id>.slice`): memory, with swap bounded to the same figure, processes, and the
  pod's CPUs, never with the kernel's OOM killer turned off. `[limits]` in `config.toml` sets them
  per machine, sizes as you write them (`agent_memory = "4g"`, `gate_memory`, `sidecar_memory`,
  `review_memory`, `control_memory`, the `_pids` of each, `pod_memory`, `pod_cpus`, `pod_pids`,
  `disk`); a container's limit past the pod's is refused by name. The pod's figures are a budget
  the containers' limits are checked against and the CPUs each gets, not a limit on the slice
  yet. A scratch tmpfs (`/tmp`, the verification's and the reviewer's `/config`) is half of its
  container's memory, 2 GiB for the agent by default, instead of a fixed 4 GB.
- Every task has a disk of its own: a sparse XFS image `disk.img` in the task's folder, attached
  by the root helper when the task starts and mounted at `<task>/disk`, which holds what the pod
  writes outside the clone (its Docker's data, the agent's home, what a build installs, the
  verification's clone and the reviewer's copy of the commits), each a directory there instead of a Docker volume;
  only the Docker socket stays in a volume. Its size, `disk` under `[limits]` (20 GiB), is the
  most the task can write there, and a full disk is the task's, never the machine's. Deleting
  the task detaches the disk and deletes the image; one the helper could not detach is kept and
  said. The clone stays beside the image, outside it. Tasks created before this change are from
  an earlier vivibox: delete them and create them again.
- A task stops for you when the kernel kills a process of its pod for memory or its disk is
  full, after the turn or the verification it happened in, with the status `out of memory` or
  `out of disk space`: which container and its limit, the kernel's words (`oom_kill 1`), and the
  command that raises it (`vivibox limits <id> --agent-memory 6g`, `vivibox disk grow <id> 40g`),
  then `vivibox start <id>`. No fix round is spent on it. The event
  `resource.limit.exceeded` (kind, workload, limit, peak or used) is on the task's timeline; a
  kill is reported once. A slice or a disk that cannot be read while the pod runs stops the task
  on an error, never passes as fine.
- The task's panel has a Resources section: each container's limits and, while the pod runs,
  what it uses (from the kernel's cgroup files, never `docker stats`), the pod's CPUs and
  budget, and the disk's use of its size, nearly full from four fifths with the command that
  grows it. `vivibox limits <id>` prints the same; `vivibox limits <id> --agent-memory 6g` (one
  option per key of `[limits]` but `disk`) sets the task's own limits over the machine's in
  `.task/limits.json`, applied at the task's next start; `vivibox disk grow <id> 40g` makes its
  disk larger at once (a disk never shrinks). `vivibox usage` and `u` count a task's disk from
  its mount.

### Changed

- The clone command a refused accept prints for a submodule the work adds says whose its
  address is: the one the work's `.gitmodules` names, written by the agent and not checked by
  vivibox, to look at before running the command, with where to clone from if the submodule
  is elsewhere. The address is printed whole. Fewer addresses are put in a command: a plain
  `https://`, `git://` or `ssh://` URL or `user@host:path` still is; one with a password, with
  a user in an `https://` or `git://` URL (`https://host@elsewhere`), with a host, user or
  part of its path that starts with a hyphen, with `::` in it, with a character written as
  `%XX`, or with no path is not. An address left out of the command was only said to be
  there; it is now shown whole as text, between `«` and `»`, with its invisible characters as
  code points, and the command keeps a place for the address you know. The same for a
  submodule that only a submodule's history has, whose `.gitmodules` the agent may have
  written.
- Accepting work reads no more than 64 MiB of what one git prints while it checks the work's
  submodules (the listing of a submodule's history, of a commit's files, a `.gitmodules`), on
  stdout and stderr together, counted as it comes. A listing is as long as the writer made it:
  a repository of 18 objects made `git log --raw` print 90 MB, which vivibox held whole in
  memory, three times over while it read it. Past the limit git is ended with every process
  it started, nothing of the answer cut short is used, and the accepting is refused as what
  vivibox could not check: the limit, what was being read (`reading the history of lib`), and
  the reply that sends the work back. The time limit holds as before, whichever comes first,
  and now ends the processes git started too: one that held git's output after git ended was
  left running. The task and your repositories are as they were.
- The verification fails when the work lists a folder in the index as a submodule that is
  none: a folder with no repository in it (its `.git` deleted, say, after `git add` had listed
  it), that the task's base did not list and `.gitmodules` does not name. git commits the
  pointer and none of the folder's files, and its status is clean, so the verification was
  green, a review was paid for, and only accepting refused, with a clone to make from a
  `.gitmodules` that was not there. The writer is told among what is not committed: the
  folder, that none of its files is, and to take it out of the index and add them. A
  submodule of the base (checked out or not, moved or not) and one the work adds with its
  entry in `.gitmodules` are left alone; what accepting checks is unchanged.
- What an agent wrote in the details panel is drawn behind a rule: a heavy line (`┃`) down its
  left, on every row of it, which vivibox's own lines never have. A line without the rule is
  vivibox's, a line with it is not, whatever it says. Drawn so: a plan (under review, accepted,
  or a finished task's), the agent's question, a review's notes, the acceptance criteria, the
  writer's steps, what the browser prompt tells a chat about the repository, and a finished or
  deleted task's summary. Each is read as markdown on its own, apart from the lines around it:
  a code block, a list, a quote or a table it leaves open ends with it and no longer takes in
  the sections after it, and a heading in it is a bold line however it was written (a space
  before the `#`, a line of `=` under the words, inside a list or a quote), so the panel's
  headings are vivibox's sections alone. A question or a writer's step could draw a review with
  its verdict and ticked criteria as sections of the panel; a finished task's plan had its
  headings at their own size. A writer's step and a criterion are one line of their list each,
  whatever line breaks they hold. A horizontal rule is drawn where it was written, not above
  the list or the quote it stands in.
- vivibox asks for Textual 8 (`textual>=8.2.8,<9`) and names `markdown-it-py` (4.x) and
  `pygments` (2.x), which the details panel uses directly: what the panel does with a link and a table's cell is tested
  on that Textual, and the next major is taken up with its tests run.
- A task that tracks a repository of its own (`git init` in a folder, then `git add`) or adds a
  worktree is still stopped, with no way to approve either, but is no longer told to be deleted:
  it is blocked until what it added is removed or taken out of what git tracks. The
  verification does not run and the task waits for you with `verification could not run`, no
  round spent, instead of stopping on an error nothing but a delete got it out of; the message
  says what to do before it says why, so a notification cut short still has it; `r` sends the
  writer back, which reads in `verify-feedback.md` how to take the repository or the worktree
  out, in words that hold no command made of a name it chose, and `g` verifies again. Found at
  a review instead (added after the verification passed), it sends the task back to the
  verification, which waits for you the same way. The turn your reply starts sends the writer
  to `verify-feedback.md` first (the turn a failed verification starts, then your reply's, both
  in their own words), and that file is written at your reply to say how, also where the clone
  was blocked after its last verification; a reply that cannot write it is refused whole. The
  verification's log, which the writer reads as `verify.log`, says what blocks the clone and
  not what you are told to do. The feedback says in which order a repository goes out, and
  that its `.git` deleted alone leaves none of its files in the commits. In a box, which nobody is sent back to, the message says what
  closes it: the repository's folder removed, ignored, or its files kept as ordinary ones
  (taken out of the index alone it comes back, as closing a box adds every file git does not
  ignore). Any other change to the clone's git files is refused in the words it was.
- The verification refuses, in a line the work adds, the Hangul fillers (U+115F, U+1160,
  U+3164, U+FFA0: letters drawn as nothing, so a name of their own nobody sees) and the
  combining grapheme joiner (U+034F), as it does zero-width and bidi characters. A variation
  selector (the U+FE0F of an emoji) and the blank braille pattern (U+2800) are not refused.
- A link in text an agent wrote (a plan, a question, a review's notes, a criterion) is no
  longer a link in the details panel: it is shown as its label, then the address it leads to,
  whole, in parentheses (`the docs (https://example.org/x)`), the address alone when the label
  is the address, whatever syntax made it (inline, by reference, in angle brackets, an image);
  a bare address is plain text. A click on it opens nothing. The addresses of what the task is
  running, which vivibox writes itself, open in your browser as before, now once per click
  instead of twice.
- A task whose flow has a planner other than the writer (Planner → Executor, Planner → Writer →
  Reviewer, Supervisor ⇄ Worker) runs the planner in a container of its own,
  `vivibox-<id>-control`, made at the task's start after the agent's and kept for the whole
  task, instead of in the agent's container; in the Supervisor ⇄ Worker flow the same
  conversation reviews the work there. The container is the reviewer's kind (its root
  read-only, no capability, no Docker socket, no build cache), under the task's slice with the
  `control_*` limits, in a third network of the task's, the planner's, `vivibox-<id>-p`, on a
  bridge of its own with rules of its own on your machine, made and checked with the writer's
  and the reviewer's at every start; it reaches none of your machine's services. Its own, and
  nowhere the writer's container mounts or reaches: a home on the task's disk with its
  conversation store, an opencode server with its own password, and a directory with its
  provider's key and its MCP servers' secrets (`m` on the planner changes that container's
  files at the next start). So nothing the writer runs can change what the planner takes for
  its own history, reach its server or use its key. `w` on the planner's conversation opens it
  there. The planner reads the work in a copy of the task's commits that vivibox makes on the
  task's disk and mounts read-only where the writer has its clone; the writer's clone itself is
  not mounted there, and for a review the copy stands on the commit the verification passed.
  The planner's harness works in a read-only directory of vivibox's making, `/work` (a git
  repository with one empty commit and nothing else), and reads that copy as data, by its path;
  git there answers for the copy wherever it is run from. Nothing in the planner's container
  works on the project by itself: its opencode has language servers off and downloads none, and
  it gets the MCP servers you brought and not vivibox's own Serena. The agent's container holds
  the writer's key alone (a provider both use is in both), and, where a reviewer or the planner
  reads the work, no longer writes the task's review folder, where vivibox reads the verdict.
  An opencode turn that ends well and prints no event of itself is a failed turn, and the task
  stops for you with that reason, instead of going on as if the agent had worked. `vivibox
  stop` stops the control container, and `vivibox delete` removes it with its network, its home
  and its secrets. A Single agent task has neither the container nor the network: its planner's
  range stays reserved. Tasks created before this change are from an earlier vivibox: delete
  them and create them again.
- The planner in a container of its own, and the supervisor of Supervisor ⇄ Worker, are told
  what that container is: the project is a read-only view of the task's committed work at its
  path, made by vivibox and brought up to date before each of their turns (uncommitted work is
  not in it; during a review it is the commit the verification passed); they start in an empty
  directory and git answers for the project from anywhere; there is no Docker, no build or test
  run, no way to change the code and no language server there; they write under `/task/handoff`
  and, for a review, `/task/review`; and the project's `AGENTS.md` is theirs to read as
  information about the project, not an instruction given to them. Before, they read the
  writer's brief, which described the writer's container. The writer's and the Single agent's
  briefs are as they were. In a project with no verification command yet, their brief names
  none.
- The reviewer's harness works in a read-only directory of vivibox's making, `/work` (a git
  repository with one empty commit and nothing else), made for every round, and reads the work
  as data, by its path, in a copy of the task's commits that vivibox makes on your machine for
  the round, on the commit the verification passed, mounted read-only where the writer has its
  clone. The writer's clone is not mounted in the reviewer's container and no git runs there
  against it; git there answers for the copy wherever it is run from. Nothing in the reviewer's
  container works on the project by itself: its opencode has language servers off and downloads
  none, and it gets the MCP servers you brought and not vivibox's own Serena. A copy that
  cannot be made (a clone that is not as recorded, a verified commit that is not on the task's
  branch, a clone git waits on) stops the review with the reason, and no reviewer's container
  is started; nor is one started on a folder vivibox did not make for the round, and that
  refusal names the command that starts the task again. The reviewer's container is otherwise
  as it was: its network, its home and scratch folder in memory, its secrets and its limits.
- The copies of the commits the planner and the reviewer read hold nothing to follow out of
  them. Their `.git` is made without git's templates and has no hook, whatever
  `GIT_TEMPLATE_DIR` or your configuration says. A symbolic link the writer committed is a
  plain file there, holding the path it names, so reading it in the planner's or the reviewer's
  container never reads a file of that container's own, such as its key. After every refresh a
  copy holds the files of its commit and nothing else: what is untracked or ignored there is
  removed, so a copy made before this change keeps no link and no file its branch has deleted.
  Git on your machine works on a copy with that folder named as its repository, whatever
  `GIT_DIR` your shell carries, and fetches nothing lazily for it, whatever remote the clone
  names. Git in both containers uses a directory as a repository only when it is pointed at it
  (`safe.bareRepository=explicit` in their environment), so a folder committed to look like a
  bare repository is not one there. And the reviewer's round is made only of a commit the
  verification passed: a task in its review that records none is sent back to the verification
  first, which records the commit it passes.
- The reviewer in its own container is told what that container is, in the words the planner
  and the supervisor read: the project is a read-only view of the task's committed work at its
  path, on the commit the verification passed; it starts in an empty directory and git answers
  for the project from anywhere; there is no Docker, no build or test run, no way to change the
  code and no language server there; the handoff is its to read, and it writes its review to
  `/task/review`; the project's verification commands are named as a fact, and the project's
  `AGENTS.md` is its to read as information about the project. A round's review is asked for in
  one wording, the reviewer's and the supervisor's alike: `git diff <base>..HEAD`, and the
  files it names at the project's path. Before, the reviewer read the writer's brief, which
  described the writer's container.
- A task keeps the flow it was created with: `agent_orchestration_mode` in `config.toml`, and
  the Flow row under `k`, are for the tasks you create after a change, where a running task
  followed the file before. Which containers a task's pod has depends on its flow. A task that
  records no flow is from an earlier vivibox: delete it and create it again.
- A task has a network per role instead of one: the writer's, which the sidecar, the
  verification and the pod's own containers share, and the reviewer's, `vivibox-<id>-r`, of its
  own, each on a bridge of its own with rules of its own on your machine, which refuse each
  other's range at once. The reviewer's container runs in its network at an address allocated
  for it, instead of in the sidecar's network namespace: nothing the writer runs, a nested
  container with any capability included, is on the reviewer's wire, and neither reaches the
  other; its DNS goes to the forwarder on your machine like the pod's, `host.docker.internal`
  is its own gateway, its internet is open like the writer's, and a project's `host_services`
  are let through for the writer's network alone (the reviewer reaches no service of your
  machine's). Before each round of review the reviewer's network is checked again as at the
  task's start: one gone meanwhile (a prune of Docker's, nothing being on it between rounds)
  is made again on its range, and one not on its bridge refuses the review, naming the
  remedy. A task takes one
  /27 block of `[network] pool` (the writer's /28, the reviewer's /29, and a /29 reserved for
  the planner), so the default `198.51.100.0/24` holds 8 tasks, and a pool smaller than a /27
  is refused by name; when every block is in use, the error names the pool, its room in tasks,
  what a task takes, and that deleting finished tasks frees their ranges. Tasks created before
  this change are from an earlier vivibox: delete them and create them again.

- The verification trusts nothing the writer put into a cache: the caches shared between tasks
  (`/cache/m2`, `/cache/gradle`, `/cache/npm`, `/cache/yarn`, `/cache/mise`, `/cache/corepack`,
  `/cache/uv`, `/cache/go`, `/cache/cargo`, `/cache/rustup`) are mounted read-only in the gate's
  container, its `PATH` is the image's without mise's shims from the shared cache, and every build
  tool gets a cache of the gate's own under its directory on the task's disk, made fresh for every
  verification (`caches/m2`, `caches/gradle`, `caches/npm`, …). Maven reads the shared downloads
  through a file repository asked before the project's own (`settings.xml` under the gate's home,
  written by vivibox), with its own local repository and its build cache where it was; Gradle reads
  them through its read-only dependency cache (`GRADLE_RO_DEP_CACHE`) with a user home of its own;
  npm, Yarn, corepack, uv, Go and Cargo fetch cold into the gate's directory. A project JDK
  (`java`) is the one the writer's mise installed in the shared cache, found by `mise where` with
  that cache read-only and linked under the gate's home; a tool a project brings (`tools`, its
  `mise.toml`) is installed by the gate into a mise data directory of its own, never into the
  shared one, and the gate's mise never installs java; it runs `mise install` only for a checkout
  that pins a tool in a file of mise's, wherever mise reads one (`mise.toml`, `.tool-versions`,
  `conf.d`, the `mise.<env>.toml` and local variants, `.mise/` and `.config/mise/`). A shim of
  mise's for a tool with no version set hands over to the next program on PATH, so the agent
  gets the image's Python and uv until a project pins others. A JDK in `tools` (`java@17`) is
  refused at load, pointing at `java = "17"`: the gate would download it for every verification.
  Yarn Classic's cache in the gate is a user-level `.yarnrc`
  under its home, which a project's own `cache-folder` outranks, as for the agent.
- Python 3.13 and uv are the image's own, like Node: a CPython of uv's in the image's root
  filesystem, `python3` and `python` in `/usr/local/bin`, uv 0.12. Nothing is pinned for mise by
  default any more (`/etc/mise/config.toml` is gone): Python, uv and Java 21 come from the image,
  and a project that wants other versions names them in its `mise.toml` or its settings (`java`,
  `tools`), as before. Build the image again (`vivibox image build`).
- `pod_cpus`, the CPUs each container of a pod may use, defaults to the machine's count when it
  has fewer than six, and a value past the machine's count is refused by name (Docker refuses
  such a `--cpus`, and the default would not have started a pod on a four-core machine).
- The sidecar's Docker daemon writes to the container's output, which Docker keeps in three files
  of 10 MB, instead of a file in the container that grew for as long as the task lived; a daemon
  that did not start is quoted from there. A stopped sidecar made with other limits is made again
  at the task's next start, so a raised limit arrives.
- A pod's DNS queries to port 53, UDP or TCP, whatever server they name, go to a forwarder on your
  machine, which passes them to your machine's resolver unchanged; DNS over TLS (port 853) is
  refused. DNS over HTTPS looks like any HTTPS and is not stopped yet. One pod holds at most a
  quarter of the queries and connections the forwarder waits on, so a flood of names nobody
  answers in one task leaves the others their DNS. `vivibox hostd` runs
  it, on `[network] host_address` (default `203.0.113.1/32`, an interface of its own,
  `vivibox0`) at port 5053, and the first task's start starts it; `vivibox hostd` says whether
  it runs, `start` and `stop` start and stop it. A pod does not start while it is down, a running
  pod resolves no names then, and the header says so and what to run, its pid file gone with a
  logout included. Tasks started at once prepare the host and the forwarder one at a time; a
  session without a runtime folder (`XDG_RUNTIME_DIR`) is told so.
- A pod reaches your machine's services (`host.docker.internal`) on its own network's gateway,
  and the Docker Hub mirror listens on `network.host_address` instead of Docker's bridge.
- A pod's egress rules are loaded by a new root helper, `/usr/local/libexec/vivibox-host`, through
  one sudo rule, instead of `vivibox-netns`. Tasks created before this change are from an earlier
  vivibox: delete them and create them again.
- `host/setup.sh` installs that helper and its sudo rule and removes `vivibox-netns` and its rule;
  gives your machine its address for the pods on `vivibox0`, refusing, by name, one another
  interface or route already holds; needs `nft` and `python3`; and lists the tasks from an earlier
  vivibox whose pod still runs, to delete and create again. After a restart of your machine,
  `--check` says `vivibox0` comes back at the next pod start, and finds nothing to do.
  `host/uninstall.sh` removes the pods' rules, `vivibox0` and the forwarder too, stopping only a
  process that is the forwarder.
- Tasks created by an earlier vivibox can't be run any more: they are not carried over to the
  new layout of a task's files. The list and `vivibox status` show them as `from an earlier
  vivibox` (and say when their supervisor or pod still runs); delete them with
  `vivibox delete <id>` and create them again. A new task records the layout it was created
  under and the vivibox that created it; one from a newer vivibox is only shown, not deleted,
  even when its state holds what this vivibox does not know. `vivibox status` waits at most 5 s
  for Docker to say whether such a task's pod is up, and says when it could not tell.
- `vivibox init` gives a Maven project Java 25 when `.mvn/jvm.config` starts Maven with options
  Java 21 refuses (OpenFeign's), and notes a `pom.xml` that asks for other JDKs through the
  toolchains plugin.
- `vivibox init` finds a Maven reactor kept under another name at the root (`build-pom.xml`) and
  verifies it by modules with `-f`, and keeps `--no-daemon --console=plain` in the command it
  writes for a Gradle project of modules.
- A verification by modules also builds the modules nested under the ones in scope (an API check
  kept in a project of its own, for one), and narrows an npm or pnpm command to the modules'
  folders when one runner at the root tests every package, instead of `--filter`.
- Gradle projects and npm or pnpm workspaces can be verified by modules too, with
  `{modules:FORMAT}` in the command (`./gradlew {modules:%p:check}`); `vivibox init` finds their
  modules and verifies a Gradle project of three or more that way.
- The verification dialog (`i`, `e`) offers "build only the modules a task changes" for a project
  of two modules or more, with the command it makes; `vivibox init` verifies a Maven project of
  three modules or more that way, where it took ten.
- A plan made in a chat or an agent's CLI, in a project verified by its modules, names them in a
  "Modules:" line, which the import puts in the plan and asks for when it is missing; the
  details and `vivibox status` say which modules a task is verified by.
- A task verified by modules builds the plan's modules and any other module its work changed,
  instead of the whole project; only a change in no module, such as the root's build file,
  builds everything. The timeline says which modules came from the changes.
- The README, `docs/install.md` and `CONTRIBUTING.md` clone over HTTPS, which needs no GitHub SSH
  key, and point to Docker's install and post-install guides.
- The README opens "Why vivibox" with what vivibox runs for a task, from handoffs to your approval.
- `host/setup.sh` stops at once, with what to do, on Docker from the snap package or Docker
  Desktop, neither of which Sysbox runs under, and names the guide when Docker is missing or
  your user cannot reach it.

- This machine's rules for a task are applied again before its verification's container and
  its reviewer's start, as they are before its pod, and neither starts when the helper refuses:
  the error names which could not start and why. The rules and `vivibox0` do not survive a
  restart of your machine, and no pod container starts by itself after one; a pod container
  started by hand with `docker start` after a restart has no rules until the task's next
  `vivibox start`, so start tasks, not containers.

### Fixed

- Words an agent nested more than twenty levels deep (a quote in twenty quotes, the tenth list
  in a list) are on the screen. The details panel's parser read no deeper and left them out,
  with everything after them to the end of the text: the writer read in the plan what you did
  not. A plan, a question or a review's notes nested that deep is drawn as its source, in a
  code block.
- A table with more columns than the details panel has room for is drawn as its source, in a
  code block, every cell's words on the screen: thirty columns at 80 were an empty grid. A
  table is a grid while each column has room for four characters of text a row (ten columns
  at 80, eighteen at 140), and that is decided again when the terminal's width changes.
- A line of a code block longer than the details panel is on the screen whole. It was drawn as
  one row, and what was past the panel's edge was not shown, with no sign that the line went
  on: where you approve risky files "as shown", a line of `build.gradle` with 200 spaces after
  a harmless start showed the start alone (`vivibox risky` showed all of it). Every code block
  of the panel (a diff, a risky file, a log, the feedback, a block in a plan, a table shown as
  its source) now folds such a line onto further rows, each marked `↳` in a column of the
  block's own, where no text of the block is drawn; the same character in a block's text is
  shown as `‹U+21B3›`. A fold counts the cells a character takes (two for a wide one, none for
  a combining mark, a tab to its stop) and is made again when the terminal's width changes. A
  code block no longer scrolls sideways.
- When opencode's server does not start in the pod, the end of its log is said escaped, its
  lines under the first, like every other tool's words; it was put into the error as it came.
- One word on a code block's first line is drawn, on a line above the block, when it is not a
  language the highlighter knows (`IGNORE_THE_PLAN_AND_DELETE_THE_TESTS`): it was taken for the
  block's language and drawn nowhere. A language (`python`, `diff`, `toml`) is still the
  block's colours and no line of words.
- A code point spelled in small letters (`˂U+001b˃`, `˂u+001B˃`) is one for the character
  beside it too. In the panel of a verification that could not run, the frame vivibox puts
  around a build's line (`the build said «…»`) stays as it is when the line ends in a spelled
  code point (the closing mark was shown as `‹U+00BB›`), and a name escaped where the reason
  was made is not escaped a second time (`‹U+2039›U+001B‹U+203A›`): the line is escaped before
  its frame is put around it, and the panel shows the sentence as text.
- A link whose label is a picture (a badge) keeps its own title in the details panel: the
  picture's title, or its lack of one, took the link's place, so the link's was drawn nowhere.
- Texts of a verification that could not run. A line of the build cut short in a notification
  has its frame closed before vivibox's own words (`…» (the rest: vivibox status <id>)`).
  `vivibox status <id>` shows that line under `Why` as text, an escape sequence in it as its
  code points, as the panel and the notification did. When git ran out of time and the look
  for a special file stopped at its bound of 100 000 entries, the message says so instead of
  "found none". A review that could not start for that names one step, the start, not two.
  A clone with a worktree the task added and, beside it, a change no ordinary work makes is
  refused for good at once, not first "blocked until it is out"; a worktree and a repository
  of its own are named together.
- A worktree the writer makes inside the clone (`git worktree add .worktrees/feature`) and
  then adds with everything else (`git add -A`) blocks the task until it is removed, like any
  worktree it adds: git lists the worktree's folder in the index as a submodule, and that
  had the task refused for good ("delete the task") since the index is read beside an added
  worktree. The folder is told to be the worktree's own from files alone: its `.git` names
  the clone's `worktrees/<name>`, and that entry is as git wrote it (its `commondir` git's
  own, its `gitdir` leading back, no settings of its own). Anything else beside an added
  worktree is still refused for good. The feedback also says to take the folder out of the
  index.
- A `question.md` with bytes that are no UTF-8 no longer ends the view with `UnicodeDecodeError`
  while its task's panel is open, nor stops the task on that error's text: the bytes are shown
  as their codes (`\xff`). The list, `vivibox status`, the panel and the supervisor now read
  the file through one function, so they agree on whether there is a question: a file with
  nothing to see in it (white space however much, a byte order mark, zero-width spaces, NUL
  bytes) is none to all of them. Its first 65,536 characters are read, and a longer one says
  it goes on.
- A task whose `state.json` cannot be read as a state (not JSON, cut short, empty, not an
  object, not readable, or of this layout with a field missing or of another kind) no longer
  ends `vivibox status` and the view with a traceback for every task, and can be deleted. It
  is `state unreadable`, like a state that records no version: shown, not run ("its
  state.json is not a task's state as vivibox writes one … delete it and create it again"),
  and deleted by `vivibox delete` and `x`. A file that cannot be read says nothing of the
  vivibox that wrote it, so a newer vivibox's task with a damaged state is deleted here like
  any other, of what this vivibox knows a task to have. A supervisor already running when a
  task's state stops being one this vivibox runs gives it no turn and writes nothing.
- A review whose work is no longer what the verification passed (its commits moved, or the task
  added what blocks its clone) goes back to the verification once. The second time in a row,
  with no turn of the writer's and no decision of yours between, the task stops for you with
  the status `review could not finish`, the reason and what to do, and no further review is
  started. Something left running in the pod that changed the clone after every verification
  had the task verified and reviewed again without end, a paid reviewer's turn each time, with
  no round spent and nobody told.
- A task blocked by a repository or a worktree it added (see under Changed) starts again after
  its pod was stopped or your machine restarted, in every flow. With a planner in a container
  of its own (Planner → Executor, Planner → Writer → Reviewer, Supervisor ⇄ Worker) the start
  ended in `could not start`, with a message that told you to reply where no reply is taken.
  The planner's copy of the commits is not brought up to date from a blocked clone (no git of
  your machine reads one): it stays as it was, the timeline says so, and the planner gets no
  turn until the clone is as recorded again.
- A clone found blocked while its task is still planned (a repository or a worktree added
  there before a plan was accepted) stopped the task with a message that told you to reply,
  where no reply is taken and no writer has had a turn. It names what works there: take it
  out in the task's pod (`vivibox pod shell <id>`), then start the task.
- Any character outside ASCII that touches a code point's spelling in a name or a line of a
  file is shown as its own code point, not only the brackets of a list: `❬U+001B❭`,
  `⧼U+001B⧽`, `≺U+001B≻` read as an escape that is not there, as did a spelling with a
  fullwidth plus (`U＋001B`).
- Two more errors made of a tool's words are escaped where they are made, their lines under
  the first: why your repository at a task's start could not be read (git's first line), and
  what the pod answered when opencode made no session.
- Words of a plan, a question or a review that the details panel left out are drawn: a link's
  or a picture's title, after its address in quotes (`the docs (https://example.org/x "its
  title")`), and an address inside a picture's own words; the words after the language on a
  code block's first line, on a line above the block; and a table with a row of more cells
  than it has columns, which markdown would draw without them, as its source in a code block.
- A table in a plan, a question or a review no longer ends the view when the mouse rests on a
  cell that holds brackets (`[/health]`: `MarkupError`), and a cell of valid markup no longer
  draws a tooltip in styles of its own: the panel's tables have no tooltips. Their cells are
  drawn whole instead, folded onto as many rows as they take in columns of equal width, where
  a cell was one row cut with an ellipsis: a long address in a cell is on the screen to its end.
- Three prints of the command line showed text of an agent's or a chat's as it came, control
  characters and all: `vivibox plan import` naming the criterion it refuses (to you, and in
  the words for the chat), `vivibox review <id> --import` naming a note without a place, and
  `vivibox wait` saying why a task stopped (an agent's failed turn is its own words). They are
  escaped now, and what takes several lines stands indented under the first, never at the left
  edge. `vivibox wait --json` is as it was.
- A project whose `package.json` or `pnpm-workspace.yaml` names a workspace outside it (an
  absolute pattern such as `/etc/*`, one that leaves through `..`, or an empty one) no longer
  ends `vivibox init`, the new-project dialog or a verification with a traceback, nor has a
  folder outside it taken for one of its modules: such a pattern is ignored. So is the project
  itself as a workspace (pnpm's `- '.'`), which ended them the same way; `./packages/*` is
  `packages/*`.
- When Docker does not answer `vivibox status` in time, the line that says so no longer calls
  every task this vivibox does not run one "from an earlier vivibox" (a newer one's is among
  them), and says how to ask again.
- Refusals to approve risky files say what is true and what to do in more cases. A file that
  became a named pipe or a link out of the repository between the look at it and `p` is said
  to be one whose change cannot be approved, with what undoes it, as the other refusals are. A
  removed file vivibox kept no approved copy of is listed as `removed`, with that reason,
  instead of `(empty)`. When vivibox cannot tell whose the unchecked files are, the message
  ends with the step: repair what it names and verify again (`vivibox verify-again <id>`), or
  delete the task. In a box, which has no writer, a refusal names whoever works in the box
  instead of telling you to reply. When git warns before it fails in your repository, the
  reason given is its error, not the warning.
- A line of a build's output that says a verification could not run is said to be the build's,
  between `«` and `»` (`the build said «Cannot connect to the Docker daemon»`), in the
  notification, the status, the panel and the writer's feedback: nothing the build printed
  reads as a sentence of vivibox's.
- `l` on a finished task no longer says `0 blocking · 0 not blocking` of a round whose review
  vivibox could not read: it says `unreadable` and why, as it does while the task lives, from
  the events the archive kept; in an archive without them, a text with no Blocking or Not
  blocking section is said to be no review instead of counted.
- A task stopped on a `question.md` that vivibox did not read (a link, or not a plain file) is
  no longer called `agent asks` in the list and in `vivibox status`: its status is `question
  not read`, and `Why` gives vivibox's reason instead of the path of a file not to open. A
  `question.md` that is there and empty asks nothing: the task has the status of what it is
  blocked on, as its panel already said.
- A blocked task whose `question.md` was empty no longer stops on an error (`list index out of
  range`) when you verify again and the build fails again: it waits for you as it did, and the
  notification says the file is empty.
- When git in a task's clone runs out of time and vivibox finds no special file where git reads
  its settings or refs, the message no longer says a named pipe made git wait: it says which
  git did not finish in how many seconds, where, that vivibox looked for such a file and found
  none, and to try again. A cause is named only where one was found.
- A notification of a verification that could not run, cut short to fit, ends with where the
  rest is (`vivibox status <id>`) instead of in the middle of the sentence.
- A task whose state records, where the layout's version is, something that is no version (text,
  a fraction, `true`, `null`, zero, a negative number) is no longer said to come from a newer
  vivibox, which nothing in it says. Its status is `state unreadable`; every command that runs
  or moves it refuses, saying its state is unreadable or damaged and to delete it and create it
  again, and `vivibox delete` and `x` delete it, which they refused. A task with no version at
  all is still one from an earlier vivibox, and one with a higher version still a newer
  vivibox's, shown and not deleted.
- A model's name with brackets in it, from a provider's catalogue, no longer ends the view with
  `MarkupError` when the new-task form opens: in the form's lists a model's name is text, as is
  a file's name offered after `@` (a name with `[black on black]` in it hid its own rest).
- A name or a line of a file cannot spell an escape that is not there with brackets drawn like
  vivibox's own (`˂U+001B˃`, `〈U+001B〉`, `＜U+001B＞`): such a bracket before or after a code point
  is shown as its own code point, as `‹` and `›` always were. Around anything else it reads
  as it is.
- More characters with nothing to see are shown as their code points wherever a name, a line
  of a file or a tool's words are shown: the Hangul fillers, the variation selectors (U+FE00
  to U+FE0F, U+E0100 to U+E01EF, the Mongolian ones), the combining grapheme joiner, the Khmer
  inherent vowels and the blank braille pattern (U+2800). In text shown whole (a plan, a goal,
  a notification) the same, but for the selector of an emoji (`⚠` with U+FE0F, a keycap), which
  stays where it follows a symbol.
- A project's panel shows its settings as one code block whatever they hold: a line of three
  backticks in them (in a command kept from a task, say) closed the block, and the rest was
  read as markdown.
- An error of git or Docker that takes several lines keeps them, each after the first indented
  under it, in the panel, a notification, `vivibox status <id>` (under `Why`) and a command's
  error; it was one line with `‹U+000A›` where the lines ended. A line break in a name the tool
  quotes still cannot draw a line that reads as vivibox's own.
- What a tool said is escaped in one place, where the error is made, for every error built
  from it: also Docker's log when a pod's Docker does not start, what Docker said of a network
  or of the Hub mirror, what the root helper refused (`vivibox disk grow` among them) and why
  usage could not be measured, which reached the terminal with their control characters.
- The details panel shows HTML in text an agent wrote as the text it is: a tag was read and
  not drawn, and a block of tags was left out whole. A link's definition on a line of its own
  (`[name]: address`) stays on the screen too.
- A bidi override or a zero-width character spelled as an entity (`&#x202e;`) in a plan, a
  question or a review is shown as its code point in the details panel, as the character itself
  already was.
- The view no longer ends with a traceback when an answer comes for a dialog you have already
  closed: the branch of your checkout for the new-task dialog, the branches for the branch
  picker, the providers opencode knows for the add-provider dialog, each read in the
  background and, on a busy machine or a slow repository, back only after Esc. The answer is
  dropped (the list of providers is still kept for the next time), and so is what a step
  still under way reports while the view is closing.
- Leaving the view while a step finishes no longer risks `RuntimeError: Set changed size during
  iteration`: the steps still under way are counted over a copy of their set.
- The view no longer ends with a traceback (`FileNotFoundError: …/.task/state.json`) when a
  task is deleted at the moment the list is refreshed: accepted or deleted in a shell or from
  a notification, or by the view's own removal still under way. The list stays as it was and
  the next refresh draws it without the task.
- A task's start on a busy machine no longer fails with `the DNS forwarder … did not start: it
  ended` while the forwarder was in fact coming up. The forwarder wrote its pid file in place,
  so the file was empty for a moment, and a process just started has no command line for a
  moment either: the start that looked just then took the forwarder for ended, and stopped it
  or left it running with no pid file. The pid file is now put in place whole, and the start
  asks the process it started whether it has ended.
- Git variables exported in the shell vivibox is started from no longer redirect the git it
  runs on your machine. Every `GIT_*` variable but `GIT_TERMINAL_PROMPT` is taken out of that
  git's environment: a `GIT_DIR`, `GIT_INDEX_FILE` or `GIT_OBJECT_DIRECTORY` (set inside a git
  hook, or by a tool) no longer aims it at another repository, index or object store of yours,
  and `GIT_CONFIG_COUNT`, `GIT_CONFIG_GLOBAL`, `GIT_TEMPLATE_DIR` or `GIT_EXTERNAL_DIFF` no
  longer hand it configuration or a program to run. Your git configuration is read from the
  files where git keeps it (`~/.gitconfig`, `$XDG_CONFIG_HOME/git/config`, the system's, the
  repository's own); a file your shell names in `GIT_CONFIG_GLOBAL` or `GIT_CONFIG_SYSTEM` is
  not, and when your identity is only there, vivibox says so and names the commands that set
  it where it is read. The one git vivibox sends to your remotes, the fetch of a new task's
  submodules, is still handed how you reach them, and it alone: `GIT_SSH`, `GIT_SSH_COMMAND`,
  `GIT_SSH_VARIANT`, `GIT_ASKPASS`, `GIT_SSL_CAINFO`, `GIT_SSL_CAPATH`, `GIT_SSL_CERT`,
  `GIT_SSL_KEY` and `GIT_PROXY_COMMAND`; never `GIT_ALLOW_PROTOCOL` or `GIT_SSL_NO_VERIFY`,
  and only when the task starts from a commit of your own: one started from work you have
  not accepted (`--base refs/vivibox/<id>`) gets none of them.
  `GIT_AUTHOR_*` and `GIT_COMMITTER_*` are taken out everywhere: the accepted work is
  committed as your git configuration names you, as the task's own commits and its branch
  always were, so every commit vivibox makes as you has one source for who you are. For an
  identity of its own in one folder, use `includeIf "gitdir:…"` in your git configuration.
- Work that moved a submodule you have checked out comes back for review without git going to
  the submodule's remote: bringing a task's commits to your repository reads the task's clone
  and nothing else, so it no longer fails where that remote does not answer, or lacks a commit
  the writer made in the submodule. vivibox no longer fetches the submodule's new commits into
  your checkout on the way, and never runs `git submodule update` for you: after accepting, you
  do.
- Accepting work refuses while a submodule commit it keeps is kept by no repository of yours:
  accepting deletes the task, and a commit the writer made inside a submodule exists only in
  the task's clone. Every submodule the work adds or moves is checked, and under it the
  submodules its own commit adds or moves, in your repositories, with nothing fetched and
  within a time limit; with `--branch`, or where your branch has moved since the task started,
  every commit of the work is checked, since a branch keeps them all. A commit counts when
  your submodule's `HEAD` or one of its refs reaches it, not when it is only an object there.
  The refusal names each submodule and commit and the commands that keep it (a fetch in your
  submodule from its remote, or from the task's clone under `refs/vivibox/<id>/…`), ten in
  full and the rest counted, and leaves the task as it was. Then you accept again. It holds
  for every way of accepting: `vivibox accept`, `--branch`, the view's `a` (which says it
  briefly and names the command that says it whole), and a box's work.
- Accepting work also refuses while the history of a submodule commit it keeps points a
  submodule of its own at a commit that no repository of yours keeps: an earlier commit of the
  submodule that a later one moved back stays in your submodule, and the commit it points at
  went with the task. The submodule's new history is listed once in your repository (since the
  commit the task started from; for a submodule the work adds, what no branch of a remote in
  your copy reaches, the rest being trusted to the remote the work names); the refusal names
  the commit of that history and the commit it needs, with the same commands, and the reply
  that is left where neither the remote nor the task has it. A history that cannot be listed
  is refused as what vivibox could not check: with the fetch that brings the commit the task
  started from, where your submodule lacks it, or the git that says why; so is one not read
  within the check's time.
- The verification no longer stops on "Argument list too long" where the work lists many
  folders as submodules that hold no repository (six hundred, with names of four thousand
  characters): git was handed every such path in one command line, the task stopped on the
  system's words and `vivibox verify` ended in a traceback. git is asked of them a handful at
  a time, and each is answered as before. A single path longer than a command line holds
  stops the verification with that reason.
- A refused accept leaves your repository's refs as they were: accepting brings the work
  under `refs/vivibox/<id>` before it checks its submodules, and where no review copy had
  brought it there already, a refusal left that ref behind. It now takes back the ref it
  brought, and never one that was there before, another task's, or the refs you made in your
  submodules with the refusal's commands.
- Accepting work reads a submodule's commits as git keeps them, not as a replace ref of yours
  shows them (`GIT_NO_REPLACE_OBJECTS=1` for the check's git): a commit of a submodule's
  history that moves a pointer was not seen where `git replace` showed another in its place,
  while git's clean-up, a clone and a push keep the commit that was replaced. The `git log`
  a refusal prints for a history it could not list says `--no-replace-objects` too.
- Accepting work as a branch, or where your branch has moved, refuses when git does not list
  the work's commits in your repository: it was accepted on its last commit alone, with the
  earlier commits, which a branch keeps, not looked at. The refusal names the git that says
  why.
- What a refused accept says, where it said something else than what is: a submodule your
  project points at with no entry in `.gitmodules` and no copy in your checkout was to be
  checked out, to which git answers "No url found", and is now to be cloned from where you
  know it to be; "Then accept the work again." no longer follows a `git log` that only says
  why a history could not be listed (the entry says to accept once it lists them); and the
  short form of a refusal with both a commit you do not keep and a submodule vivibox could
  not check names each among what it is (`commits you do not keep (tools), and vivibox could
  not check others (lib)`), where it counted both as commits you do not keep.
- The gate no longer deletes `init.d` and `gradle.properties` from the shared Gradle cache before a
  verification: its Gradle home is its own, so nothing the writer put there runs in the gate, and
  nothing of the writer's is removed.
- `detach-disk` unmounts a task's disk again: the helper held the mounted root open while it
  called `umount`, which is a reference on the mount, so every detach ended "target is busy"
  and the disk stayed mounted after the task was deleted. It now holds the task's directory
  open and unmounts `disk` inside it by the syscall itself (`umount2`, no link followed), and
  refuses, saying so, when the mount is still there after.
- The root helper gives `mount` and `umount` the mount point's descriptor with
  `--no-canonicalize`: without it they read the descriptor's link back to a path and the kernel
  resolved that path anew, so the directory held open protected nothing. `pod_cpus` counts the
  CPUs this process may run on (the affinity set, what Docker counts), the machine's count only
  where the kernel does not say.
- An OOM kill no container of the task's own saw (one the pod's Docker runs) is said as a
  process killed in the pod, with `vivibox limits <id>` alone as the next step, not as the pod
  running out of a budget nothing enforces. `vivibox disk grow <id> 1.5g` hands the helper the
  size in its shape (`1536m`) and says yours back. The task's panel reads the containers' ids
  from the pod's view, filled off the view's thread, and asks Docker nothing itself; its Disk
  line reads free space from the field the supervisor stops the task by.
- Deleting a task from an earlier vivibox removes the five volumes it had before a task's data
  was on its disk (its Docker's store, what Maven installed, the verification's own, the agent's
  home), which stayed behind, listed nowhere, until a `docker volume prune`.
- `vivibox limits <id> --<role>-memory …` checks the task's own limit against this machine's
  `[limits]`, not the defaults: a machine with `pod_memory = "16g"` takes `--agent-memory 8g`,
  and one with `pod_memory = "4g"` refuses `--agent-memory 5g` by name at once, instead of at
  the task's next start.
- An OOM kill is told from the counts read as the turn or the verification began, not from
  counts kept with the task: the kernel's counters start again with every container and with
  the machine, so a second kill after `vivibox limits … --agent-memory` and a start was read as
  the pod's with no option to raise, and one after a restart of the machine was not seen at all.
  The event `resource.limit.exceeded` carries kind, workload, limit, peak or used, and the
  kernel's words.
- The root helper refuses a task's disk whose mount point, image or any directory of the disk
  is a symbolic link, a special file or someone else's, before anything runs: a link at
  `<task>/disk` put there by the person's own uid would have had the disk mounted, and its
  owner changed, wherever the link pointed. The image is made by the helper itself (never
  through a link), the mount point is given to `mount` and `umount` as a directory descriptor,
  and the disk's directories are made and owned through descriptors, 0755 whatever the umask.
  A loop device left by a mount that failed is used again, not doubled.
- A task's disk counts as full, and the task stops with `out of disk space`, when less than
  64 MiB (1 % on a large disk) is left, not only at no block at all: a write that failed with
  "No space left on device" leaves XFS a few blocks free, and the task went on as if it had room.
  The message says what is left (`statvfs: 3 MiB of 1 GiB free`); the panel's Disk line says
  `full` by the same rule, `nearly full` from four fifths as before.
- A pod does not start on a network of its name that is not on the bridge vivibox named, as the
  kernel lists it: this machine's rules would not hold there. Deleting a task says when this
  machine kept its rules (the helper refused), instead of saying nothing; accepting one finishes
  all the same, with the commit step, and says so on the screen and on the task's record.
- Root in a pod can no longer remove its egress rules: they are on your machine, keyed by the
  bridge of the task's network, loaded before the pod's first packet and replaced in one step.
  Private ranges, your LAN, your machine but for the project's host services, other tasks' ranges,
  IPv6, UDP other than DNS and DNS over TLS are refused; the internet stays open. Nothing on
  your LAN reaches the host's address for the pods, where the forwarder and the mirror listen.
- The agent can no longer change your replies. vivibox keeps them in the task's own folder,
  where no agent writes, and the agents and the reviewer read them read-only where they always
  did. A link, a folder or a file the agent leaves where they are read is never followed: a
  link or a file is replaced, and a folder or a special file stops the start with the reason.
  Tasks created before this change are from an earlier vivibox.
- The agent's container no longer holds the reviewer's key, nor the reviewer's container the
  planner's or the writer's, unless they share a provider. A key a container no longer needs,
  such as one for a provider you moved a role off, is removed when that container next starts.
- An agent can no longer make git on your machine run a program, or read objects from elsewhere,
  through what git follows from the task's clone besides its config and hooks: `commondir`,
  `gitdir`, alternates, `config.worktree`, `HEAD` as a link, a submodule's `.git`, a repository
  it adds to the index, or a hook rewritten under its old name. vivibox records all of them when
  it makes the clone and runs no git there, nor copies it for the verification or the review,
  once any differs; the reason names what changed. Alternates are read-only in the pod, and a
  named pipe in those places no longer hangs that check. A link the agent puts in place of a git
  file is no longer mounted into the pod as what it leads to on your machine. An index the agent
  broke, or one past 128 MiB, is refused like the rest, in well under a second, and the risky
  files of a refused clone are all scanned.
- A file name or a line of a risky file that holds a control or an invisible character (ESC, a
  carriage return, a bidi override, a zero-width space) can no longer hide lines or draw text of
  its own in the details panel, in `vivibox risky` and in the messages that name the file: each
  such character is shown as its code point, `‹U+001B›`, and the change is still yours to approve
  as the bytes it is. Backticks, markdown and markup in a name or a file are shown as they are,
  never as formatting or a link. The copy of a large risky file to read has a name of vivibox's
  own, so the `less -L` command shown for it holds nothing the agent named.
- A risky file's line breaks are shown as they are: a line ends at `\n` alone, and a carriage
  return, a form feed or U+2028 in it is shown in its line, so a change from `\r\n` to `\r`
  can no longer be approved unseen, and a diff always names its file and its change.
- What the agent wrote can no longer colour, hide or link anything on your screen: the build's
  output, the verification's feedback and the names of conflicting files in the details panel
  are shown as text; the task's summary in the list, `vivibox status` and the timeline, desktop
  notifications (no markup) and `f`, which now pages the diff with every escape sequence of the
  agent's shown and vivibox's own colours, are too.
- The logs under `l` open in less with `-+R -+r`, whatever your `LESS` says, and the
  verification's log under `w` is shown as text: what the agent's code printed can no longer
  hide lines, make links or move the cursor there.
- A pager other than less (`cat`, `more`, and the `more` vivibox falls back on) reads an escaped
  copy of the log; the files to commit after `a`, the writer's command in its dialog and its
  notification are shown as text, never as markup (`[/b]` in a name no longer breaks the dialog);
  a line break in a message is indented in the supervisor's window; no empty block stands where
  there is no verification feedback.
- No notification or dialog reads text vivibox did not write as markup or passes its control
  characters on: what the agent left running when the app starts, its question on how to run
  it, an earlier task's way to run it, file and branch names, a provider's error. A name with
  `[/b]` in it no longer ends the view. `l` with a pager other than less says why when its copy
  cannot be made instead of ending the view.
- A risky file the task adds or changes that is not text vivibox can show you (bytes that are not
  UTF-8, such as a build file in another encoding, or a binary file such as a wrapper jar) can no
  longer be approved after the word "(binary)": `p` refuses it, and the writer can take its
  change back when you reply; a change you want, you make yourself once you accept the rest.
  A large one is read as text as it is copied for you, and a copy that is not text is deleted.
  Your files that the task did not change are not affected, and text that replaces a binary file
  is shown whole.
- A named pipe an agent puts where git reads its settings or refs in the task's clone
  (`.gitignore`, `.gitattributes`, `.git/info/exclude`, `.git/packed-refs`) no longer hangs the
  verification with no word: git there gets a minute, and then the task waits for you with
  "verification could not run", the file named and no round spent. Bringing the work back, and
  closing a box (whose git is stopped in its pod, your shell left in it), stop with the same
  reason instead of hanging. A file the commits have is to be restored from them, not deleted.
  The reason names the file in the task's clone as text, its control characters escaped, and
  never as part of a command to paste: the names are the agent's.
- The verification's own clone of the task's clone and its fetch of its submodules, and the copy
  of the commits made for the reviewer, no longer wait forever on such a file: past ten minutes
  the verification's container goes down and the verification waits for you, or the review
  stops on an error that names the file.
- A plan brought in twice, by the supervisor from the file your CLI wrote and then by
  `vivibox plan import`, counts once: one goal, one planning in the CLI usage.
- `vivibox init` keeps a quote that belongs to a command from the pipeline, such as the closing
  one of `-pl "$(…)"`.
- vivibox no longer writes through a link or a special file an agent puts in the task's handoff
  folder in place of its feedback, checklist, review, your comments or how to run the app: it
  leaves the file alone and stops the task or refuses the step, saying which file and why. The
  list, `vivibox status` and the details say "verification could not run" with that reason, and
  the details never show what such a file points to or wait on it. Run app reads its instruction
  and its conversation the same way, so a link's file never reaches the pod, and `v` says why
  instead of closing the view.
- vivibox reads the files an agent or the reviewer hands over (its question, plan draft, review,
  checklist, red evidence, proposed command, report for your chat, the preparation's log) without
  following a link or waiting on a FIFO or a device: such a file counts as missing, never as
  something more, and the details, `l` or the verification say which file it is and why. A
  link in place of the reviewer's folder is refused too, and accepting a task leaves such a file
  out of the history, where the timeline names it.
- The verification, the risky files' approval and the project's detection no longer wait on a
  FIFO, a socket or a device in a task's repository, or read a file outside it through a link: a
  link is followed only while it stays inside the repository, so one to a pipeline or build file
  of yours never names a command to the writer. They read no more than a limit either (1 MiB of a
  build file, 64 MiB of a file checked whole, 1 GiB of a risky file hashed in small chunks), so a
  huge or sparse file costs no memory; a risky file past 64 MiB is still checked, and its diff
  says it is too large to show and where to read a copy of it as text, not in your IDE. A new or
  changed risky file, or what a risky link inside the repository points to, can be approved only
  when vivibox showed it to you or copied it for you whole, the very content it approves.
  One verification (its risky and untracked files together), one view of the details and one
  approval each read no more than 1 GiB of all their files, so many large files cannot keep a
  verification or the view busy for minutes; the view and `p` hash each risky file once. A file
  past that is not checked, and says so: such a risky file stays changed and cannot be approved,
  and the verification stops for you with why, with no round of the writer's spent; an untracked
  one fails the verification. A file
  vivibox did not read counts against the check, never for it: the verification lists it as not
  checked, and a risky file too large to verify, or a risky link out of the repository that your
  checkout's base commit of the task does not have with that target, stays changed and cannot be
  approved; the approval says why, and whether it is the writer's to undo. A link out that was
  yours is tolerated by its target alone and never followed. Detection's notes name a build file
  it left unread for its size. A review in your agent's CLI reads vivibox's own copies of the
  checklist, the red evidence and the writer's reply, and its prompt says which one was left out
  and why.
- A verification that could not run (a program missing, no command to run, a handoff file vivibox
  does not read through) no longer counts as passed: the whole build before your review no longer sends
  the work to you as ready when it never ran, no commit is taken as verified by it, and
  `vivibox verify` exits 1.

### Internal

- A test holds that a tool's stderr becomes a message only through `display.said`
  (`tests/test_structure.py`): every other read of it is listed there with why.
- The command line's commands for your decisions on a task (`accept`, `plan`, `reply`,
  `risky`, `approve-risky`, `verify-again`) are in `vivibox/cli_decisions.py`, moved out of
  `cli.py` as they were, which is 641 lines now.
- What a task's status needs of its `question.md` (whether it says anything, or was refused) is
  the bounded start every reader of it takes (`handoff.read_start`,
  `supervisor.read_question`), with bytes that are no text tolerated: the list asks for every
  task at every refresh.
- The test that keeps text vivibox did not write from being read as markup
  (`tests/test_markup_rules.py`) looks at more shapes: an update of a widget, an Option, a
  Selection, a Select's options, an option list filled later, a Button and its label,
  `from_markup`, a border's title. An exception names the function it is in and is counted, so
  a second call of the same words elsewhere is not covered by it. A dialog's title and a
  yes-button's label are escaped, the usage view's notes are made with markup off, and the
  branches of the commit dialog are text objects.
- A task's allocation file (`.task/network.json`) records three ranges, one per role (`w`, `r`,
  `p`), cut from one /27 block of the pool (`pod_network.split_block`); the policy given to the
  root helper lists the networks that have a bridge (`pod_network.MADE_ROLES`: the writer's and
  the reviewer's), the planner's reserved range left out until it has one. The helper takes a
  pool no narrower than a /27, as `config.toml` does. `tests/fixtures/host-policy-roles.nft` is
  the reviewed rule file for two role networks of one task.
- `host/vivibox-host` 3: a policy's host services are each network's own (`networks[i].hostsvc`,
  none without the key) instead of one list for the task. A task's start asks the helper its
  version first and stops on another than this vivibox's, naming both and `host/setup.sh`,
  before anything of your machine is touched; the live tests skip for the same reason.
- The `gate-installed` directory of a task's disk is gone: the gate's Maven installs under the
  gate's own repository in its directory, fresh for every verification, so nothing of the gate's
  is mounted over the shared repository's `installed/` any more. The root helper makes six
  directories on a task's disk instead of seven; a disk made with seven keeps the extra one.
- Your decisions on a task (`accept_plan`, `accept_command`, `plan_prompt`, `import_plan`,
  `save_verify`, `reply`, `verify_again`, `risky_target`, `risky_diffs`, `approve_risky`) moved from
  `actions.py` to `vivibox/decisions.py`, re-exported from `actions` as before.
- `host/vivibox-host` 2 attaches, detaches and grows a task's disk: `attach-disk <id>
  {"dir", "size"}` makes a sparse XFS image `<dir>/disk.img`, formatted once and never over
  a file that holds no filesystem, on a loop device mounted `discard,nosuid,nodev` at
  `<dir>/disk` with the directories the pod binds, owned by the task's owner; `detach-disk`
  unmounts and frees the device and never deletes the image; `grow-disk` enlarges the image, the
  device and the filesystem and refuses to shrink. `dir` is one level under the tasks directory
  of `/etc/vivibox/host.json` (root's) or `/srv/vivibox`, the task's own, with no symbolic link
  in its path; a size is `1g` to `2t`. `--dry-run` prints the steps. `host/setup.sh` installs
  `xfsprogs` and writes that file; `host/uninstall.sh` unmounts every task disk under the tasks
  directory before anything else goes, and removes the file.
- `host/vivibox-host`, the root helper in place of `vivibox-netns`: a closed list of
  operations (`prepare-engine host`, `apply-policy`), every value of its JSON checked before
  anything runs as root, and the host's nft rules written by itself; `--dry-run` prints them.
  `names.bridge` names a task network's bridge, which those rules are keyed by.
- The timeline `l` opens is written to a folder of yours under the temporary one, not into the
  task: looking at a task writes nothing in it.
- The tests reach no Docker but in a test marked `docker`: a `docker` first on PATH answers as an
  empty daemon, and a test that asks it to change something fails.
- The tests send no desktop notification and post to no ntfy topic: the supervisor's notify and
  `ntfy.post` are recorders in every test but one marked `real_notify`.
- The pod's container recipes, its demo commands and its network rules moved from `pod.py` to
  modules of their own (`podspec.py`, `pod_demo.py`, `pod_egress.py`); nothing changes for a person.
- The project's panel, a finished or deleted task's, the projects the view offers and the view's
  kept choices moved from `panel.py` to `panels.py`; nothing changes for a person.
- What the host makes for a container that only reads the work (its harness's working
  directory, its view of the commits) moved from `control.py` to `reading.py`, for the
  reviewer's container to use too; nothing changes for a person.

## [0.1.0] - 2026-09-29

First public version.

### Added

- A task planned in Codex counts what that session used too: its thread from `CODEX_THREAD_ID`,
  each response's usage from the rollout at list prices, and the share of the plan's 5-hour and
  weekly windows used, in the Cost section.
- What an agent's CLI used on a task shows as `$0.41 sub`, in a colour of its own: in the PLAN
  and REVIEW cells, beside today's spending above the list, in a Cost section of the details, in
  `vivibox status`, `u`, `vivibox usage` and `vivibox stats`, and in the help's legend.
- A task planned in Claude Code counts what that session used on it: at the plan's import, each
  review round's import, and when the task is accepted or deleted, vivibox reads the transcript's
  token counts (never its text) and prices them at list prices, apart from money spent on keys.
- A task made with `--plan-in-cli` from Claude Code keeps that session's id and the path of its
  transcript, taken from the environment; `vivibox status <id>` and `vivibox info` name it.
- Supervisor ⇄ Worker with the plan made in an agent's CLI: the CLI reviews each round, with
  `vivibox review <id> --prompt` and `--import`, and `vivibox wait` returns with the reason
  `review` when a round waits for it; blocking notes go back to the writer as a reviewer's do.
- A round waiting for the CLI's review says so in the list (*review in your CLI*), the details
  and a notification, with the way on when that conversation is closed: a new one, or `m` to put
  the supervisor on a model, which then reviews the round itself.
- The vivibox skill offers Supervisor ⇄ Worker to a task planned in the CLI and reviews each of its
  rounds itself, reading only, then sums the rounds up at the end.
- `host/setup.sh` installs the vivibox skill where Claude Code or Codex is installed, and
  `--check` says when a copy is older than the checkout; `host/uninstall.sh` removes vivibox's
  copies.
- `vivibox skill install` and `uninstall`: a skill for Claude Code and Codex that makes a vivibox
  task, plans it with you in the conversation, waits for it and brings every checkpoint back to
  you; `vivibox info` and the view say when a copy is older than vivibox.
- `vivibox info [path] [--json]`: the project of a folder, the flows and which of them a planner
  in your CLI can run, and the roles' models, for an agent's CLI that drives vivibox.
- `vivibox wait <id>...`: returns when a task needs you, is done, has a problem or stopped, with
  `--timeout` (exit code 124) and `--json`, for an agent's CLI that drives vivibox.
- `vivibox new --plan-in-cli`: you plan the task in an agent's CLI (Claude Code, Codex) on your
  machine. The writer's report on the repository is skipped, and `C`, `vivibox plan prompt` and
  the panel give the CLI's prompt alone.
- `{modules}` in a project's verify command, for a project of many modules: a task's plan names
  the modules it changes, and the task is verified with them while its commits stay inside; a
  change outside them is verified with the same command without them, and the timeline names the
  files. `vivibox init` writes such a command for a Maven reactor of ten modules or more.
- "whole build before review" under `k` (`limits.whole_build_before_review`, off): a task
  verified by its modules is built whole once before the work comes to you.
- Shared cache disk usage, shown separately in `u` and `vivibox usage`, including when no tasks
  are live; the breakdown covers all projects without charging shared bytes to each task.

- The agent runs in a pod: an unprivileged container with its own Docker daemon under Sysbox, cut
  off from the host, the LAN and the host's Docker socket. Scratch space in a pod is capped at 4 GB.
- A task goes plan, accept, implement, gate, review, accept. The agent works on a clone and its
  work comes back as a review copy; files that run code on IDE import, `AGENTS.md` among them,
  need approval. The repository's `AGENTS.md` is read as instruction.
- The gate builds and tests the commits on a fresh clone with a build cache of its own, and checks
  the acceptance criteria, the commit messages, switched-off tests and the agent's `red.md`. A
  criterion that is only the build passing is refused.
- Orchestration modes (`agent_orchestration_mode`, in `config.toml`, under `k` and per task under
  `n`): `single_agent` (P+W+R → Gate), `planner_executor` (P → W+R → Gate),
  `planner_writer_reviewer` (P → W → Gate → R ⇄ W, the default) and `supervisor_worker`
  (P → W → Gate → (P+R) ⇄ W). Roles joined in one agent share one conversation and the first
  role's model; a writer that reviews its own work gets a self-review turn before every
  verification; a supervisor, the planner, reviews the worker's work after a green
  verification in the pod, with the plan still in its conversation.
  One limit, `limits.max_rounds` (3), counts the fix turns the writer gets, from the
  verification or from a review, before the work comes to you; your reply gives them back.
  `max_iterations`, `max_reviews` and the reviewer's `mode` are read no more, and a file that
  still has them is told once. Without `[roles.reviewer]` the default mode reviews on the
  writer's model.
- A reviewer on a model of its own reads the work after a green gate; blocking notes go back to
  the writer by themselves. The reviewer reads the writer's reply in a later round. Its cost
  stands in a column of its own. What is reviewed, and what comes to you, is the commit the
  verification ran on: commits that changed since go through it again first.
- A project without a verification command asks the writer for one, and the proposal waits for
  your review before the first verification (`review the command`); a proposal narrowed to some
  tests is refused. The writer is told to allow its own whole build the verification's time
  limit, so its tool's default timeout does not read as a failure.
- A box: a pod of your own in a project, with opencode, to work in by hand (`b`).
- Projects prepare once in a new task's clone (`prepare`), asked for under `i` and by `vivibox
  init` with the build files' suggestion of a build without tests; Node projects get their dependencies
  installed by their lockfile, in the folder a command starts in; Maven keeps what a task
  installs in a volume of its own; Testcontainers images and the host's certificate authorities
  reach the pod.
- Providers, models and MCP servers from an imported `opencode.json`; keys in vivibox's own
  store; a retired model is not offered, and a start on one says so.
- The view: tasks waiting for you first, projects by name, a task's row kept in place; `w`
  watches or talks to the agent, or the verification as it runs, asking which when there is a choice and saying when the verification is over, from inside tmux too; `l`
  reads the timeline, a verification log or the supervisor's; `f` pages the diff; `S` stops by
  force; `h` and `H` show accepted and deleted tasks; the header counts what waits and works,
  today's cost, and says in red when Docker is down or a provider has no key; `?` names every key and
  the build.
- Attachments: `@path` in a task's description copies a file or folder for the agent; a path
  in the repository is a reference. Secrets folders are refused.
- Notifications on your phone through ntfy; a cost warning and a cost limit per task;
  `vivibox stats`, `vivibox timeline`, `vivibox status`. The timeline records what a task started
  with (roles, review, limits, verification, preparation, base, the build of vivibox) and what
  `m` or `e` changed while it ran.
- `host/setup.sh` and `host/uninstall.sh`; Docker's default networks moved off ranges a VPN
  uses; the pod's TCP MSS clamped to the uplink's MTU.
- The commit suggested at acceptance tells the whole of the task's work, its subject from the
  task and every commit of the agent as a line.
- A finished task keeps its timeline, its verification logs, its reviews and the supervisor's
  log with its plan, and `l` on its row reads them.
- Each role's conversation is kept turn by turn in `log/planner.log`, `log/writer.log` and
  `log/reviewer.log`: the prompt in short, what the agent said, the tools it called with their
  arguments cut short, the cost and the time. `l` lists them after the timeline, with their
  turns and cost, on a live task and from a finished one's archive.

- `init` suggests how a Python project is tested and prepared: `uv run --frozen pytest` and
  `uv sync --frozen` with a `uv.lock`, else `pytest` in a throwaway environment from
  `pyproject.toml` or the requirements files, with the dependency group or extra that has
  pytest. uv's cache is shared between tasks and the gate.

- Go and Rust projects: `init` writes the toolchain their files ask for to the project file
  (`tools = ["go@1.25.3"]`, `rust@stable`), which the pod installs with mise for the agent and
  the gate, and suggests `go test ./...` or `cargo test` and a build to prepare with. Go's and
  Cargo's downloads and builds, and Rust itself, are kept in caches shared between tasks.
- `u` shows how long each task took, one a row: its planner's, writer's and reviewer's turns,
  its verifications and the whole task; `vivibox usage [--json]` prints the same. A
  verification's event says how many seconds it took.
- `u` and `vivibox usage` show what each live task's pod uses now: CPU and memory of its
  containers and the disk of its volumes and folder, measured in a thread only while `u` is
  open; `--json` has them per container and per volume.
- Accepted work is committed on the branch the task started on or on a new one named after the
  task (`feature/<title>`, `bugfix/<title>`, or `<title>` for other work, with a number when the
  name is taken); the commit dialog asks which, a new branch first when the task started on
  `main`, and the checkout stays on the branch committed on. `vivibox accept` asks the same.
- The details panel (`d`) has one layout in every state: the header, a line with the project,
  the times and the cost so far, `Next:`, what the state asks of you, then the newest review,
  the acceptance criteria, a Roles list (a line per agent: what it runs on, its turns and its cost, or "not yet") and
  the plan. The review stays in the panel while the writer fixes its notes. Headings of a
  review or a plan no longer stand larger than the panel's own, and the accepted plan leaves its
  criteria to their section, where they are ticked.
- `vivibox new --flow <mode>` picks the task's orchestration mode, as the Flow row in `n`.
- A Docker Hub mirror, off until `hub_mirror = true` under `[network]` in `config.toml`: a
  pull-through cache in one container on your Docker, `vivibox-mirror`, that every pod's daemon
  pulls through, so an image one task pulled is not downloaded again for the next. Each pod
  keeps its own daemon and images; the mirror listens only where pods reach the host, takes no
  pushes, holds no credentials, and forgets a layer nobody pulled for two weeks. When it is
  down, a pod pulls from Docker Hub itself. `vivibox mirror` says what it holds, `vivibox
  mirror remove` removes it.

### Changed

- When `~/.local/bin` is not on PATH yet, `host/setup.sh` also prints the `export PATH=…` line
  that makes `vivibox` work in the terminal it ran in, besides logging in again; the README's
  quick start no longer asks everyone to run it.
- A decision whose pod you stopped (plan, command, work or risky files) says so: `review the work
  · stopped`, marked ‖ in the waiting colour, still among what waits for you.
- The README introduces the task workflow and CLI planning with fewer words. Its 44-second
  tour shows all four flows, CLI plan and review checkpoints, and subscription usage apart
  from API spending; the static view and recording instructions match the current UI.
- The planning prompt for a chat or an agent's CLI asks for criteria that name an input and what
  it must give, not a kind of value; the skill says at the start that `vivibox accept` refuses a
  checkout with staged changes, and offers `--branch`.
- Desktop notifications are off until you turn them on (`desktop = true` under
  `[notifications]`, or under `k`); a `config.toml` that names no value now means off.
- The vivibox skill recommends Supervisor ⇄ Worker for a task planned in the CLI, and Planner →
  Executor for a small, routine change: in three comparisons the quality was the same, and the
  CLI reviewed the work at the end anyway.
- The flow Planner → Writer → Reviewer is `planner_writer_reviewer`, after its title, in
  config.toml and `--flow`; `planner_maker_checker`, its former name, is read still, and
  config.toml's says so once.
- A review has a third section, `## Checked`: what the reviewer read and checked, at least one
  line, so a review with no notes says what it stands on; a review without it goes back to the
  reviewer once, like one without places on its notes.
- `host/setup.sh` says `update` for what is there but older than the checkout (the firewall
  helper, the vivibox skill) or installed from another checkout (the vivibox command), and
  `missing` only for what is not there.
- What a flow is best for says the same in the view as in the README.
- A proposed command that builds one part of the project (`-pl`, `:module:test`, a Go package,
  `cargo -p`, a workspace) is told as narrowed, like one that picks some tests: the first task's
  writer proposes the part that task was about, and every task is verified with it. Keeping the
  command says it is kept for every task of the project.
- The brief says a global install (`mise use --global`, `npm install -g`) is invisible to the
  verification, and that a tool the plan does not call for is a question for you.
- `vivibox usage --json` now returns an object with `tasks` and `shared_caches`, instead of a
  task array alone. `shared_caches` is null when Docker could not be measured.

- The view has one look: a theme of its own (`vivibox/look.py`) with neutral surfaces and one
  accent for what takes your keys. Focus, selection, status and the primary button are drawn
  apart: the focused field's label and band in the accent, the row under the cursor a lighter
  surface that keeps its colours, a status a mark and a word (● waits for you, ✕ failed, ‖
  stopped, ✓ done, a spinner at work), the primary button tinted and solid once focused.
- Every dialog is framed the same: its title in the top edge, its keys in the bottom one
  (`ctrl+s create  esc cancel`), buttons one line high. The pickers (logs, models, editors,
  sessions, roles) are one list dialog; `i`, the folder browser, Add a provider, Providers &
  MCP, the import of an opencode.json and Manage are forms like `n`, their helper buttons (New
  folder…, Import opencode.json…, Manage…, Remove…) in the row they act on, never among the
  closing ones, `ctrl+s` for the import and for Manage's Save; `k` and a project's `e` align
  their rows under upper-case sections, mute what is only shown and mark with › what opens a
  screen.
- The list: the project's name muted in a task's id, times as `now`, `12m`, `3h`, `2d`, numbers
  on the right, a dot for nothing, finished tasks muted; a medium terminal shows the cost as
  one figure (COST); DEMO is APP. Above it a bar says what waits and what works in their
  colours; the footer groups decisions, the row's keys and the keys that work anywhere.
- Words: the modes are "One agent", "Planner, then writer", "Planner, writer, reviewer" and
  "Planner supervises writer", their steps in words (`plan → write → verify → review ⇄ write`);
  the form calls the mode Flow; Build is a list of two answers; settings say "cost limit",
  "verification timeout", "tasks folder", "Review copy".

- Flow under `n`: the modes are Single agent, Planner → Executor, Planner → Writer → Reviewer
  and Supervisor ⇄ Worker, each with what tells it from the others (`3 sessions · independent
  review`). A model row per agent of the flow, named as the flow names it (Agent, Executor,
  Supervisor, Worker), not a planner, writer and reviewer where one agent plays two. Rounds says
  what one round is in the flow. The floating paragraph on hover is gone: under the fields a
  help says the highlighted flow in facts (sessions, the models picked, review, rounds, best
  for) and its diagram, `P → W → Gate → R ⇄ W`; `k` says the same. The Flow list opens upward
  so the help stays in view.
- Focus never resizes a dialog: `k`'s help has a fixed number of lines for the terminal, and
  `n`'s too; moving between rows changes the words, not the frame. The keys that close a dialog
  stand in its last row, inside the frame. In `n`, Flow's details show only while Flow has
  focus; every field has a line of help instead. The workflow reads Plan review, Flow, the
  agents' models, Fix rounds (the number alone). Branch shows the whole name and a › (`Current
  (main)  ›`), Files says `or add @path in Goal`. `k` lists the flow first, then planner,
  writer, reviewer. The list's goal column reaches the screen's edge; live task ids are in the
  foreground; the command bar's words are the secondary tone.
- Contrast: three surfaces a step apart, off-white text for what you read, labels and headings
  a step quieter (secondary), metadata quieter still, disabled controls dim; placeholders
  readable; the focused field has an edge of the accent and selection is a blue-grey tint.

### Fixed

- Tasks started at the same moment no longer fail with "Pool overlaps": a task whose network
  range another took first tries the next one.
- `vivibox plan import` and `vivibox review --import` without a file no longer wait for good on
  a standard input that is an open socket, as Claude Code's shell gives one; and a plan the supervisor brings in from the
  answer file counts the CLI session's planning too.
- The vivibox skill writes exactly the plan the user said yes to; an addition is shown and asked
  about first.
- A test moved within its file, or to another test file, is no longer reported as removed, and
  one in a deleted test file now is.
- `vivibox wait --json` carries `said`, what vivibox said of the task, so a CLI tells the user of
  removed tests; the skill treats a criterion not proved as blocking and sends open notes back
  through the gate instead of fixing them itself.
- A plan's summary longer than a commit subject (72 characters) is refused when the plan is
  brought in or accepted, and the planning prompts give that limit: past it, the proposed commit's
  subject was the agent's last commit, often a review round's small fix.
- The vivibox skill asks once about the plan: the user's yes brings it in and accepts it, and it
  reads the staged work in the review copy (`git diff --cached`).
- `vivibox plan import` from a shell with no terminal (an agent's CLI) takes the answer file
  instead of emptying it with an empty standard input.
- `vivibox plan import` refuses what accepting the plan would, such as a criterion asking the build
  to pass, while the chat can still fix it; the planning prompts say a criterion never does.
- `host/setup.sh` checks the task network pool `network.pool` in `config.toml` sets, not always
  the default, so the advice it gives works. A range something else routes is listed with the
  Docker network that holds it, and the other checks go on instead of stopping there.
- The list's columns are as wide as what they show now: a long status gone from the list no
  longer keeps its column wide and pushes the goal off the screen.
- The list no longer breathes: its columns kept a frame at their headings' width on every
  refresh, which the mouse made visible.
- A Maven or Gradle wrapper is proposed by its path: `./mvnw` when committed executable,
  `bash ./mvnw` when not, in the verification, the preparation and the notes. `bash mvnw` made a
  wrapper that finds the project by `${0%/*}` look for it under `mvnw/`.
- A writer's proposed command written as Markdown (a heading, a fenced block with its language, a
  sentence around the command) gives the command, not "# Verification proposal" or "bash".
- A planner that asks instead of planning (the goal is met already) leaves the task at "agent
  asks" with its question in the panel, not at "review the plan" with the template as the plan,
  whose accept could only fail.
- A planner's draft that is still refused after its repair turn is the plan you edit under `e`,
  not the template.
- A plan `a` refuses says how to fix it: e to edit the plan, r to send it back.
- The paths suggested after `@` in a new task show which one is picked, Enter takes a folder as it
  is (Tab opens it), Escape closes only the list, and a click takes a path without leaving the
  goal.
- `vivibox attach` run without a terminal says it needs one, instead of leaving the agent's
  window running in the pod.
- A command the writer proposes with Maven's or Gradle's debug output on (`mvn -X`, `--debug`,
  copied from jsoup's pipeline) is refused like one that picks some tests: every verification
  log would have been megabytes of it. The proposal prompt says so, and the checkpoint names it.
- `vivibox --version`, the timeline and the history name the commit that runs when vivibox runs
  from a checkout, as an editable install does: they named the version of the day it was
  installed, and every pull since went unnamed in a bug report.
- "Started … (model)" and the timeline's "started on" name the model the writing runs on in the
  task's mode: the planner's in `single_agent`, where they named the writer's, which never ran.
- `vivibox init` on a bun project (`bun.lock`): bun from mise in `tools`, `bun install
  --frozen-lockfile` as the preparation, `bun run test` as the suggestion. The agent had
  installed bun in its own home, which the gate does not have. `bun.lock`, `bun.lockb` and
  `bunfig.toml` are risky files.
- `vivibox init` on a Go module whose `go` line is older than 1.21 names the latest Go: that
  line was a floor nothing enforced, and cobra's `go 1.15` got a Go its tests do not build on.
- A build's output that git ignores (`dist/package.json`) no longer stops a task for approval
  as a risky file: the review copy is made from the commits, and they never carry it.
- A tool the gate's fresh home has no version of (`mise ERROR No version is set for shim: bun`,
  after the agent installed bun in its own) is a failure of the environment: the task waits
  for you instead of spending the writer's attempt.
- A turn that committed nothing builds again when the project's toolchain changed since (a tool
  or a JDK given to it): the reused failure had spent the writer's rounds on what no commit of
  its could change.
- The gate's fresh clone has the project's git submodules, as the task's clone has them: the
  Angular RealWorld app's build read its theme from one. A submodule that cannot be fetched is
  a failure of the environment.
- Yarn downloads persist across verification containers and tasks, for Classic and modern Yarn,
  including the mirror of a project-local cache. Project cache choices remain available;
  installed files and verification build results retain their separate lifetimes.

- A Rust project's build files are risky files, their changes waiting for your approval like
  `pom.xml`'s: `Cargo.toml`, `Cargo.lock`, `build.rs`, `.cargo/`, `rust-toolchain(.toml)`.
  rust-analyzer runs build scripts and proc macros as your IDE opens the project.

- `vivibox new` without the agent image says so before it creates a task, not after, as a task
  that stands as "could not start" (a draft still needs no image). `vivibox delete` run with no
  terminal to answer on says to add `--yes` instead of a traceback.
- A writer asked for a project's verification command is told what the build files and the
  pipeline name (`go test ./...` (go.mod), the pipeline's steps), as `vivibox init` shows them.
- The planners' briefs say what the gate refuses: a criterion never asks any build or test
  command to pass, not only the verification's (the planner wrote `mvn -Dtest=… test passes` and
  took a repair turn in most tasks). A goal the code already meets is a question for you, not a
  plan. The repair turn, when it happens, says why in the timeline (`planner (plan repair: …)`).

- The commit dialog: Message beside the first line of its text, not above it, as Goal under `n`;
  Branch a row apart from the files above it. The README's pictures show the view as it is now,
  a frame of Flow's help under `n` included.

- `?` showed what `o` opens with on a line of its own that a long command (JetBrains Toolbox's
  `~/.local/share/.../idea`) ran off the dialog's edge; it is the `o` line of an "On this machine"
  section, your home as `~`, and every line of the help wraps under its own words, not under
  the keys.

- The orchestration mode under `k` reads in lines: its name, its flow on a line of its own, when
  it fits, what it costs, and the symbols last.
- The new task form fits 80×24 with a reviewer too: Orchestration and Rounds were a scroll away.
- Under `k` and a project's `e`, a line under the list says what the highlighted setting does
  (what a round is, what `cost_warning` does, the orchestration mode it is on with its legend);
  switching the orchestration no longer puts its description in a notification, and a change
  shows on its row without a notification repeating it; when it applies is on that line.
- In `n`, Kind, Branch and Build start in one column, each on a band of its own; Rounds has a
  band like the lists, and Attach… looks like a button.
- The pod has a C compiler (gcc): Rust's build scripts failed with "linker `cc` not found", and a
  Python package built from source had none either.
- `pip install` in a pod outside a virtual environment is refused: it installed into mise's
  Python, shared by every task and the gate, and put one task's clone on the others' import path.
- A Python environment in the clone (`uv sync`'s `.venv`, `.tox`) no longer stops a task for risky
  files: it is skipped while git tracks nothing in it.
- Python in a pod writes no `__pycache__` folders, which stopped the gate as uncommitted files.
- A task's row says what sent the writer on its fix turn (`implementing (round 1/3: 2 criteria
  not met)`), and the supervisor's window logs every change of state with its reason.
- A commit whose message is about the task's files ("Record test red evidence", "Tick criteria")
  is refused by the gate, and the commit `a` proposes leaves such lines out of its body.
- Task columns follow the current terminal width immediately after a resize.
- Settings name what a row is for: "Manual review" and "IDE / text editor (o)" (in the footer
  and `?` too), a "Notifications" group, and "verification gate timeout" shown in minutes and
  edited as `30m` or `1800`.
- New tasks require your Git name and email before opening the form, with commands to set them.
- Esc on an open list in a dialog closes the list; it closed the whole dialog, and a new task's
  description went with it.
- The plan's repair turn, and the review's, went on in a second session briefed as the role
  again, doing the first turn's work over: they continue the role's own conversation.

### Internal

- The repository: a code of conduct, a pull request template and code owners; CI pins its
  actions to commits (on Node 24), reads with a read-only token, and Dependabot proposes updates
  of the actions and the Python dependencies weekly.
- A task's network lives in `pod_network.py`, out of `pod.py`; behavioural runs can go in
  parallel (`-n auto`) under one spending limit for the whole run.
- The unit tests run in parallel (`uv run pytest -n auto`, pytest-xdist), locally and in CI:
  under a minute instead of about five; behavioural runs stay in one process for their limit.
- `pricing.py` holds the list prices of the Claude and OpenAI models an agent's CLI runs on, and
  a task's cost keeps what a subscription's use would have cost apart from money spent on keys,
  in its history line too; a harness turn with `metered = False` counts there.
- The supervisor's review state lives in `review_round.py`, and the prompt rules test reads
  every turn prompt from `prompts.py`.
- Counts ("1 turn", "2 turns") are said by one function, `ui.count`.
- The details panel's sections live in `sections.py`; `panel.py` keeps the header and the states.
- The gate's feedback text moves to `vivibox/feedback.py`.
- The agent's window (tmux, `w`) lives in `window.py`, reached through `actions`.
- `vivibox auth` and `vivibox models` live in `cli_providers.py`, under the size limit.
- `vivibox image` and `vivibox mirror` live in `cli_host.py`, under the size limit.
- What a proposed command is refused for lives in `whole_build.py`; `gate` re-exports it.
- `CHANGELOG.md` is written as the work is done: a change under `vivibox/` changes it in the
  same commit, and `tests/test_structure.py` fails on a branch that does not.
