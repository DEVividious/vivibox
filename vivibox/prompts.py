"""What starts each turn of an agent: the prompts of the supervisor's states, and the words put
before one after an interruption or a preparation. docs/prompt-guidelines.md says how they are
written; tests/test_prompt_rules.py holds them to it.
"""

from __future__ import annotations

from .states import State

# Every prompt that starts a turn ends by naming what ends it: the two endings the brief
# (templates/instructions.md) allows, and nothing else. docs/prompt-guidelines.md says why.
PLAN_PROMPT = """Read the goal in /task/plan.md and explore the repository. Write the plan to
/task/handoff/plan-draft.md, a copy of /task/plan.md filled in:
- keep the header between the +++ lines, except: set summary to one line of at most 100
  characters naming what the task does;
- under "## Acceptance criteria", replace the line "Replace with an observable outcome you can
  check" with concrete "- [ ]" items, each checkable by reading or running code; keep the
  first item;
- fill in the other sections as their <!-- notes --> say. The writer may not remember this
  conversation: the plan says everything.
Do not change code. End the turn when the draft is written, or when a question is in
/task/handoff/question.md."""

PLAN_REPAIR_PROMPT = """The plan draft in /task/handoff/plan-draft.md is not ready: {problem}. Fix
that in the draft. End the turn when it is fixed."""

IMPLEMENT_PROMPT = """The plan in /task/plan.md is accepted. Carry it out: write the code and the
tests, commit, and tick each item in /task/handoff/criteria.md the moment you have verified it.
End the turn when every item is ticked and committed, or when a question is in
/task/handoff/question.md."""

FEEDBACK_PROMPT = """Verification failed. Read /task/handoff/verify-feedback.md: it names what
failed and quotes the lines that say why (the whole output is in /task/handoff/verify.log). Fix
what it names and commit. End the turn when that is done, or, if the cause is outside the code,
when you have written it to /task/handoff/question.md."""

REVIEW_PROMPT = """The verification passed: the build and the tests are green, do not run them.
Read /task/plan.md, /task/handoff/criteria.md, /task/handoff/red.md and /task/handoff/comments.md,
then the work itself: `git diff {base}..HEAD` in the repository you are in. Write
/task/review/review.md with two sections. Under "## Blocking": what keeps the work from being
what the plan says, or from proving it: a test that cannot fail, a criterion ticked but not met,
behaviour the plan rules out. Under "## Not blocking": the rest. Each note is one line,
"path:line — what is wrong and what would make it right"; a section may be empty. Do not report
what the verification already checks: commits, ticks, switched-off tests, red evidence named.
End the turn when the review is written."""

# Before a round after the first: the writer may have answered the last round instead of acting
# on it; a round that does not read the answer repeats its note, and the dispute goes to you.
REVIEW_AGAIN_PREFIX = """Where the writer disagreed with your last round it said why in
/task/handoff/review-N-reply.md (N is the round before this one): read it first. A note it
answered stays under Blocking only with one sentence on why the answer does not hold.

"""

REVIEW_REPAIR_PROMPT = """The review in /task/review/review.md is not one the orchestrator can
read: {problem}. Rewrite it with the two sections, "## Blocking" and "## Not blocking", and a
place (path:line) on every note. End the turn when it is rewritten."""

REVIEW_FIX_PROMPT = """The reviewer read your work. Read the newest /task/handoff/review-N.md (N is
the round): fix every note under Blocking, commit, and keep the ticks in /task/handoff/criteria.md
true. Where you disagree, say why in one paragraph in /task/handoff/review-N-reply.md instead.
End the turn when that is done and committed, or when a question is in
/task/handoff/question.md."""

COMMENT_PROMPT = """The user replied. Read the newest entry in /task/handoff/comments.md and do
what it asks. End the turn when that is done and committed, or when you have written a new
question to /task/handoff/question.md."""

PLAN_COMMENT_PROMPT = """The user commented on your plan. Read the newest entry in
/task/handoff/comments.md and update /task/handoff/plan-draft.md. End the turn when the draft is
updated."""

RESUME_PREFIX = """You were interrupted. Check `git status` and the files in /task/handoff/ for what
is already done, then go on with this:

"""

# Before the first implementing turn of a project that is prepared (Project.prepare): what ran, so
# the writer builds on it instead of building everything again, and where to look when it failed.
PREPARED_PREFIX = """Before this turn the orchestrator ran {commands} once in the repository, so what it
built and installed is there; its output is in /task/handoff/prepare.log. Build only the modules you
change, and all of them in one command (Maven: `-pl core,app`): a module built on its own takes its
neighbours as they were installed, before your change.

"""

# Before the first implementing turn of a task whose project has no verification command: the
# writer, who runs the build anyway, says what builds and tests it; the gate verifies the task with
# that, and you decide on its own whether the project keeps it.
PROPOSE_PREFIX = """No command verifies this project yet. When the work is done, write the one command
that builds the whole project and runs all its tests, as its pipeline would, on one line in
/task/handoff/verify-proposal.md: the orchestrator verifies your work with it, and the user
decides whether the project keeps it. The whole build may take long: run it with a timeout of
{minutes} minutes on your shell tool, the same the verification has; a build cut short by a
timeout is no result and no reason to ask. A command that picks some tests (`-Dtest=`,
`--tests`, `-k`, a test file) is refused, and so is one with the build tool's debug output
(`mvn -X`, `--debug`), which a pipeline may copy and a verification log does not need; you
then propose again.

"""
# After PROPOSE_PREFIX, when the project's files name commands (init.candidates): what they name,
# as data to start from, not an instruction.
PROPOSE_FOUND = """The project's own files name these commands: {commands}. The one its pipeline
runs is the likeliest.

"""


def resume_prompt(state: State) -> str:
    """After a stop or a crash: the state's own prompt again, behind a word about the interruption.
    A session that survived would go on from a bare "continue"; one that was lost would not know
    what the state asks for."""
    return RESUME_PREFIX + {State.PLAN: PLAN_PROMPT, State.IMPLEMENT: IMPLEMENT_PROMPT}[state]


# In a mode where the writer reviews its own work (W+R): after every turn of the writer's, before
# the verification, in the same conversation. The checklist is the brief's; this names it.
SELF_REVIEW_PROMPT = """Before the verification, read your own work as a reviewer would: `git diff
{base}..HEAD`, then /task/plan.md, /task/handoff/criteria.md and /task/handoff/red.md against it.
Look for what the verification cannot see: a test that cannot fail, a criterion ticked but not
met, behaviour the plan rules out, red evidence that is not what the test showed. Fix what you
find and commit, and keep the ticks true; leave what is right alone. End the turn when that is
done and committed, or when a question is in /task/handoff/question.md."""
