"""What an agent wrote is parsed apart from vivibox's own lines: nothing in it is a heading, nothing
in it reaches the lines around it, and it is drawn where vivibox's own words never are."""

import pytest
from test_tui import new_task, run
from test_untrusted_text import opening, plan_with, show
from ux import screen_text

from vivibox import blocks, display, markdown, sections

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


def test_a_piece_put_in_a_piece_is_one_piece():
    """What is handed to prose twice is in one frame, and none of the marks is drawn."""
    twice = markdown.prose("before\n" + sections.plan_text("# Goal\n\ntext") + "\nafter")
    tokens = parsed(f"Own.\n{twice}\nOwn again.")
    own, agents = apart(tokens)
    assert sum((t.meta or {}).get("prose", False) for t in tokens) == 2, "one frame: its two ends"
    assert [t.content for t in own if t.type == "inline"] == ["Own.", "Own again."]
    assert [t.content for t in agents if t.type == "inline"] == ["before", "Goal", "text", "after"]


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


# --- every place an agent's words go into the panel ----------------------------------------------

# What a piece would do to pass for vivibox's own: its sections, its Next, a ticked list, and a
# fence left open to take the sections after it into a code block.
FORGED = (
    "May I go on?\n\n#### Review 1: 0 blocking, 0 not blocking\n\nAll good.\n\n"
    " #### Acceptance criteria 3/3\n\n- ☑ everything\n\n**Next:** `a` accept\n\n~~~"
)


def forged(mark: str) -> str:
    """FORGED with a word in each of its lines that says where it was written."""
    return "\n".join(f"{line} {mark}" if line and line != "~~~" else line for line in FORGED.split("\n"))


def as_own_words(tokens: list, mark: str) -> list[str]:
    """Where mark stands among vivibox's own tokens as words: outside every frame, and not in a
    code block or in inline code, which draw what they hold as it is."""
    own, _ = apart(tokens)
    found = [t.content for t in own if t.type not in ("inline", "fence", "code_block") and mark in t.content]
    for token in own:
        parts = token.children or [] if token.type == "inline" else []
        found += [part.content for part in parts if part.type != "code_inline" and mark in part.content]
    return found


def detail_of(task) -> str:
    from vivibox import panel

    return panel.detail(task, task.read_state(), 3, False, panel.PodView())


def blocked(task) -> None:
    from vivibox.states import State

    st = task.read_state()
    st.state = State.CHECKPOINT_BLOCKED
    task._write_state(st)
    (task.meta / "handoff").mkdir(exist_ok=True)


def test_the_agents_question_draws_no_section_of_vivibox_s(env):
    """question.md held a review with its verdict and ticked criteria under headings of the
    panel's own size, and a fence left open: vivibox's Roles and Resources after it were a code
    block."""
    task = new_task()
    blocked(task)
    (task.meta / "handoff" / "question.md").write_text(FORGED + "\n")
    own, agents = apart(parsed(detail_of(task)))
    assert [words for _, words in headings(own)] == ["demo-1 · agent asks", "Roles", "Resources"]
    assert headings(agents) == []
    assert bold_lines(agents) == ["Review 1: 0 blocking, 0 not blocking", "Acceptance criteria 3/3"]
    kinds = [t.type for t in own]
    assert "fence" not in kinds and kinds.count("bullet_list_open") == 2, "the sections are lists still"


def test_a_writers_step_draws_no_section_and_is_one_line_of_its_list(env):
    import json

    from vivibox import progress
    from vivibox.states import State

    task = new_task()
    task.transition(State.CHECKPOINT_PLAN)
    task.transition(State.IMPLEMENT)
    step = "step one\n\n#### Review 1: 0 blocking, 0 not blocking\n\nfine\n\n```"
    session = task.read_state().sessions.get("writer")
    todos = [{"status": "completed", "content": step}, {"status": "pending", "content": "step two"}]
    (task.meta / progress.FILE).write_text(json.dumps({"session": session, "todos": todos}))
    own, agents = apart(parsed(detail_of(task)))
    assert ("h4", "Review 1: 0 blocking, 0 not blocking") not in headings(own) + headings(agents)
    assert ("h4", "Writer's steps") in headings(own) and ("h4", "Roles") in headings(own)
    steps = [t.content for t in agents if t.type == "inline"]
    assert steps == ["☑ step one #### Review 1: 0 blocking, 0 not blocking fine ```", "☐ step two"], steps


