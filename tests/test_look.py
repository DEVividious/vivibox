"""The design system of docs/ux-guidelines.md §10: one theme, and focus, selection, status and the
primary action each drawn their own way, the same in every dialog."""

import inspect
import re
from pathlib import Path

import pytest
from test_tui import AVAILABLE, implementing, new_task, rows, run
from textual.widgets import Input, Label, OptionList
from textual.widgets._footer import FooterKey
from ux import screen_text

from vivibox import (
    app_support,
    branches,
    browse,
    dialogs,
    logs,
    look,
    newtask,
    providers_ui,
    settings,
    table,
    ui,
    usage_view,
    verify_ui,
    widgets,
)
from vivibox.config import ORCHESTRATION_MODES
from vivibox.states import State

VIEW = [
    app_support,
    branches,
    browse,
    dialogs,
    logs,
    newtask,
    providers_ui,
    settings,
    table,
    usage_view,
    widgets,
]


def test_colours_are_named_by_meaning_not_by_hue():
    """A colour in the view comes from look.py by what it means: a hue written in a module is a
    colour nobody decided on, and the next screen picks another."""
    hue = re.compile(r"\[(?:yellow|green|red|cyan|blue|magenta|grey\d+|dim)\]|\"(?:yellow|green|red|cyan)\"")
    for module in VIEW:
        found = hue.findall(Path(module.__file__).read_text())
        assert not found, f"{module.__name__}: {found}"


def test_every_status_has_a_mark_so_it_reads_without_colour():
    """● waits for you, ✕ failed, ○ nobody runs it, ‖ you stopped it, ✓ done; at work, the spinner."""
    marks = {rank: mark for rank, (mark, _) in look.MARKS.items()}
    shown = [mark for rank, mark in marks.items() if rank != ui.AT_WORK]
    assert len(set(shown)) == len(shown) and all(shown), marks
    view = ui.TaskView("agent turn failed", ui.WAITS, ui.FAILED)
    assert "✕ agent turn failed" in look.badge(view)
    working = ui.TaskView("implementing", ui.WORKS, ui.AT_WORK)
    assert "⠋ implementing" in look.badge(working, "⠋")


def test_the_row_under_the_cursor_keeps_its_colours(env):
    """Selection is a lighter surface, not the accent over every cell: a failed task's red and a
    waiting one's amber read the same under the cursor."""
    failed = implementing("Rate limited")
    failed.set_paused(True, problem="agent turn failed: 429")

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(failed.id))
        await pilot.pause()
        assert app.table.cursor_foreground_priority == "renderable"
        cursor = app.table.get_component_rich_style("datatable--cursor")
        assert cursor.bgcolor and cursor.bgcolor.triplet.hex.lower() == look.SELECTION
        assert cursor.bgcolor.triplet.hex.lower() != look.ACCENT, "the accent is for focus"

    run(scenario)


def test_the_command_bar_puts_decisions_first_and_what_works_anywhere_last(env):
    task = new_task("Plan me")
    task.transition(State.CHECKPOINT_PLAN)

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        bar = app.query_one(app_support.LiveFooter)
        order = [(w.action, w.has_class("-decision")) for w in bar.query(FooterKey)]
        actions = [a for a, _ in order]
        assert order[0] == ("accept", True), order
        assert actions[-4:] == ["new", "new_project", "help", "quit"], "anywhere, at the end"
        rule = bar.query(".bar-rule")
        assert rule and rule.first().region.x < bar.query_one(".bar-spacer").region.x, (
            "a rule after the decisions"
        )

    run(scenario)


