import re
import shlex

import pytest

from vivibox import skill
from vivibox.cli import main, parser


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_the_skill_goes_to_every_cli_installed_here(home, capsys):
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    assert main(["skill", "install"]) == 0
    out = capsys.readouterr().out
    for folder in (home / ".claude" / "skills" / "vivibox", home / ".agents" / "skills" / "vivibox"):
        assert (folder / "SKILL.md").read_text().startswith("---\nname: vivibox\n")
        assert (folder / skill.VERSION_FILE).exists()
        assert str(folder) in out
    assert not (home / ".codex" / "skills").exists(), "one copy for Codex, or it lists the skill twice"
    assert all(copy.current for copy in skill.copies())


def test_no_cli_no_copy(home, capsys):
    assert main(["skill", "install"]) == 1
    assert "Claude Code" in capsys.readouterr().err
    assert not any(home.iterdir())


def test_a_skill_of_the_same_name_that_is_not_vivibox_s_is_left_alone(home, capsys):
    theirs = home / ".claude" / "skills" / "vivibox"
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("mine")
    assert main(["skill", "install"]) == 0
    assert "left alone" in capsys.readouterr().out
    assert (theirs / "SKILL.md").read_text() == "mine"
    assert main(["skill", "uninstall"]) == 0
    assert (theirs / "SKILL.md").read_text() == "mine"


def test_installing_again_updates_a_copy_left_behind_and_uninstall_removes_it(home, capsys):
    (home / ".claude").mkdir()
    main(["skill", "install"])
    copy = home / ".claude" / "skills" / "vivibox"
    (copy / "SKILL.md").write_text("an older skill")
    assert [c.current for c in skill.copies()] == [False]
    main(["skill", "install"])
    assert [c.current for c in skill.copies()] == [True]
    assert main(["skill", "uninstall"]) == 0
    assert str(copy) in capsys.readouterr().out and not copy.exists()


def test_info_says_whether_the_copy_is_current(env, home, capsys):
    import json

    (home / ".claude").mkdir()
    main(["skill", "install"])
    (home / ".claude" / "skills" / "vivibox" / "SKILL.md").write_text("an older skill")
    capsys.readouterr()
    assert main(["info", "--json", str(env / "repo")]) == 0
    [copy] = json.loads(capsys.readouterr().out)["skill"]
    assert copy["cli"] == "Claude Code" and copy["version"] and copy["current"] is False


SKILL_TEXT = skill._source()
COMMAND = re.compile(r"`(vivibox [^`]*)`")


def test_every_command_and_flag_in_the_skill_is_one_vivibox_has():
    """The skill tells an agent what to run; a command or a flag vivibox does not have is one the
    agent would run and fail on, in front of the user."""
    commands = {name: sub for name, sub in parser()._subparsers._group_actions[0].choices.items()}
    found = COMMAND.findall(SKILL_TEXT)
    assert len(found) > 20, "the pattern no longer finds the commands"
    for text in found:
        words = shlex.split(text.replace("<", "").replace(">", ""))[1:]
        if not words:
            continue
        assert words[0] in commands, text
        flags = {s for action in commands[words[0]]._actions for s in action.option_strings}
        for word in words[1:]:
            if word.startswith("--"):
                assert word in flags, f"{text}: {word}"


def test_the_view_says_once_that_a_copy_was_left_behind(env, home):
    from test_tui import run

    (home / ".claude").mkdir()
    skill.install()
    said = []

    async def scenario(app, pilot):
        said.extend(str(n.message) for n in app._notifications)

    run(scenario, notifications=True)
    assert not any("vivibox skill install" in m for m in said), "a current copy is not news"
    (home / ".claude" / "skills" / "vivibox" / "SKILL.md").write_text("an older skill")
    said.clear()
    run(scenario, notifications=True)
    assert sum("vivibox skill install" in m for m in said) == 1, said
