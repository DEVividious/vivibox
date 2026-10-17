"""What the command line prints is read in a terminal, which obeys what it is sent: an ESC in a
line erases the one above, a carriage return writes over it, a bidi override turns it around,
and a line break in the middle of a value starts a line that reads as vivibox's own. Much of
what is printed quotes text vivibox did not write: a plan's summary, an agent's question, a
command a writer proposed, a build's output, what a tool said.

Every print of the package is looked at here, by what it is made of. Each piece is a literal;
or made by display (terminal, under, said, name and the others of ESCAPED), which show such a
character as its code point; or a number; or JSON, which escapes them itself; or one of the
values vivibox knows to be its own or yours (OWN); or it is listed below with why
(ACCOUNTED_FOR), counted, so that one more print of the same words is looked at for itself."""

import ast
from collections import Counter
from pathlib import Path

import pytest
from test_manual import PLAN, manual_env  # noqa: F401 (a fixture)

PACKAGE = Path(__file__).parent.parent / "vivibox"

# display's functions that leave no character to the terminal as itself.
ESCAPED = ("terminal", "under", "block", "said", "name", "content", "quoted")
# What makes a number, whatever it is given.
NUMBERS = ("len", "int", "float", "sum", "round", "max", "min", "abs")
# The last character of a format that only a number takes ({cost:.2f}, {n:d}, {seconds:g}).
NUMERIC = "dfgeEx%"


def _called(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _of_display(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in ESCAPED
        and isinstance(func.value, ast.Name)
        and func.value.id == "display"
    )


def _json(call: ast.Call) -> bool:
    """json.dumps as it escapes by default: every character outside printable ASCII as \\uXXXX."""
    func = call.func
    whole = isinstance(func, ast.Attribute) and func.attr == "dumps" and ast.unparse(func.value) == "json"
    return whole and not any(k.arg == "ensure_ascii" for k in call.keywords)


class Pieces:
    """What the values a function prints are made of, down to the pieces that are neither
    literal nor escaped nor a number: each as it is written. A name the function assigned is
    what it was assigned, however many times."""

    def __init__(self, function: ast.AST | None):
        self.assigned: dict[str, list[ast.AST]] = {}
        for node in ast.walk(function) if function is not None else ():
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                self.bind(node.targets[0], node.value)
            elif isinstance(node, ast.AnnAssign | ast.NamedExpr | ast.AugAssign) and node.value is not None:
                self.bind(node.target, node.value)
            elif isinstance(node, ast.For | ast.comprehension):  # each of what it goes through
                through = node.iter
                each = through.elt if isinstance(through, ast.GeneratorExp | ast.ListComp) else None
                self.bind(node.target, each if each is not None else ast.Name(f"{ast.unparse(through)}[…]"))

    def bind(self, target, value) -> None:
        if isinstance(target, ast.Name):
            self.assigned.setdefault(target.id, []).append(value)
        elif (
            isinstance(target, ast.Tuple)
            and isinstance(value, ast.Tuple)
            and len(target.elts) == len(value.elts)
        ):
            for one, its in zip(target.elts, value.elts, strict=True):
                self.bind(one, its)
        elif isinstance(target, ast.Tuple):  # unpacked from something: each name is that thing's
            each = value.elt if isinstance(value, ast.GeneratorExp | ast.ListComp) else None
            for one in target.elts:
                if isinstance(one, ast.Name):
                    named = (
                        each if each is not None else ast.Name(f"{ast.unparse(value).removesuffix('[…]')}[…]")
                    )
                    self.assigned.setdefault(one.id, []).append(named)

    def of(self, node, seen: tuple[str, ...] = ()) -> list[str]:
        if node is None or isinstance(node, ast.Constant):
            return []
        if isinstance(node, ast.JoinedStr):
            return [piece for value in node.values for piece in self.of(value, seen)]
        if isinstance(node, ast.FormattedValue):
            spec = ast.unparse(node.format_spec)[2:-1] if node.format_spec else ""
            return [] if spec[-1:] and spec[-1] in NUMERIC else self.of(node.value, seen)
        if isinstance(node, ast.BinOp):
            return self.of(node.left, seen) + self.of(node.right, seen)
        if isinstance(node, ast.IfExp):
            return self.of(node.body, seen) + self.of(node.orelse, seen)
        if isinstance(node, ast.BoolOp):
            return [piece for value in node.values for piece in self.of(value, seen)]
        if isinstance(node, ast.Starred | ast.GeneratorExp | ast.ListComp):
            return self.of(node.value if isinstance(node, ast.Starred) else node.elt, seen)
        if isinstance(node, ast.List | ast.Tuple):
            return [piece for item in node.elts for piece in self.of(item, seen)]
        if isinstance(node, ast.Call):
            return self.of_call(node, seen)
        if isinstance(node, ast.Name) and node.id in self.assigned and node.id not in seen:
            return [p for value in self.assigned[node.id] for p in self.of(value, (*seen, node.id))]
        return [ast.unparse(node)]

    def of_call(self, call: ast.Call, seen: tuple[str, ...]) -> list[str]:
        func, given = call.func, [*call.args, *(k.value for k in call.keywords)]
        if _of_display(call) or _json(call) or _called(func) in NUMBERS:
            return []
        if (
            _called(func) == "map"
            and given
            and ast.unparse(given[0]) in [f"display.{name}" for name in ESCAPED]
        ):
            return []  # map(display.name, names): each of them escaped
        # ", ".join(…) and "{} of {}".format(…) are what goes into them; a text cut, padded, in
        # another case or with a piece replaced is itself and what it was given.
        written = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Constant | ast.JoinedStr)
        if isinstance(func, ast.Attribute) and (func.attr in SAME_TEXT or (written and func.attr in MADE_OF)):
            return self.of(func.value, seen) + [piece for arg in given for piece in self.of(arg, seen)]
        return [ast.unparse(call)]


