"""Textual reads a message as markup unless told not to, and what vivibox shows is often text it
did not write: the agent's output, commands and questions, file names and git's words, a build's
output, a plan, a provider's error. Such text never reaches a notification, a Label or a Static as
markup: [/b] in it would end the view (MarkupError), [black on black] hide what follows. Every call
with a message that is not a plain string either says markup=False, or is markup vivibox wrote,
listed here with why, the values in it escaped."""

import ast
import os
from pathlib import Path

from vivibox import actions, demo

PACKAGE = Path(__file__).parent.parent / "vivibox"
BIDI = chr(0x202E)
EVIL = f"x[/b] [black on black]hidden{BIDI}\x1b[2K"

# (module, the message as written): why it is markup of vivibox's own.
VIVIBOX_MARKUP = {
    ("browse.py", "f'New folder in {escape(shown_path(self.folder))}:'"): "the path escaped",
    ("dialogs.py", "escape(self.about)"): "escaped",
    ("dialogs.py", "look.colored(self.warning, look.WAITING)"): "look.colored escapes the text",
    ("dialogs.py", "f'[b {look.ERROR}]Deleted[/]  {escape(self.goes)}'"): "vivibox's tags, the text escaped",
    (
        "dialogs.py",
        "f'[b {look.SUCCESS}]Kept[/]     {escape(self.stays)}'",
    ): "vivibox's tags, the text escaped",
    ("dialogs.py", "f'vivibox {version.current()}'"): "vivibox's version",
    ("dialogs.py", "help_text(self.note)"): "the help, written by vivibox with its tags",
    ("dialogs.py", "NOTHING_TO_SEND"): "a constant of vivibox's",
    ("newtask.py", "name.capitalize()"): "a role's name, vivibox's",
    ("providers_ui.py", "escape(shown_path(self.source))"): "the path escaped",
    ("providers_ui.py", "heading"): "a section heading of vivibox's",
    ("tui.py", "CODE_CHANGED.capitalize() + '.'"): "a constant of vivibox's",
    ("widgets.py", "said"): "the key bar, vivibox's keys and words with its tags",
}


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _textual_notify(func) -> bool:
    """self.notify, self.app.notify, app.notify: Textual's; supervisor.notify and ports.notify
    are a task's messages, plain text by their own escaping."""
    if not isinstance(func, ast.Attribute) or func.attr != "notify":
        return False
    owner = func.value
    return (isinstance(owner, ast.Name) and owner.id in ("self", "app")) or (
        isinstance(owner, ast.Attribute) and owner.attr == "app"
    )


def messages():
    """(module, line, what, the message as written, markup off) for every notify, Label and Static
    that is given a message."""
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            args, what = node.args, _name(node.func)
            if what == "call_from_thread" and args and _textual_notify(args[0]):
                args, what = args[1:], "notify"
            elif what == "notify" and not _textual_notify(node.func):
                continue
            if what not in ("notify", "Label", "Static") or not args:
                continue
            off = any(
                k.arg == "markup" and isinstance(k.value, ast.Constant) and k.value.value is False
                for k in node.keywords
            )
            yield path.name, node.lineno, what, args[0], off


def test_no_message_vivibox_did_not_write_is_read_as_markup():
    offenders = [
        f"{module}:{line} {what}({ast.unparse(message)})"
        for module, line, what, message, off in messages()
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str))
        and not off
        and (module, ast.unparse(message)) not in VIVIBOX_MARKUP
    ]
    assert not offenders, "markup=False, or vivibox's own markup listed here with why:\n" + "\n".join(
        offenders
    )


def test_every_listed_markup_is_still_there():
    found = {(module, ast.unparse(message)) for module, _, _, message, _ in messages()}
    assert not set(VIVIBOX_MARKUP) - found, set(VIVIBOX_MARKUP) - found


# --- the demo: the three places the review found -------------------------------------------------


def test_what_the_agent_left_running_is_said_as_text(env, monkeypatch):
    from test_tui import new_task, run

    task = new_task()
    stopped = demo.Demo(commands=["x"], starting=True, stopped=[f"python3 -m http.server {EVIL} (port 8000)"])
    monkeypatch.setattr(actions, "demo", lambda task_id, ask=False, reply="": stopped)

    async def scenario(app, pilot):
        app.run_demo(task.id)
        await app.workers.wait_for_complete()
        await pilot.pause()
        said = [n for n in app._notifications if "left running" in n.message]
        assert said and said[-1].markup is False and "x[/b] [black on black]hidden" in said[-1].message
        assert "\x1b" not in said[-1].message and BIDI not in said[-1].message

    run(scenario, notifications=True)


def label_text(app) -> str:
    from textual.widgets import Label

    return "\n".join(str(label.render()) for label in app.screen.query(Label))


def test_the_agents_question_about_running_it_is_shown_as_text(env):
    from test_tui import new_task, run

    from vivibox.dialogs import Reply

    task = new_task()

    async def scenario(app, pilot):
        app.answer_demo(task.id, f"Which port? {EVIL}")
        await pilot.pause()
        assert isinstance(app.screen, Reply)
        shown = label_text(app)
        assert "Which port? x[/b] [black on black]hidden" in shown and "\x1b" not in shown, shown

    run(scenario)


def test_an_earlier_tasks_way_to_run_it_is_shown_as_text(env, monkeypatch):
    from test_tui import new_task, run

    from vivibox.widgets import Confirm

    new_task()
    monkeypatch.setattr(actions, "demo_commands", lambda project, task: ([], ""))
    monkeypatch.setattr(actions, "demo_from_history", lambda name: f"npm start {EVIL}")

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.action_demo()
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        shown = label_text(app)
        assert "npm start x[/b] [black on black]hidden" in shown and "\x1b" not in shown, shown

    run(scenario)


# --- l with a pager other than less, where the copies cannot go -----------------------------------


def test_l_says_why_when_the_copies_folder_is_not_yours_alone(env, monkeypatch):
    import tempfile

    from test_tui import new_task, run

    task = new_task()
    monkeypatch.setenv("PAGER", "cat")
    folder = Path(tempfile.gettempdir()) / f"vivibox-{os.getuid()}"
    folder.mkdir(exist_ok=True)
    folder.chmod(0o755)  # as another's would be to you: not yours alone

    async def scenario(app, pilot):
        app.reload()
        await pilot.pause()
        app.action_show_log()
        await pilot.pause()
        said = [n.message for n in app._notifications]
        assert any("not a folder of yours alone" in m for m in said), said

    try:
        run(scenario, notifications=True)
    finally:
        folder.chmod(0o700)
    assert task.read_state()


def test_the_suite_writes_no_copy_in_your_own_temporary_folder():
    import tempfile

    from conftest import TEMPORARY

    assert Path(tempfile.gettempdir()) == TEMPORARY, "the suite's own temporary folder, not yours"