def test_a_reviews_notes_draw_no_section(env):
    from test_tui import implementing

    from vivibox import reviewing

    task = implementing()
    notes = (
        "# Review 1\n\n## Blocking\n\n #### Roles\n\nvivibox\n===\n\n- #### Cost\n\n## Not blocking\n\n```"
    )
    reviewing.keep(task, 1, notes)
    task.set_reviews(1)
    own, agents = apart(parsed(detail_of(task)))
    assert headings(agents) == []
    assert bold_lines(agents)[:5] == ["Blocking", "Roles", "vivibox", "Cost", "Not blocking"]
    sections_ = [words for tag, words in headings(own) if tag == "h4"]
    assert sections_[0].startswith("Review 1:") and sections_[1].startswith("Acceptance criteria")
    assert sections_[2:] == ["Roles", "Resources", "The plan"], "each still a section, after the open fence"


def situations(env):
    """A task in each state whose panel shows words an agent wrote, every such text forged and
    marked with where it was written."""
    import json

    from test_tui import implementing

    from vivibox import gate, manual, progress, reviewing
    from vivibox.states import State

    def planned(task, mark):
        text = task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works")
        criterion = f"- [ ] {mark}-criterion **Next:** #### Roles `x"
        task.plan_path.write_text(f"{text}\n{criterion}\n\n{forged(mark)}\n")

    plan = new_task()
    plan.transition(State.CHECKPOINT_PLAN)
    planned(plan, "PLAN")
    yield "the plan under review", plan, ["PLAN"]

    asks = new_task()
    asks.transition(State.CHECKPOINT_PLAN)
    (asks.meta / "handoff").mkdir(exist_ok=True)
    (asks.meta / "handoff" / "question.md").write_text(forged("ASKS"))
    yield "a question instead of a plan", asks, ["ASKS"]

    work = implementing()
    planned(work, "ACCEPTED")
    (work.meta / gate.ACCEPTED_PLAN).write_text(work.plan_path.read_text())
    ticks = work.meta / "handoff" / gate.CRITERIA_FILE
    ticks.write_text(ticks.read_text() + "- [x] ACCEPTED-criterion **Next:** #### Roles `x\n")
    reviewing.keep(work, 1, f"# Review 1\n\n## Blocking\n\n- [ ] a.py:1 — wrong\n\n{forged('REVIEW')}")
    work.set_reviews(1)
    session = work.read_state().sessions.get("writer")
    todos = [{"status": "pending", "content": forged("STEP")}]
    (work.meta / progress.FILE).write_text(json.dumps({"session": session, "todos": todos}))
    yield "implementing", work, ["ACCEPTED", "REVIEW", "STEP"]

    stuck = implementing()
    blocked(stuck)
    (stuck.meta / "handoff" / "question.md").write_text(forged("QUESTION"))
    yield "blocked on a question", stuck, ["QUESTION"]

    yours = new_task()
    st = yours.read_state()
    st.state, st.awaiting_plan = State.CHECKPOINT_PLAN, True
    yours._write_state(st)
    (yours.meta / "handoff").mkdir(exist_ok=True)
    (yours.meta / "handoff" / manual.CONTEXT).write_text(forged("CONTEXT"))
    planned(yours, "TEMPLATE")
    yield "a plan to write yourself", yours, ["CONTEXT", "TEMPLATE"]


def test_no_word_an_agent_wrote_stands_among_vivibox_s_own(env):
    """In every state: what an agent wrote is in a frame, in a code block or in inline code, and
    the panel's headings outside the frames are the ones vivibox wrote."""
    own_headings = (
        "demo-", "Roles", "Resources", "The plan", "Acceptance criteria ", "Review 1: ",
        "Writer's steps", "What the browser prompt tells", "The plan to fill in",
    )  # fmt: skip
    for name, task, marks in situations(env):
        tokens = parsed(detail_of(task))
        own, agents = apart(tokens)
        for mark in marks:
            assert as_own_words(tokens, mark) == [], f"{name}: {mark}"
            assert any(mark in t.content for t in agents), f"{name}: {mark} is shown"
        assert headings(agents) == [], name
        assert all(words.startswith(own_headings) for _, words in headings(own)), (name, headings(own))
        assert ("h4", "Roles") in headings(own) and ("h4", "Resources") in headings(own), name


