"""The shape of the code, as AGENTS.md states it: a module past 800 lines is split before a feature
is added to it. Checked here so the debt is seen when it is made, not after."""

import ast
import re
import subprocess
from pathlib import Path

LIMIT = 800
ROOT = Path(__file__).parent.parent

# The docker verbs that start or resume a container, as a word of an argument list ("docker",
# "run"; "docker", "container", "start"; ["docker"] + ["run"]) or of a string split into one
# ("docker run -d".split()).
VERB = r"(?:run|start|restart|unpause)"
BETWEEN = r"[\s,+*\[\]]*"
STARTER = re.compile(
    rf'["\']docker["\']{BETWEEN}(?:["\']container["\']{BETWEEN})?["\']{VERB}["\']'
    rf'|["\']docker\s+(?:container\s+)?{VERB}\b'
)
# Where one may be composed, how many, and why: the pod's own, and containers that are not of a pod.
STARTERS = {
    "pod.py": 1,  # docker start of the sidecar; the runs of its containers come from podspec
    # the argv of the sidecar, the agent, the gate, the review and the control, built only here
    "podspec.py": 5,
    "mirror.py": 2,  # the Docker Hub mirror, a container of your Docker, not of any pod
    "image.py": 1,  # the image's checks, a throwaway container on no network
    "roles.py": 2,  # a throwaway container that lists an agent CLI's models
    "cli_providers.py": 1,  # a throwaway container that lists an agent CLI's providers
}
# The builders of a pod container's argv (podspec, and Pod's methods over them): public, and a
# ready argv to anyone who calls one; so nobody outside the pod does.
BUILDER = re.compile(r"\b(?:sidecar|agent|gate|review|control)_command\b")
BUILDERS = {
    "podspec.py": 5,  # defined here
    "pod.py": 16,  # Pod's methods over them, and their only callers: up, gate_up, review_start
    "keys_plan.py": 2,  # the view's own review_command: the `review the command` decision, no pod in it
    "tui.py": 1,  # the same, called
}


# Where an argument list of git's own is written (a list or a tuple that begins with "git"),
# how many, and why. git on the host runs only through repo.run_git and repo._bounded, which
# give it repo.git_environment: your shell's GIT_DIR, GIT_INDEX_FILE or GIT_CONFIG_COUNT
# reaches none of them. git in a container (pod.exec, pod.gate_exec) is no such list: its
# environment is the container's.
GIT_ARGV = {
    "repo.py": 2,  # run_git, and git handing its command to _bounded: both in git_environment
    # the diff under f, run by keys_work.diff_pager in git_environment
    # (tests/test_git_environment.py runs it with every variable exported)
    "panel.py": 1,
    "pod.py": 1,  # compared with a docker exec's command to bound it; runs nothing
}


def git_argv_sites() -> dict[str, int]:
    package = ROOT / "vivibox"
    found = {}
    for path in sorted(package.rglob("*.py")):
        lists = (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.List | ast.Tuple))
        firsts = (n.elts[0] for n in lists if n.elts)
        if count := sum(1 for first in firsts if isinstance(first, ast.Constant) and first.value == "git"):
            found[str(path.relative_to(package))] = count
    return found


def test_git_on_the_host_runs_only_through_the_helper_that_cleans_its_environment():
    """A module that writes git's argument list itself would run it in your shell's environment,
    with whatever GIT_* variables it carries. The sites that write one are listed here, each
    with its reason, so a new one is a failure until it goes through repo.run_git, repo.git or
    repo.in_clone, or is accounted for."""
    assert git_argv_sites() == GIT_ARGV, "git on the host runs through repo.run_git, repo.git or in_clone"


def keyword_sites(name: str, unless) -> dict[str, int]:
    """Where a call in the package has the keyword name, by module, but for a value unless
    accepts."""
    package = ROOT / "vivibox"
    found = {}
    for path in sorted(package.rglob("*.py")):
        calls = (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Call))
        if count := sum(1 for c in calls for k in c.keywords if k.arg == name and not unless(k.value)):
            found[str(path.relative_to(package))] = count
    return found


def constant(value):
    """Whether a node is that constant."""
    return lambda node: isinstance(node, ast.Constant) and node.value is value


