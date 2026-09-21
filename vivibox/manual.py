"""The manual planner: you plan in a chat of your own, vivibox writes the prompt and reads the answer.

For planning on a subscription (Claude, Gemini, ChatGPT) or on any model no harness reaches. vivibox
runs no model and holds no credential here: it hands you a prompt and takes back the plan you paste.
Everything after the plan runs as it always does, on the writer's key (ADR-0014).

There are two prompts, one for each place you might plan:

- in a browser, where the model cannot see the repository. The prompt carries what an agent found
  in it: handoff/context.md, written in a turn of the writer's own before you are asked.
- in a CLI on your machine (claude, gemini), where the model reads the repository itself. It reads
  your checkout, not the task's clone: an agent has written to the clone, and a CLI started there
  would load whatever settings and hooks it left for one.

Your answer is kept in the task's own directory, which the agent can read and cannot write, so what
you see when you open it is what you or your chat put there.
"""

from __future__ import annotations

import re
from pathlib import Path

from .opencode import HarnessError, Turn
from .plan import parse_plan, without_notes
from .task import Task

NAME = "manual"
PROMPT = "plan-prompt.md"
PROMPT_CLI = "plan-prompt-cli.md"
ANSWER = "plan-answer.md"
CONTEXT = "context.md"

RECON_PROMPT = """Someone who cannot see this repository will plan the task in /task/plan.md. Read
the goal there, explore the repository, and write /task/handoff/context.md for them: the stack and
its versions, how the project is built and tested (the exact commands), how it is laid out, and the
parts the goal touches, with the code that matters quoted, each quote headed by its path. Be concrete
and brief; they will paste all of it into a chat. Do not change anything in the repository and do
not plan the task. End your turn when context.md is written."""

FORMAT = """When I say the plan is final, {deliver}
Keep the header between the +++ lines, set summary to one sentence of at most 100 characters naming
what the task does, and if verify is empty, set it to the command that builds and tests the project
once the plan is carried out, e.g. verify = ["npm test"]. List concrete, checkable items under
"## Acceptance criteria" as "- [ ]" lines. The comments in the template are guidance for you; drop
them."""
# Four backticks: a plan quotes code in blocks of three, which would end a block of three early.
WHOLE_PLAN = "answer with the whole plan in one code block fenced with ````markdown, nothing else in it."

PLAN_BLOCK = re.compile(r"^(```+|~~~+)[^\n]*\n(\+\+\+\n.*?)^\1\s*$", re.MULTILINE | re.DOTALL)


class Manual:
    """Stands in the planner's place and runs nothing; the supervisor asks you instead."""

    name = NAME
    metered = True
    manual = True

    def turn(self, prompt: str, session: str = "", title: str = "") -> Turn:
        raise HarnessError("the manual planner runs no turns; plan in your own chat")


def repository_is_empty(repo: Path) -> bool:
    """A new project has nothing to report on, and the goal is all the planner needs."""
    return not repo.is_dir() or not any(p.name != ".git" for p in repo.iterdir())


def _plan_template(task: Task, context_dir: str) -> str:
    """The task's plan as the agent would get it, with its files named where you can find them."""
    return task.plan_path.read_text().replace("/task/context/", f"{context_dir}/")


def _attachments(task: Task) -> list[Path]:
    folder = task.meta / "context"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def prompts(task: Task, source: Path) -> tuple[str, str]:
    """The prompt for a browser chat and the one for a CLI in your checkout at source."""
    context = task.meta / "handoff" / CONTEXT
    found = context.read_text().strip() if context.exists() else ""
    attached = _attachments(task)
    files = "".join(f"- {p}\n" for p in attached)

    web = [
        "Plan a software task with me. You cannot see the repository; what an agent found in it is",
        "below. Ask me what you need, discuss the approach, and do not write code.",
        "",
        FORMAT.format(deliver=WHOLE_PLAN),
        "",
    ]
    if attached:
        web += ["I will attach these files the goal refers to:", files]
    web += ["# The plan to fill in", "", _plan_template(task, "(attached)"), ""]
    web += ["# What is in the repository", "", found or "Nothing: this is a new project.", ""]

    cli = [
        f"Plan a software task with me. The repository is {source}; read it, and do not change",
        "anything in it or anywhere else except the one file named below. Ask me what you need,",
        "discuss the approach, and do not write code.",
        "",
        FORMAT.format(deliver=f"write the whole plan to {task.meta / ANSWER} and nothing else there."),
        "",
        "# The plan to fill in",
        "",
        _plan_template(task, str(task.meta / "context")),
        "",
    ]
    return "\n".join(web), "\n".join(cli)


def write_prompts(task: Task, source: Path) -> None:
    web, cli = prompts(task, source)
    (task.meta / PROMPT).write_text(web)
    (task.meta / PROMPT_CLI).write_text(cli)


def extract(answer: str) -> str:
    """The plan in what you pasted: the code block holding it, or the whole text when it starts
    with the header. A chat puts words around the block, and you will paste those too."""
    text = answer.replace("\r\n", "\n")
    if text.lstrip().startswith("+++"):
        return text.strip() + "\n"
    blocks = PLAN_BLOCK.findall(text)
    if blocks:
        return blocks[-1][1].strip() + "\n"
    return text.strip() + "\n"


def repair_prompt(error: str) -> str:
    """For the same chat, when its answer does not read as a plan."""
    return (
        f"vivibox could not read that plan: {error}. Answer again with the whole plan in one "
        'code block fenced with ````markdown: the header between +++ lines first, then the "## Acceptance '
        'criteria" section with "- [ ]" items. Nothing else in the block.'
    )


def import_answer(task: Task) -> str:
    """Makes your answer the task's plan. Returns its summary; PlanError says what is wrong."""
    plan_text = without_notes(extract((task.meta / ANSWER).read_text()))
    plan = parse_plan(plan_text)
    task.plan_path.write_text(plan_text)
    if plan.summary:
        task.set_goal(plan.summary)
    task.event("plan_imported", criteria=len(plan.criteria))
    task.set_awaiting_plan(False)
    return plan.summary
