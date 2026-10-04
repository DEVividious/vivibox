"""Textual reads a string as markup unless told not to, and what vivibox shows is often text it
did not write: the agent's output, commands and questions, file names and git's words, a build's
output, a plan, a provider's error, a model's name from a provider's catalogue. Such text never
reaches a widget as markup: [/b] in it would end the view (MarkupError), [black on black] hide
what follows.

Every place a string becomes what a widget draws is looked at here, by its shape: a notification,
a Label, a Static, a Button, an Option, a Selection, a Select's options, an option list filled
later, an update of a widget, from_markup, a border's title, a button's label. What it is given is
a plain string, or says markup=False, or is made by something that cannot be markup (escape, Text,
Content), or is listed below as markup vivibox wrote, with why, the values in it escaped. An entry
names the function it is in and is counted: a second call of the same words elsewhere is not
covered by it."""

import ast
import os
from collections import Counter
from pathlib import Path

from vivibox import actions, demo

PACKAGE = Path(__file__).parent.parent / "vivibox"
BIDI = chr(0x202E)
EVIL = f"x[/b] [black on black]hidden{BIDI}\x1b[2K"

# (module, the function it is in, what takes it, the text as written): why it is markup of
# vivibox's own, or not markup at all.
VIVIBOX_MARKUP: dict[tuple[str, str, str, str], str] = {
    ("dialogs.py", "Reply.send", "notify", "NOTHING_TO_SEND"): "a constant of vivibox's",
    ("dialogs.py", "ReplyWithCriteria.send", "notify", "NOTHING_TO_SEND"): "a constant of vivibox's",
    ("dialogs.py", "NewProject.show_verify", "update", "how"): "the commands escaped, else vivibox's words",
    ("dialogs.py", "NewProject.show_prepare", "update", "how"): "the commands escaped, else vivibox's words",
    (
        "dialogs.py",
        "help_text",
        "from_markup",
        "('\\n' if i else '') + heading",
    ): "a heading of the help, written by vivibox with its tags",
    ("dialogs.py", "Help.compose", "Label", "f'vivibox {version.current()}'"): "vivibox's version",
    ("dialogs.py", "Help.compose", "Static", "help_text(self.note)"): "a Group of Text, never a string",
    ("newtask.py", "NewTask.compose", "Select", "NEW_PROJECT"): "a constant of vivibox's",
    ("newtask.py", "NewTask.compose", "Label", "name.capitalize()"): "a role's name, vivibox's",
    (
        "newtask.py",
        "NewTask.follow_mode",
        "update",
        "agents[role]",
    ): "the flow's name for an agent (config.ORCHESTRATION_MODES), vivibox's",
    (
        "providers_ui.py",
        "ChooseImport.compose",
        "Selection",
        "import_label(f)",
    ): "vivibox's tags, the name, what it is and the key escaped (import_label)",
    ("providers_ui.py", "ChooseImport.compose", "Label", "heading"): "a section heading of vivibox's",
    (
        "providers_ui.py",
        "ManageProviders.fill",
        "add_options",
        "row_label(n, said, on)",
    ): "vivibox's tags, the name and what it says escaped (row_label)",
    (
        "providers_ui.py",
        "ManageItems.compose",
        "Selection",
        "row_label(n, said, True)",
    ): "vivibox's tags, the name and what it says escaped (row_label)",
    ("providers_ui.py", "ManageItems.compose", "Select", "said"): "vivibox's words for a mode of Serena",
    (
        "providers_ui.py",
        "AddProvider.show",
        "add_options",
        "label",
    ): "vivibox's tags, the provider's name and what you typed escaped (find_providers)",
    (
        "settings.py",
        "Rows.fill",
        "Option",
        "f'{gap}[b {look.SECONDARY}]{escape(label.upper())}[/]'",
    ): "vivibox's tags, the heading escaped; gap is a line break or nothing",
    (
        "settings.py",
        "Rows.fill",
        "Option",
        "f'  {look.secondary(label.ljust(width))}  {shown}{mark}'",
    ): "the value escaped the line above, the mark vivibox's",
    (
        "table.py",
        "TaskTable.fill_table",
        "from_markup",
        "cells.get(name, '')",
    ): "a cell of the list: vivibox's tags, every value in it escaped where the row is planned",
    ("table.py", "TaskTable.fill_table", "from_markup", "cells.get('GOAL', '')"): "the goal, escaped there",
    ("table.py", "TaskTable.fill_table", "update", "'' if ids else NO_PROJECTS"): "a constant of vivibox's",
    ("table.py", "TaskTable.cell", "from_markup", "markup"): "a cell of the list, as above",
    ("blocks.py", "Panel._redraw", "update", "self.source"): "the panel's own text again: markdown",
    ("table.py", "TaskTable.spin", "update", "datetime.now().strftime('%H:%M')"): "the clock",
    ("tui.py", "Vivibox.check_code", "notify", "CODE_CHANGED.capitalize() + '.'"): "a constant of vivibox's",
    (
        "tui.py",
        "Vivibox.show_detail",
        "update",
        "display.terminal(text)",
    ): "the details panel: markdown, never markup (blocks.Panel)",
    (
        "widgets.py",
        "ContextHelp.draw",
        "update",
        "said.lines(self.full) if isinstance(said, look.Explained) else said",
    ): "a field's help: vivibox's sentences, or the text object Explained makes",
    (
        "widgets.py",
        "Dialog.mark_row",
        "update",
        "said if isinstance(said, str) else said.title",
    ): "a field's help: vivibox's sentences",
    ("widgets.py", "Dialog.reframe", "update", "said"): "the key bar: look.hints, or nothing",
    ("widgets.py", "Dialog.reframe", "Static", "said"): "the key bar: look.hints, or nothing",
}