# Methods of a string that add nothing to it but what they are given.
SAME_TEXT = ("strip", "rstrip", "lstrip", "ljust", "rjust", "lower", "upper", "capitalize", "replace")
MADE_OF = ("join", "format")


def prints(source: str) -> list[tuple[str, int, list[str]]]:
    """(the function it is in, its line, its pieces) for everything a source sends to the
    terminal: print, but for one into a file of its own; a write to sys.stdout or sys.stderr,
    by either name of them; a question (input, getpass); and what the process is ended with
    (sys.exit, SystemExit), which Python prints when it is words."""
    tree = ast.parse(source)
    found = []

    def visit(node, scope: list[str], function) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = child if not isinstance(child, ast.ClassDef) else function
                visit(child, [*scope, child.name], inner)
                continue
            if isinstance(child, ast.Call) and _to_the_terminal(child):
                pieces = Pieces(function)
                found.append((".".join(scope), child.lineno, [p for a in child.args for p in pieces.of(a)]))
            visit(child, scope, function)

    visit(tree, [], tree)
    return found


def _to_the_terminal(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "print":
        to = next((k.value for k in call.keywords if k.arg == "file"), None)
        return to is None or _a_stream(to)
    if isinstance(func, ast.Name) and func.id in (*PROMPTS, *EXITS):
        return True
    return isinstance(func, ast.Attribute) and (
        (func.attr == "write" and _a_stream(func.value))
        or ast.unparse(func) in ("getpass.getpass", "sys.exit")
    )


def _a_stream(node) -> bool:
    """Whether what is written to is the terminal's: sys.stdout or sys.stderr, by that name, by
    the bare one an import from sys gives it, or as one of the two a choice is made between."""
    if isinstance(node, ast.IfExp):
        return _a_stream(node.body) or _a_stream(node.orelse)
    return ast.unparse(node) in STREAMS


STREAMS = ("sys.stdout", "sys.stderr", "sys.__stdout__", "sys.__stderr__", "stdout", "stderr")
# What ends the process with words: Python prints them on stderr before it goes.
EXITS = ("SystemExit",)
# What asks you something in the terminal, its question printed first: input, and the one
# function of the command line that asks through it (cli_providers._yes).
PROMPTS = ("input", "_yes")


# The values vivibox knows to be its own or yours, wherever they are printed, and why none
# can hold what an agent or a tool wrote.
OWN = {
    "task.id": "a task's id: letters, digits and dashes (task.TASK_ID)",
    "done.task_id": "the same, of a task that is finished",
    "task['id']": "the same, in a row vivibox wait made",
    "project.name": "a project's name: letters, digits and dashes (config.PROJECT_NAME)",
    "time.strftime('%H:%M:%S')": "the clock",
    "version.current()": "vivibox's version, from git's tag",
    "image.image_ref()": "the agent image's name with the digest of its definition",
    "config_dir()": "your folder of vivibox's settings",
    "keys.store()": "your folder of keys",
}


def own(piece: str) -> bool:
    """One of OWN; what you typed on the command line (args.…); or a constant of vivibox's, by
    its name in capitals (mirror.NAME, ui.WORKING[target])."""
    name = piece.split("[")[0].split(".")[-1]
    return piece in OWN or piece.startswith("args.") or (name.isupper() and name.isidentifier())


YOURS = "yours: a folder, a branch or a setting of your machine"
ESCAPED_THERE = "a whole text of vivibox's, which escapes what it quotes where it is made"
# (module, the function it is in, the piece as written): how many times, and why it is no text
# of an agent's or a tool's sent to the terminal as it is.
ACCOUNTED_FOR: dict[tuple[str, str, str], tuple[int, str]] = {
    ("cli.py", "cmd_new", "task.read_state().base_commit[:10]"): (1, "a commit's id, as git gave it"),
    ("cli.py", "cmd_new", "task.plan_path"): (1, "a file of the task's, under your tasks folder"),
    ("cli.py", "cmd_init", "found.name"): (2, "a name made of your folder's (init.project_name)"),
    ("cli.py", "cmd_init", "found.repo"): (1, YOURS),
    (
        "cli.py",
        "cmd_init",
        "actions.setup_project(where, name, found.verify, found.java, create=args.git, prepare=found.prepare)",  # noqa: E501
    ): (1, "the project file it wrote, in your folder of settings"),
    ("cli.py", "cmd_timeline", "timeline.render(task)"): (1, f"{ESCAPED_THERE} (timeline.lines)"),
    (
        "cli.py",
        "cmd_status",
        "ui.task_detail(task, _criteria, config.max_rounds, args.events, style, running(task))",
    ): (1, f"{ESCAPED_THERE}, with its own colours (ui.task_detail)"),
    (
        "cli.py",
        "cmd_status",
        "ui.task_list(tasks, _criteria, config.max_rounds, style, running=running, finished=finished)",
    ): (1, f"{ESCAPED_THERE}, with its own colours (ui.task_list)"),
    ("cli.py", "cmd_status", "still_running([task])[…]"): (
        2,
        "a sentence of vivibox's about Docker, or nothing",
    ),
    ("cli.py", "cmd_status", "still_running(tasks)[…]"): (2, "the same"),
    (
        "cli.py",
        "cmd_rm",
        "ui.view(task, st, actions.supervisor_running(task), load_config().max_rounds).status",
    ): (1, "a status of the table in docs/ux-guidelines.md (tests/test_ux_rules.py)"),
    ("cli.py", "cmd_rm", "actions.remove(task, project)"): (
        1,
        "the review copy's folder, of vivibox's making",
    ),
    ("cli.py", "cmd_pod", "pod.agent"): (1, "the container's name, made of the task's id"),
    ("cli.py", "cmd_verify", "result.build_skipped"): (
        1,
        "one of two sentences of vivibox's (gate.run_gate)",
    ),
    ("cli.py", "cmd_verify", "c.seconds"): (1, "seconds vivibox counted"),
    ("cli.py", "cmd_verify", "result.hidden_characters[…]"): (2, "escaped on the line above (display.name)"),
    ("cli.py", "cmd_verify", "c.kind"): (1, "added, changed or removed: vivibox's word"),
    ("cli.py", "cmd_verify", "result.log"): (1, "a file of the task's, under your tasks folder"),
    ("cli.py", "cmd_demo", "where.get(result.source, result.source)"): (
        1,
        "vivibox's word for where it found it",
    ),
    ("cli.py", "cmd_demo", "result.urls[…]"): (1, "an address vivibox made of the pod's and a port's number"),
    ("cli.py", "cmd_demo", "listener.port"): (1, "a port's number"),
    ("cli.py", "cmd_demo", "listener.why_not"): (1, "vivibox's words for why a port is not reached (probe)"),
    ("cli.py", "cmd_box", "found.name"): (1, "a name made of your folder's (init.project_name)"),
    ("cli.py", "cmd_box", "Path(args.new).expanduser().resolve()"): (1, YOURS),
    ("cli.py", "main", "e.returncode"): (1, "an exit code"),
    ("cli.py", "", "main()"): (1, "sys.exit with the number main returns: no words are printed"),
    ("__main__.py", "", "main()"): (1, "the same"),
    ("__main__.py", "", "main(sys.argv[1:])"): (1, "the same, of the index wrapper's (vivibox/index)"),
    ("index_sync.py", "", "main(sys.argv[1:])"): (1, "the same, in the index's container: nobody reads it"),
    ("relay.py", "", "main(sys.argv[1:])"): (1, "the code index's relay, in its container: nobody reads it"),
    ("provision.py", "", "main(sys.argv[1:])"): (
        1,
        "the index's provisioning run, in its container: nobody reads it",
    ),
    ("cli_agent.py", "cmd_wait", "task['status']"): (1, "a status of the table in docs/ux-guidelines.md"),
    ("cli_agent.py", "cmd_wait", "task['next'][0]"): (1, "a command of vivibox's with the task's id"),
    ("cli_agent.py", "cmd_skill", "p.cli"): (1, "an agent CLI vivibox knows (skill.PLACES)"),
    ("cli_agent.py", "cmd_skill", "p.home"): (1, "the same: its folder's name"),
    ("cli_agent.py", "cmd_skill", "done[…]"): (
        2,
        "a folder in your home and vivibox's words for what it did",
    ),
    (
        "cli_agent.py",
        "cmd_review",
        "reviewing.cli_prompt(task)",
    ): (1, "a prompt for an agent's CLI to take as it is: escaped, it would be another prompt"),
    ("cli_agent.py", "cmd_review", "ui.count(len(review.blocking), 'blocking note')"): (
        1,
        "a count and its noun",
    ),
    ("cli_agent.py", "cmd_review", "actions.prepare_review(task, project)"): (1, "the review copy's folder"),
    ("cli_decisions.py", "cmd_accept", "actions.close_box(task, project)"): (1, "the review copy's folder"),
    ("cli_decisions.py", "report_finished", "done.cost"): (3, "money vivibox counted (ui.Spend)"),
    ("cli_decisions.py", "report_finished", "done.branch"): (2, "the task's branch, named by its id"),
    ("cli_decisions.py", "report_finished", "done.source"): (2, YOURS),
    ("cli_decisions.py", "report_finished", "actions.current_branch(done.source)"): (1, YOURS),
    ("cli_decisions.py", "offer_commit", "done.source"): (1, YOURS),
    ("cli_decisions.py", "offer_commit", "where_to_commit(done)[…]"): (
        1,
        "the branch you chose, or the task's",
    ),
    ("cli_decisions.py", "where_to_commit", "enumerate(choices, 1)[…]"): (1, "the number of a choice"),
    ("cli_decisions.py", "where_to_commit", "label"): (1, "a branch of yours, and vivibox's words for it"),
    ("cli_decisions.py", "where_to_commit", "done.new_branch"): (1, "the task's branch, named by its id"),
    ("cli_decisions.py", "where_to_commit", "values.index(actions.default_branch(done))"): (1, "a number"),
    (
        "cli_decisions.py",
        "cmd_plan",
        "actions.plan_prompt(task, cli=args.cli)",
    ): (1, "a prompt for a chat or an agent's CLI to take as it is"),
    ("cli_decisions.py", "cmd_risky", "view.identity"): (2, "a digest, in hexadecimal"),
    ("cli_decisions.py", "cmd_risky", "view.diffs"): (1, f"{ESCAPED_THERE} (risky._show: display.diff)"),
    (
        "cli_decisions.py",
        "cmd_approve_risky",
        "actions.approve_risky(task, project, args.shown)[…]",
    ): (2, "a count, and the review copy's folder"),
    ("cli_host.py", "cmd_image_build", "image.build(pull=args.pull, force=args.force)[…]"): (
        1,
        "the image's name",
    ),
    ("cli_host.py", "cmd_mirror", "mirror.found()[…]"): (
        1,
        "the address the mirror listens on, from your settings",
    ),
    ("cli_host.py", "cmd_mirror", "mirror.size()"): (1, "a size vivibox worked out"),
    ("cli_host.py", "cmd_image_check", "check.name"): (
        1,
        "a check's name, of vivibox's own table (image.checks)",
    ),
    ("cli_host.py", "cmd_hostd", "hostd.problem(config.host_ip)"): (
        1,
        "a sentence of vivibox's about its forwarder",
    ),
    ("cli_host.py", "cmd_hostd", "config.host_ip"): (1, YOURS),
    ("cli_host.py", "cmd_hostd", "hostd.running()"): (1, "a process's number"),
    ("cli_pod.py", "cmd_limits", "e"): (2, "vivibox's words about a limit, around what you typed or set"),
    (
        "cli_pod.py",
        "cmd_limits",
        "panel_resources.text(task, scopes=pod.scopes() if pod.running() else {})",
    ): (1, "figures and vivibox's names for them"),
    ("cli_pod.py", "cmd_disk", "e"): (2, "vivibox's words about a size, around what you typed"),
    ("cli_providers.py", "_yes", "question"): (1, "what its callers ask: each is looked at where it calls"),
    ("cli_providers.py", "cmd_auth", "keys.masked(keys.get_key(args.provider))"): (
        1,
        "the key you just typed, masked",
    ),
    ("cli_providers.py", "cmd_auth", "f.kind"): (1, "provider or MCP server: vivibox's word"),
    ("cli_providers.py", "cmd_auth", "said[f.status]"): (1, "one of three notes written in the function"),
    ("cli_reports.py", "cmd_stats", "stats.report(found)"): (
        1,
        "figures vivibox counted, and its names for them",
    ),
    (
        "cli_reports.py",
        "cmd_usage",
        "usage.report(rows, None if problems else caches)",
    ): (1, "tasks' ids and figures vivibox counted"),
    ("cli_reports.py", "cmd_usage", "problems[…]"): (
        1,
        "Docker's words, escaped where the error is made (resources)",
    ),
    ("hostd.py", "serve", "address"): (
        2,
        "the forwarder's own address, from your settings or the command line",
    ),
    ("hostd.py", "serve", "port"): (2, "its port"),
    ("hostd.py", "serve", "e"): (2, "the system's words for why it cannot listen, or vivibox's own"),
    ("hostd.py", "serve", "stub[0]"): (1, "your resolver's address"),
    ("hostd.py", "serve", "stub[1]"): (1, "and its port"),
    ("review_round.py", "ReviewRound._decide", "n"): (3, "the round's number"),
    ("review_round.py", "ReviewRound._decide", "_sup._notes(blocking)"): (1, "a count and its noun"),
    # The summary has three forms; the one printed here is of a review with blocking notes,
    # which is never the form that quotes why a review could not be read.
    ("review_round.py", "ReviewRound._decide", "refused"): (1, "in the form that is not printed here"),
    ("review_round.py", "ReviewRound._decide", "reviewing.problem(text)"): (1, "the same"),
    ("supervise.py", "cmd_supervise", "config.notice"): (1, "a sentence of vivibox's about your settings"),
    ("supervisor.py", "_await_click", "action.label"): (1, "a button's words, vivibox's"),
    ("supervisor.py", "Supervisor._go", "target"): (1, "a state's name"),
    ("tui.py", "run", "app.executor.unfinished()"): (1, "a count"),
}
# Printed as it is, and not vivibox's own: known, and not escaped yet. An entry leaves this list
# with the print it names.
RAW_STILL: dict[tuple[str, str, str], tuple[int, str]] = {}


def pieces_printed() -> Counter:
    """(module, function, piece) for every piece of every print that is not one of OWN."""
    found: Counter = Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        for scope, _, pieces in prints(path.read_text()):
            found.update((path.name, scope, piece) for piece in pieces if not own(piece))
    return found


def test_every_print_is_made_of_what_cannot_reach_the_terminal_as_itself():
    """A print with a piece that is neither a literal, nor escaped by display, nor a number, nor
    JSON, nor a value vivibox knows (OWN), is a failure until the piece goes through
    display.terminal, display.under or display.name, or is listed with why. What this cannot
    see: where a listed piece's text comes from (the list says, nothing holds it to that); a
    value put together in another function and handed in by a name; a write to a stream handed
    in by another name (logs.follow_verification's out, which escapes what it writes); and what
    a program vivibox starts in your terminal prints there itself (a pager, an editor, docker).
    Nor a line break: display.terminal, which the guard takes for escaped, keeps it, and so
    does the one printer of errors (cli.main), since an error of vivibox's own has lines (a
    refusal's commands). What an error quotes is put on one line, or under its first, where
    the error is made (display.name, display.said, display.under), in whatever module that is:
    this guard reads what is printed, never what is raised."""
    found, listed = pieces_printed(), {**ACCOUNTED_FOR, **RAW_STILL}
    new = {key: n for key, n in found.items() if n > listed.get(key, (0, ""))[0]}
    assert not new, "escape it (display.terminal, under, name), or list it with why:\n" + "\n".join(
        f"{module} in {scope}: {piece} ({n})" for (module, scope, piece), n in sorted(new.items())
    )


def test_every_listed_print_is_still_there_as_many_times():
    """An entry covers the prints it was written for: gone, or fewer, it is taken off the list
    or counted again, so that it cannot cover another print of the same words later."""
    found, listed = pieces_printed(), {**ACCOUNTED_FOR, **RAW_STILL}
    stale = {key: (n, found.get(key, 0)) for key, (n, _) in listed.items() if found.get(key, 0) != n}
    assert not stale, f"listed, and not printed that many times: {stale}"
    assert not set(ACCOUNTED_FOR) & set(RAW_STILL) and all(why for _, why in listed.values())
    assert not [key for key in listed if own(key[2])], "one of OWN needs no entry"


def unaccounted(source: str) -> list[str]:
    """The pieces of a source's prints that are not vivibox's own by any rule."""
    return [piece for _, _, pieces in prints(source) for piece in pieces if not own(piece)]


def test_the_guard_on_prints_sees_text_from_outside_however_it_is_printed():
    for shape in (
        "print(x)",
        "print(f'Plan brought in: {x}.')",
        "print('The agent asks:\\n\\n' + x)",
        "print('Note: %s' % x)",
        "print('{} of {}'.format(1, x))",
        "print(', '.join(x for x in found))",
        "print(', '.join(found))",
        "print('a' if quiet else x)",
        "print(x or '(none)')",
        "print(x.strip().replace('\\n', ' '))",
        "print('Note: N'.replace('N', x))",
        "print(f'{x:20} and {x!r}')",
        "print('vivibox: ', x, file=sys.stderr)",
        "sys.stdout.write(x)",
        "sys.stderr.write(f'vivibox: {x}\\n')",
        "input(f'Delete {x}? [y/N] ')",
        "getpass.getpass(f'API key for {x}: ')",
        "sys.exit(f'vivibox: {x}')",
        "sys.exit(x)",
        "raise SystemExit(f'vivibox: {x}')",
        "from sys import stderr\nprint(x, file=stderr)",
        "from sys import stdout\nstdout.write(x)",
        "print(x, file=sys.stderr if failed else sys.stdout)",
        "def f():\n    said = f'why: {x}'\n    print(said)",
        "def f():\n    said = display.name(x)\n    said = x\n    print(said)",
        "def f():\n    for line in x.splitlines():\n        print(line)",
        "print(json.dumps(x, ensure_ascii=False))",
        "print(theirs.terminal(x))",
        "print(str(x))",
    ):
        assert unaccounted(shape), shape
    for safe in (
        "print('Stopped.')",
        "print()",
        "print(display.terminal(x), end='')",
        "print(f'vivibox: {display.under(str(e))}', file=sys.stderr)",
        "print(', '.join(display.name(f) for f in found))",
        "print(', '.join(map(display.name, found)))",
        "print(f'{task.id} is done ({cost:.2f}, {len(found)} files, {seconds:g} s)')",
        "print(f'Started {args.task}.')",
        "print(f'Removed {mirror.NAME} at {time.strftime(\"%H:%M:%S\")}')",
        "print(json.dumps({'tasks': x}))",
        "print(json.dumps(x, indent=2))",
        "def f():\n    said = display.under(x)\n    print(f'why: {said}')",
        "def f():\n    for line in map(display.name, x):\n        pass\n    print('done')",
        "def f():\n    for a, b in ((display.name(c), display.name(d)) for c, d in x):\n        print(a, b)",
        "print(x, file=log)",  # a file of vivibox's own, which no terminal reads as it is written
        "sys.exit(1)",
        "sys.exit(f'vivibox: {display.name(x)}')",
        "raise SystemExit(2)",
        "log.write(x)",
    ):
        assert unaccounted(safe) == [], safe


def test_what_the_guard_takes_for_escaped_escapes():
    """The guard trusts display's functions by their names: each shows a character with nothing
    to see as its code point, whatever it is given, so nothing of it reaches the terminal as
    itself. And JSON, as json.dumps writes it by default, holds no such character either."""
    import json

    from vivibox import display

    for name in ESCAPED:
        made = getattr(display, name)
        shown = made("the build", EVIL) if name == "quoted" else made(EVIL)
        assert not hidden(shown) and "‹U+001B›" in shown, name
    assert not hidden(json.dumps({"problem": FORGED})) and json.dumps(FORGED).isascii()
    assert "\n" not in display.name(FORGED) and "\n    vivibox:" in display.under(FORGED)


# --- the prints a review found, and those of their shape -----------------------------------------

BIDI = chr(0x202E)
# What an agent can put into a value: an escape that erases the line, a carriage return, a bidi
# override, and a line of its own that reads as one of vivibox's.
EVIL = f"x\x1b[2K\r{BIDI}"
FORGED = f"{EVIL}\nvivibox: demo-1 is running at http://198.51.100.2:5173"
EVIL_SHOWN = "x‹U+001B›[2K‹U+000D›‹U+202E›"


def hidden(text: str) -> list[str]:
    import unicodedata

    kinds = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
    return [c for c in text if c not in "\n\t" and unicodedata.category(c) in kinds]


def own_lines(text: str) -> list[str]:
    """The lines that start where vivibox's own do: at the left edge."""
    return [line for line in text.splitlines() if line and not line.startswith(" ")]


def test_plan_import_names_the_plan_s_summary_escaped(manual_env, capsys, monkeypatch):  # noqa: F811
    from vivibox import actions
    from vivibox.cli import main

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    plan = PLAN.replace(
        'summary = "Add a health endpoint"', 'summary = "Add \\u001b[2K\\u001b[1A\\u202e endpoint"'
    )
    assert plan != PLAN
    actions.answer_path(manual_env).write_text(plan)
    assert main(["plan", "import", manual_env.id]) == 0
    said = capsys.readouterr().out
    assert not hidden(said) and "Plan brought in: Add ‹U+001B›[2K‹U+001B›[1A‹U+202E› endpoint." in said, said


def a_demo(monkeypatch, result, **more):
    from vivibox import actions

    monkeypatch.setattr(actions, "demo", lambda task_id, ask=False, reply="": result)
    monkeypatch.setattr(actions, "use_instruction", lambda task_id, text: result)


def test_demo_says_the_agent_s_question_escaped_and_off_the_left_edge(env, capsys, monkeypatch):
    from test_tui import new_task

    from vivibox import demo
    from vivibox.cli import main

    task = new_task()
    a_demo(monkeypatch, demo.Demo(question=f"Which port?{FORGED}"))
    capsys.readouterr()
    assert main(["demo", task.id]) == 1
    said = capsys.readouterr().out
    assert not hidden(said), repr(said)
    assert own_lines(said) == ["The agent asks:", f'Answer with: vivibox demo {task.id} --reply "…"']
    assert f"\n\n  Which port?{EVIL_SHOWN}\n  vivibox: demo-1 is running at" in said


def test_demo_says_how_an_earlier_task_was_run_and_what_it_runs_escaped(env, capsys, monkeypatch):
    from test_tui import new_task

    from vivibox import demo
    from vivibox.cli import main

    task = new_task()
    a_demo(monkeypatch, demo.Demo(proposed=f"Run `npm start`.{FORGED}"))
    capsys.readouterr()
    assert main(["demo", task.id]) == 0
    said = capsys.readouterr().out
    assert not hidden(said) and len(own_lines(said)) == 2, repr(said)
    assert f"\n\n  Run `npm start`.{EVIL_SHOWN}\n  vivibox: demo-1 is running at" in said
    for stopped in (False, True):
        ran = demo.Demo(
            commands=[f"npm start {EVIL}"], source="task", log=f"listening{FORGED}", starting=not stopped
        )
        a_demo(monkeypatch, ran)
        assert main(["demo", task.id, "--no-ask"]) == int(stopped)
        said = capsys.readouterr().out
        assert not hidden(said) and f"  npm start {EVIL_SHOWN}\n" in said, repr(said)
        assert f":\n  listening{EVIL_SHOWN}\n  vivibox: demo-1 is running at" in said
        assert not [line for line in own_lines(said) if line.startswith("vivibox:")]


TRACE = (
    'Exception in thread "main" java.lang.IllegalStateException: no port\n'
    "\tat com.example.Main.main(Main.java:5)\n"
    "\tat com.example.Boot.run(Boot.java:12)\n"
    "ok  \tdeps\t0.3s"
)


def test_what_a_program_printed_keeps_its_tabs_under_vivibox_s_line(env, capsys, monkeypatch):
    """An app's output, an earlier task's instruction and an agent's question are lines of their
    own under a line of vivibox's (display.block). A tab in them is a column's edge: a stack
    trace's frames begin with one, a test runner's columns are parted by them. It was shown
    as its code point in every frame. A tab cannot pass for vivibox's words either: it moves
    what follows to the right, to the next stop, so a line that is indented stays off the left
    edge whatever it begins with."""
    from test_tui import new_task

    from vivibox import demo, display
    from vivibox.cli import main

    task = new_task()
    shown = TRACE.replace("\n", "\n  ")
    a_demo(monkeypatch, demo.Demo(commands=["npm start"], source="task", log=TRACE))
    capsys.readouterr()
    assert main(["demo", task.id, "--no-ask"]) == 1
    said = capsys.readouterr().out
    assert f"It stopped without listening. Last output:\n  {shown}\n" in said, repr(said)
    assert "U+0009" not in said and said.count("\tat com.example.") == 2
    a_demo(monkeypatch, demo.Demo(question="Which of these?\n\tnpm start\n\tnpm run dev"))
    assert main(["demo", task.id]) == 1
    assert "The agent asks:\n\n  Which of these?\n  \tnpm start\n  \tnpm run dev\n" in capsys.readouterr().out
    a_demo(monkeypatch, demo.Demo(proposed="Run:\n\n\tmake run\t# the back end"))
    assert main(["demo", task.id]) == 0
    assert "run like this:\n\n  Run:\n  \n  \tmake run\t# the back end\n" in capsys.readouterr().out
    # The claim, on a line made to try it: a tab first, then words that read as vivibox's.
    block = display.block(f"listening\n\tvivibox: demo-1 is done\n\t\r{EVIL}", 2)
    assert block == f"listening\n  \tvivibox: demo-1 is done\n  \t‹U+000D›{EVIL_SHOWN}"
    assert own_lines(block) == ["listening"] and all(
        line.expandtabs(8).startswith("  ") for line in block.splitlines()[1:]
    ), "as a terminal draws it: every later line begins with the two columns, and the tab adds to them"
    assert display.block("a\n  b\nc", 6) == "a\n        b\n      c", (
        "every line alike: the lines keep their places"
    )


def test_a_tool_s_words_in_a_reason_stand_under_it_once(env, capsys, monkeypatch):
    """What git or Docker said is put under its first line where the error is made
    (display.said: four columns). A reason printed after a word of vivibox's goes through
    display.under, which put it four more in: eight, for the second line of git's words in
    `vivibox verify` and in "Not committed". A line that is that far in already stays there."""
    from test_tui import new_task

    from vivibox import actions, cli_decisions, display, gate, ui

    said = display.said(
        "fatal: unable to access 'https://example.com/lib.git/'\nfatal: clone of 'lib' failed"
    )
    assert said.splitlines()[1] == "    fatal: clone of 'lib' failed"
    assert (
        display.under(f"the submodules could not be fetched: {said}").splitlines()[1] == said.splitlines()[1]
    )
    assert display.under("a\nb\n    c\n  d") == "a\n    b\n    c\n      d", (
        "what is not that far in is put there"
    )
    assert (
        display.under(f"could not start: {said}", 6).splitlines()[1]
        == "          fatal: clone of 'lib' failed"
    )
    task = new_task()
    result = gate.GateResult(task.meta / "log" / "verify-1.log")
    result.environment = f"the project's submodules could not be fetched: {said}"
    monkeypatch.setattr(actions, "task_pod", lambda task_id: type("P", (), {"up": lambda self: None})())
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: result)
    from vivibox.cli import main

    capsys.readouterr()
    assert main(["verify", task.id]) == 1
    assert "lib.git/'\n    fatal: clone of 'lib' failed\n" in capsys.readouterr().out

    def refuses(*a):
        raise gate.GateError(f"the commit failed; the changes stay staged: {said}")

    done = actions.Finished("demo-1", Path("/src"), ui.Spend(), "Add it")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(cli_decisions, "where_to_commit", lambda done: ("", False))
    monkeypatch.setattr(actions, "commit_work", refuses)
    cli_decisions.offer_commit(done)
    assert capsys.readouterr().out.endswith(
        "Not committed: the commit failed; the changes stay staged: fatal: unable to access "
        "'https://example.com/lib.git/'\n    fatal: clone of 'lib' failed\n"
    )