def test_one_git_on_the_host_is_handed_how_you_reach_your_remotes():
    """repo.TO_YOUR_REMOTES (your ssh command, your askpass, your certificates) pass to a git
    that is asked for with to_remotes: the fetch of a new clone's submodules in repo.prepare,
    which asks by whether the commit it starts from is your own (repo.yours), and no other.
    The rest of repo.py only hands the choice down, and only by name: the functions take it
    as a keyword alone, so no call says it out of this count's sight, and none says it
    outright."""
    assert keyword_sites("to_remotes", unless=constant(False)) == {"repo.py": 3}, (
        "prepare, by repo.yours; git to run_git; run_git to git_environment"
    )
    assert keyword_sites("to_remotes", unless=lambda node: not constant(True)(node)) == {}


def test_nothing_is_run_through_a_shell():
    """shell=True hands a string to sh: a git in it is no argument list for the guard above, and
    would run in your shell's environment."""
    assert keyword_sites("shell", unless=constant(False)) == {}


# Where a tool's stderr is read outside display.said, how many times, and why: an error built
# from it is escaped and keeps its lines under the first only through that one function.
RAW_STDERR = {
    # the agent CLI's own words, kept as they came for the task's record and the next turn;
    # escaped where they are shown (the panel's block, display.under in status and wait)
    "claudecode.py": 1,
    "opencode.py": 1,
    # the log of the verification and a command's result in it: what the agent reads
    "gate.py": 2,
    "image.py": 1,  # what a check of the image printed, compared with what it expects
    "repo.py": 1,  # the pipe of a git past its time, closed and never read
    "supervisor.py": 1,  # handed to supervisor.notify, which escapes whatever it is given
}


def raw_stderr_sites() -> dict[str, int]:
    """Every read of an attribute stderr (a finished process's, an open pipe's; never sys.stderr)
    that is not within the arguments of display.said, by module."""
    package = ROOT / "vivibox"
    found = {}
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text())
        escaped = {
            id(inner)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "said"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "display"
            for inner in ast.walk(call)
        }
        reads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "stderr"
            and isinstance(node.ctx, ast.Load)
            and not (isinstance(node.value, ast.Name) and node.value.id == "sys")
        ]
        if count := sum(1 for node in reads if id(node) not in escaped):
            found[str(path.relative_to(package))] = count
    return found


def test_what_a_tool_said_on_stderr_becomes_an_error_through_display_said():
    """git's and Docker's words quote names an agent chose: read anywhere else than in a call of
    display.said, they reach a message with their control characters, and their second line at
    the left edge. The reads that stay raw are listed above, each with why, so one more is a
    failure until it goes through display.said or is accounted for. What this cannot see: text
    taken from stdout alone, from an exception's own words, or from a log file."""
    assert raw_stderr_sites() == RAW_STDERR, "a tool's stderr goes into a message through display.said"


# The modules that put the details panel's text together.
PANEL_MODULES = ("panel.py", "panels.py", "sections.py", "panel_resources.py")
# What reads a file an agent may have written, by the name it is called by there.
# A file's text by any way of reading one, the writer's proposed command, and the task's events,
# whose text may quote what an agent chose (a command, a folder's name).
READERS = {
    "read", "handed", "read_handoff", "read_text", "read_bytes", "readlines", "open", "read_question",
    "for_you", "proposed", "events",
}  # fmt: skip
# Where what was read cannot pass for vivibox's own words: the one function for an agent's prose
# and those that end in it, a code block, and the two that make an inline value one line that
# is no markdown (display.code, display.words). Not a parser: what it takes out of a text is
# that text still, and is accounted for where it is read.
KEPT_APART = {"prose", "listed", "plan_text", "told", "fenced", "code", "words"}
# The reads that stand outside such a call, and where each one's text goes.
READS_ACCOUNTED_FOR = {
    "panel.py": [
        "for_you(path)",  # told: its text to prose, vivibox's reason beside it and never in it
        "handed(handoff / 'verify-feedback.md')",  # asked only whether it quotes the log itself
        "handed(handoff / 'verify-feedback.md')",  # into display.fenced, two lines below
        "handed(log)",  # build_said: gate.log_excerpt of it, into display.fenced
        "progress.read(task)",  # the writer's steps, into markdown.listed
        "proposal.proposed(task)",  # the proposed command: display.code, three lines below
        "read(log)",  # the verification's log: its commands into display.code, its tail fenced
        "read(task.plan_path)",  # plans_verify: the plan's commands, each into display.code
        "read_handoff(path)",  # handed's own body
        "supervisor.read_question(task)",  # agents_question: its text to prose, the reason apart
        "task.events()",  # removed_tests: a count, and names into display.code(display.name)
        "task.events()",  # Serena: on or off, and vivibox's own reason, a count of files in it
        "task.events()",  # recent events: vivibox's names for them, each value into display.code
    ],
    "panels.py": [
        "path.read_text()",  # read's own body
        "view_state_path().read_text()",  # view.json, which vivibox writes: never drawn
    ],
    "sections.py": [
        "(task.meta / gate.ACCEPTED_PLAN).read_text()",  # criteria: a count of them
        "(task.meta / gate.ACCEPTED_PLAN).read_text()",  # checklist: each into markdown.listed
        "handoff.read_text(path)",  # the review: counted by parse_review, its notes to prose
        "path.read_text()",  # read's own body
        "task.events()",  # last_gate: passed or failed, and a time
        "task.events()",  # gate_failed: passed or not
        "task.events()",  # build_files_left: a file's name and a command, each display.code
        "task.events()",  # agents: turns counted and summed by vivibox's own names of roles
        "task.plan_path.read_text()",  # criteria: a count of them
    ],
}