# What takes a string and draws it: the callable's name, and the assignments that do.
WIDGETS = ("Label", "Static", "Button", "Option", "Selection")
FILLED = ("set_options", "add_options", "add_option")
ASSIGNED = ("border_title", "border_subtitle", "label", "tooltip")
# What makes text that is not markup, whatever it is given: rich's escape, and text objects.
PLAIN = ("escape", "Text", "Content", "assemble")
# vivibox's own: the helpers of look.py that wrap a tag of the palette around text they escape,
# and those that return text objects. test_the_helpers_taken_for_plain_are held to that.
ESCAPING = ("colored", "muted", "secondary", "faint", "subscription")
OBJECTS = ("hints", "branch_label", "mode_option")


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _textual_notify(func) -> bool:
    """self.notify, self.app.notify, app.notify: Textual's; supervisor.notify and ports.notify
    are a task's messages, plain text by their own escaping."""
    if not isinstance(func, ast.Attribute) or func.attr != "notify":
        return False
    owner = func.value
    return (isinstance(owner, ast.Name) and owner.id in ("self", "app")) or (
        isinstance(owner, ast.Attribute) and owner.attr == "app"
    )


def _off(call: ast.Call) -> bool:
    return any(
        k.arg == "markup" and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in call.keywords
    )


def plain(node) -> bool:
    """Whether what node makes cannot be markup an outsider wrote: a constant, text made by
    escape, Text or Content, or a choice or a joining of such."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Call):
        return _name(node.func) in (*PLAIN, *ESCAPING, *OBJECTS)
    if isinstance(node, ast.Attribute):  # look.ERROR: a colour of the palette, by its name
        return isinstance(node.value, ast.Name) and node.value.id == "look" and node.attr.isupper()
    if isinstance(node, ast.IfExp):
        return plain(node.body) and plain(node.orelse)
    if isinstance(node, ast.BoolOp):
        return all(plain(value) for value in node.values)
    if isinstance(node, ast.JoinedStr):
        return all(plain(v.value) for v in node.values if isinstance(v, ast.FormattedValue))
    return False


def _each(node) -> list:
    """The items a list is made of, as written: of a comprehension its element, of a list its
    members (a starred one as it is), else the thing itself."""
    if isinstance(node, ast.ListComp | ast.GeneratorExp):
        return _each(node.elt)
    if isinstance(node, ast.List | ast.Tuple) and not _pair(node):
        return [item for member in node.elts for item in _each(member)]
    if isinstance(node, ast.Starred):
        return _each(node.value)
    return [node]


def _pair(node) -> bool:
    """(what is drawn, its value): a Select's option."""
    return isinstance(node, ast.Tuple) and len(node.elts) == 2 and not isinstance(node.elts[0], ast.Tuple)


def _ids(tree: ast.AST) -> set[str]:
    """The ids of the widgets a module makes with markup=False."""
    calls = (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _off(n))
    return {
        k.value.value
        for c in calls
        for k in c.keywords
        if k.arg == "id" and isinstance(k.value, ast.Constant)
    }


def _queried(node) -> str:
    """The id a query_one("#id", …) asks for, or ""."""
    if isinstance(node, ast.Call) and _name(node.func) == "query_one" and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and str(first.value).startswith("#"):
            return str(first.value)[1:]
    return ""


