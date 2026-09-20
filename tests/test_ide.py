from pathlib import Path

from vivibox import ide


def test_command_takes_the_path_where_you_put_it():
    assert ide.command_for(Path("/srv/x"), "code -n {path}") == ["code", "-n", "/srv/x"]
    assert ide.command_for(Path("/srv/x"), "idea") == ["idea", "/srv/x"], "or at the end"
    assert ide.is_terminal("nvim {path}") and not ide.is_terminal("/usr/bin/code {path}")


def test_candidates_come_from_path_toolbox_desktop_and_editor(monkeypatch, tmp_path):
    desktop = tmp_path / "applications"
    desktop.mkdir()
    (desktop / "ide.desktop").write_text(
        "[Desktop Entry]\nName=Some IDE\nMimeType=text/plain;inode/directory;\n"
    )
    (desktop / "player.desktop").write_text("[Desktop Entry]\nName=Player\nMimeType=audio/mpeg;\n")
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path))
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.setattr(
        ide.shutil, "which", lambda name: "/usr/bin/code" if name in ("code", "gio") else None
    )
    monkeypatch.setattr(ide, "TOOLBOX", tmp_path / "none")
    labels = [e.label for e in ide.candidates()]
    assert "VS Code" in labels and "Some IDE (desktop)" in labels and "Player (desktop)" not in labels
    editor = next(e for e in ide.candidates() if e.label.endswith("(from $EDITOR)"))
    assert editor.terminal and editor.command == "nano {path}"


def test_your_choice_is_written_to_the_configuration(env):
    config = env / "config" / "config.toml"
    assert ide.remember("code {path}") == config
    assert 'ide = "code {path}"' in config.read_text()
    ide.remember("idea {path}")
    text = config.read_text()
    assert text.count("[review]") == 1 and text.count("ide =") == 1
    assert 'ide = "idea {path}"' in text
