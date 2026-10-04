"""What the agent reads in handoff/ after a red verification: each failure with the lines that
say why, so the log is there to consult, not to read through. docs/prompt-guidelines.md says how
feedback is written; tests/test_prompt_rules.py holds every failure the gate records to a line here.
"""

from __future__ import annotations

from . import clone_read, handoff, protection, repo, risky
from .gate import DEBUG_OUTPUT, MAX_LISTED, SELECTS_TESTS, GateResult
from .task import Task


def _listed(items: list[str], line: str) -> list[str]:
    shown = [line.format(item) for item in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        shown.append(f"  … and {len(items) - MAX_LISTED} more")
    return shown


# How the writer takes out what blocks its clone (repo.Blocked), by what it added. Never a
# command line made of a name: the names are the agent's, and are quoted before these as text.
TAKE_OUT = {
    protection.REPOSITORY: (
        "A folder named there has a `.git` of its own and the index lists it as a submodule, which "
        "the orchestrator's git does not go into. Take the folder out of the index first (`git rm -f "
        "--cached` with its path, which leaves its files), then delete the `.git` inside it, add its "
        "files as ordinary files and commit; or delete the folder, when the task does not need it. "
        "With the `.git` deleted and the folder still in the index, none of its files is in your "
        "commits."
    ),
    protection.WORKTREE: (
        "That is a worktree of this repository, which the orchestrator's git does not go into. "
        "Remove it (`git worktree remove` with its path, which `git worktree list` names), then run "
        "`git worktree prune`, so that `.git/worktrees` is gone, and work in the repository's own "
        "folder."
    ),
}


def blocked(found: list[protection.Added]) -> str:
    """For the writer, of a clone its own additions block: which, quoted, and how each kind is
    taken out; never what you are told to do."""
    how = " ".join(TAKE_OUT[what] for what in sorted({change.what for change in found}))
    return f"- Verification could not run: {repo.named_changes(found)}. {how} Nothing is verified until then."


def feedback(result: GateResult) -> str:
    """What the agent reads in handoff/ before the next iteration: each failure with the lines
    that say why, so the log is there to consult, not to read through."""
    parts = ["# Verification failed\n"]
    if found := getattr(result.environment, "found", None):
        parts.append(blocked(found))
    elif said := "; ".join(
        filter(None, [risky.stop_for_the_writer(result.risky_stopped), result.environment])
    ):
        parts.append(
            f"- Verification could not run: {said}. That is outside the code; the user"
            " has been told and will run the verification again. Do not change code for it."
        )
    if result.build_skipped:
        parts.append(f"- The build was not run: {result.build_skipped}. Fix that first.")
    if result.narrowed:
        fault = (
            f"turns on the build tool's debug output ({result.narrowed})"
            if DEBUG_OUTPUT.fullmatch(result.narrowed)
            else f"picks some tests ({result.narrowed})"
            if SELECTS_TESTS.fullmatch(result.narrowed)
            else f"builds one part of the project ({result.narrowed})"
        )
        parts.append(
            f"- The command you proposed {fault}, so it was not run: write to"
            " /task/handoff/verify-proposal.md the one command that builds the whole project and runs"
            " all its tests, as its pipeline would, without debug output."
        )
    if result.unchanged:
        parts.append(
            f"- You committed nothing since the last verification (commit {result.unchanged[:10]}),"
            " so the build was not run again; its result stands:"
        )
    for c in result.commands:
        if not c.ok:
            parts.append(
                f"- Command failed: `{c.command}`. What it said (all of it: /task/handoff/verify.log):"
            )
            # Indented like the list item it is in, every line: an unindented line would end the
            # item, and the closing fence would open a block that swallows the rest.
            said = "\n".join(f"  {line}" for line in (c.said or "(no output)").splitlines())
            parts.append(f"  ```\n{said}\n  ```")
    for c, wrote in result.reworded.items():
        parts.append(
            f"- Criterion reworded, so not counted (you wrote: {wrote}); restore this exact line"
            f" and tick it: {c}"
        )
    parts += [f"- Criterion not ticked: {c}" for c in result.missing_criteria if c not in result.reworded]
    parts += [f"- Commit rule: {p}" for p in result.commit_problems]
    hidden, unread = clone_read.split_checked(result.hidden_characters)
    parts += _listed(hidden, "- Invisible character, remove it: {}")
    parts += _listed(unread, "- {}")
    parts += [f"- Test switched off, switch it on again: {t}" for t in result.switched_off]
    if result.switched_off:
        parts.append(
            "  A test that does not run checks nothing. If something outside the code keeps it from"
            " running here (Docker, network, credentials, a service), write that to"
            " /task/handoff/question.md with the error and end the turn."
        )
    parts += _listed(result.uncommitted, "- Not committed (verification uses your commits only): {}")
    parts += _listed(
        result.no_red_evidence,
        "- No red evidence for test file {}: run its new or changed tests before the change that makes"
        " them pass and record the failing assertion in /task/handoff/red.md, naming the file; a line"
        " naming the file and saying why it has no new test counts too",
    )
    return "\n".join(parts) + "\n"


def write_blocked(task: Task, blocked: repo.Blocked) -> None:
    """The feedback and the log in handoff/ for a clone blocked now, written as your reply sends
    the writer back (decisions.reply): its turn names the feedback, which then says how each
    addition is taken out, whatever verification wrote the two last (none, when the clone was
    blocked after the work was verified). HandoffError where vivibox does not write through
    what is there."""
    folder = task.meta / "handoff"
    handoff.write_text(folder / "verify-feedback.md", feedback(GateResult(folder, environment=blocked.why)))
    handoff.write_text(folder / "verify.log", f"# the verification does not run: {blocked.logged}\n")


def write_feedback(task: Task, result: GateResult) -> None:
    """A file there vivibox does not write through is a problem outside the code: the agent could
    not read its feedback, so the task stops for you and no round is spent."""
    folder = task.meta / "handoff"
    try:
        handoff.write_text(folder / "verify-feedback.md", feedback(result))
        handoff.copy_file(result.log, folder / "verify.log")
    except handoff.HandoffError as e:
        # What happened and why only: the notification and the panel say what to do (press g).
        result.environment = f"{result.environment}; {e.why}" if result.environment else e.why
