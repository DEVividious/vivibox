"""The shape of the code, as AGENTS.md states it: a module past 800 lines is split before a feature
is added to it. Checked here so the debt is seen when it is made, not after."""

import ast
import re
import subprocess
import sys
from pathlib import Path

LIMIT = 800
ROOT = Path(__file__).parent.parent

# The docker verbs that start or resume a container.
VERBS = ("run", "start", "restart", "unpause")
# Where one may be composed, by the function that writes the words (how many of the verbs it
# writes), and why: the pod's own, and containers that are not of a pod.
STARTERS = {
    "pod.py": {"Pod.up": 1},  # docker start of the sidecar; the runs of its containers come from podspec
    # the argv of the sidecar, the agent, the gate, the review and the control, built only here
    "podspec.py": {
        "sidecar_command": 1,
        "agent_command": 1,
        "gate_command": 1,
        "review_command": 1,
        "control_command": 1,
    },
    # the Docker Hub mirror, a container of your Docker, not of any pod: made, and started again
    "mirror.py": {"command": 1, "ensure": 1},
    "image.py": {"run_checks": 1},  # the image's checks, a throwaway container on no network
    # a throwaway container that lists an agent CLI's models
    "roles.py": {"fetch_provider_catalog": 1, "provider_models": 1},
    "cli_providers.py": {"cmd_models": 1},  # a throwaway container that lists an agent CLI's providers
}
# The builders of a pod container's argv (podspec, and Pod's methods over them): public, and a
# ready argv to anyone who calls one; so nobody outside the pod does.
BUILDER_NAMES = ("sidecar_command", "agent_command", "gate_command", "review_command", "control_command")
# Every place one is named, as it is written there, and why.
BUILDERS = {
    "podspec.py": [f"def {name}" for name in BUILDER_NAMES],  # defined here
    # Pod's methods over them, and their only callers: up, _outdated_sidecar, gate_up, review_start
    "pod.py": [
        *(f"def Pod.{name}" for name in BUILDER_NAMES),
        *(f"podspec.{name}" for name in BUILDER_NAMES),
        "self.agent_command",
        "self.control_command",
        "self.gate_command",
        "self.review_command",
        "self.sidecar_command",
        "self.sidecar_command",
    ],
    # the view's own review_command: the `review the command` decision, no pod in it
    "keys_plan.py": ["def PlanKeys.review_command", "self.review_command"],
    "tui.py": ["self.review_command"],  # the same, called
}
# What the two guards were before they read the code as a tree: kept for the test of the shapes
# they did not see.
STARTER_AS_TEXT = re.compile(
    r"""["']docker["'][\s,+*\[\]]*(?:["']container["'][\s,+*\[\]]*)?["'](?:run|start|restart|unpause)["']"""
    r"""|["']docker\s+(?:container\s+)?(?:run|start|restart|unpause)\b"""
)
BUILDER_AS_TEXT = re.compile(r"\b(?:sidecar|agent|gate|review|control)_command\b")


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


def git_argv(source: str) -> int:
    """How many argument lists of a source begin with "git": all the guard above counts."""
    lists = (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.List | ast.Tuple))
    firsts = (n.elts[0] for n in lists if n.elts)
    return sum(1 for first in firsts if isinstance(first, ast.Constant) and first.value == "git")


def modules() -> list[tuple[str, str]]:
    """(its path in the package, its source) for every module of vivibox."""
    package = ROOT / "vivibox"
    return [(str(path.relative_to(package)), path.read_text()) for path in sorted(package.rglob("*.py"))]


def docstrings(tree: ast.AST) -> set[int]:
    """The ids of the nodes that are a module's, a class's or a function's docstring."""
    owners = (
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and n.body
    )
    firsts = (owner.body[0] for owner in owners)
    return {id(s.value) for s in firsts if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)}


def strings(tree: ast.AST) -> list[ast.Constant]:
    """Every string a source writes, the pieces of an f-string among them, but for docstrings."""
    told = docstrings(tree)
    constants = (n for n in ast.walk(tree) if isinstance(n, ast.Constant) and id(n) not in told)
    return [n for n in constants if isinstance(n.value, str)]


