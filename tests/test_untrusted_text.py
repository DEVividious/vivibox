"""Text an agent can put into what you read (a plan, a question, a review's notes, a name git or
Docker quotes) is data: shown, it opens nothing on your machine, changes no formatting around it,
hides nothing, and never passes for vivibox's own words."""

import pytest
from test_tui import new_task, run

from vivibox import markdown, panel, tui
from vivibox.probe import Listener
from vivibox.states import State

EVIL = "https://evil.invalid"
POD = "198.51.100.2"
BIDI = chr(0x202E)

# Every way markdown makes a link, each in a paragraph of its own, and what the panel shows of it.
LINKS = {
    f"[click here]({EVIL}/inline)": f"click here ({EVIL}/inline)",
    f"<{EVIL}/auto>": f"{EVIL}/auto",
    f"bare {EVIL}/bare here": f"bare {EVIL}/bare here",
    "bare www.evil.invalid/www here": "bare www.evil.invalid/www here",
    f"![a picture]({EVIL}/image.png)": f"a picture ({EVIL}/image.png)",
    f'[`code` *em* [b]x[/b]]({EVIL}/label "a title")': f"code em [b]x[/b] ({EVIL}/label)",
    f"[https://good.example/]({EVIL}/other)": f"https://good.example/ ({EVIL}/other)",
    f"[{EVIL}/same]({EVIL}/same)": f"{EVIL}/same",
    f"[![alt]({EVIL}/i.png)]({EVIL}/both)": f"alt ({EVIL}/i.png) ({EVIL}/both)",
    f"[]({EVIL}/empty)": f"{EVIL}/empty",
}
REFERENCE = f"[by reference][r] and [r] and [collapsed][]\n\n[r]: {EVIL}/ref\n[collapsed]: {EVIL}/collapsed"


def blocks(app) -> list:
    """Every piece of the panel's text as it is drawn."""
    from textual.widgets._markdown import MarkdownBlock

    return [b for b in app.text.query(MarkdownBlock) if getattr(b, "_content", None) is not None]


def drawn(app) -> list[str]:
    return [block._content.plain for block in blocks(app)]


def clicks(app) -> list[str]:
    """What a click anywhere in the panel's text would run."""
    spans = [span for block in blocks(app) for span in block._content.spans]
    metas = [getattr(span.style, "meta", None) or {} for span in spans]
    return [meta["@click"] for meta in metas if "@click" in meta]


def plan_with(task, text: str) -> None:
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text() + "\n" + text + "\n")