def test_an_agent_s_words_in_why_a_demo_could_not_be_worked_out_draw_no_line_of_vivibox_s(env, monkeypatch):
    """The error is printed by the one printer of errors, which keeps an error's own lines where
    they are (a refusal's commands are lines of their own): the agent's words in it are put
    under its first line where the error is made."""
    from types import SimpleNamespace

    from test_tui import new_task

    from vivibox import demo, opencode
    from vivibox.pod import PodError

    task = new_task()
    failed = SimpleNamespace(ok=False, error="", text=f"no idea{FORGED}", cost=0.0, tokens=0, session="")
    monkeypatch.setattr(opencode, "OpenCode", lambda pod: SimpleNamespace(turn=lambda *a, **k: failed))
    with pytest.raises(PodError) as refused:
        demo.ask_agent_how_to_run(task, SimpleNamespace())
    said = f"vivibox: {refused.value}"
    assert not hidden(said) and len(own_lines(said)) == 1, repr(said)
    assert said.startswith(
        f"vivibox: the agent could not work out how to run this: no idea{EVIL_SHOWN}\n    "
    )


def test_verify_says_a_command_a_criterion_and_a_commit_s_problem_escaped(env, capsys, monkeypatch):
    from test_tui import new_task

    from vivibox import actions, gate
    from vivibox.cli import main

    task = new_task()
    result = gate.GateResult(task.meta / "log" / "verify-1.log")
    result.commands = [gate.CommandResult(f"npm test {EVIL}", False, 1.5)]
    result.missing_criteria = [f"works offline {EVIL}"]
    result.commit_problems = [f"abc1234: subject is not in English{FORGED}"]
    result.build_files = [(f"npm run {EVIL}", f"package{EVIL}.json")]
    result.environment = f"the build said «{EVIL_SHOWN}»{FORGED}"
    monkeypatch.setattr(actions, "task_pod", lambda task_id: type("P", (), {"up": lambda self: None})())
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: result)
    capsys.readouterr()
    assert main(["verify", task.id]) == 1
    said = capsys.readouterr().out
    assert not hidden(said), repr(said)
    for line in (
        f"NOTE  package{EVIL_SHOWN}.json names `npm run {EVIL_SHOWN}`, and the project runs nothing yet",
        f"ENV   verification could not run: the build said «{EVIL_SHOWN}»{EVIL_SHOWN}\n    vivibox: demo-1",
        f"FAIL  npm test {EVIL_SHOWN}  (1.5s)",
        f"TODO  criterion not ticked: works offline {EVIL_SHOWN}",
        f"FAIL  commit abc1234: subject is not in English{EVIL_SHOWN}\n    vivibox: demo-1",
    ):
        assert line in said, (line, said)
    assert not [line for line in own_lines(said) if line.startswith("vivibox:")]