# git named as a program in a string: the word alone ("git" in an argument list at any place,
# handed to a runner or to shutil.which), a path that ends in it, or a command line with it first,
# after what a shell begins a command with (; & | ( a quote, a new line), after a variable set
# for it (GIT_DIR=x git), after env or exec.
GIT_NAMED = re.compile(r"""(?:^|[;&|('"\n]\s*|/|=\S*\s+|\b(?:env|exec)\s+)(git(?:\s+\S+){0,2})(?:\s|$)""")
# Where a string names git so, as its first words, and why each is no git on the host outside
# repo.run_git and repo._bounded.
GIT_STRINGS = {
    "baseline.py": [
        "git could not",  # words to a person, of the git repo.run_git ran
        "git could not",  # the same
        "git exited with",  # the same
    ],
    "box.py": [
        "git",  # box_git: in the task's container (pod.exec), under timeout
        "git",  # its error's first word, to a person
    ],
    "gate.py": ["git"],  # in the gate's container (pod.gate_exec)
    "image.py": [
        "git",  # the name of a check of the image
        "git --version",  # what the check runs, in a throwaway container of the image
        "git version",  # what it expects there
    ],
    "keys_work.py": ["git diff:"],  # words to a person, of the diff diff_pager ran
    "panel.py": ["git"],  # the diff under f: GIT_ARGV above
    "pod.py": [
        "git",  # _copying_clone: where git's own arguments begin in a docker exec, for its error
        "git",  # gate_clone: in the gate's container (gate_exec)
        "git",  # gate_exec: compared with the command, to bound a git in the container
    ],
    "repo.py": [
        "git",  # run_git: GIT_ARGV above
        "git",  # git, to _bounded: GIT_ARGV above
        "git",  # git's error's first word, to a person
        "git",  # timed_out: the command's first word, to a person
        "git config",  # identity_hint: a command for a person to run
        "git config",  # the same, the email
        "git files in",  # words to a person
        "git on the",  # words to whoever calls git wrongly
    ],
    "submodules.py": [
        "git",  # a command for a person to run, begun
        "git -C",  # the same: fetch in a submodule of yours
        "git -C",  # the same: its fetch of one commit
        "git -C",  # the same: submodule update in the one above
        "git -C",  # the same: submodule update in your checkout
        "git -C",  # the same: update-ref
        "git -C /",  # CLONE: a command for a person to run
        "git -C /",  # CLONE_YOURS: the same
        "git printed more",  # words to a person
    ],
}


def git_named(source: str) -> list[str]:
    """Every place a source names git as a program in a string (GIT_NAMED), and every string it
    splits into words that has the word git, by the first words from git on."""
    tree = ast.parse(source)
    found = {
        (id(node), match.start()): match.group(1)
        for node in strings(tree)
        for match in GIT_NAMED.finditer(node.value)
    }
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        if name != "split":
            continue
        # "…".split() and shlex.split("…"): the string a command line is made of
        for part in (getattr(call.func, "value", None), *call.args):
            for node in strings(part) if part is not None else ():
                words = node.value.split()
                for i in (i for i, word in enumerate(words) if word == "git"):
                    found.setdefault((id(node), node.value.find("git")), " ".join(words[i : i + 3]))
    return sorted(" ".join(said.split()) for said in found.values())


def test_no_string_names_git_as_a_program_but_those_accounted_for():
    """The guard above sees a list that begins with "git" and nothing else: git after env, found
    by shutil.which, in a string split into words, in a command for sh -c, or handed to a runner
    that takes the program as its first argument, is no such list. Every string that names git
    as a program is listed here, with what it is, so a new one is a failure until it runs
    through repo.run_git, repo.git or repo.in_clone (which write no "git" of their own), or is
    accounted for. What this cannot see: a name put together (a letter at a time, from a
    variable or a setting), a command between backquotes (the commands vivibox's sentences and
    prompts name), and the program of a command a person configured (a verify command, a
    pager)."""
    found = {module: named for module, source in modules() if (named := git_named(source))}
    assert found == {module: sorted(named) for module, named in GIT_STRINGS.items()}, (
        "git on the host runs through repo.run_git, repo.git or in_clone"
    )


