# UX guidelines

Read this before changing anything a person sees: the view (`vivibox/tui.py`, `vivibox/table.py`,
`vivibox/keys_*.py`, `vivibox/panel.py`, `vivibox/ui.py`), the dialogs (`vivibox/dialogs.py`,
`vivibox/browse.py`, `vivibox/providers_ui.py`, `vivibox/settings.py`, `vivibox/logs.py`, `vivibox/widgets.py`,
`vivibox/branches.py`, `vivibox/usage_view.py`), their look (`vivibox/look.py`, `vivibox/vivibox.tcss`),
`vivibox/cli.py`, notification texts, the README and `docs/`. The rules can be checked, and
`tests/test_ux_rules.py` checks the mechanical ones. A change to a user-facing string updates the
tables here in the same commit.

## 1. The view never lies

- A status says what is happening now, not what should be happening. When nothing runs, no status
  reads as work in progress.
- Success is reported after the effect, never after the request. A decision that needs the task
  started starts it, and says so.
- Whatever will not move without the person is listed under "Waiting for you". "Stopped" holds only
  the tasks they stopped themselves.
- One model says what a task is doing: `ui.view()`. The list, the details panel and the command
  line render it; none of them works a status out on its own.

## 2. Words

One word per thing, everywhere: list, panel, dialogs, command line output, notifications, README.