def test_accept_names_the_kept_command_the_model_and_the_commit_escaped(env, capsys, monkeypatch):
    from pathlib import Path as P
    from types import SimpleNamespace

    from vivibox import actions, cli_decisions, gate, ui

    task = SimpleNamespace(id="demo-1")
    monkeypatch.setattr(actions, "carry_on", lambda task: f"provider/model{EVIL}")
    cli_decisions.carry_on(task)
    said = capsys.readouterr().out
    assert said == f"Started demo-1 (provider/model{EVIL_SHOWN}): nobody was working on it.\n"
    done = actions.Finished(
        "demo-1", P("/src"), ui.Spend(), f"Add it {EVIL}\n\nBody{FORGED}", left=f"rules{FORGED}"
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(actions, "current_branch", lambda source: "main")
    cli_decisions.report_finished(done)
    out, err = capsys.readouterr()
    assert not hidden(out + err), repr(out + err)
    assert "  Add it x‹U+001B›[2K\n" in out and "\n  vivibox: demo-1 is running" in out, (
        "each line of it under vivibox's"
    )
    assert not [line for line in own_lines(out) if line.startswith("vivibox:")]
    assert len(own_lines(err)) == 1 and f"is done, but rules{EVIL_SHOWN}\n    vivibox: demo-1" in err
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(cli_decisions, "where_to_commit", lambda done: ("", False))
    monkeypatch.setattr(actions, "commit_work", lambda *a: f"abc1234 Add it {EVIL}")
    cli_decisions.offer_commit(done)
    assert f"Committed: abc1234 Add it {EVIL_SHOWN}\n" in capsys.readouterr().out

    def refuses(*a):
        raise gate.GateError(f"the subject is not in English{FORGED}")

    monkeypatch.setattr(actions, "commit_work", refuses)
    cli_decisions.offer_commit(done)
    said = capsys.readouterr().out
    assert (
        not hidden(said) and f"Not committed: the subject is not in English{EVIL_SHOWN}\n    vivibox:" in said
    )


def test_accept_names_a_writer_s_command_it_keeps_escaped(env, capsys, monkeypatch):
    from test_tui import implementing

    from vivibox import actions
    from vivibox.cli import main
    from vivibox.states import State

    task = implementing("Add health endpoint")
    task.transition(State.CHECKPOINT_COMMAND)
    project = env / "config" / "projects" / "demo.toml"
    monkeypatch.setattr(actions, "accept_command", lambda *a: project.write_text(
        project.read_text().replace('verify = ["true"]', 'verify = ["npm test \\u001b[2K\\u202e"]')
    ))  # fmt: skip
    capsys.readouterr()
    assert main(["accept", task.id]) == 0
    said = capsys.readouterr().out
    assert not hidden(said) and "demo is verified with `npm test ‹U+001B›[2K‹U+202E›` from now on" in said


def test_auth_names_what_an_imported_file_holds_escaped(env, capsys, monkeypatch):
    from vivibox import keys, providers
    from vivibox.cli import main

    found = providers.Found("provider", f"deep{EVIL}", f"3 models {EVIL}", f"from {EVIL}", "new", {})
    reading = providers.Reading([found], [f"agent{EVIL}"])
    monkeypatch.setattr(providers, "read_opencode", lambda source: reading)
    monkeypatch.setattr(providers, "bring_over", lambda chosen: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["auth", "import"]) == 0
    said = capsys.readouterr().out
    assert not hidden(said), repr(said)
    assert f"deep{EVIL_SHOWN}" in said and f"3 models {EVIL_SHOWN}, key from {EVIL_SHOWN}" in said
    assert f"not for vivibox: agent{EVIL_SHOWN}." in said and f"Imported deep{EVIL_SHOWN};" in said
    monkeypatch.setattr(keys, "list_keys", lambda: {f"deep{EVIL}": f"{EVIL[:3]}… (40)"})
    assert main(["auth", "list"]) == 0
    assert not hidden(capsys.readouterr().out)


def test_auth_import_names_what_it_refuses_in_a_file_on_one_line(env, capsys, tmp_path):
    """The name of a provider is a key of the file you import, which you may have downloaded.
    One that cannot be a name in vivibox is said in the error, which the one printer of errors
    prints with its line breaks: a line break in the name drew a second line at the left edge,
    reading as vivibox's own. The name is escaped where the error is made."""
    import json

    from vivibox.cli import main

    file = tmp_path / "opencode.json"
    forged = "x\nvivibox: demo-1 is running at http://198.51.100.2:5173"
    for kind in ("provider", "mcp"):
        file.write_text(json.dumps({kind: {f"{forged}{EVIL}": {}}}))
        capsys.readouterr()
        assert main(["auth", "import", str(file)]) == 1
        said = capsys.readouterr().err
        assert not hidden(said) and own_lines(said) == [said.rstrip("\n")], repr(said)
        named = f"'x‹U+000A›vivibox: demo-1 is running at http://198.51.100.2:5173{EVIL_SHOWN}'"
        assert said == f"vivibox: {file}: {kind} {named} cannot be named that way in vivibox\n"
    file.write_text(json.dumps({"provider": {"deepseek": []}}))
    assert main(["auth", "import", str(file)]) == 1
    assert (
        capsys.readouterr().err
        == f"vivibox: {file}: provider 'deepseek' cannot be named that way in vivibox\n"
    )


def test_image_check_says_what_a_failed_check_printed_escaped_under_its_line(env, capsys, monkeypatch):
    from vivibox import image
    from vivibox.cli import main

    check = image.Check("java", "java -version", "openjdk")
    monkeypatch.setattr(image, "exists", lambda ref, runner=None: True)
    monkeypatch.setattr(image, "run_checks", lambda ref: [(check, False, f"not found{FORGED}\n\tsee\tPATH")])
    assert main(["image", "check"]) == 1
    said = capsys.readouterr().out
    assert not hidden(said), repr(said)
    assert f"FAIL  java\n      not found{EVIL_SHOWN}\n      vivibox: demo-1 is running" in said
    assert "\n      \tsee\tPATH\n" in said, "what the check printed keeps its tabs"


def test_init_and_info_say_what_a_repository_s_files_name_escaped(env, capsys, monkeypatch):
    from vivibox import about, actions
    from vivibox import init as project_init
    from vivibox.cli import main

    found = project_init.Detected(
        "shop", env / "repo", [f"npm test {EVIL}"], notes=[f"package.json names {EVIL}"]
    )
    monkeypatch.setattr(actions, "propose_project", lambda where: found)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["init", str(env / "repo")]) == 1
    said = capsys.readouterr().out
    assert not hidden(said) and f"  - package.json names {EVIL_SHOWN}" in said, repr(said)
    monkeypatch.setattr(about, "gather", lambda path: {})
    monkeypatch.setattr(about, "describe", lambda found: f"Project shop, verified with: npm test {EVIL}\n")
    assert main(["info", str(env / "repo")]) == 0
    assert capsys.readouterr().out == f"Project shop, verified with: npm test {EVIL_SHOWN}\n"