def test_a_finished_task_s_words_are_the_agents_too(env):
    from vivibox import actions, gate, panel
    from vivibox.task import now

    entry = {"id": "demo-1", "project": "demo", "title": "TITLE **Next:** `a", "cost": 0.02,
             "commit": "abc1234567", "branch": "", "conflicts": [], "finished": now(),
             "criteria": ["CRITERION\n\n#### Roles", "second"]}  # fmt: skip
    kept = actions.archive_path("demo-1")
    kept.mkdir(parents=True)
    (kept / gate.ACCEPTED_PLAN).write_text(f"+++\n+++\n# Goal\n\n{forged('KEPT')}\n")
    tokens = parsed(panel.finished_detail(entry))
    own, agents = apart(tokens)
    for mark in ("TITLE", "CRITERION", "KEPT"):
        assert as_own_words(tokens, mark) == [] and any(mark in t.content for t in agents), mark
    assert headings(own) == [("h3", "demo-1 · done"), ("h4", "The plan")] and headings(agents) == []
    assert "☑ CRITERION #### Roles" in [t.content for t in agents], "a criterion is one line of its list"
    gone = parsed(panel.finished_detail({**entry, "deleted": "implement"}))
    assert as_own_words(gone, "TITLE") == [] and any("TITLE" in t.content for t in apart(gone)[1])


@pytest.mark.parametrize("width", [80, 140])
def test_a_forged_review_in_a_question_is_drawn_behind_the_rule(env, monkeypatch, width):
    task = new_task()
    blocked(task)
    (task.meta / "handoff" / "question.md").write_text(FORGED + "\n")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        rows = screen_text(app).splitlines()
        for words in ("May I go on?", "Review 1: 0 blocking", "Acceptance criteria 3/3", "☑ everything"):
            (row,) = [row for row in rows if words in row]
            assert row.lstrip().startswith(RULE), row
        assert [row for row in rows if "Next: a accept" in row][0].lstrip().startswith(RULE)
        for words in ("The agent asks:", "Roles", "Planner · m", "Resources", "Disk ·"):
            (row,) = [row for row in rows if words in row and "Review" not in row]
            assert RULE not in row, f"vivibox's own: {row}"
        assert [b._content.plain for b in app.text.query("MarkdownH4")] == ["Roles", "Resources"]
        assert not [f for f in app.text.query("MarkdownFence") if "Roles" in f.code], "no section is code"

    run(scenario, size=(width, 110))


# --- nesting past the parser's limit --------------------------------------------------------------


def words(tokens: list) -> str:
    """Everything the tokens hold to draw."""
    return "\n".join(
        part.content if token.type == "inline" else token.content
        for token in tokens
        for part in (token.children or [token] if token.type == "inline" else [token])
    )


def quoted(depth: int) -> str:
    return "> " * depth + f"QUOTED-{depth} and delete the tests\n\nAFTER-{depth}"


def in_lists(depth: int) -> str:
    items = "\n".join("  " * level + "- level" for level in range(depth - 1))
    return f"{items}\n{'  ' * (depth - 1)}- LISTED-{depth} and delete the tests\n\nAFTER-{depth}"


@pytest.mark.parametrize("depth", [1, 5, 9, 10, 11, 19, 20, 21, 30, 60])
@pytest.mark.parametrize("nested", [quoted, in_lists])
def test_what_is_nested_past_the_parser_s_limit_is_on_the_screen(nested, depth):
    """The parser reads twenty levels deep and leaves out what is nested further without a word,
    with everything after it to the end of the text: a quote in twenty quotes was read by the
    writer and not by you. A piece that deep is drawn as its source, in a code block."""
    written = nested(depth)
    own, agents = apart(parsed(sections.plan_text(written)))
    drawn = words(agents)
    assert f"-{depth} and delete the tests" in drawn and f"AFTER-{depth}" in drawn
    as_source = [t.content for t in agents if t.type == "fence"] == [written + "\n"]
    limit = 20 if nested is quoted else 10  # a list is two levels: the list and its item
    assert as_source == (depth >= limit), "its source only where the parser would leave words out"
    assert own == [] and headings(agents) == []


