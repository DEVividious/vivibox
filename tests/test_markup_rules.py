"""Textual reads a string as markup unless told not to, and what vivibox shows is often text it
did not write: the agent's output, commands and questions, file names and git's words, a build's
output, a plan, a provider's error, a model's name from a provider's catalogue. Such text never
reaches a widget as markup: [/b] in it would end the view (MarkupError), [black on black] hide
what follows.

Every place a string becomes what a widget draws is looked at here, by its shape: a notification
(asked of anything, now or handed on for later), a Label, a Static, a Button, a Checkbox, an
Option, a Selection, a Select's options and its words while nothing is chosen, a list of options
as it is made and as it is filled later, a table's headings and cells, an update of a widget,
from_markup, a tooltip, a border's title, a button's label; each by its first argument or by the
name it takes it by. What it is given is
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
    (
        "app_support.py",
        "LiveFooter.compose",
        "tooltip",
        "tooltip or binding.description",
    ): "a key's own words in the footer (BINDINGS), vivibox's",
    (
        "table.py",
        "TaskTable.set_columns",
        "add_column",
        "Text(name, justify='right') if name in RIGHT else name",
    ): "a column's name, vivibox's",
    (
        "table.py",
        "TaskTable.fill_table",
        "add_row",
        "goal if name == 'GOAL' else self.cell(name, cells.get(name, ''))",
    ): "text objects: the goal cut to its width, and what cell makes (listed there)",
    (
        "table.py",
        "TaskTable.spin",
        "update_cell",
        "self.cell('STATUS', self.status(st))",
    ): "a text object, made by cell (listed there)",
    (
        "tui.py",
        "Vivibox.pods_answered",
        "update_cell",
        "self.demo_cell(task_id)",
    ): "vivibox's tag around its own word for a demo's state, and a count",
    (
        "tui.py",
        "Vivibox.notify",
        "notify",
        "display.terminal(str(message))",
    ): "the app's own notify, which hands on what its caller says of markup: each caller is looked at",
    ("usage_view.py", "Usage.compose", "add_columns", "usage.COLUMNS"): "the columns' names, vivibox's",
    (
        "usage_view.py",
        "Usage.show",
        "add_row",
        "usage.cells(row)",
    ): "a task's id and figures vivibox counted: no bracket in any (held below)",
    (
        "verify_ui.py",
        "AskVerify.compose",
        "Checkbox",
        "MODULES.format(count=len(self.modules))",
    ): "a constant of vivibox's with a count in it",
    ("verify_ui.py", "AskVerify.compose", "Checkbox", "WRITER"): "a constant of vivibox's",
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

# What takes a string and draws it: the callable's name, with the name it takes the string by
# when that is not its first argument alone.
WIDGETS = {
    "Label": "content",
    "Static": "content",
    "Button": "label",
    "Option": "prompt",
    "Selection": "prompt",
    "Checkbox": "label",
    "RadioButton": "label",
}
# What is given a list whose every member is drawn (a pair's first), and by which names.
FILLED = {
    "Select": ("options",),
    "set_options": ("options",),
    "add_options": ("new_options", "items"),
    "add_option": ("option", "item"),
}
# What draws every argument it is given in a row: the strings of a list of options, a table's cells
# and headings.
SPREAD = ("OptionList", "SelectionList", "add_row", "add_columns")
# What draws the argument at a place of its own: where, and by which name.
PLACED = {
    "from_markup": (0, ("text", "markup")),
    "add_column": (0, ("label",)),
    "with_tooltip": (0, ("tooltip",)),
    "update_cell": (2, ("value",)),
    "update_cell_at": (1, ("value",)),
    "replace_option_prompt": (1, ("prompt",)),
    "replace_option_prompt_at_index": (1, ("prompt",)),
}
# A word of these, given by name to anything, is drawn by what takes it: a widget's tooltip, a
# Select's words while nothing is chosen, a row's label.
DRAWN_BY_NAME = {"tooltip": "tooltip", "prompt": "Select", "label": "add_row"}
ASSIGNED = ("border_title", "border_subtitle", "label", "tooltip")
# What makes text that is not markup, whatever it is given: rich's escape, and text objects.
PLAIN = ("escape", "Text", "Content", "assemble")
# vivibox's own: the helpers of look.py that wrap a tag of the palette around text they escape,
# and those that return text objects. test_the_helpers_taken_for_plain_are held to that.
ESCAPING = ("colored", "muted", "secondary", "faint", "subscription")
OBJECTS = ("hints", "branch_label", "mode_option")
# Whose a name of those must be, when it is asked of something (look.muted, Content.assemble):
# re.escape and html.escape are neither, and leave a bracket as it is.
OWNERS = {
    "escape": ("markup",),
    "assemble": ("Text", "Content"),
    **dict.fromkeys(ESCAPING, ("look",)),
    "hints": ("look",),
    "branch_label": ("newtask",),
    "mode_option": ("newtask",),
}
# The messages of a task (supervisor.notify, a supervisor's ports): plain text by their own
# escaping, and no notification of Textual's. Every other notify is one.
TASKS_OWN = ("supervisor", "ports")
# The names a module may have Textual's widgets by. One the guard does not know draws what
# nobody looked at: a new one is added here with the shape it is drawn through, above.
KNOWN_WIDGETS = {
    *WIDGETS, "Select", "OptionList", "SelectionList", "DataTable",
    "Input", "TextArea", "Markdown", "DirectoryTree", "Footer",
    # the parts of Textual's own widgets the view is built on: the footer's keys, a Select's
    # list, the panel's blocks (tests/test_structure.py holds the names)
    "FooterKey", "SelectOverlay", "MarkdownBlock", "MarkdownBlockQuote", "MarkdownFence",
    "MarkdownHorizontalRule", "MarkdownTable", "MarkdownTableContent",
}  # fmt: skip
# Not drawn as markup, whatever it holds: a field's text and its placeholder, a file's name in
# the tree of folders (both plain text), and the panel's Markdown, which
# tests/test_agent_prose.py and tests/test_untrusted_text.py hold.
NOT_MARKUP = ("Input", "TextArea", "Markdown", "DirectoryTree")


def _name(func) -> str:
    if isinstance(func, ast.Subscript):  # SelectionList[int](…): the class, whatever it is a list of
        return _name(func.value)
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _textual_notify(func) -> bool:
    """A notify asked of anything (self, the app, a screen, a widget, super()) is Textual's, but
    for the two that are a task's messages (TASKS_OWN)."""
    if not isinstance(func, ast.Attribute) or func.attr != "notify":
        return False
    owner = func.value
    named = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return named not in TASKS_OWN