def test_a_command_that_failed_is_named_escaped_by_the_printer_of_errors(env, capsys, monkeypatch):
    import subprocess

    from vivibox import cli

    def fails(args):
        raise subprocess.CalledProcessError(2, ["git", "checkout", f"branch{EVIL}"])

    monkeypatch.setattr(cli, "cmd_status", fails)
    assert cli.main(["status"]) == 1
    said = capsys.readouterr().err
    assert said == f"vivibox: command failed (2): git checkout branch{EVIL_SHOWN}\n"


def test_start_names_the_model_escaped(env, capsys, monkeypatch):
    from test_tui import new_task

    from vivibox import actions
    from vivibox.cli import main

    task = new_task()
    monkeypatch.setattr(actions, "start", lambda task_id, resume=False, on_step=None: f"p/m{EVIL}")
    capsys.readouterr()
    assert main(["start", task.id]) == 0
    assert capsys.readouterr().out.startswith(f"Started {task.id} (p/m{EVIL_SHOWN}). Watch the agent:")


def test_what_a_server_said_of_a_notification_is_said_escaped_in_the_supervisor_s_window(capsys):
    from vivibox import ntfy

    ntfy.said(f"ntfy: could not send: HTTP Error 400: {EVIL}")
    said = capsys.readouterr().out
    assert not hidden(said) and said.endswith(f"] ntfy: could not send: HTTP Error 400: {EVIL_SHOWN}\n")


def test_wait_json_is_byte_for_byte_what_a_script_was_given_before(env, capsys):
    """What an agent's own tooling parses is not escaped for a terminal: JSON escapes a control
    character itself, and the text inside is the task's, as it is."""
    import json

    from test_tui import new_task

    from vivibox.cli import main

    task = new_task()
    task.set_paused(True, problem=f"agent turn failed: {FORGED}")
    capsys.readouterr()
    main(["wait", task.id, "--timeout", "0", "--json"])
    out = capsys.readouterr().out
    assert out.isascii() and not hidden(out) and out.count("\n") == 1, repr(out)
    shown = json.loads(out)
    assert shown["tasks"][0]["problem"].endswith(FORGED) and "\\u001b[2K\\r\\u202e\\nvivibox: demo-1" in out
    assert out == json.dumps(shown) + "\n", "nothing but the document, as json.dumps writes it"
    assert list(shown) == ["version", "tasks"]
    assert list(shown["tasks"][0]) == ["id", "state", "reason", "status", "problem", "said", "next"]