def test_the_guard_on_git_sees_the_shapes_that_are_no_list_beginning_with_it():
    """Each a git on the host that the count of lists beginning with "git" walks past."""
    for shape in (
        'subprocess.run(["env", "GIT_DIR=x", "git", "status"])',
        'subprocess.run([shutil.which("git"), "status"])',
        'subprocess.run("git status".split())',
        'subprocess.run(shlex.split("env LANG=C git status"))',
        'subprocess.run(shlex.split(f"nice git -C {there} status"))',
        'subprocess.run(["sh", "-c", "cd there && git status"])',
        'subprocess.run(["sh", "-c", "GIT_DIR=x git status"])',
        'subprocess.run(["/usr/bin/git", "status"])',
        'run("git", "status")',
        'os.execvp("git", arguments)',
    ):
        assert git_argv(shape) == 0 and git_named(shape), shape
    assert git_named('"""git status, as a docstring says it."""\nx = ".git"\ny = "the git of yours"') == []
    assert git_named('p = subprocess.run(["git", "status"])') == ["git"], "and the list that begins with it"


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


def scopes(tree: ast.AST):
    """(its dotted name, the nodes that are its own) for the module ("") and every class and
    function in it: what is written in a function is that function's, not the one's around it."""

    def walk(node, name):
        own, inner, left = [], [], list(ast.iter_child_nodes(node))
        while left:
            child = left.pop()
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner.append(child)
            else:
                own.append(child)
                left.extend(ast.iter_child_nodes(child))
        yield name, own
        for child in sorted(inner, key=lambda found: found.lineno):
            yield from walk(child, f"{name}.{child.name}" if name else child.name)

    yield from walk(tree, "")


def _words(text: str) -> list[str]:
    """The words of a string as a shell would take them, near enough: without quotes or brackets."""
    return [word.strip("'\"`();&|") for word in text.split()]


def _is_docker(word: str) -> bool:
    return word == "docker" or word.endswith("/docker")


def starters(source: str) -> dict[str, int]:
    """Where a source composes a docker command that starts or resumes a container: every scope
    that names docker in a string (the word, a path that ends in it, a word of a command line)
    and writes one of VERBS, by how many it writes: a string that is the verb, or a verb after
    docker in a command line. However the two are put together there: side by side, in lists
    added up, with an option of docker's own between them, in a list built a word at a time."""
    tree = ast.parse(source)
    told = docstrings(tree)
    found = {}
    for name, nodes in scopes(tree):
        texts = [n.value for n in nodes if isinstance(n, ast.Constant) and id(n) not in told]
        texts = [text for text in texts if isinstance(text, str)]
        if not any(_is_docker(word) for text in texts for word in _words(text)):
            continue
        count = sum(1 for text in texts if text in VERBS)
        for words in (_words(text) for text in texts if text not in VERBS):
            after = next((i for i, word in enumerate(words) if _is_docker(word)), len(words))
            count += sum(1 for word in words[after + 1 :] if word in VERBS)
        if count:
            found[name or "the module itself"] = count
    return found


def test_only_the_pod_starts_a_container_of_a_pod():
    """Every operation that can start or resume a pod's container reconciles the host's rules first,
    fail-closed, through the pod (up, gate_up, review_start). A docker run, start, restart or
    unpause composed anywhere else would start one without them: the functions that compose one
    are listed here, each with its reason, so a new one, or one more in a listed function, is a
    failure until it is accounted for. What this cannot see: a docker whose name or whose verb
    is no string of the function that runs it (a constant of the module, a setting, an
    argument)."""
    found = {module: made for module, source in modules() if (made := starters(source))}
    assert found == STARTERS, "a new docker run or start: a pod container starts only through pod.py"