def opening(monkeypatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(tui.Vivibox, "open_url", lambda self, url, **how: opened.append(url))
    monkeypatch.setattr(tui, "pod_views", lambda ids: {})
    return opened


async def show(app, pilot) -> None:
    app.reload()
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


async def click_on(app, pilot, words: str) -> None:
    """A click of the mouse on the first of words, where the panel draws them."""
    block = next(b for b in blocks(app) if words in b._content.plain)
    line = block._content.plain.split("\n")[0]
    assert words in line and len(line) < block.size.width, "one row, so a column is a character"
    await pilot.click(block, offset=(line.index(words) + 1, 0))
    await pilot.pause()


# --- links ---------------------------------------------------------------------------------------


def test_a_link_an_agent_wrote_is_text_with_its_address_in_full(env, monkeypatch):
    task = new_task()
    plan_with(task, "\n\n".join(LINKS))
    opened = opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        shown = drawn(app)
        assert not clicks(app), "nothing in a plan is a link"
        for written, read in LINKS.items():
            assert read in shown, f"{written!r} is drawn as {read!r}: {shown}"
        await click_on(app, pilot, "click here")
        await click_on(app, pilot, f"{EVIL}/auto")
        await click_on(app, pilot, "a picture")
        assert opened == [], "a click on it opens nothing"

    run(scenario, size=(140, 60))


def test_a_link_by_reference_is_the_text_it_was_written_as_with_its_definition(env, monkeypatch):
    task = new_task()
    plan_with(task, REFERENCE)
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        shown = "\n".join(drawn(app))
        assert not clicks(app)
        assert "[by reference][r] and [r] and [collapsed][]" in shown
        assert f"[r]: {EVIL}/ref" in shown, "no line is taken out of what you read"
        assert f"[collapsed]: {EVIL}/collapsed" in shown

    run(scenario, size=(140, 60))


def test_the_address_of_what_the_task_runs_still_opens_on_a_click(env, monkeypatch):
    task = new_task()
    address = f"http://{POD}:5173"
    plan_with(task, f'[{POD}:5173]({address})\n\n[the same address, in a plan]({address} "title")')
    opened = opening(monkeypatch)
    up = panel.PodView(POD, [Listener(5173, True)], demo=True)
    monkeypatch.setattr(tui, "pod_views", lambda ids: {i: up for i in ids})

    async def scenario(app, pilot):
        await show(app, pilot)
        assert clicks(app) == [f"link({address!r})"], "one link: the one vivibox wrote"
        await click_on(app, pilot, "the same address, in a plan")
        assert opened == [], "the plan's link to the same address is text"
        await click_on(app, pilot, f"open at {POD}:5173")  # a click on "open": not the link yet
        assert opened == []
        pod_line = next(b for b in blocks(app) if "demo running" in b._content.plain)
        column = pod_line._content.plain.index(f"{POD}:5173", pod_line._content.plain.index("open at"))
        await pilot.click(pod_line, offset=(column + 1, 0))
        await pilot.pause()
        assert opened == [address], "opened once"

    run(scenario, size=(140, 60))


def test_a_link_is_vivibox_s_by_the_function_that_wrote_it_never_by_its_address():
    """The same label and address, written by hand, with a title or none, a key guessed or the
    title of another: text. Only what own_link returns is a link."""
    address = f"http://{POD}:5173"

    def links(text: str) -> list[str]:
        children = [c for t in markdown.parser().parse(text) for c in (t.children or [])]
        return [c.attrs["href"] for c in children if c.type in ("link_open", "image")]

    own = markdown.own_link(f"{POD}:5173", address)
    assert links(own) == [address]
    for forged in (
        f"[{POD}:5173]({address})",
        f'[{POD}:5173]({address} "")',
        f'[{POD}:5173]({address} "{"0" * 32}")',
        f"<{address}>",
        address,
        f"![{POD}:5173]({address})",
        f"# [{POD}:5173]({address})",
        f"- [{POD}:5173]({address})",
        f"> [{POD}:5173]({address})",
        f"| a |\n|---|\n| [{POD}:5173]({address}) |",
        f"[{POD}:5173][pod]\n\n[pod]: {address}",
    ):
        assert links(forged) == [], forged
        assert links(f"{forged}\n\n{own}\n\n{forged}") == [address], "beside vivibox's own, still text"


def test_what_an_entity_spells_reaches_the_screen_escaped(env, monkeypatch):
    """The panel's source is escaped before it is parsed, and the parser makes a character of
    &#x202e; after that: in text and in a link's label. In an address it is percent-encoded."""
    task = new_task()
    plan_with(task, f"bidi&#x202e;x zero&#x200b;width\n\n[label&#8238;]({EVIL}/a&#x202e;b)\n\n`code&#x202e;`")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        shown = "\n".join(drawn(app))
        assert BIDI not in shown and chr(0x200B) not in shown, repr(shown)
        assert "bidi‹U+202E›x zero‹U+200B›width" in shown
        assert f"label‹U+202E› ({EVIL}/a%E2%80%AEb)" in shown
        assert "code&#x202e;" in shown, "in code an entity is the characters it is written with"

    run(scenario, size=(140, 60))


def test_html_an_agent_wrote_is_shown_as_the_text_it_is(env, monkeypatch):
    """The widget draws no tag: one in the text was read and not shown, a block of them left out
    whole."""
    task = new_task()
    plan_with(task, "before <b>bold</b> after\n\n<div>\na block\n</div>\n\n<script>x()</script>")
    opening(monkeypatch)

    async def scenario(app, pilot):
        await show(app, pilot)
        shown = "\n".join(drawn(app))
        assert "before <b>bold</b> after" in shown
        assert "<div> a block </div>" in shown and "<script>x()</script>" in shown

    run(scenario, size=(140, 60))


@pytest.mark.parametrize(
    ("label", "target", "shown"),
    [
        ("docs", f"{EVIL}/x", f"docs ({EVIL}/x)"),
        (f"{EVIL}/x", f"{EVIL}/x", f"{EVIL}/x"),
        ("", f"{EVIL}/x", f"{EVIL}/x"),
    ],
)
def test_the_form_a_link_is_shown_in_is_decided_in_one_place(label, target, shown):
    assert markdown.link_text(label, target) == shown
