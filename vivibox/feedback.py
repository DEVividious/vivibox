"""What the agent reads in handoff/ after a red verification: each failure with the lines that
say why, so the log is there to consult, not to read through. docs/prompt-guidelines.md says how
feedback is written; tests/test_prompt_rules.py holds every failure the gate records to a line here.
"""

from __future__ import annotations

import shutil

from .gate import DEBUG_OUTPUT, MAX_LISTED, SELECTS_TESTS, GateResult
from .task import Task


def _listed(items: list[str], line: str) -> list[str]:
    shown = [line.format(item) for item in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        shown.append(f"  … and {len(items) - MAX_LISTED} more")
    return shown


def feedback(result: GateResult) -> str:
    """What the agent reads in handoff/ before the next iteration: each failure with the lines
    that say why, so the log is there to consult, not to read through."""
    parts = ["# Verification failed\n"]
    if result.environment:
        parts.append(
            f"- Verification could not run: {result.environment}. That is outside the code; the user"
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
    parts += _listed(result.hidden_characters, "- Invisible character, remove it: {}")
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


def write_feedback(task: Task, result: GateResult) -> None:
    handoff = task.meta / "handoff"
    (handoff / "verify-feedback.md").write_text(feedback(result))
    shutil.copyfile(result.log, handoff / "verify.log")