def _given(call: ast.Call, place: int, names: tuple[str, ...] | str, args: list | None = None):
    """What a call is given at a place, or by one of the names; None when it is given neither."""
    args = call.args if args is None else args
    if len(args) > place and not any(isinstance(arg, ast.Starred) for arg in args[: place + 1]):
        return args[place]
    names = (names,) if isinstance(names, str) else names
    return next((k.value for k in call.keywords if k.arg in names), None)


def _off(call: ast.Call) -> bool:
    return any(
        k.arg == "markup" and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in call.keywords
    )


def _makes_plain(func) -> bool:
    """Whether a callable is one of those taken for making plain text: by its bare name, or
    asked of what it is known to be of (OWNERS)."""
    if isinstance(func, ast.Name):
        return func.id in (*PLAIN, *ESCAPING, *OBJECTS)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in OWNERS.get(func.attr, ())
    return False


def plain(node) -> bool:
    """Whether what node makes cannot be markup an outsider wrote: a constant, text made by
    escape, Text or Content, or a choice or a joining of such."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Call):
        return _makes_plain(node.func)
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


def _returned(tree: ast.AST) -> dict[str, ast.AST]:
    """The functions of a module that return a list written where they return it, by name: what
    hands a widget such a function's result hands it that list. Not one that returns in two
    places, nor two functions of one name."""
    found: dict[str, list] = {}
    for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        returns = [n.value for n in ast.walk(function) if isinstance(n, ast.Return)]
        found.setdefault(function.name, []).append(returns)
    return {
        name: returns[0]
        for name, (returns, *others) in found.items()
        if not others and len(returns) == 1 and isinstance(returns[0], ast.List | ast.ListComp)
    }


class Sinks(ast.NodeVisitor):
    """(function, line, what, the text as written, markup off) for every place of a module that
    hands a widget what it draws."""

    def __init__(self, tree: ast.AST, view: bool):
        self.scope: list[str] = []
        self.found: list[tuple[str, int, str, ast.AST, bool]] = []
        self.view, self.off_ids = view, _ids(tree)
        self.names: dict[str, str] = {}  # a variable of the function, and the id it was queried by
        self.lists: dict[str, ast.AST] = {}  # a variable of the function, and the list it was given
        self.returned = _returned(tree)  # a function of the module, and the list it returns
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
        """A list handed over by a variable of the function, or made by a function of the
        module that returns one (self.options()): the list itself, as written."""
        if isinstance(node, ast.Name):
            return self.lists.get(node.id, node)
        func = node.func if isinstance(node, ast.Call) and not node.args and not node.keywords else None
        own = isinstance(func, ast.Name) or (
            isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self"
        )
        return self.returned.get(_name(func), node) if own else node

    def members(self, node) -> list:
        """The items of what is given as a list, a variable of the function among them or
        spread into it (*rows): each as written."""
        found = []
        for item in _each(self.listed(node)):
            found += _each(self.listed(item)) if self.listed(item) is not item else [item]
        return found

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
        what, func = _name(node.func), node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in WIDGETS:
            what = func.value.id  # Button.success(…): a Button by another door
        # A notification handed on to be made later: call_from_thread(self.notify, text),
        # call_later, a partial.
        handed = next((i for i, arg in enumerate(node.args) if _textual_notify(arg)), None)
        if handed is not None:
            self.add_given(node, "notify", _given(node, 0, "message", node.args[handed + 1 :]), _off(node))
        elif what == "notify" and _textual_notify(func):
            self.add_given(node, what, _given(node, 0, "message"), _off(node))
        elif what in WIDGETS:
            self.add_given(node, what, _given(node, 0, WIDGETS[what]), _off(node))
        elif what in PLACED:
            self.add_given(node, what, _given(node, *PLACED[what]))
        elif what in FILLED:
            given = _given(node, 0, FILLED[what])
            for item in self.members(given) if given is not None else ():
                self.add(node, what, item.elts[0] if _pair(item) else item)
        elif what in SPREAD:
            for item in (item for arg in node.args for item in self.members(arg)):
                self.add(node, what, item.elts[0] if _pair(item) else item, _off(node))
        elif what == "update" and self.view:
            owner = node.func.value
            asked = _queried(owner) or (self.names.get(owner.id, "") if isinstance(owner, ast.Name) else "")
            self.add_given(node, what, _given(node, 0, "content"), asked in self.off_ids)
        for keyword in node.keywords:
            taken = DRAWN_BY_NAME.get(keyword.arg or "")
            if taken and (taken == "tooltip" or taken == what):
                self.add(node, taken if taken == "tooltip" else f"{what} {keyword.arg}", keyword.value)
        self.generic_visit(node)

    def add_given(self, node, what: str, text, off: bool = False) -> None:
        if text is not None:
            self.add(node, what, text, off)


def of_the_view(name: str, source: str) -> bool:
    """Whether a module is the view's, where an update is a widget's (elsewhere it is a set's
    or a digest's): one that names Textual, one that asks the screen for a widget, and the
    groups of the view's keys (keys_*.py), which are mixed into the app and import neither."""
    return "textual" in source or "query_one" in source or name.startswith("keys_")


def messages():
    """(module, function, line, what, the text as written, markup off) for every sink."""
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text()
        view = of_the_view(path.name, source)
        for found in Sinks(ast.parse(source), view).found:
            yield (path.name, *found)


def offends(text, off: bool) -> bool:
    """Whether what a sink is given could be markup an outsider wrote. An Option or a Selection
    handed to a list is looked at where it is made."""
    made = isinstance(text, ast.Call) and _name(text.func) in WIDGETS
    return not off and not plain(text) and not made


def markup():
    """The sinks given something that could be markup an outsider wrote: (key, where)."""
    for module, scope, line, what, text, off in messages():
        if offends(text, off):
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


def flagged(source: str, module: str = "dialogs.py") -> list[str]:
    """What the guard holds against a source, were it a module of that name: each text handed
    to something that draws it, neither plain nor with markup off, as written."""
    found = Sinks(ast.parse(source), view=of_the_view(module, f"import textual\n{source}")).found
    return [ast.unparse(text) for _, _, _, text, off in found if offends(text, off)]


def test_the_guard_sees_every_shape_a_string_is_drawn_through():
    """A call the guard does not look at is one nobody decided on: each shape, with text from
    outside, is an offender; made plain, or its widget made with markup=False, it is not."""
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
        *BY_ANOTHER_SHAPE,
    ):
        assert flagged(shape) == ["x"], shape
    for safe in (
        "Label('words')",
        "Label(x, markup=False)",
        "Label(content=x, markup=False)",
        "Button(escape(x))",
        "Select([(Content(x), v) for x, v in rows])",
        "options.set_options([Content(x) for x in rows])",
        "frame.border_title = escape(x) or None",
        "Label(look.colored(x, look.ERROR))",
        "Label(f'[b {look.ERROR}]Deleted[/] {escape(x)}')",
        "def f(self):\n    yield Label('', id='a', markup=False)\n    self.query_one('#a', Label).update(x)",
        "def f(self):\n    yield Label('', id='a', markup=False)\n    a = self.query_one('#a')\n    a.update(x)",  # noqa: E501
        "def f(self):\n    yield Label('', id='a', markup=False)\n    self.query_one('#a').update(x, layout=False)",  # noqa: E501
        "def f(self):\n    rows = [(Content(x), v) for x, v in found]\n    yield Select(rows)",
        "def f(self):\n    rows = [escape(x) for x in found]\n    yield OptionList(*rows, 'Browse…')",
        "def options(self):\n    return [Option(escape(x)) for x in found]\n"
        "def f(self):\n    yield OptionList(*self.options())",
        "OptionList(*rows, markup=False)",
        "table.add_row(Text(x), escape(y), key=x)",
        "self.call_from_thread(self.notify, x, markup=False)",
        "self.screen.notify(message=x, markup=False)",
        "self.notify('Saved.', title=x)",  # a notification's title is drawn as text, never as markup
        "supervisor.notify(task.id, x)",
        "self.ports.notify(task.id, x)",
        "Label(Content.assemble(x, (y, 'bold')))",
        "Input(x, placeholder=y)",
    ):
        assert flagged(safe) == [], safe
    # A name, a list made elsewhere, a widget whose markup is on: not taken on trust.
    assert flagged("def f(self):\n    yield Label('', id='a')\n    self.query_one('#a', Label).update(x)")
    assert flagged("def f(self, rows):\n    yield Select(rows)") == ["rows"]
    assert flagged("Label(f'{x} {escape(y)}')") == ["f'{x} {escape(y)}'"]
    assert flagged("def f(self, rows):\n    yield OptionList(*rows)") == ["rows"]
    raw = "def options(self):\n    return [x for x in found]\n"
    raw += "def f(self):\n    yield OptionList(*self.options())"
    assert flagged(raw) == ["x"], "a list of strings behind a method of the module"
    assert flagged(raw.replace("self.options()", "other.options()")) == ["other.options()"], "not another's"
    assert flagged("table.add_row(x, Text(y), z)") == ["x", "z"]
    assert flagged("table.add_columns(*names, (x, 'key'))") == ["names", "x"]