class Sinks(ast.NodeVisitor):
    """(function, line, what, the text as written, markup off) for every place of a module that
    hands a widget what it draws."""

    def __init__(self, tree: ast.AST, view: bool):
        self.scope: list[str] = []
        self.found: list[tuple[str, int, str, ast.AST, bool]] = []
        self.view, self.off_ids = view, _ids(tree)
        self.names: dict[str, str] = {}  # a variable of the function, and the id it was queried by
        self.lists: dict[str, ast.AST] = {}  # a variable of the function, and the list it was given
        self.visit(tree)

    def _scoped(self, node) -> None:
        self.scope.append(node.name)
        kept, self.names, self.lists = (self.names, self.lists), {}, {}
        self.generic_visit(node)
        self.scope.pop()
        self.names, self.lists = kept

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def add(self, node, what: str, text, off: bool = False) -> None:
        self.found.append((".".join(self.scope), node.lineno, what, text, off))

    def listed(self, node):
        """A list handed over by a variable of the function: the list it was given, as written."""
        return self.lists.get(node.id, node) if isinstance(node, ast.Name) else node

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and (asked := _queried(node.value)):
                self.names[target.id] = asked
            if isinstance(target, ast.Name) and isinstance(node.value, ast.List | ast.ListComp):
                self.lists[target.id] = node.value
            if isinstance(target, ast.Attribute) and target.attr in ASSIGNED:
                self.add(node, target.attr, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        args, what = node.args, _name(node.func)
        if what == "call_from_thread" and args and _textual_notify(args[0]):
            args, what = args[1:], "notify"
        elif what == "notify" and not _textual_notify(node.func):
            args = []
        if what in ("notify", *WIDGETS) and args:
            self.add(node, what, args[0], _off(node))
        elif what == "from_markup" and args:
            self.add(node, what, args[0])
        elif what in ("Select", *FILLED) and args:
            for item in _each(self.listed(args[0])):
                self.add(node, what, item.elts[0] if _pair(item) else item)
        elif what == "update" and self.view and len(args) == 1 and not node.keywords:
            owner = node.func.value
            asked = _queried(owner) or (self.names.get(owner.id, "") if isinstance(owner, ast.Name) else "")
            self.add(node, what, args[0], asked in self.off_ids)
        self.generic_visit(node)


def messages():
    """(module, function, line, what, the text as written, markup off) for every sink."""
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text()
        view = "textual" in source  # an update elsewhere is a set's or a digest's
        for found in Sinks(ast.parse(source), view).found:
            yield (path.name, *found)


def markup():
    """The sinks given something that could be markup an outsider wrote: (key, where)."""
    for module, scope, line, what, text, off in messages():
        # An Option or a Selection handed to a list is looked at where it is made.
        made = isinstance(text, ast.Call) and _name(text.func) in WIDGETS
        if not off and not plain(text) and not made:
            said = ast.unparse(text)
            yield (module, scope, what, said), f"{module}:{line} in {scope}: {what}({said})"


def test_no_message_vivibox_did_not_write_is_read_as_markup():
    offenders = [where for key, where in markup() if key not in VIVIBOX_MARKUP]
    assert not offenders, "markup=False, or vivibox's own markup listed here with why:\n" + "\n".join(
        offenders
    )


def test_every_listed_markup_is_still_there_once():
    """An entry covers the one call it was written for: gone, it is taken off the list; a second
    call of the same words in the same function is looked at and listed for itself."""
    found = Counter(key for key, _ in markup())
    assert not set(VIVIBOX_MARKUP) - set(found), set(VIVIBOX_MARKUP) - set(found)
    twice = {key: n for key, n in found.items() if n > 1 and key in VIVIBOX_MARKUP}
    assert not twice, f"the same words twice in one function, one entry: {twice}"


def test_the_helpers_taken_for_plain_are():
    """What the guard takes on trust, held to it: look's helpers escape the text they colour, and
    the others return a text object, which is never read as markup."""
    from textual.content import Content

    from vivibox import look, newtask
    from vivibox.config import ORCHESTRATION_MODES

    text = "x[/b] [black on black]y"
    for name in ESCAPING:
        made = getattr(look, name)(text, look.ERROR) if name == "colored" else getattr(look, name)(text)
        assert Content.from_markup(made).plain == text, name
    assert isinstance(look.hints(("a", text)), Content) and text in look.hints(("a", text)).plain
    assert isinstance(newtask.branch_label(text), Content) and text in newtask.branch_label(text).plain
    assert all(isinstance(newtask.mode_option(name), Content) for name in ORCHESTRATION_MODES)
    assert {name for name in OBJECTS} == {"hints", "branch_label", "mode_option"}, "each held above"


def test_the_guard_sees_every_shape_a_string_is_drawn_through():
    """A call the guard does not look at is one nobody decided on: each shape, with text from
    outside, is an offender; made plain, or its widget made with markup=False, it is not."""

    def flagged(source: str) -> list[str]:
        found = Sinks(ast.parse(source), view=True).found
        return [ast.unparse(text) for _, _, _, text, off in found if not off and not plain(text)]

    for shape in (
        "self.notify(x)",
        "Label(x)",
        "Static(x)",
        "Button(x)",
        "Option(x)",
        "Selection(x, 1)",
        "Select([(x, 1)])",
        "Select([(x, v) for x, v in rows])",
        "Select([*((x, x) for x in rows), ('new', 0)])",
        "options.set_options(x)",
        "options.add_options([x for x in rows])",
        "options.add_option(x)",
        "self.query_one('#a', Label).update(x)",
        "Text.from_markup(x)",
        "Content.from_markup(x)",
        "frame.border_title = x",
        "frame.border_subtitle = x",
        "button.label = x",
        "self.app.call_from_thread(self.notify, x)",
    ):
        assert flagged(shape) == ["x"], shape
    for safe in (
        "Label('words')",
        "Label(x, markup=False)",
        "Button(escape(x))",
        "Select([(Content(x), v) for x, v in rows])",
        "options.set_options([Content(x) for x in rows])",
        "frame.border_title = escape(x) or None",
        "Label(look.colored(x, look.ERROR))",
        "Label(f'[b {look.ERROR}]Deleted[/] {escape(x)}')",
        "def f(self):\n    yield Label('', id='a', markup=False)\n    self.query_one('#a', Label).update(x)",
        "def f(self):\n    yield Label('', id='a', markup=False)\n    a = self.query_one('#a')\n    a.update(x)",  # noqa: E501
        "def f(self):\n    rows = [(Content(x), v) for x, v in found]\n    yield Select(rows)",
    ):
        assert flagged(safe) == [], safe
    # A name, a list made elsewhere, a widget whose markup is on: not taken on trust.
    assert flagged("def f(self):\n    yield Label('', id='a')\n    self.query_one('#a', Label).update(x)")
    assert flagged("def f(self, rows):\n    yield Select(rows)") == ["rows"]
    assert flagged("Label(f'{x} {escape(y)}')") == ["f'{x} {escape(y)}'"]


# --- the demo: the three places the review found -------------------------------------------------


def test_what_the_agent_left_running_is_said_as_text(env, monkeypatch):
    from test_tui import new_task, run

    task = new_task()
    stopped = demo.Demo(commands=["x"], starting=True, stopped=[f"python3 -m http.server {EVIL} (port 8000)"])
    monkeypatch.setattr(actions, "demo", lambda task_id, ask=False, reply="": stopped)

    async def scenario(app, pilot):
        app.run_demo(task.id)
        await app.workers.wait_for_complete()
        await pilot.pause()
        said = [n for n in app._notifications if "left running" in n.message]
        assert said and said[-1].markup is False and "x[/b] [black on black]hidden" in said[-1].message
        assert "\x1b" not in said[-1].message and BIDI not in said[-1].message

    run(scenario, notifications=True)


def label_text(app) -> str:
    from textual.widgets import Label

    return "\n".join(str(label.render()) for label in app.screen.query(Label))


def test_the_agents_question_about_running_it_is_shown_as_text(env):
    from test_tui import new_task, run

    from vivibox.dialogs import Reply

    task = new_task()

    async def scenario(app, pilot):
        app.answer_demo(task.id, f"Which port? {EVIL}")
        await pilot.pause()
        assert isinstance(app.screen, Reply)
        shown = label_text(app)
        assert "Which port? x[/b] [black on black]hidden" in shown and "\x1b" not in shown, shown

    run(scenario)


def test_an_earlier_tasks_way_to_run_it_is_shown_as_text(env, monkeypatch):
    from test_tui import new_task, run

    from vivibox.widgets import Confirm

    new_task()
    monkeypatch.setattr(actions, "demo_commands", lambda project, task: ([], ""))
    monkeypatch.setattr(actions, "demo_from_history", lambda name: f"npm start {EVIL}")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.action_demo()
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        shown = label_text(app)
        assert "npm start x[/b] [black on black]hidden" in shown and "\x1b" not in shown, shown

    run(scenario)


# --- l with a pager other than less, where the copies cannot go -----------------------------------


def test_l_says_why_when_the_copies_folder_is_not_yours_alone(env, monkeypatch):
    import tempfile

    from test_tui import new_task, run

    task = new_task()
    monkeypatch.setenv("PAGER", "cat")
    folder = Path(tempfile.gettempdir()) / f"vivibox-{os.getuid()}"
    folder.mkdir(exist_ok=True)
    folder.chmod(0o755)  # as another's would be to you: not yours alone

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.action_show_log()
        await pilot.pause()
        said = [n.message for n in app._notifications]
        assert any("not a folder of yours alone" in m for m in said), said

    try:
        run(scenario, notifications=True)
    finally:
        folder.chmod(0o700)
    assert task.read_state()


def test_the_suite_writes_no_copy_in_your_own_temporary_folder():
    import tempfile

    from conftest import TEMPORARY

    assert Path(tempfile.gettempdir()) == TEMPORARY, "the suite's own temporary folder, not yours"