def reads_outside() -> dict[str, list[str]]:
    """Every call of a reader in the panel's modules that is not within the arguments of a call
    that keeps its text apart, by module, as the call is written."""

    def called(call: ast.Call) -> str:
        func = call.func
        return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")

    found = {}
    for module in PANEL_MODULES:
        calls = [
            n for n in ast.walk(ast.parse((ROOT / "vivibox" / module).read_text())) if isinstance(n, ast.Call)
        ]
        apart = {
            id(inner)
            for call in calls
            if called(call) in KEPT_APART
            for argument in [*call.args, *call.keywords]
            for inner in ast.walk(argument)
        }
        if reads := sorted(ast.unparse(c) for c in calls if called(c) in READERS and id(c) not in apart):
            found[module] = reads
    return found


def test_what_an_agent_wrote_goes_into_the_panel_through_one_function():
    """A file an agent may have written, the command a writer proposed and the task's events are
    read, in the modules that build the panel's text, within a call that keeps the words apart
    from vivibox's own: markdown.prose (or listed, plan_text, told, which end in it), a code
    block, display.code or display.words, which hold an inline value on one line that is no
    markdown. A parser is no such call: what parse_plan takes out of a plan is the plan's text
    still. Text joined into the panel's lines any other way is read with vivibox's own, where a
    heading in it is a section and an open fence takes the sections after it. The reads outside
    such a call are listed above, each with where its text goes, so one more is a failure until
    it goes through one of them or is accounted for. What this cannot see: where a listed
    read's text goes after it is read (the list says, nothing holds it to that); a read in
    another module that these call by another name (ui.view, ui.modules_note,
    actions.context_notes, reviewing.unreadable, review.proposed_message); the task's state and
    the history's entries. For those, tests/test_agent_prose.py marks every text an agent
    writes and looks for the marks among vivibox's own words."""
    assert reads_outside() == READS_ACCOUNTED_FOR, "an agent's words go into the panel through markdown.prose"


def sites(pattern: re.Pattern) -> dict[str, int]:
    package = ROOT / "vivibox"
    return {
        str(path.relative_to(package)): n
        for path in sorted(package.rglob("*.py"))
        if (n := len(pattern.findall(path.read_text())))
    }


def test_only_the_pod_starts_a_container_of_a_pod():
    """Every operation that can start or resume a pod's container reconciles the host's rules first,
    fail-closed, through the pod (up, gate_up, review_start). A docker run, start, restart or
    unpause composed anywhere else would start one without them: the sites that compose one are
    listed here, each with its reason, so a new one is a failure until it is accounted for."""
    assert sites(STARTER) == STARTERS, "a new docker run or start: a pod container starts only through pod.py"


def test_only_the_pod_takes_a_pod_containers_argv():
    """The argv of a pod container is a ready docker run to whoever calls its builder: a caller
    outside the pod would run it past _rules_before with no docker literal of its own."""
    assert sites(BUILDER) == BUILDERS, "a pod container's argv is built and run in pod.py only"