# The shapes a review found the guard walking past, each with text from outside in it: given by
# name, with another argument beside it, as the strings of a list or the cells of a table, as a
# tooltip, as a notification of something else than the app, or handed on to be made later.
BY_ANOTHER_SHAPE = (
    "Label(content=x)",
    "Static(content=x, classes='wrap')",
    "Button(label=x)",
    "Button.success(x)",
    "Option(prompt=x)",
    "Selection(prompt=x, value=1)",
    "Select(options=[(x, 1)])",
    "Select([], prompt=x)",
    "self.notify(message=x)",
    "self.notify(message=x, severity='error')",
    "self.screen.notify(x)",
    "self.app.screen.notify(x)",
    "super().notify(x)",
    "self.call_later(self.notify, x)",
    "self.call_after_refresh(self.screen.notify, x)",
    "self.set_timer(1, partial(self.notify, x))",
    "self.query_one('#a', Label).update(x, layout=False)",
    "self.query_one('#a', Label).update(content=x)",
    "OptionList(x)",
    "OptionList(*[x for x in rows])",
    "OptionList('Browse…', x, id='sources')",
    "SelectionList((x, 1))",
    "SelectionList(*[(x, v) for x, v in rows])",
    "options.set_options(options=x)",
    "options.add_options(new_options=[x])",
    "options.add_option(option=x)",
    "options.replace_option_prompt('id', x)",
    "options.replace_option_prompt_at_index(0, prompt=x)",
    "table.add_row(x)",
    "table.add_row('1', x, key='k')",
    "table.add_row('1', label=x)",
    "table.add_column(x)",
    "table.add_column(label=x, key='k')",
    "table.add_columns('TASK', x)",
    "table.update_cell(row, column, x)",
    "table.update_cell(row, column, value=x, update_width=True)",
    "table.update_cell_at(where, x)",
    "Checkbox(x)",
    "Checkbox(label=x, value=True)",
    "RadioButton(x)",
    "Label('words').with_tooltip(x)",
    "Label('words', tooltip=x)",
    "FooterKey(key, shown, said, action, tooltip=x)",
    "Text.from_markup(text=x)",
    "Content.from_markup(markup=x)",
)
# And what was taken for plain by a name alone, whatever it was a name of.
BY_A_NAME_ALONE = (
    "Label(re.escape(x))",
    "Label(html.escape(x))",
    "Label(manual.assemble(task, x))",
    "Label(stats.subscription(x))",
    "Label(theirs.muted(x))",
    "Label(self.hints(x))",
)