| Thing | Word | Not |
|---|---|---|
| unit of work | task | job, run |
| checks after implementation | verification ("verify" as a step of a flow) | "gate", which stays in code and docs/security.md |
| a fix turn the writer is sent on, by the verification or the review | round | attempt, iteration, retry |
| stopping and continuing a task | stop, start | pause; `resume` is a command line alias |
| removing a task | delete | rm, remove |
| the copy you review | review copy | worktree |
| agent container and its Docker sidecar | pod | container, sandbox |
| running the project for you to look at | run app, stop app; the list's column APP | demo, "it" |
| the roles | planner, writer, reviewer | maker, checker, executor, worker, supervisor |
| how the roles share a task | flow (the mode's name, e.g. "Planner, writer, reviewer") | orchestration, outside config.toml's key |

Status labels, the only ones allowed:

| Situation | Label | Group |
|---|---|---|
| created, never started | `not started` | Waiting for you |
| planning, agent at work | `planning` | Working |
| plan ready | `review the plan` | Waiting for you |
| the writer's verification command proposed, in a project with none; the first verification waits for it | `review the command` | Waiting for you |
| manual planner, no plan yet | `plan it yourself` | Waiting for you |
| implementing; from the first fix turn `(round n/N: why)` follows: fix turns used, of the task's limit, and what sent the writer back (`2 criteria not met`, `1 blocking note`) | `implementing` | Working |
| the project's preparation running, the writer's first turn waiting for it | `preparing` | Working |
| verification running | `verifying` | Working |
| the reviewer reading the work | `reviewing` | Working |
| risky files changed | `approve risky files` | Waiting for you |
| blocked on the agent's question | `agent asks` | Waiting for you |
| blocked with every round used | `verification failed N×` | Waiting for you |
| blocked by something outside the code (Docker, network, a credential, the time limit); no round spent | `verification could not run` | Waiting for you |
| work ready | `review the work` | Waiting for you |
| stopped by the person | `stopped` | Stopped |
| stopped by force (`S`), the turn under way lost | `stopped by force` | Waiting for you |
| the agent's turn failed | `agent turn failed` | Waiting for you |
| the supervisor hit an error | `stopped on an error` | Waiting for you |
| stopped at the cost limit | `cost limit reached` | Waiting for you |
| a start failed | `could not start` | Waiting for you |
| at work on paper, no supervisor alive | `not running` | Waiting for you |
| a box, its pod up, for you to work in | `box open` | Working |
| a box whose pod is down (`s`); closing a box is `a`, which brings its work to review | `box stopped` | Stopped |
| accepted | `done` | Done |

Raw state names (`checkpoint:blocked`) never reach the screen. Times are on the person's clock
(`ui.clock`), never cut out of a UTC timestamp.

Stopping a blocked task, including `verification could not run`, shows `stopped`. The panel keeps
the error and offers start, reply and verification again. At plan review, work review and risky-file
approval, stopping just the pod keeps the decision visible in Waiting for you; the next decision
starts the pod when needed.

## 3. Keys

- A key is a verb and the selected row is its object: `e` edits, `x` deletes, `o` opens in the
  IDE / text editor, `n` makes a new task, `b` opens a box in a project, `w` watches an agent, or the verification while it runs, or enters a box. A letter has one verb; a pair of opposites may share one (`s` for
  start and stop, `v` for run app and stop app).
- Decisions: `a` accept, `r` reply, `p` approve, `g` verify again (a blocked task, without the
  agent). `l` opens the newest log in the pager, `f` the work as a diff in git's.
- `v` runs the app only while nobody works in the pod: once the work is back with you, or in a
  box. The view and `vivibox demo` ask the same function (`actions.demo_allowed`).
- Dialogs: Esc cancels, Enter submits a one-line field, Ctrl+S submits a multi-line one, arrows
  move between fields. A dialog that cannot send what it was given stays open and says why.
- Red is for a button that stops, deletes or forgets. Such a dialog with lasting loss opens with
  Cancel focused.
- The footer is a command bar of the keys that do something for the selected row, in three
  groups: decisions, their words in the foreground, then a rule and the row's actions, and at
  the right end `n`, `i`, `?` and `q`; everything else is under `?`. On a narrow terminal the
  keys that work anywhere lose their words first, then the longest words shorten to their first. The bar above the list counts what
  waits for you and what works, what `h` and `H` hide, and what today's tasks have cost, each in
  its meaning's colour; first of all it says, in red, what would keep every task from starting
  (Docker down, a provider without a key). At 80 columns every key of the footer fits. A key named in any text is a key the footer offers at that moment: ask the same
  function that enables the key (`watchable`, `check_action`), never repeat its condition.
- The list is projects with their tasks under them. Enter on a project folds or unfolds it, and
  that choice is kept. A folded project's status says how many tasks wait, and projects with a
  task waiting come first. Within a project a row keeps its place whatever its task does: newest
  first by number, the finished ones under the live ones. What waits says so by its colour and the
  project's count, and the cursor starts on the first task waiting. A project's own problem (a variable it passes that is not set, a repository that is
  gone) is its status, before any task is created.

## 4. Dialogs

- Every dialog is a `widgets.Dialog`: a frame of one thin line, its title in the top edge
  (`New task`, `Delete demo-5?`, `Reply · demo-5`), the keys that close it in the bottom edge
  (`ctrl+s create  esc cancel`), and a padding of one line and two columns. Its content is the
  size it needs, 90 columns at most; on a small terminal it takes the screen less a line. No
  sentence among the fields names a key.
- A dialog that picks one of a list is a `widgets.Choose`: names in a column, what each is muted
  beside it, Enter takes, Esc leaves. The logs, the models, the editors, the sessions and the
  roles are this one dialog.
- A form is a column of one-word labels on the left, one field per row, under upper-case
  section headings (TASK, WORKFLOW; PROJECT, FOR ITS TASKS), a blank row between sections and
  none between rows. The only boxes are a multi-line text and an open list. A text field and a
  button have a band; a list is its value and a faint ▼, its band only with focus. On a short
  terminal the headings go first, then the flow's steps and the blank row between sections,
  before the multi-line text shrinks below three lines of text.
  Roles stand in the order they work: planner, writer, reviewer, and under them Flow (the
  orchestration mode) with Rounds at its right: the mode's name on the list, its steps in words
  on the line under it (`plan → write → verify → review ⇄ write`), and when it fits, the models,
  the trade-off and the legend on hover. The reviewer's model row shows only where the mode has
  a reviewer of its own. Build is a list of two answers (build and test the work; nothing to
  build or test), not a box to tick.
- A button that helps fill a field stands in that field's row, compact (Attach…, Browse…,
  Change…, New folder… beside what the browser picked, Import opencode.json… beside the
  provider's search), or is the last entry of that field's list ("+ set up another project…").
  A screen that manages a list (Providers & MCP) has what changes the list in a row under it,
  the action it is opened for first and primary, and closes with Close alone. An action on the
  highlighted row of a list (Remove…) stands under that list too. Ticking lists (what an
  opencode.json brings, what is on) are sections of a form, `space` ticks and `ctrl+s` presses
  the primary button. The closing
  row holds one primary button, then Cancel; the key that presses the primary button is in the
  frame.
- Every field of a form starts in the same column; Branch, a field that opens a picker, is drawn
  as a list is, flush with the lists and their width.
- A field's help is one place: the line under the fields (`#about`, after a rule) says what the
  focused field is for (`Dialog.field_help`), or in `k` what the highlighted row does. The form
  itself stays one word per label.
- A dialog fits 80×24 with every field on the screen. When the terminal is short, the multi-line
  text gives way first, down to three lines, before anything scrolls.
- A list in a dialog (settings rows, a catalog, the file tree) grows with the terminal: on a tall
  one it shows everything, on a short one it scrolls. Its height is a share of the screen (`vh`),
  never a fixed number of lines.
- A list that opens over the form (a select) is framed and has a background of its own: bare, it
  ends right above the next row, whose value reads as one more of its options.
- The new task's Branch field opens a searchable list of locally known branches. Current is
  first. Selecting another branch changes the task's base, without switching the person's checkout.
  The field names the current branch before the list opens. Loading and errors stay visible while
  typing; only a completed lookup can say there are no matches.
  Acceptance into the checkout requires that it contain the task's base commit. Otherwise it
  refuses without applying changes and offers switching to a branch containing that base or
  `vivibox accept <id> --branch`, so changes outside the review cannot slip into the checkout.
- The proposed commit is prepared with the review copy and visible before acceptance. It describes
  actual commits, using the latest subject with earlier changes as bullets, without copying the
  acceptance checklist. Once the work is applied, the separate
  commit dialog opens while the pod is cleaned up; accepting shows `accepting…` until it finishes.
- The commit dialog is a form: Branch, then Message. Branch offers the branch the task started on
  and a new one named after the task (`feature/`, `bugfix/` or no prefix by the task's kind, a
  number when taken); a task started on `main` or `master` has the new branch chosen, and the
  start branch says "(your main branch)". The checkout stays on the branch committed on.
- Esc works from the inside out: an open list closes, then the dialog, then the one under it;
  never the dialog under an open list.

Settings name what a row is for, not its key, in words, never `snake_case`: the editor section is
"Review copy" and its row "IDE / text editor (o)", the same row on a project's screen, and the
footer and `?` call `o` that too; "Notifications" groups desktop, ntfy topic, server and events;
"rounds" says what the round limit is for; "verification timeout" shows minutes from a minute up
(`30 min`), seconds below that (`45 s`), and its field takes minutes (`30m`) or seconds (`1800`);
"cost warning", "cost limit", "tasks folder"; a project's "preparation", "run app (v)",
"variables". The keys in `config.toml` stay as they are. A row's name and value stand in two
columns under an upper-case section; a row that is only shown is muted, a row that opens a
screen of its own ends in a muted ›, and a value is cut to the dialog's width, never wrapped.

Under the list of `k`, and of a project's `e`, a line says what the highlighted row does, for
someone who has not read the docs (what one round is, what `cost_warning` does). It follows the
highlight and the value: flow describes the mode it is on, its name in bold, its steps
alone on the next line in bold, when it fits and what it costs a line each, the legend of the
signs last, and Enter moving to the next mode changes the line, not a notification. A change shows
on its row, and when it applies ("from a task's next start") is on that line before it is made:
no notification says the new value again. A notification is for what the screen does not show,
such as a value refused and why; it never carries a description, which is gone before it is read.

## 5. Errors

Before opening the new task form, `n` checks the project's Git identity. Missing `user.name`
or `user.email` keeps the list open and shows a bottom-right error toast with the two
`git config --global` commands, and the option to run them without `--global` in the project.
The chosen project's identity is checked again before creating the task.

Every error has three parts, in this order:

1. What happened, in the person's terms ("could not start").
2. Why, in the failing tool's own words, a few lines at most.
3. What to do: a key or a command.

An error belongs to its task. It is written to the task (`Task.set_paused(True, problem=…)`,
`Task.set_problem`) as `"<what happened>: <why>"` and shown at the top of the panel until the task
starts again. A toast may repeat it; a toast alone is not enough, because it is gone in seconds.

## 6. Work in progress

- Anything slower than half a second shows a verb and the spinner in the task's status
  (`busy_with`), and its key is off until it ends.
- A state that can last minutes shows how long it has lasted.
- What costs Docker seconds (`docker stats`, `docker system df`) is asked only by the screen that
  shows it, in its own thread, while it is open: `u`. The list and the panel never wait for it.

## 7. The next step is always visible

- The panel's first line after the header is `Next:` with the keys that move the task on
  (`tui.next_steps`), before any log or diff. The body explains; it does not repeat the keys.
- The panel is a header (`<id> · <status>`), one line of where and how long (project, created,
  updated, the cost so far), `Next:`, what the state asks of the person, and then the same
  sections in the same order, each only when it has something: the newest review
  (`#### Review n: …`), `#### Acceptance criteria n/N`, `#### Roles` (a line per agent: what it
  runs on, its turns and its cost, the total last; roles one agent plays share a line; a list,
  never a table, whose frame takes rows the panel does not have) and `#### The plan`. A
  plan under review is the body itself, not a section below it. A review's or a plan's own
  headings are bold lines, never larger than the section they are in; the accepted plan leaves
  its criteria to their section.
- `vivibox status <id>` ends with the same next steps as commands, never with itself.
- No dead ends: for every label above there is a way out the screen names.

## 8. Narrow and crowded

- At 80×24 the list shows task, status and goal, and the footer shows every decision key.
- With fifteen tasks, the ones waiting for the person are on the first screen.

## 9. Testing UX

- Test what the person sees. Run the view with Textual's Pilot and assert on rendered text with
  `tests/ux.py: screen_text(app)`, or on `tui.detail()` and the command's output; not on widget
  internals.
- A new situation gets a row in the status table, a case in `situations()` in
  `tests/test_ux_rules.py`, and a test of its label, its way out and its footer keys.
- An error path gets a test of its three parts, and that it is still shown after a refresh.
- A success message gets a test that the effect happened.

The writer's OpenCode todos appear as "Writer's steps" during implementation. An item whose
text exactly matches an accepted criterion can mark its checkbox complete; unrelated or reworded
steps never satisfy acceptance criteria. Pending, reopened or cancelled todos never clear verified
criteria. The writer can clear a checkbox in the checklist itself; verification still reads that file.

## 10. Look

`vivibox/look.py` holds the palette and builds the Textual theme from it; the stylesheet uses the
theme's variables and the code the constants, by meaning, never a hue by name
(`tests/test_look.py` fails on `[yellow]` in a view module).

| Token | Means | Used for |
|---|---|---|
| background, surface, panel | where things stand | the list; a dialog; a field's band |
| foreground | what you read | values, goals, a live task |
| muted | what explains | labels, headings, metadata, hints, help, finished tasks |
| faint | what is out of play | placeholders, frames, the dot of an empty cell, the project in an id |
| accent | what takes your keys | the focused field's label and band, a focused button, a key in a hint |
| selection | where the cursor is | the row under the cursor in the list and in a dialog's list |
| working | an agent or the verification at work | a working status, "N working" |
| warning | waits for you | ● statuses, "N waiting for you" |
| error | failed, refused, destroys | ✕ statuses, a problem, the Delete button |
| success | done, passed | ✓ done, a ticked box |

- **Focus** is the accent: the focused row's label, in bold, and its field's band; a focused
  button is solid accent. One row at a time (`.row.-focused`, set by `Dialog`).
- **Selection** is a lighter surface that keeps the row's colours: a status reads the same under
  the cursor (`cursor_foreground_priority="renderable"`).
- **Status** is a mark and a word, so it reads without colour: ● waits for you, ✕ failed, ○ nobody
  runs it, ‖ you stopped it, ✓ done, – deleted, the spinner at work (`look.MARKS`).
- **Primary action** is the one tinted button of a dialog; solid when focused, so Enter's target
  is the solid thing on the screen. A destructive one is tinted red.
- Text has five levels: a dialog's title (bold, in its frame); a section heading (upper case,
  bold, muted); a label (muted); a value (foreground); help and metadata (muted).
- Space: a dialog's padding is one line and two columns; one blank row between sections, none
  between rows; the help under a rule, a blank row above its text.
- A key is named as `key action`, the key bold in the accent, the action muted (`look.hints`),
  in the footer and in a dialog's frame alike; a key in running text is inline code, drawn the
  same way in the panel.
- An empty state says so in muted italics with the key that fills it (`no tasks yet  n new task`).
- The list: a project row is its name in bold after a muted ▾ or ▸; a task is indented, the
  project's part of its id faint; figures and times (`now`, `12m`, `3h`, `2d`) on the right; a
  faint dot for nothing; finished tasks muted. Columns go with the width: under 100, TASK,
  STATUS and GOAL; under 130, CRITERIA, COST (one figure) and UPDATED too; from 130, APP, PLAN,
  IMPL, REVIEW (where someone other than the writer reviews) and CREATED. The goal takes what
  is left and ends in an ellipsis.

## Known gaps

Rules above that the code does not meet yet. Remove a line when it is fixed.

None now.