def test_no_module_is_past_the_limit():
    package = Path(__file__).parent.parent / "vivibox"
    big = {
        str(path.relative_to(package)): lines
        for path in sorted(package.rglob("*.py"))
        if (lines := len(path.read_text().splitlines())) > LIMIT
    }
    assert not big, f"split before adding to it: {big}"


def test_the_panels_parser_is_declared_and_textual_is_bound_below_its_next_major():
    """vivibox/markdown.py imports Textual's parser itself, and its rules hold for the way this
    Textual builds a link, a cell and a tooltip. An install from git reads pyproject.toml, never
    uv.lock: without the bound it would take a Textual nobody ran the panel's tests on."""
    import tomllib
    from importlib.metadata import version

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    bounds = {d.partition(">")[0]: d for d in declared}
    assert bounds["textual"].endswith(",<9") and bounds["markdown-it-py"].endswith(",<5"), declared
    assert bounds["pygments"].endswith(",<3"), "the highlighter the panel asks for a language's name"
    assert version("textual").split(".")[0] == "8" and version("markdown-it-py").split(".")[0] == "4"
    assert version("pygments").split(".")[0] == "2"
    assert bounds.get("rich", "").endswith(",<16") and version("rich").split(".")[0] == "15", (
        "what counts the cells of a code block's row as the row is drawn (blocks._cuts)"
    )


def test_what_the_panel_s_widgets_take_from_inside_textual_is_still_there():
    """vivibox/blocks.py is built on names Textual does not promise (its module of markdown
    widgets is private) and on how its widget walks the tokens. A Textual that moved one would
    fail when the view starts, or at the first table; one that stopped handing an unknown token
    to unhandled_token would fail without a word: an agent's --- would be drawn nowhere."""
    from markdown_it.token import Token
    from textual.widgets import Markdown
    from textual.widgets import _markdown as theirs

    from vivibox import blocks, markdown

    for name in ("MarkdownBlock", "MarkdownBlockQuote", "MarkdownFence", "MarkdownHorizontalRule",
                 "MarkdownTable", "MarkdownTableContent"):  # fmt: skip
        assert isinstance(getattr(theirs, name, None), type), name
    assert {"fence", "code_block", "table_open", "blockquote_open"} <= set(Markdown.BLOCKS)
    used = {
        theirs.MarkdownFence: ("highlight", "set_content", "notify_style_update"),
        theirs.MarkdownTable: ("_get_headers_and_rows",),
        Markdown: ("_parse_markdown", "unhandled_token", "update", "source"),
    }
    for owner, inside in used.items():
        assert all(hasattr(owner, name) for name in inside), owner
    panel = blocks.Panel.__new__(blocks.Panel)  # no app: the walk of the tokens alone
    asked = []
    panel.unhandled_token = lambda token: asked.append(token.type)
    assert list(Markdown._parse_markdown(panel, [Token(markdown.RULE, "hr", 0)])) == []
    assert asked == [markdown.RULE], "a token Textual does not know is handed over, the rule among them"
    (frame, *inner, end) = markdown.parser().parse(markdown.prose("above\n\n---\n\nbelow"))
    assert (frame.type, end.type) == ("blockquote_open", "blockquote_close") and frame.meta[markdown.PROSE]
    assert markdown.RULE in [token.type for token in inner] and "hr" not in [token.type for token in inner]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def test_a_change_under_the_package_has_its_line_in_the_changelog():
    """AGENTS.md: a branch that changes vivibox/ changes CHANGELOG.md in the same change, so the
    release notes are written as the work is done. Compared with main, committed and uncommitted
    alike, so it fails before the commit; on main itself, or where main is unknown (a shallow
    checkout in CI), there is nothing to compare with."""
    branch = git("branch", "--show-current").stdout.strip()
    if branch in ("", "main") or git("rev-parse", "--verify", "-q", "main").returncode:
        return
    changed = git("diff", "--name-only", "main", "--").stdout.split()
    if any(path.startswith("vivibox/") for path in changed):
        assert "CHANGELOG.md" in changed, "a change under vivibox/ needs its line in CHANGELOG.md"
