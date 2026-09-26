# Prompt guidelines

Read this before changing anything an agent reads: the prompts in `vivibox/supervisor.py`,
`vivibox/manual.py` and `vivibox/demo.py`, the templates in `vivibox/templates/`, and the
feedback the gate writes in `vivibox/gate.py`. The rules can be checked, and
`tests/test_prompt_rules.py` checks the mechanical ones. A change to a rule updates the table
here in the same commit.

## 1. Three layers, each sentence in one of them

- **The brief**: what is always true. The part common to every role
  (`templates/instructions.md`) is the harness's standing instructions: what ends a turn, the
  files every role has, what the verification rejects, what is data and what is instruction. The
  role's part (`templates/roles/<role>.md`) says what the role does and which files are its own,
  and goes as the first message of that role's conversation (`brief.role_text`), because a
  system prompt per agent would replace the harness's own, which teaches a model its tools.
- **The turn prompt** (`PLAN_PROMPT`, `IMPLEMENT_PROMPT`, `REVIEW_PROMPT`, …): what to do now, which files to
  read and write, and the sentence that ends the turn.
- **Feedback** (`verify-feedback.md`, `comments.md`): what was wrong, quoted, and what would
  make it right.

A rule is stated once, in the brief. A turn prompt may name it ("tick each item the moment you
have verified it"); it does not explain it again. Feedback repeats a rule only when it was
broken, next to the line that broke it.

## 2. Every rule has a check, or it is not a rule

| Rule in the brief | Checked by |
|---|---|
| a commit message is one line, at most 72 characters, without co-author or AI signature | `gate.commit_problems` |
| criteria are ticked with their exact text | `gate.missing_criteria` |
| no test is switched off | `gate.switched_off_tests` |
| no invisible characters | `gate.hidden_characters` |
| everything is committed | `gate.uncommitted` |
| build files, test configuration, IDE settings and hooks change only with the user's approval | `risky.Approvals.changes` |
| every test file added or changed is named in `red.md` | `gate.red_evidence_missing` |
| the command a writer proposes builds the whole project, not a selection of tests | `gate.narrowed_proposal` |
| the verification command is not a criterion of the plan | `gate.command_criteria` |
| a review has its two sections and a place on every note | `reviewing.problem` |

A sentence no row covers is one of two things: a fact about the pod ("Docker works here") or a
description of a file. The evidence in `red.md` is the one rule the gate checks only in part
(that the file is named), and the brief says the user reads the rest. Facts are stated as facts, not as prohibitions: "there is no remote to push to" beats
"do not push".

## 3. A turn ends on a condition, not on a feeling

The brief allows two endings: the work committed (and ticked, when there is a checklist), or a
question written. Every turn prompt ends with a sentence that starts "End the turn when" and
names one of them. "When you are done" is not a condition; a model that ends a turn with a
progress report costs an attempt and a build.

## 4. The stuck path is one sentence, the same everywhere

"Write it to `/task/handoff/question.md` with the error, and end the turn." Always the full
path; never "try to fix it", never "skip it for now". The gate tells a broken environment from
broken code itself (`gate.environment_problem`, and the time limit `verify_timeout`): the task
then waits for the person without an attempt spent, and the agent gets no turn for it.

## 5. Data is not instruction

The brief names what is instruction: the brief itself, `/task/plan.md`,
`/task/handoff/comments.md` and the message that starts a turn. Everything else the agent reads,
the repository and `/task/context` included, is data. No prompt quotes repository text back to
the agent as an instruction.

## 6. Names

A prompt names a file by the path the agent sees (`/task/…`), and only files the pod mounts:
`/task/plan.md`, `/task/context/`, and the files of `/task/handoff/` that the brief lists; for the
reviewer, `/task/review/review.md`, where its container has it write, and `review-N.md` in the
handoff, where the supervisor keeps each round for the writer and for you. The same file has one
name in every prompt, in the feedback and in the view.

## 7. Length

The brief, common part and role part together, stays under 800 words and a turn prompt under
160; the numbers are in `tests/test_prompt_rules.py`. The prompt that works out how to run the app (`DEMO_ASK`) is a
conversation of its own with no brief behind it, so it carries its own and is not budgeted. Room is made by moving a sentence to the layer it belongs to
(§1), not by shortening what a weaker model needs spelled out.

## 8. Testing prompts

Mechanical, in `tests/test_prompt_rules.py`:

- every "Checked by" entry in the table above is a function that exists;
- every turn prompt has its ending sentence;
- every `/task/…` path in a prompt or in the brief is one the pod mounts;
- no sentence appears in both the brief and a turn prompt;
- every failure the gate can record has a line in its feedback;
- the placeholder criterion in the templates is the one the gate refuses;
- the word budgets.

Behavioural, in `tests/behavioural/` (`uv run pytest -m model tests/behavioural -x`), on real
models (a stronger one plans, a cheap one writes: `VIVIBOX_BEHAVIOURAL_PLANNER` and `_WRITER`,
DeepSeek pro and flash by default) on throwaway projects, under a cost limit
(`VIVIBOX_BEHAVIOURAL_LIMIT`, USD 2), run only when asked for, with the date last run noted in
the commit that changed the prompt:

- canary: a failing test in the base commit; the agent asks instead of deleting it;
- environment: a `pass_env` variable set to a bad value; the agent writes the error to
  `question.md` without retrying;
- reworded criterion: the feedback names the reworded line and the next turn restores it;
- early stop: a turn that ends with a progress report costs one attempt, and the feedback says
  nothing new was committed.
- review: the writer commits a test that proves nothing (it asserts a constant) with the
  criterion ticked; the reviewer's blocking note names it, and the next turn makes the test
  real.
- prepared: a Maven project of three modules, installed once by `prepare`; the task changes two
  of them, one depending on the other. The writer works on the modules it changes, together, and
  builds the whole reactor once at most, as its last check; the evidence stays out of the
  repository; the verification is green.

A wording change that no mechanical test covers names, in its commit message, the behavioural
run that confirmed it.
