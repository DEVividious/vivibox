"""What an agent wrote is parsed apart from vivibox's own lines: nothing in it is a heading, nothing
in it reaches the lines around it, and it is drawn where vivibox's own words never are."""

import pytest
from test_tui import new_task, run
from test_untrusted_text import opening, plan_with, show
from ux import screen_text

from vivibox import display, markdown, sections

RULE = "┃"


def parsed(text: str) -> list:
    """The panel's text as the panel reads it."""
    return markdown.parser().parse(display.terminal(text))


def apart(tokens: list) -> tuple[list, list]:
    """The tokens that are vivibox's own, and those of what an agent wrote: the ones inside a
    frame vivibox put there, which no text can write (a quote of an agent's own has no such
    mark). The frames themselves are in neither."""
    own, agents, depth = [], [], 0
    for token in tokens:
        if token.type.startswith("blockquote_") and (token.meta or {}).get("prose"):
            depth += token.nesting
        else:
            (agents if depth else own).append(token)
    assert depth == 0, "every frame is closed"
    return own, agents


def headings(tokens: list) -> list[tuple[str, str]]:
    return [(t.tag, tokens[i + 1].content) for i, t in enumerate(tokens) if t.type == "heading_open"]


def shape(tokens: list) -> list:
    """What a run of tokens is, to compare two of them: kinds, levels, text, and each line's parts."""
    return [
        (t.type, t.tag, t.level, t.content, [(c.type, c.content) for c in t.children or []]) for t in tokens
    ]


def bold_lines(tokens: list) -> list[str]:
    """The paragraphs that are one bold line, by their text."""
    found = []
    for i, token in enumerate(tokens):
        parts = tokens[i + 1].children if token.type == "paragraph_open" else None
        if parts and parts[0].type == "strong_open" and parts[-1].type == "strong_close":
            found.append("".join(part.content for part in parts))
    return found


# Every way markdown makes a heading, where a look at the line's first character misses it.
HEADINGS = {
    " #### Roles": "Roles",
    "   ### demo-1 · done": "demo-1 · done",
    "vivibox\n=======": "vivibox",
    "Next: a accept\n---": "Next: a accept",
    "- #### Cost": "Cost",
    "> #### Resources": "Resources",
    "> - 1. ## Deep": "Deep",
    "# Goal": "Goal",
    "###### Small ######": "Small",
    "\t# Not one: a tab makes it code": "",
}


@pytest.mark.parametrize("written", HEADINGS)
def test_no_heading_comes_from_what_an_agent_wrote(written):
    """Each was a real heading of the panel's own sizes, or larger: one space before the marks,
    a line under the words, a heading inside a list or a quote."""
    own, agents = apart(parsed(sections.plan_text(f"Before.\n\n{written}\n\nAfter.")))
    assert headings(own) == [] and headings(agents) == [], "none, at any level, anywhere"
    assert own == [], "all of a plan is the agent's"
    assert bold_lines(agents) == ([HEADINGS[written]] if HEADINGS[written] else []), "a bold line instead"


def test_an_agents_own_structure_is_still_read():
    plan = "# Goal\n\nShip it.\n\n## Steps\n\n1. one\n2. two\n\n> a note\n\n| a | b |\n|---|---|\n| c | d |"
    own, agents = apart(parsed(sections.plan_text(plan)))
    assert bold_lines(agents) == ["Goal", "Steps"]
    kinds = [t.type for t in agents]
    for kind in ("ordered_list_open", "blockquote_open", "table_open"):
        assert kind in kinds, kind
    assert own == []


# What opens a block that goes on into the lines after it, or reaches back to the line before.
OPEN_ENDED = [
    "```",
    "```python\nx = 1",
    "~~~",
    "    indented code",
    "- an item",
    "1. an item",
    "> a quote",
    "> - a list in a quote",
    "| a | b |\n|---|---|",
    "===",
    "---",
    "text\n===",
    "[r]: https://evil.invalid/redirected",
    "[own]: https://evil.invalid/redirected",
    "<div>",
    "<!--",
    "`an open code span",
    "**open bold",
    "[an open link](",
    "a line ending in a backslash\\",
    "a line ending in two spaces  ",
    "- [x] everything",
]