def test_the_guard_on_starting_a_container_sees_the_command_however_it_is_put_together():
    """Each starts a container, and none was a match for the guard while it read the source as
    text and looked for the verb right after "docker"."""
    for shape in (
        'run(("docker",) + ("run", "-d", image))',
        'run(["docker", "-H", socket, "run", "-d", image])',
        'run(["docker", "--context", name, "container", "start", name])',
        'run("docker -H unix:///run/x.sock run -d image".split())',
        'run(["/usr/bin/docker", *options, "unpause", name])',
        'run(["sh", "-c", "exec docker --log-level error restart name"])',
        'command = ["docker"]\ncommand += options\ncommand.append("run")',
    ):
        assert not STARTER_AS_TEXT.findall(shape) and starters(shape) == {"the module itself": 1}, shape
    inside = 'class Pod:\n    def again(self):\n        self._run("docker", "-D", "start", self.agent)\n'
    assert starters(inside) == {"Pod.again": 1}
    for quiet in (
        'self._run("docker", "rm", "-f", self.agent)',
        'def f():\n    """docker run, as a docstring says it."""\n    return "start"',
        'def f():\n    run(["docker", "ps"])\n\ndef g():\n    return ["opencode", "run"]',
        'said = "start Docker, then run vivibox doctor"',
    ):
        assert starters(quiet) == {}, quiet


def builder_sites(source: str) -> list[str]:
    """Every place a source names one of the builders of a pod container's argv, as it is
    written there: its definition with the class it is in, the name with what it is asked of
    (self.review_command, podspec.review_command, pod.review_command), an import, a string."""
    tree = ast.parse(source)
    found = []
    for _, nodes in scopes(tree):
        for node in nodes:
            if isinstance(node, ast.Attribute) and node.attr in BUILDER_NAMES:
                found.append(ast.unparse(node))
            elif isinstance(node, ast.Name) and node.id in BUILDER_NAMES:
                found.append(node.id)
            elif isinstance(node, ast.alias) and node.name.split(".")[-1] in BUILDER_NAMES:
                found.append(f"import {node.name}")
            elif isinstance(node, ast.Constant) and node.value in BUILDER_NAMES:
                found.append(repr(node.value))
    for scope, _ in scopes(tree):
        if scope.split(".")[-1] in BUILDER_NAMES:
            found.append(f"def {scope}")
    return sorted(found)


def test_only_the_pod_takes_a_pod_containers_argv():
    """The argv of a pod container is a ready docker run to whoever calls its builder: a caller
    outside the pod would run it past _rules_before with no docker literal of its own. Every
    place that names a builder is listed as it is written, so the view's own review_command
    (the decision on a writer's command) asked of a pod instead is another entry, not the same
    count."""
    found = {module: named for module, source in modules() if (named := builder_sites(source))}
    assert found == {module: sorted(named) for module, named in BUILDERS.items()}, (
        "a pod container's argv is built and run in pod.py only"
    )


def test_the_guard_on_the_builders_tells_the_view_s_own_from_the_pod_s():
    """The view has a review_command of its own. While the guard counted the names in a
    module, calling the pod's in its place left the count as it was."""
    view = (
        "class Keys:\n    def review_command(self, task): ...\n"
        "    def go(self):\n        self.review_command(1)\n"
    )
    for swapped in (
        view.replace("self.review_command(1)", "self.pod.review_command([], {})"),
        view.replace("self.review_command(1)", "podspec.review_command(pod, [], {}, [], net)"),
        view.replace("self.review_command(1)", "getattr(pod, 'review_command')([], {})"),
        view.replace("self.review_command(1)", "pod.gate_command()"),
    ):
        assert len(BUILDER_AS_TEXT.findall(swapped)) == len(BUILDER_AS_TEXT.findall(view)) == 2
        assert builder_sites(swapped) != builder_sites(view), swapped
    assert builder_sites(view) == ["def Keys.review_command", "self.review_command"]
    assert builder_sites("from .podspec import agent_command as made") == ["import agent_command"]


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
    from textual.app import App
    from textual.rlock import RLock
    from textual.widget import Widget

    used = {
        theirs.MarkdownFence: ("highlight", "set_content", "notify_style_update"),
        theirs.MarkdownTable: ("_get_headers_and_rows",),
        Markdown: ("_parse_markdown", "unhandled_token", "update", "source"),
        # blocks.Kept and Panel.gives_up: where Textual hands every error, and the panel's lock
        App: ("_handle_exception", "call_later"),
        Widget: (
            "render_line",
            "get_content_height",
            "get_content_width",
            "get_selection",
            "remove_children",
        ),
    }
    assert callable(RLock) and isinstance(Widget.__new__(Widget).__dict__, dict)
    assert "self.lock = RLock()" in Path(sys.modules[Widget.__module__].__file__).read_text(), "Widget.lock"
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