def test_every_dialog_names_its_keys_in_its_frame(env):
    """The keys that close a dialog are in its frame's bottom edge, the same place in every one,
    never a sentence of their own among the fields."""
    dialog_classes = [
        cls
        for module in (
            dialogs,
            browse,
            providers_ui,
            settings,
            logs,
            verify_ui,
            branches,
            usage_view,
            newtask,
            widgets,
        )
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, widgets.Dialog) and cls.__module__ == module.__name__
    ]
    assert len(dialog_classes) >= 20, dialog_classes
    for module in (dialogs, browse, providers_ui, settings, verify_ui, newtask):
        source = Path(module.__file__).read_text()
        assert not re.search(r"Label\([^)]*\((?:ctrl\+s|Esc|Enter) [a-z]+\)", source), module.__name__
        assert "ctrl+s creates" not in source and "Esc closes" not in source, module.__name__

    async def scenario(app, pilot):
        for screen in (
            dialogs.Reply("demo-1"),
            settings.Ask("A value:", "x"),
            dialogs.ChooseSession([("writer", "at work")]),
            verify_ui.AskVerify("demo", ["make"]),
        ):
            app.push_screen(screen)
            await pilot.pause()
            frame = screen.query_one(".dialog")
            assert frame.border_subtitle and "esc" in str(frame.border_subtitle), type(screen).__name__
            app.pop_screen()
            await pilot.pause()

    run(scenario)


def test_the_label_of_the_focused_row_is_the_one_in_the_accent(env):
    """Focus is one signal in a form: one row at a time is marked, and it moves with the keys."""

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        form = app.screen
        marked = [row for row in form.query(".row.-focused")]
        assert [r.id for r in marked] == ["task-row"], "the goal has the keys when the form opens"
        form.query_one("#plan").focus()
        await pilot.pause()
        marked = list(form.query(".row.-focused"))
        assert len(marked) == 1 and marked[0].query("#plan"), "the mark moved with the focus"
        label = marked[0].query_one(".key", Label)
        assert label.styles.color.hex.lower() == look.ACCENT

    run(scenario)


def test_the_project_form_says_what_the_focused_field_is_for(env):
    """Help for a field is one line under the form that follows the focus, not a sentence between
    the fields."""

    async def scenario(app, pilot):
        await pilot.press("i")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, dialogs.NewProject)
        assert "repository you already have" in str(form.query_one("#about", Label).render())
        form.query_one("#name", Input).focus()
        await pilot.pause()
        assert "What the list" in str(form.query_one("#about", Label).render())
        form.query_one("#change").focus()
        await pilot.pause()
        assert "fresh clone" in str(form.query_one("#about", Label).render())

    run(scenario)


def test_a_list_dialog_is_one_of_names_and_what_they_are(env):
    """The pickers are one dialog: names in a column, what each is muted beside it, Enter takes."""
    picked = []

    async def scenario(app, pilot):
        app.push_screen(
            dialogs.ChooseSession([("planner", "planning"), ("writer", "at work")]), picked.append
        )
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.has_focus and options.highlighted == 0
        text = screen_text(app)
        assert "planner  planning" in text and "writer   at work" in text, "the names in one column"
        await pilot.press("down", "enter")
        await pilot.pause()

    run(scenario)
    assert picked == ["writer"]


def test_a_small_terminal_keeps_the_whole_footer_and_the_list_readable(env):
    new_task("A goal long enough to be cut where the terminal ends, and not run off it at all")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        lines = screen_text(app).splitlines()
        assert all(len(line) <= 80 for line in lines)
        assert "q Quit" in lines[-1] and "n New" in lines[-1]
        assert any("…" in line for line in lines), "the goal is cut with an ellipsis"

    run(scenario, size=(80, 24))


def test_at_eighty_columns_the_bar_keeps_every_key_of_finished_work(env):
    """Work to review offers the most keys; at 80 columns every one of them is on the bar, the
    decisions with their words, the keys that work anywhere at least as keys."""
    task = implementing("Done")
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index(task.id))
        await pilot.pause()
        last = screen_text(app).splitlines()[-1]
        for key in ("a Accept", "r Reply"):
            assert key in last, last
        assert last.split()[-4:] in (["n", "i", "?", "q"], ["?", "Help", "q", "Quit"]), (
            "the keys that work anywhere"
        )

    run(scenario, size=(80, 24))


