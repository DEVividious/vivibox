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


# --- what git and Docker say of a name ------------------------------------------------------------

# A name an agent chose, as a tool quotes it on stderr: control characters, an escape sequence,
# markup's brackets, vivibox's own notation, and a line that reads as one of vivibox's.
NAME = f"x\x1b[2K\r{BIDI}[/b][black on black]‹U+0041›\nvivibox: demo-1 is done"
NAME_SHOWN = "x‹U+001B›[2K‹U+000D›‹U+202E›[/b][black on black]‹U+2039›U+0041‹U+203A›"


def hidden(text: str) -> list[str]:
    import unicodedata

    kinds = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
    return [c for c in text if c not in "\n\t" and unicodedata.category(c) in kinds]


def own_lines(text: str) -> list[str]:
    """The lines that start where vivibox's own do: at the left edge."""
    return [line for line in text.splitlines() if line and not line.startswith(" ")]


def gits_error(env):
    from vivibox import repo

    with pytest.raises(repo.RepoError) as e:
        repo.git("checkout", NAME, cwd=env / "repo")
    return e.value


def dockers_error():
    from pathlib import Path
    from subprocess import CompletedProcess

    from vivibox.pod import Pod, PodError

    said = f"Error response from daemon: no such container: {NAME}\nSee 'docker rm --help'.\n"
    pod = Pod("demo-1", Path("."), "", runner=lambda cmd, **how: CompletedProcess(cmd, 1, "", said))
    with pytest.raises(PodError) as e:
        pod._run("docker", "rm", "-f", "x")
    return e.value


@pytest.mark.parametrize("made", [gits_error, dockers_error])
def test_what_a_tool_said_of_a_name_is_escaped_where_the_error_is_made(env, made):
    error = made(env) if made is gits_error else made()
    said = str(error)
    assert not hidden(said), repr(said)
    assert NAME_SHOWN in said, said
    assert len(own_lines(said)) == 1, f"no line of it reads as one of vivibox's own: {said!r}"
    words = said.partition("…: ")[2]  # after the command, where a name is on one line
    assert "\n    vivibox: demo-1 is done" in words, "a further line is indented under the first"
    assert "‹U+000A›" not in words, "and still a line"


def test_a_tools_words_keep_their_lines_under_the_first():
    from vivibox import display

    said = "error: pathspec 'x' did not match\r\nhint: try this\n\n\nfatal: stopped\n"
    assert display.said(said) == "error: pathspec 'x' did not match\n    hint: try this\n    fatal: stopped"
    assert display.said("one line\n") == "one line" and display.said("") == ""
    assert display.said("a\rb") == "a‹U+000D›b", "a carriage return within a line is shown"


def test_an_error_quoting_a_name_is_text_on_the_command_line(env, capsys, monkeypatch):
    from vivibox import actions
    from vivibox.cli import main

    task = new_task()
    for error in (gits_error(env), dockers_error()):

        def fail(*args, error=error, **how):
            raise error

        monkeypatch.setattr(actions, "risky_diffs", fail)
        capsys.readouterr()
        assert main(["risky", task.id]) == 1
        said = capsys.readouterr().err
        assert not hidden(said), repr(said)
        assert NAME_SHOWN in said and said.startswith("vivibox: ")
        assert len(own_lines(said)) == 1, said


def test_an_error_quoting_a_name_is_text_in_the_view(env, monkeypatch):
    task = new_task()
    error = gits_error(env)
    opening(monkeypatch)

    async def scenario(app, pilot):
        app.fail(error)
        await pilot.pause()
        last = list(app._notifications)[-1]
        assert last.markup is False and not hidden(last.message), repr(last.message)
        assert NAME_SHOWN in last.message and len(own_lines(last.message)) == 1
        task.set_paused(True, problem=f"could not start: {error}")
        await show(app, pilot)
        block = next(text for text in drawn(app) if "pathspec" in text)
        assert NAME_SHOWN in block and not hidden(block), repr(block)
        assert len(own_lines(block)) == 1, block

    run(scenario, size=(140, 60), notifications=True)


def test_status_shows_a_problem_of_several_lines_under_why(env, capsys):
    from vivibox.cli import main

    task = new_task()
    task.set_paused(True, problem=f"could not start: {gits_error(env)}")
    capsys.readouterr()
    assert main(["status", task.id]) == 0
    said = capsys.readouterr().out
    why = said[said.index("Why") :].split("\nNext")[0]
    assert not hidden(said) and "‹U+000A›" not in why.partition("…: ")[2], repr(said)
    assert NAME_SHOWN in why and len(own_lines(why)) == 1, why
    assert "\n          vivibox: demo-1 is done" in why, "git's second line, under its first"


def test_the_dockers_log_of_a_pod_that_did_not_start_draws_no_line_of_vivibox_s(monkeypatch):
    from pathlib import Path
    from subprocess import CompletedProcess

    from vivibox import pod as pod_module

    log = f"failed to start daemon: {NAME}\nmore\n"

    def runner(cmd, **how):
        return CompletedProcess(cmd, 0 if "logs" in cmd else 1, "", log if "logs" in cmd else "")

    pod = pod_module.Pod("demo-1", Path("."), "", runner=runner)
    with pytest.raises(pod_module.PodError) as e:
        pod._wait_for_daemon(0)
    said = str(e.value)
    assert not hidden(said) and NAME_SHOWN in said and len(own_lines(said)) == 1, repr(said)


def test_what_the_root_helper_refused_is_said_escaped_by_the_one_printer_of_errors(env, capsys, monkeypatch):
    from functools import partial
    from subprocess import CompletedProcess

    from vivibox import actions
    from vivibox.cli import main
    from vivibox.pod import Pod

    def refusing(cmd, timeout=None):
        grows = list(cmd)[:1] == ["sudo"] and cmd[3] == "grow-disk"
        said = f"vivibox-host: {NAME}\nvivibox: the disk is 40g now" if grows else ""
        return CompletedProcess(list(cmd), 1 if grows or cmd[0] != "sudo" else 0, "", said)

    monkeypatch.setattr(actions, "Pod", partial(Pod, runner=refusing))
    new_task()
    capsys.readouterr()
    assert main(["disk", "grow", "demo-1", "40g"]) == 1
    err = capsys.readouterr().err
    assert not hidden(err) and NAME_SHOWN in err, repr(err)
    assert [line[:28] for line in own_lines(err)] == ["vivibox: the task's disk cou"], err