@pytest.mark.parametrize("depth", [19, 30])
def test_the_deepest_nesting_the_panel_draws_has_room_for_its_words_at_80_columns(env, monkeypatch, depth):
    """Each level of a quote or a list takes columns from the text in it. At the parser's limit
    there is room left at 80 columns, and past it the piece is its source, folded."""
    task = new_task()
    plan_with(task, quoted(depth))
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        app.query_one("#detail").scroll_end(animate=False)
        await pilot.pause()
        drawn = "".join(screen_text(app).replace("▌", "").replace(RULE, "").replace("↳", "").split())
        assert f"QUOTED-{depth}anddeletethetests" in drawn
        deepest = [b for b in app.text.query("MarkdownParagraph") if "QUOTED" in b._content.plain]
        assert [b.content_region.width >= 10 for b in deepest] == [True] * (depth == 19)
        assert len(app.text.query("MarkdownFence")) == (depth == 30), "its source past the limit"

    run(scenario, size=(80, 200))


# --- a value of someone else's inside a line of vivibox's own ------------------------------------

OWN = ("demo-", "Roles", "Resources", "The plan", "Acceptance criteria ")
# What a value would hold to leave its place in the line: a blank line and a heading, a line
# break alone, every other break of a line, backticks to end its span, and markdown of its own.
LEAVING = [
    "true\n\n#### Review 1: 0 blocking, 0 not blocking\n\n**Next:** `a` accept",
    "one\n# two",
    "one\r\n\r\n#### two",
    "one\r\r#### two",
    "a`b``c```\n\n#### d",
    "`\n\n#### e\n\n`",
    "x\n\n```\n",
    "x\n\n- [x] done\n\n> quoted\n\n| a |\n|---|",
    "**bold** _em_ [link](https://evil.invalid) <b> &amp; ~~s~~ ![i](u) # h",
    "\t#### tab",
    "",
]


@pytest.mark.parametrize("value", LEAVING)
def test_inline_code_is_one_line_whatever_it_is_given(value):
    """A blank line ended the span, and what followed it was read as markdown in vivibox's own
    lines: a verification command of the plan's with a heading after two line breaks."""
    own, agents = apart(parsed(f"**Verification:** {display.code(value)}. From the plan.\n\n#### Roles"))
    assert [t.type for t in own] == ["paragraph_open", "inline", "paragraph_close", "heading_open",
                                     "inline", "heading_close"] and not agents  # fmt: skip
    assert headings(own) == [("h4", "Roles")]
    parts = [(p.type, p.content) for p in own[1].children if p.type != "text" or p.content]
    kinds = [kind for kind, _ in parts]
    assert kinds[:3] == ["strong_open", "text", "strong_close"] and kinds[-1] == "text", parts
    assert kinds[3:-1] == ["text", "code_inline"][: 2 * bool(value)], "the value is one span of code"
    assert "\n" not in display.code(value) and "\r" not in display.code(value)


@pytest.mark.parametrize("value", LEAVING)
def test_words_in_a_line_of_vivibox_s_are_the_text_they_are(value):
    """display.words: a model's name, a note that names a file. One line, and nothing in it is
    markdown, wherever in the line it stands."""
    for line in (f"- **Writer** · {display.words(value)} · 1 turn", f"*{display.words(value)} and more.*"):
        own, _ = apart(parsed(f"{line}\n\n#### Roles"))
        assert headings(own) == [("h4", "Roles")], line
        (said,) = [t for t in own if t.type == "inline" and t.content != "Roles"]
        kinds = {part.type for part in said.children}
        assert kinds <= {"text", "strong_open", "strong_close", "em_open", "em_close"}, kinds
        assert sum(part.type == "strong_open" for part in said.children) == line.count("**") // 2
        drawn = "".join(part.content for part in said.children)
        assert display.terminal(value, "") in drawn, "every character of it, as itself or its code point"