def test_the_shapes_a_review_found_are_each_an_offender_now_and_were_none_before():
    """What the guard looked at before: a first argument of a known widget, an update with one
    argument and nothing else, a notify of self or the app, and any function by the name of
    escape. None of BY_ANOTHER_SHAPE was held against its source by that."""

    def before(source: str) -> bool:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and node.args:
                what, first = _name(node.func), node.args[0]
                trusted = isinstance(first, ast.Call) and _name(first.func) in (*PLAIN, *ESCAPING, *OBJECTS)
                app_s = (
                    what == "notify"
                    and isinstance(node.func.value, ast.Name | ast.Attribute)
                    and (
                        getattr(node.func.value, "id", "") in ("self", "app")
                        or getattr(node.func.value, "attr", "") == "app"
                    )
                )
                later = what == "call_from_thread" and len(node.args) > 1
                drawn = (
                    what in ("Label", "Static", "Button", "Option", "Selection", "from_markup")
                    or app_s
                    or later
                )
                drawn = drawn or (what == "update" and len(node.args) == 1 and not node.keywords)
                drawn = drawn or what in ("Select", "set_options", "add_options", "add_option")
                if drawn and not trusted and not isinstance(first, ast.Constant | ast.List):
                    return True
        return False

    assert (
        before("Label(x)") and before("self.notify(x)") and before("w.update(x)") and not before("Label('a')")
    )
    for shape in BY_ANOTHER_SHAPE:
        assert not before(shape) and flagged(shape) == ["x"], shape
    for shape in BY_A_NAME_ALONE:
        assert not before(shape) and len(flagged(shape)) == 1, shape