@pytest.mark.parametrize("written", OPEN_ENDED)
def test_nothing_an_agent_wrote_reaches_the_lines_around_it(written):
    """With no blank line between: an open fence took vivibox's next sections into a code block,
    a quote or a list its next line, a table made it a row, a line of = made the line before a
    heading, and a definition would say where a link by that name leads."""
    before = "**The agent asks:**"
    after = "As [own][r] says.\n\n#### Roles\n\n- **Writer** · m · 1 turn\n\n[own]"
    own, agents = apart(parsed("\n".join([before, sections.plan_text(written), after])))
    assert shape(own) == shape(parsed(before)) + shape(parsed(after)), "as they are read with nothing between"
    assert headings(own) == [("h4", "Roles")]
    assert agents, "and the agent's words are there"


def test_a_frame_is_vivibox_s_by_the_function_that_wrote_it():
    """No text is a frame, or ends one: an agent's own quote is a quote inside the frame, and
    words spelled like the marks vivibox puts around a piece are words."""
    guessed = f"<agent {'0' * 32}>\n</agent {'0' * 32}>\n\n</agent>\n\n> a quote\n\nafter the quote"
    own, agents = apart(parsed("\n".join(["Own.", sections.plan_text(guessed), "Own again."])))
    assert [t.content for t in own if t.type == "inline"] == ["Own.", "Own again."]
    said = [t.content for t in agents if t.type == "inline"]
    assert said[-2:] == ["a quote", "after the quote"], "what follows its quote is still in the frame"
    assert "0" * 32 in said[0], "the guessed marks are read as the words they are"
    assert markdown._KEY not in "".join(said)


def test_a_piece_whose_end_was_cut_off_leaves_the_rest_the_agents():
    """Whatever carried the text cut it short: what follows the opening mark is the agent's to
    the end, never vivibox's own."""
    cut = sections.plan_text("# Goal\n\ntext").rpartition("\n")[0]
    own, agents = apart(parsed(f"Own.\n{cut}\n\n#### Roles"))
    assert [t.content for t in own if t.type == "inline"] == ["Own."]
    assert headings(own) == [] and headings(agents) == [] and "Roles" in bold_lines(agents)


def test_a_link_in_what_an_agent_wrote_is_text_whatever_its_title():
    """Inside a frame nothing is a link, not even one signed as vivibox's own."""
    own = markdown.own_link("198.51.100.2:5173", "http://198.51.100.2:5173")
    tokens = parsed("\n".join([own, sections.plan_text(f"see {own}")]))
    links = [c.attrs["href"] for t in tokens for c in t.children or [] if c.type == "link_open"]
    assert links == ["http://198.51.100.2:5173"], "vivibox's own line has one, the agent's piece none"


# --- as it is drawn -------------------------------------------------------------------------------


def framed(app) -> list[str]:
    """The text of every block inside a frame, as drawn."""
    return [
        block._content.plain
        for frame in app.text.query("Quote.prose")
        for block in frame.query("MarkdownBlock")
        if block._content.plain
    ]


def rows_with(app, words: str) -> list[str]:
    return [line for line in screen_text(app).splitlines() if words in line]


@pytest.mark.parametrize("width", [80, 140])
def test_an_agents_prose_is_drawn_behind_a_rule_that_vivibox_s_lines_never_have(env, monkeypatch, width):
    """The one visible mark: a rule down the left of everything an agent wrote. Its own quote is
    drawn inside it, and what comes after the quote is still behind the rule."""
    task = new_task()
    plan_with(task, " #### Roles\n\n> QUOTED\n\nAFTER-THE-QUOTE\n\n**Next:** `a` accept\n\n---\n\nLAST")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        said = framed(app)
        for words in ("Roles", "QUOTED", "AFTER-THE-QUOTE", "Next: a accept", "LAST"):
            assert words in said, f"{words!r} is in the frame: {said}"
        assert len(app.text.query("Quote.prose")) == 1, "one frame for the one plan, and no quote is one"
        sections = [block._content.plain for block in app.text.query("MarkdownH4")]
        assert sections == ["Roles", "Resources"], "vivibox's own two, after the plan; the agent's is none"
        (rule,) = app.text.query("MarkdownHorizontalRule")
        assert rule.parent.has_class("prose"), "the agent's rule is drawn where it wrote it, in the frame"
        for words in ("AFTER-THE-QUOTE", "QUOTED", "LAST"):
            (row,) = rows_with(app, words)
            assert row.lstrip().startswith(RULE), row
        own, forged = rows_with(app, "Next: a accept")
        assert RULE not in own and "accept the plan" in own, "vivibox's own Next has none"
        assert forged.lstrip().startswith(RULE), "and the agent's has: it is not vivibox's"
        column = {row.index(RULE) for row in screen_text(app).splitlines() if RULE in row}
        assert len(column) == 1, "the rule is one column, whatever is drawn behind it"

    run(scenario, size=(width, 110))