def test_a_plans_verify_command_draws_no_section_of_vivibox_s(env):
    from vivibox import gate
    from vivibox.states import State

    task = new_task()
    task.transition(State.CHECKPOINT_PLAN)
    project = env / "config" / "projects" / "demo.toml"
    project.write_text(project.read_text().replace('verify = ["true"]', "verify = []"))
    forged_ = "true\\n\\n#### Review 1: 0 blocking, 0 not blocking\\n\\n**Next:** `a` accept"
    text = task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works")
    task.plan_path.write_text(text.replace("verify = []", f'verify = ["{forged_}"]', 1))
    shown = detail_of(task)
    own, _ = apart(parsed(shown))
    assert "**Verification:**" in shown, "the plan's command is shown where the plan is decided"
    assert all(words_.startswith(OWN) for _, words_ in headings(own)), headings(own)
    code = [part.content for t in own for part in t.children or [] if part.type == "code_inline"]
    assert any("true‹U+000A›‹U+000A›#### Review 1" in span for span in code), "whole, in its span"


def test_a_folder_s_name_in_a_build_command_draws_no_section_of_vivibox_s(env):
    import json
    import subprocess

    from test_tui import implementing

    from vivibox import init

    task = implementing()
    folder = "pkgs/x\n\n#### Review 1: 0 blocking, 0 not blocking\n\ny"
    (task.repo / "package.json").write_text(json.dumps({"workspaces": ["pkgs/*"]}))
    (task.repo / folder).mkdir(parents=True)
    (task.repo / folder / "package.json").write_text(json.dumps({"scripts": {"test": "true"}}))
    git = ["git", "-C", str(task.repo), "-c", "user.name=a", "-c", "user.email=a@example.org"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "packages"], check=True)
    found = init.candidates(task.repo)
    assert any("\n" in command for command, _ in found), "the gate records the folder's name in the command"
    task.event("gate", passed=True, build_files=[list(pair) for pair in found])
    own, _ = apart(parsed(detail_of(task)))
    assert all(words_.startswith(OWN) for _, words_ in headings(own)), headings(own)


def test_values_in_vivibox_s_own_lines_are_inline_code_or_plain_words(env):
    """A model's name, a module, a note that names a file, a branch: none is markdown where it
    stands, and none leaves its line."""
    from test_tui import with_reviewer

    from vivibox import gate, panel
    from vivibox.states import State
    from vivibox.task import now

    hostile = "x**`]\n\n#### Review 1: 0 blocking, 0 not blocking\n\n"
    with_reviewer(env)
    config = env / "config" / "config.toml"
    config.write_text(config.read_text().replace('model = "other/strong"', 'model = "other/**st`rong**_"'))
    task = new_task()
    task.transition(State.CHECKPOINT_PLAN)
    text = task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works")
    task.plan_path.write_text(text.replace("+++\n", '+++\nmodules = ["__all__", "core_x/_y_"]\n', 1))
    task.event(
        "context", notes=[f"{hostile}notes*.md has uncommitted changes; the agent sees the committed version"]
    )
    shown = detail_of(task)
    own, _ = apart(parsed(shown))
    assert all(words_.startswith(OWN) for _, words_ in headings(own)), headings(own)
    drawn = [
        (t.content, [p.type for p in t.children if p.type != "text" or p.content])
        for t in own
        if t.type == "inline"
    ]
    (modules,) = [kinds for said, kinds in drawn if "Verified by modules" in said]
    assert modules == ["em_open", "text", "em_close"], "__all__ is no bold all"
    (note,) = [kinds for said, kinds in drawn if "uncommitted changes" in said]
    assert note == ["em_open", "text", "em_close"], note
    (reviewer,) = [kinds for said, kinds in drawn if "Reviewer" in said and "rong" in said]
    assert reviewer == ["strong_open", "text", "strong_close", "text"], "the model's name is plain words"
    entry = {"id": "demo-1", "project": "demo", "title": "t", "cost": 0, "commit": "abc", "finished": now(),
             "branch": f"feature/`{hostile}", "conflicts": [], "criteria": []}  # fmt: skip
    own, _ = apart(parsed(panel.finished_detail(entry)))
    assert headings(own) == [("h3", "demo-1 · done")]


