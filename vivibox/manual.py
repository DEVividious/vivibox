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

import json
import re
import textwrap
import tomllib
from pathlib import Path

from .opencode import HarnessError, Turn
from .plan import HEADING, PlanError, _split_header, parse_plan, without_notes
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
and brief, under about 150 lines: every line of it will be pasted into a chat. Do not change
anything in the repository and do not plan the task. End the turn when context.md is written."""

# The chat decides what the plan says; the header is vivibox's bookkeeping, and it already has it.
# Asking a chat to reproduce a TOML header after an hour of discussion got it back as a line of
# prose, so the chat is asked only for what it decides, and asked last, where it is still read.
FORMAT = """When I say the plan is final, {deliver} It starts with a line
"Summary: <one sentence of at most 100 characters naming what the task does>"{verify}, then the
sections of the plan above as markdown headings, with each acceptance criterion as a "- [ ]" line
that can be checked. Keep the first criterion as it is. The comments in the plan are guidance for
you; leave them out."""
# Who reads the plan. Without it a planning chat asked what UI it was talking to.
READER = """How the plan is used: I paste it into vivibox, a tool that runs coding agents. An agent then
carries it out alone, in an isolated container with a copy of the repository: it writes the code and
tests, commits, and ticks off the acceptance criteria. vivibox then builds and tests the commits on a
fresh clone and checks every criterion is ticked, and I review the result. Nobody can ask you or me
anything on the way, so the plan must say everything, and each criterion must be checkable."""

# What a planning chat asked about in the first real run, and cannot know: vivibox decides these.
DECIDED = """Already decided, not for the plan: the agent keeps red.md and its checklist of criteria in
its own handoff folder, and the task's kind and mode are set. The gate builds and tests the project
with {verify}."""
VERIFY_LINE = (
    ', then a line "Verify: <the command that builds and tests the project once the plan is'
    ' carried out>", e.g. Verify: npm test'
)
# Four backticks: a plan quotes code in blocks of three, which would end a block of three early.
WHOLE_PLAN = "answer with the whole plan in one code block fenced with ````markdown, nothing else in it."

PLAN_BLOCK = re.compile(r"^(```+|~~~+)[^\n]*\n(.*?)^\1\s*$", re.MULTILINE | re.DOTALL)
SUMMARY = re.compile(r"^summary\s*:\s*(.+)$", re.IGNORECASE)
VERIFY = re.compile(r"^verify\s*[:=]\s*(.+)$", re.IGNORECASE)
VERIFY_LIST = re.compile(r"verify\s*=\s*(\[[^\]]*\])", re.IGNORECASE)
# A chat that describes the header in prose ("Header: kind feature, verify = [...]"): vivibox keeps
# its own header, and takes only the verify command from the line.
HEADER_PROSE = re.compile(r"^header\s*:", re.IGNORECASE)


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
    """The task's plan as the agent would get it, without the header, which is vivibox's, and with
    its files named where you can find them."""
    return _body(task.plan_path.read_text()).strip().replace("/task/context/", f"{context_dir}/")


def _attachments(task: Task) -> list[Path]:
    folder = task.meta / "context"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def prompts(task: Task, source: Path, project_verify: list[str] | tuple = ()) -> tuple[str, str]:
    """The prompt for a browser chat and the one for a CLI in your checkout at source."""
    context = task.meta / "handoff" / CONTEXT
    found = context.read_text().strip() if context.exists() else ""
    attached = _attachments(task)
    # A new project has no command yet, so the chat is asked for one; otherwise the project's stands.
    verify = "" if project_verify else VERIFY_LINE

    web = [
        "Plan a software task with me. You cannot see the repository; what an agent found in it is",
        "below. Ask me what you need, discuss the approach, and do not write code.",
        "",
        READER,
        "",
    ]
    if attached:
        web += ["I will attach these files the goal refers to:", *(f"- {p}" for p in attached), ""]
    web += ["# The plan to fill in", "", _plan_template(task, "(attached)"), ""]
    if not found:
        # An empty report is not an empty repository: the writer's turn may have failed.
        found = (
            "Nothing: this is a new project."
            if repository_is_empty(task.repo)
            else "The agent wrote no report on the repository; ask me about it."
        )
    web += ["# What is in the repository", "", found, ""]
    decided = DECIDED.format(
        verify=f"`{' && '.join(project_verify)}`" if project_verify else "the command the plan names"
    )
    web += ["# When the plan is final", "", decided, "", FORMAT.format(deliver=WHOLE_PLAN, verify=verify), ""]

    answer = task.meta / ANSWER
    cli = [
        f"Plan a software task with me. The repository is {source}; read it, and do not change",
        "anything in it or anywhere else except the one file named below. Ask me what you need,",
        "discuss the approach, and do not write code.",
        "",
        READER,
        "",
        "# The plan to fill in",
        "",
        _plan_template(task, str(task.meta / "context")),
        "",
        "# When the plan is final",
        "",
        decided,
        "",
        FORMAT.format(
            deliver=f"write the whole plan to {answer} with your file tool, not to the screen.",
            verify=verify,
        ),
        "",
    ]
    return "\n".join(web), "\n".join(cli)


def write_prompts(task: Task, source: Path, project_verify: list[str] | tuple = ()) -> None:
    web, cli = prompts(task, source, project_verify)
    (task.meta / PROMPT).write_text(web)
    (task.meta / PROMPT_CLI).write_text(cli)


def extract(answer: str) -> str:
    """The plan in what you pasted: the code block holding it, or the whole text. A chat puts words
    around the block, and you will paste those too; a terminal indents what it prints."""
    text = answer.replace("\r\n", "\n")
    blocks = [b for _, b in PLAN_BLOCK.findall(text) if "- [" in b]
    if blocks:
        text = blocks[-1]
    return textwrap.dedent(text).strip() + "\n"


def _body(plan_text: str) -> str:
    try:
        return _split_header(plan_text)[1]
    except PlanError:
        return plan_text


def _plain(line: str) -> str:
    """A line without the emphasis and heading marks a chat or a terminal adds or drops."""
    return re.sub(r"[*_#]", "", line).strip().rstrip(":").strip()


def _verify(value: str) -> list[str]:
    value = value.strip().strip("`")
    if value.startswith("["):
        try:
            found = tomllib.loads(f"v = {value}")["v"]
        except tomllib.TOMLDecodeError:
            return []
        return [c for c in found if isinstance(c, str) and c.strip()]
    return [value] if value else []


def _set(header: str, key: str, value) -> str:
    """One key of the TOML header, replaced where it is or added when the header lacks it."""
    line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    pattern = re.compile(rf"^{key}\s*=.*$", re.MULTILINE)
    return pattern.sub(line, header, count=1) if pattern.search(header) else f"{header.rstrip()}\n{line}"


def assemble(task: Task, answer: str) -> str:
    """Your chat's plan with the task's own header: the chat gives the summary, the sections and
    the criteria; kind, mode and the rest were decided when the task was made. A plan that brings
    its own header is taken as it is."""
    text = extract(answer)
    if text.startswith("+++"):
        return text
    template = task.plan_path.read_text()
    header, template_body = _split_header(template)
    # The sections the task's template has, recognised with or without their '#'.
    sections = {
        _plain(m.group(1)).casefold(): m.group(0).strip()
        for line in template_body.splitlines()
        if (m := HEADING.match(line))
    }
    summary, verify, lines = "", [], []
    for line in text.splitlines():
        plain = _plain(line)
        if not summary and (m := SUMMARY.match(plain)):
            summary = m.group(1).strip().strip("`")
        elif m := VERIFY.match(plain):
            verify = _verify(m.group(1))
        elif HEADER_PROSE.match(plain):
            if m := VERIFY_LIST.search(line):
                verify = _verify(m.group(1))
        elif not line.startswith(("#", " ", "\t", "-")) and plain.casefold() in sections:
            lines.append(sections[plain.casefold()])
        else:
            lines.append(line)
    body = "\n".join(lines).strip()
    # A chat tends to leave out the goal you gave it; the writer should still read it.
    kept = re.search(r"^# Goal\s*$.*?(?=^#|\Z)", template_body, re.MULTILINE | re.DOTALL)
    if kept and not re.search(r"^# Goal\s*$", body, re.MULTILINE):
        body = f"{kept.group(0).strip()}\n\n{body}"
    if summary:
        header = _set(header, "summary", summary)
    if verify:
        header = _set(header, "verify", verify)
    return f"+++\n{header.strip()}\n+++\n\n{body}\n"


def repair_prompt(error: str) -> str:
    """For the same chat, when its answer does not read as a plan."""
    return (
        f"vivibox could not read that plan: {error}. Answer again with the whole plan in one "
        'code block fenced with ````markdown: a "Summary: ..." line first, then the sections as '
        'markdown headings, with the acceptance criteria as "- [ ]" lines under "## Acceptance '
        'criteria". Nothing else in the block.'
    )


def import_answer(task: Task) -> str:
    """Makes your answer the task's plan. Returns its summary; PlanError says what is wrong."""
    plan_text = without_notes(assemble(task, (task.meta / ANSWER).read_text()))
    plan = parse_plan(plan_text)
    if not plan.criteria:
        # With the header vivibox's own, this is what tells a plan from the chat asking a question.
        raise PlanError('no "- [ ]" acceptance criteria under an "Acceptance criteria" heading')
    task.plan_path.write_text(plan_text)
    if plan.summary:
        task.set_goal(plan.summary)
    task.event("plan_imported", criteria=len(plan.criteria))
    task.set_awaiting_plan(False)
    return plan.summary
