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