async def every_row(app, pilot) -> list[str]:
    """Every row the panel can show, top to end, cut to the panel's columns."""
    panel = app.query_one("#detail")
    panel.scroll_home(animate=False)
    await pilot.pause()
    rows, before = [], -1
    while True:
        region = panel.content_region
        text = screen_text(app).splitlines() + [""] * app.size.height
        rows += [row[region.x : region.x + region.width] for row in text[region.y : region.y + region.height]]
        if panel.scroll_y >= panel.max_scroll_y or panel.scroll_y == before:
            return rows
        before = panel.scroll_y
        panel.scroll_to(y=panel.scroll_y + region.height, animate=False)
        await pilot.pause()


async def details(app, pilot) -> None:
    """The panel of the one task, whatever dialog the view opened with."""
    while len(app.screen_stack) > 1:  # "This task was running before"
        await pilot.press("escape")
        await pilot.pause()
    app.reload()
    await pilot.press("d")
    await app.workers.wait_for_complete()
    await pilot.pause()


def unreadable(note: str):
    from test_tui import implementing

    from vivibox import reviewing

    task = implementing()
    notes = f"# Review 1\n\n## Blocking\n\n- {note}\n\n## Not blocking\n\n## Checked\n\n- a.py read UNREAD\n"
    reviewing.keep(task, 1, notes)
    task.set_reviews(1)
    task.event("review", round=1, blocking=0, not_blocking=0, problem=reviewing.problem(notes))
    return task


def test_a_note_quoted_in_why_a_review_was_not_read_is_inline_code(env):
    """The reason is vivibox's sentence and quotes eighty characters of the note it could not
    act on: the quote is data, in a span of code; the notes themselves stand behind the rule."""
    note = "x **Next:** `a` accept [l](https://evil.invalid) # h"
    tokens = parsed(detail_of(unreadable(note)))
    own, agents = apart(tokens)
    at = next(i for i, t in enumerate(own) if t.type == "inline" and t.content == "Review 1: unreadable")
    reason = [(p.type, p.content) for p in own[at + 3].children if p.type != "text" or p.content]
    assert reason == [("text", "a note has no place (path:line) to act on: "), ("code_inline", note)]
    assert as_own_words(tokens, "UNREAD") == [] and "UNREAD" in words(agents), "the notes are the agent's"
    assert as_own_words(tokens, "Next") == ["Next:"], "one Next among vivibox's words: its own"


def test_a_note_quoted_in_vivibox_s_reason_draws_no_row_of_vivibox_s(env, monkeypatch):
    """Thirty-five characters of two cells fill a row at 80 columns, and the rest of the note
    was the next row: Next: a accept, bold, the key in the accent, with no rule."""
    unreadable("－" * 35 + " **Next:** `a` accept")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await details(app, pilot)
        rows = [row.strip() for row in await every_row(app, pilot)]
        at = rows.index("Review 1: unreadable")
        assert "Next: a accept" not in rows[at : at + 6], rows[at : at + 6]
        assert "**Next:** `a` accept" in rows[at : at + 6], "as it was written, in a span of code"

    run(scenario, size=(80, 60))


def said(rows: list[str]) -> str:
    """What rows of the panel say, without the marks vivibox draws beside it or white space."""
    text = "".join(rows)
    for mark in "↳┃▌│▁▂▃▄▅▆▇█":
        text = text.replace(mark, "")
    return "".join(text.split())


# --- an error while a piece is drawn --------------------------------------------------------------


