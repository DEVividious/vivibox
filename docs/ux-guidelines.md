# UX guidelines

Read this before changing anything a person sees: the view (`vivibox/tui.py`, `vivibox/table.py`,
`vivibox/keys_*.py`, `vivibox/panel.py`, `vivibox/ui.py`), the dialogs (`vivibox/dialogs.py`,
`vivibox/browse.py`, `vivibox/providers_ui.py`, `vivibox/settings.py`, `vivibox/logs.py`, `vivibox/widgets.py`),
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
| checks after implementation | verification | verify; "gate" only in code and docs/security.md |
| one round of implementing and verifying | attempt | iteration |
| stopping and continuing a task | stop, start | pause; `resume` is a command line alias |
| removing a task | delete | rm, remove |
| the copy you review | review copy | worktree |
| agent container and its Docker sidecar | pod | container, sandbox |
| running the project for you to look at | run app, stop app | demo, "it" |

Status labels, the only ones allowed:

| Situation | Label | Group |
|---|---|---|
| created, never started | `not started` | Waiting for you |
| planning, agent at work | `planning` | Working |
| plan ready | `review the plan` | Waiting for you |
| manual planner, no plan yet | `plan it yourself` | Waiting for you |
| implementing; from the second attempt `(attempt n/N)` follows | `implementing` | Working |
| verification running | `verifying` | Working |
| risky files changed | `approve risky files` | Waiting for you |
| blocked on the agent's question | `agent asks` | Waiting for you |
| blocked with every attempt used | `verification failed N×` | Waiting for you |
| blocked by something outside the code (Docker, network, a credential, the time limit); no attempt spent | `verification could not run` | Waiting for you |
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

## 3. Keys

- A key is a verb and the selected row is its object: `e` edits, `x` deletes, `o` opens in the
  IDE, `n` makes a new task, `b` opens a box in a project, `w` watches an agent, or the verification while it runs, or enters a box. A letter has one verb; a pair of opposites may share one (`s` for
  start and stop, `v` for run app and stop app).
- Decisions: `a` accept, `r` reply, `p` approve, `g` verify again (a blocked task, without the
  agent). `l` opens the newest log in the pager, `f` the work as a diff in git's.
- `v` runs the app only while nobody works in the pod: once the work is back with you, or in a
  box. The view and `vivibox demo` ask the same function (`actions.demo_allowed`).
- Dialogs: Esc cancels, Enter submits a one-line field, Ctrl+S submits a multi-line one, arrows
  move between fields. A dialog that cannot send what it was given stays open and says why.
- Red is for a button that stops, deletes or forgets. Such a dialog with lasting loss opens with
  Cancel focused.
- The footer shows only keys that do something for the selected row, decisions first, then the
  row's actions, then `n`, `?` and `q`; everything else is under `?`. At 80 columns the whole
  footer fits. A key named in any text is a key the footer offers at that moment: ask the same
  function that enables the key (`watchable`, `check_action`), never repeat its condition.
- The list is projects with their tasks under them. Enter on a project folds or unfolds it, and
  that choice is kept. A folded project's status says how many tasks wait; projects with a task
  waiting come first, and so do those tasks within a project; the cursor starts on the first task
  waiting. A project's own problem (a variable it passes that is not set, a repository that is
  gone) is its status, before any task is created.

## 4. Dialogs

- A dialog is a form: a column of one-word labels on the left, one field per row. A list or a
  checkbox is one row with no frame; the only boxes are a multi-line text and the buttons that
  close the dialog. Related fields form a group, and a blank row separates groups; the lists of
  a group stand a blank row apart too. On a short terminal the rows between them go before the
  multi-line text shrinks below three lines.
- A button that helps fill a field stands in that field's row, compact, or is the last entry of
  that field's list ("+ set up another project…"). The closing row holds one
  primary button, then Cancel, then the key that presses the primary button.
- Focus is one signal: the focused control's text is drawn as the cursor block; a text box shows
  it by its frame.
- A dialog fits 80×24 with every field on the screen. When the terminal is short, the multi-line
  text gives way first, down to one line, before anything scrolls.

## 5. Errors

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

## 7. The next step is always visible

- The panel's first line after the header is `Next:` with the keys that move the task on
  (`tui.next_steps`), before any log or diff. The body explains; it does not repeat the keys.
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

## Known gaps

Rules above that the code does not meet yet. Remove a line when it is fixed.

- §4: only the new task dialog is a form. The other dialogs (`NewProject`, `AddProvider`,
  `ManageItems`, `Browse`, the replies) still frame every field and keep helper buttons among the
  closing ones.