def test_a_button_that_helps_fill_a_field_is_never_among_the_closing_ones(env, tmp_path):
    """New folder…, Import opencode.json… and Add/Manage stood beside Select, Add or Close and read
    as more ways to leave; they stand in the row of what they fill (§4)."""
    cases = [
        (lambda: browse.Browse("folder", "Pick", start=tmp_path), ["select", "cancel"], ["new-folder"]),
        (lambda: providers_ui.AddProvider([("deepseek", "DeepSeek")]), ["add", "cancel"], ["import"]),
        (lambda: providers_ui.ManageProviders(), ["close"], ["add", "import", "manage"]),
        (lambda: providers_ui.ManageItems(), ["save", "cancel"], ["remove"]),
    ]

    async def scenario(app, pilot):
        for make, closing, helpers in cases:
            screen = make()
            app.push_screen(screen)
            await pilot.pause()
            assert [b.id for b in screen.query(".buttons Button")] == closing, type(screen).__name__
            for helper in helpers:
                button = screen.query_one(f"#{helper}")
                assert button.parent.has_class("row"), f"{helper} stands in a row of its own dialog"
            dialog = screen.query_one(".dialog")
            assert dialog.region.bottom <= app.size.height, "the closing row is on the screen"
            app.pop_screen()
            await pilot.pause()

    run(scenario, size=(80, 24))


