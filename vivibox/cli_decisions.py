"""The command line's commands for your decisions on a task: accepting a plan, a command or the
work, bringing a plan in from your chat, replying, the risky files and their approval, verifying
again; apart from cli.py to keep it under the size limit."""

from __future__ import annotations

import argparse
import sys

from . import actions, answers, display, gate, manual, proposal, risky, submodules, ui
from .config import load_project
from .plan import PlanError
from .states import State
from .task import Task


def carry_on(task: Task) -> None:
    """Your decision was "go on": with nobody working on the task, that means starting it."""
    if model := actions.carry_on(task):
        print(f"Started {task.id} ({display.name(model)}): nobody was working on it.")


def cmd_accept(args: argparse.Namespace) -> int:
    task, project = actions.load(args.task)
    st = task.read_state()
    if st.box and st.state is State.IMPLEMENT:
        if where := actions.close_box(task, project):
            print(f"{task.id} closed; its work is ready for your review in {where}")
            print(f"Accept it with: vivibox accept {task.id}")
        else:
            print(f"{task.id} closed; it changed risky files: vivibox risky {task.id}")
    elif st.state is State.CHECKPOINT_PLAN:
        actions.accept_plan(task, project)
        print(f"Plan accepted; {task.id} moves on to implementation.")
        carry_on(task)
    elif st.state is State.CHECKPOINT_COMMAND:
        later = "" if args.verify else proposal.moved(task)  # before the task moves on
        actions.accept_command(task, project, args.verify or "")
        kept = display.name(load_project(project.name).verify[0])
        print(f"{project.name} is verified with `{kept}` from now on; {task.id} is verifying.")
        if later:
            print(f"The writer's file names another command now, which was not kept: `{display.name(later)}`")
        carry_on(task)
    elif st.state is State.CHECKPOINT_FINAL:
        try:
            done = actions.finish(task, project, branch_only=args.branch)
        except submodules.Unkept as e:  # here, the whole of it: the view's short form says so
            raise gate.GateError(e.whole) from None
        report_finished(done)
    else:
        raise gate.GateError(f"{task.id} is in {st.state}; nothing to accept (use 'vivibox reply')")
    return 0


def report_finished(done: actions.Finished) -> None:
    if done.left:
        print(f"vivibox: {done.task_id} is done, but {display.under(done.left)}", file=sys.stderr)
    if done.conflicts:
        print(f"{done.task_id} is done ({done.cost}), but its work conflicts with your checkout in:")
        print("".join(f"  {display.name(f)}\n" for f in done.conflicts), end="")
        print(f"Resolve them in your IDE and commit. The work is also on branch {done.branch}.")
    elif done.branch:
        print(f"{done.task_id} is done ({done.cost}): branch {done.branch} in {done.source}.")
    else:
        branch = actions.current_branch(done.source)
        print(f"{done.task_id} is done ({done.cost}). Uncommitted in {done.source} ({branch}):")
        print(display.terminal(done.status), end="")  # git's names of the agent's files
        offer_commit(done)


def offer_commit(done: actions.Finished) -> None:
    source, message = done.source, done.message
    shown = "".join(f"  {display.name(line)}\n" if line else "\n" for line in message.splitlines())
    if not sys.stdin.isatty():
        example = f" with this message:\n\n{shown}" if message else ""
        print(f"Commit it when you like, e.g.: git -C {source} commit{example}")
        return
    if message:
        print(f"Commit it with this message?\n\n{shown}")
        answer = input("[Y]es, [e]dit the subject, [n]o: ").strip().lower()
    else:  # a box: the message is yours to write
        answer = input("Commit it? [Y]es, with a message you type, [n]o: ").strip().lower()
        answer = "e" if answer in ("", "y", "yes") else answer
    if answer in ("e", "edit"):
        subject, _, rest = message.partition("\n")
        message = input("Subject: ") + ("\n" + rest if rest else "")
    elif answer not in ("", "y", "yes"):
        print("Left uncommitted; review or change it in your IDE and commit when you are ready.")
        return
    branch, create = where_to_commit(done)
    try:
        made = actions.commit_work(source, message, branch, create)
        print(f"Committed{f' on {branch}' if branch else ''}: {display.name(made)}")
    except gate.GateError as e:
        print(f"Not committed: {display.under(str(e))}")