def test_an_update_in_a_group_of_the_view_s_keys_is_a_widget_s():
    """keys_*.py are mixed into the app and name neither Textual nor a widget's class: an update
    there was taken for a set's, and what it was given was not looked at."""
    update = "class Keys:\n    def failed(self, e):\n        self.text.update(str(e))\n"
    assert not of_the_view("repo.py", update) and of_the_view("keys_plan.py", update)
    assert flagged(update, "keys_plan.py") == ["str(e)"]
    asked = "def failed(self, e):\n    self.query_one('#empty').update(str(e))\n"
    assert of_the_view("planning.py", asked), "and any module that asks the screen for a widget"
    keys = sorted(path.name for path in PACKAGE.glob("keys_*.py"))
    assert len(keys) >= 9 and all(of_the_view(name, (PACKAGE / name).read_text()) for name in keys)
    without = [name for name in keys if "textual" not in (PACKAGE / name).read_text()]
    assert {"keys_plan.py", "keys_project.py", "keys_work.py"} <= set(without), "the three a review named"


# Where a bare name taken for plain may come from: rich's and Textual's own, and vivibox's
# helpers from the module that defines them.
HOMES = {
    "escape": ("rich.markup", "textual.markup"),
    "Text": ("rich.text",),
    "Content": ("textual.content",),
    **dict.fromkeys((*ESCAPING, "hints"), ("look",)),
    "branch_label": ("newtask",),
    "mode_option": ("newtask",),
}