def test_the_import_and_the_manage_screens_are_forms_saved_with_ctrl_s(env, tmp_path):
    """What an opencode.json brings, under PROVIDERS and MCP SERVERS, and what is on, are forms like
    the others: sections, help under a rule, ctrl+s for the primary button."""
    from vivibox import keys, providers

    source = tmp_path / "opencode.json"
    source.write_text(
        '{"provider": {"deepseek": {"options": {"apiKey": "sk-theirs"}}},'
        ' "mcp": {"tools": {"type": "local", "command": ["npx", "tools-mcp"]}}}'
    )
    keys.set_key("openai", "sk-other")
    got = []

    async def scenario(app, pilot):
        app.push_screen(
            providers_ui.ChooseImport(source, providers.read_opencode(source, env={})), got.append
        )
        await pilot.pause()
        text = screen_text(app)
        assert "PROVIDERS" in text and "MCP SERVERS" in text and "From" in text
        assert "unticked" in str(app.screen.query_one("#about", Label).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert [f.name for f in got[0]] == ["deepseek", "tools"], "ctrl+s imports what is ticked"
        app.push_screen(providers_ui.ManageItems(), got.append)
        await pilot.pause()
        assert "ON FOR TASKS" in screen_text(app)
        assert "next time it starts" in str(app.screen.query_one("#about", Label).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert got[1] == [], "saved with nothing changed"
        assert not isinstance(app.screen, providers_ui.ManageItems)

    run(scenario)


def test_the_help_says_whole_what_o_opens_with_on_this_machine(env, monkeypatch):
    """A long editor command (JetBrains Toolbox's script under ~/.local/share) was cut at the
    dialog's edge, on a line of its own under the keys. It is the o line of a section of the help,
    wrapped, the home folder as ~."""
    import os

    from vivibox import ide

    home = os.path.expanduser("~")
    command = f"{home}/.local/share/JetBrains/Toolbox/scripts/idea"
    monkeypatch.setattr(ide, "candidates", lambda: [ide.Editor("IntelliJ IDEA", command)])

    async def scenario(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause()
        app.screen.query_one(".dialog").scroll_end(animate=False)  # the section is the help's last
        await pilot.pause()
        shown = " ".join(screen_text(app).replace("│", " ").split())
        assert "On this machine" in shown
        assert "o opens with ~/.local/share/JetBrains/Toolbox/scripts/idea, the first editor found" in shown
        assert "(k changes it)" in shown, "the note ends on the screen, not at the dialog's edge"
        lines = screen_text(app).splitlines()
        key = next(line for line in lines if "opens with" in line)
        wrapped = next(line for line in lines if "first editor" in line and "opens with" not in line)
        indent = lambda line: len(line) - len(line.lstrip("│ "))  # noqa: E731
        assert indent(wrapped) == key.index("opens with"), "a wrapped line goes on under its words"

    run(scenario, size=(80, 24))


def test_the_flow_help_follows_the_highlight_and_never_covers_the_form(env):
    """Comparing the flows: the open list goes up over the rows above it, the help under the
    fields says what the highlighted flow is, and neither covers the other or the buttons."""

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        form = app.screen
        flow = form.query_one("#orchestration")
        flow.focus()
        await pilot.press("enter")
        await pilot.pause()
        help_ = form.query_one(widgets.ContextHelp)
        assert help_.said.title == "Planner → Writer → Reviewer" and help_.said.badge == "recommended"
        await pilot.press("down")
        await pilot.pause()
        assert help_.said.title == "Supervisor ⇄ Worker", "the highlighted one, before it is picked"
        assert help_.said.diagram.index("Gate") < help_.said.diagram.index("(P+R)")
        overlay = flow.query_one("SelectOverlay")
        assert overlay.region.bottom <= flow.region.y, "the list opens upward"
        buttons = form.query_one(".buttons").region
        assert help_.region.bottom <= buttons.y, "the help ends above the buttons"
        assert not overlay.region.overlaps(help_.region), "the list leaves the help in view"
        await pilot.press("escape")
        await pilot.pause()
        assert help_.said.title == "Planner → Writer → Reviewer", "closed: the flow picked"

    run(scenario, size=(120, 40))


@pytest.mark.parametrize(("size", "shown"), [((120, 40), "full"), ((100, 30), "short"), ((80, 24), "none")])
def test_the_help_under_the_fields_gives_way_on_a_smaller_terminal(env, size, shown):
    """All of it on a tall terminal, the flow in two lines on a medium one, nothing on 80×24; the
    fields never scroll."""
    from textual.widgets import TextArea

    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        form = app.screen
        help_ = form.query_one(widgets.ContextHelp)
        assert help_.display == (shown != "none")
        assert help_.full == (shown == "full")
        assert form.query_one(widgets.Fields).max_scroll_y == 0
        assert form.query_one("#goal", TextArea).region.height >= 5, "three lines of the goal at least"
        text = screen_text(app)
        if shown == "full":
            assert "Sessions" in text and "Best for" in text
        if shown == "short":
            assert "P → W → Gate → R ⇄ W" in text and "3 sessions · independent review" in text

    run(scenario, size=size)


def test_each_flow_explains_itself_in_the_same_shape():
    for name in ORCHESTRATION_MODES:
        said = look.flow(name, {"planner": "big", "writer": "small"}, 3)
        assert [fact for fact, _ in said.facts] == ["Sessions", "Models", "Review", "Rounds", "Best for"]
        assert "Gate" in said.diagram and said.compact, name
        full = str(said.lines(True)).splitlines()
        assert len(full) == 8 and all(len(line) <= 84 for line in full[2:]), (name, full)
        assert len(str(said.lines(False)).splitlines()) == 2


def test_labels_values_metadata_and_disabled_are_four_different_tones():
    """Ordinary content never looks disabled: a value is the brightest text, a label a step
    quieter, metadata a step more, and a disabled control quieter still and dim."""
    tones = [look.FOREGROUND, look.SECONDARY, look.MUTED, look.DISABLED]
    assert len(set(tones)) == 4

    def light(color: str) -> float:
        r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    assert [light(t) for t in tones] == sorted((light(t) for t in tones), reverse=True)
    assert light(look.FOREGROUND) < light("#ffffff"), "off-white, not white"
    layers = [look.BACKGROUND, look.SURFACE, look.PANEL]
    assert [light(c) for c in layers] == sorted(light(c) for c in layers), "each layer lighter"
    assert look.SELECTION != look.ACCENT, "selection is a tint, focus the accent"
    css = Path(look.__file__).with_name("vivibox.tcss").read_text()
    assert "text-area--placeholder { color: $text-muted; }" in css, "a placeholder is readable"
    assert "Button:disabled" in css and "text-disabled-dim" in css


def test_a_focused_field_has_an_edge_of_the_accent(env):
    async def scenario(app, pilot):
        app.available = AVAILABLE
        await pilot.press("n")
        await pilot.pause()
        flow = app.screen.query_one("#orchestration")
        flow.focus()
        await pilot.pause()
        edge = flow.styles.border_left
        assert edge[0] == "outer" and edge[1].hex.lower() == look.ACCENT
        plan = app.screen.query_one("#plan")
        assert plan.styles.border_left[0] == "blank", "unfocused, the same column, empty"

    run(scenario, size=(120, 40))