def where_to_commit(done: actions.Finished) -> tuple[str, bool]:
    """A question of its own, after the message: the branch the task started on, or a new one."""
    choices = actions.branch_choices(done)
    if not choices:
        return "", False
    print("Where to commit it?")
    for number, (label, value) in enumerate(choices, 1):
        print(f"  {number}  {label if value == 'start' else f'a new branch: {done.new_branch}'}")
    values = [value for _, value in choices]
    default = values.index(actions.default_branch(done)) + 1 if actions.default_branch(done) in values else 1
    answer = input(f"[{default}]: ").strip()
    value = values[int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= len(values) else default - 1]
    return (done.new_branch, True) if value == "new" else (done.start_branch, False)


def cmd_plan(args: argparse.Namespace) -> int:
    """For a manual planner: the prompt to take to your chat, and bringing its plan back."""
    task, _ = actions.load(args.task)
    if args.action == "prompt":
        print(actions.plan_prompt(task, cli=args.cli), end="")
        return 0
    answer = answers.read(args.file)
    try:
        summary = actions.import_plan(task, answer)
    except PlanError as e:
        # What the plan lacks quotes the plan: a criterion, as the chat wrote it.
        print(f"vivibox: that is not a plan yet: {display.under(str(e))}", file=sys.stderr)
        again = display.terminal(manual.repair_prompt(str(e)))
        print(f"\nSay this in the same chat:\n\n{again}", file=sys.stderr)
        return 1
    said = f": {display.name(summary)}" if summary else ""
    print(f"Plan brought in{said}. Read it, then: vivibox accept {task.id}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    target = actions.reply(task, args.comment, args.criterion)
    added = f", with {len(args.criterion)} new criteria" if args.criterion else ""
    print(f"Sent to the agent{added}; {task.id} goes back to {ui.WORKING[target]}.")
    carry_on(task)
    return 0


def cmd_verify_again(args: argparse.Namespace) -> int:
    task, _ = actions.load(args.task)
    actions.verify_again(task)
    print(f"Verifying {task.id} again, without a turn of the agent.")
    carry_on(task)
    return 0


def cmd_risky(args: argparse.Namespace) -> int:
    """The changes, and the command that approves exactly them: it carries the identity of what
    was printed here (risky.Shown), so approve-risky takes nothing this did not show."""
    task, project = actions.load(args.task)
    view = actions.risky_view(task, project)
    approve = f"'vivibox approve-risky {task.id} {view.identity}'"
    if not view.diffs:
        print("No changes to risky files since your last approval.")
        if task.read_state().state is State.APPROVAL_RISKY:
            print(f"The task still waits for your approval: {approve} moves it on.")
        return 0
    print("\n".join(view.diffs))
    if view.identity:
        print(f"Approve these as shown with {approve}, or reject with a comment for the agent.")
    else:
        print(
            "Nothing is approved while a file above is not shown whole or cannot be approved; each says why."
        )
    return 0


def cmd_approve_risky(args: argparse.Namespace) -> int:
    """Approves what vivibox risky printed, named by the identity its last line gave: files that
    are no longer those are refused, with nothing approved. It prints and approves nothing by
    itself: what one command printed and approved at once, nobody would have read first."""
    task, project = actions.load(args.task)
    again = f"'vivibox risky {task.id}', read what it shows, then run the command it ends with"
    try:
        count, review = actions.approve_risky(task, project, args.shown)
    except risky.NotAsShown as e:
        if args.shown:
            raise risky.NotAsShown(f"{e}: run {again}") from None
        raise risky.NotAsShown(
            "approve-risky approves the risky files as vivibox risky showed them, and was told of "
            f"none: run {again}"
        ) from None
    print(f"Approved {count} change(s) to risky files.")
    if review:
        print(f"Ready for your review in {review}")
    carry_on(task)
    return 0