def strangers(module: str, source: str) -> list[str]:
    """The names taken for plain that a module has from somewhere else than their home: an
    import from another module or under another name, a function of its own, an assignment."""
    tree = ast.parse(source)
    inside = {id(f) for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for f in c.body}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            origin = getattr(node, "module", None) or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in HOMES and not (alias.name == bound and origin in HOMES[bound]):
                    found.append(f"{bound} from {origin or alias.name}")
        elif (
            isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name in HOMES and id(node) not in inside
        ):
            if module.removesuffix(".py") not in HOMES[node.name]:
                found.append(f"{node.name} defined")
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [f"{t.id} assigned" for t in targets if isinstance(t, ast.Name) and t.id in HOMES]
    return found


def test_a_name_taken_for_plain_is_the_one_that_makes_plain_text():
    """The guard takes escape(x), Text(x), Content(x) and look's helpers for plain by their
    names. Asked of something, the name must be that thing's (look.muted, Content.assemble:
    re.escape and html.escape leave a bracket as it is); bare, it must have come from where the
    real one lives, in every module of the package."""
    sources = {path.name: path.read_text() for path in sorted(PACKAGE.glob("*.py"))}
    found = {
        name: odd
        for name, source in sources.items()
        if of_the_view(name, source) and (odd := strangers(name, source))
    }
    assert found == {}, "a name the guard on markup takes for plain, bound to something else"
    assert sum(of_the_view(name, source) for name, source in sources.items()) >= 20, "the view's modules"
    for source in (
        "from html import escape",
        "from re import escape",
        "from rich.markup import render as escape",
        "import html as escape",
        "from .text import Text",
        "def escape(text):\n    return text",
        "def f():\n    def muted(text):\n        return text\n    return Label(muted(x))",
        "escape = str",
        "from .look import muted as faint",
    ):
        assert strangers("dialogs.py", source), source
    for source in (
        "from rich.markup import escape",
        "from textual.content import Content",
        "from .look import muted, hints",
        "class Stats:\n    def subscription(self):\n        return 0.0",
    ):
        assert strangers("dialogs.py", source) == [], source
    assert strangers("look.py", "def muted(text):\n    return text") == []


def test_every_widget_of_textual_s_in_the_package_is_one_the_guard_knows():
    """A widget the guard has no shape for draws what nobody looked at: the names the package
    takes from Textual's widgets are all known here, each either with its shape above or said
    to draw no markup (NOT_MARKUP)."""
    taken = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("textual.widgets"):
                taken |= {alias.name for alias in node.names}
    assert taken <= KNOWN_WIDGETS, f"decide how each is drawn, then add it: {sorted(taken - KNOWN_WIDGETS)}"
    assert {"Label", "Static", "Button", "Option", "Selection", "Checkbox", "Select", "OptionList"} <= taken
    assert set(NOT_MARKUP) <= KNOWN_WIDGETS and not set(NOT_MARKUP) & set(WIDGETS)
    from textual.widgets import DirectoryTree

    name = "x[/b] [black on black]y"
    assert DirectoryTree.process_label(None, name).plain == name, "a file's name is drawn as it is"


def test_the_cells_of_the_usage_table_hold_no_bracket():
    """The usage table is given strings, which a table reads as markup: a task's id, which is
    letters, digits and dashes, and figures vivibox counted and wrote the units of."""
    from vivibox import resources, usage
    from vivibox.task import TASK_ID

    assert not any(c in TASK_ID.pattern.replace("[a-z0-9]", "").replace("[a-z0-9-]", "") for c in "[]")
    now = resources.Resources(
        [resources.Container("agent", 250.0, 3 * 10**9, 4 * 2**30)], {"docker": 20 * 10**9}
    )
    full = usage.Usage("demo-1", "demo", True, 60.0, 3600.0, 90.0, 12.5, 4000.0, 1.25, 0.41, True, now)
    cells = [*usage.cells(full), *usage.cells(usage.Usage("demo-2", "demo", False)), *usage.COLUMNS]
    assert not [cell for cell in cells if "[" in cell or "]" in cell], cells


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