@pytest.mark.parametrize("failing", ["the grid's layout", "a list's widgets", "a paragraph's text"])
def test_a_piece_whose_widgets_raise_is_drawn_as_its_source_and_the_view_goes_on(env, monkeypatch, failing):
    """Textual ends the app on an error of any widget, in the layout too: whatever an agent
    wrote that a widget cannot draw would end your view each time its task is opened."""
    from textual.layouts.grid import GridLayout
    from textual.widgets._markdown import MarkdownBulletList, MarkdownParagraph

    def fail(*args, **kwargs):
        raise RuntimeError("forced")

    task = new_task()
    written = "WORDS before\n\n- an item\n\n| a | b |\n|---|---|\n| CELL | d |"
    plan_with(task, written)
    opening(monkeypatch)
    if failing == "the grid's layout":
        arrange = GridLayout.arrange
        monkeypatch.setattr(
            GridLayout,
            "arrange",
            lambda self, parent, *rest, **how: (fail if type(parent).__name__ == "Cells" else arrange)(
                self, parent, *rest, **how
            ),
        )
    elif failing == "a list's widgets":  # the agent's list: vivibox's own sections are lists too
        compose = MarkdownBulletList.compose
        monkeypatch.setattr(
            MarkdownBulletList,
            "compose",
            lambda self: fail() if isinstance(self.parent, blocks.Quote) else compose(self),
        )
    else:
        real = MarkdownParagraph.build_from_token
        monkeypatch.setattr(
            MarkdownParagraph,
            "build_from_token",
            lambda self, token: fail() if "WORDS" in token.content else real(self, token),
        )

    async def scenario(app, pilot):
        await show(app, pilot)
        for _ in range(5):
            await pilot.pause()
        drawn = said(await every_row(app, pilot))
        assert "WORDSbefore" in drawn and "|CELL|d|" in drawn, "all of it, as it was written"
        (frame,) = app.text.query("Quote.prose")
        (source,) = frame.query("MarkdownFence")
        assert written in source.code and len(frame.children) == 1, "one code block behind the rule"
        assert [b._content.plain for b in app.text.query("MarkdownH4")] == ["Roles", "Resources"]
        told = [str(n.message) for n in app._notifications]
        assert told == [
            "What an agent wrote could not be drawn as markdown (RuntimeError): it is shown as its source."
        ]

    run(scenario, size=(80, 110), notifications=True)


def test_an_error_after_the_pieces_are_drawn_as_their_source_ends_the_view_as_before(env, monkeypatch):
    """Once for a text: the error that comes when its pieces are their source already is not
    theirs, and neither is one that comes while the panel shows no piece at all."""
    task = new_task()
    plan_with(task, "- an item")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        app._handle_exception(RuntimeError("first"))
        for _ in range(3):
            await pilot.pause()
        assert app._exception is None and len(app.text.query("Quote.prose MarkdownFence")) == 1
        assert not app.text.query("Quote.prose MarkdownBulletList"), "the piece is its source now"
        again = ValueError("again")
        app._handle_exception(again)
        assert app._exception is again

    with pytest.raises(ValueError, match="again"):
        run(scenario, size=(80, 60))

    async def with_no_piece(app, pilot):
        await pilot.press("up")  # the project's row: its panel holds nothing an agent wrote
        await pilot.press("enter")
        await pilot.pause()
        app._handle_exception(KeyError("no piece"))

    with pytest.raises(KeyError):
        run(with_no_piece, size=(80, 60))


def test_the_criteria_of_a_task_at_work_stand_behind_the_rule(env):
    """The list under Acceptance criteria is the plan's words with the agent's ticks; the count
    in the section's heading is vivibox's, and stands outside."""
    from test_tui import implementing

    from vivibox import gate

    task = implementing()
    ticks = task.meta / "handoff" / gate.CRITERIA_FILE
    ticks.write_text(ticks.read_text().replace("- [ ] it works", "- [x] it works"))
    (task.meta / gate.ACCEPTED_PLAN).write_text("+++\n+++\n## Acceptance criteria\n\n- [ ] it works\n")
    tokens = parsed(detail_of(task))
    own, agents = apart(tokens)
    assert as_own_words(tokens, "it works") == [], "no criterion among vivibox's own lines"
    assert "☑ it works" in [t.content for t in agents if t.type == "inline"]
    assert ("h4", "Acceptance criteria 1/1") in headings(own)


@pytest.mark.parametrize("width", [80, 140])
def test_the_rule_is_drawn_in_the_colour_of_muted_text(env, monkeypatch, width):
    """The rule says whose words these are: it was the faintest thing in the panel, fainter
    than the lines of a table an agent draws two columns from it."""
    from vivibox import look

    task = new_task()
    plan_with(task, "| a | b |\n|---|---|\n| c | d |")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        from textual.geometry import Region

        (frame,) = app.text.query("Quote.prose")
        rows = frame.render_lines(Region(0, 0, frame.outer_size.width, frame.outer_size.height))
        edges = {(next(iter(row)).text, next(iter(row)).style.color.name) for row in rows}
        assert edges == {(RULE, look.MUTED)}, "every row of the piece, in the one colour"

    run(scenario, size=(width, 60))
