"""Verification by modules, chosen where a project's verification is set (ADR-0033, change of
2026-09-29): init finds the modules, a project of three or more is verified by them from the
start, and the verification dialog has a box that turns the command into the one by modules."""

from test_tui import fresh_project, rows, run
from textual.widgets import Checkbox, Input, Label

from vivibox import init, settings
from vivibox.config import load_project
from vivibox.verify_ui import AskVerify

MAVEN = "<project><modules>{}</modules></project>"


def reactor(path, *modules):
    path.mkdir(parents=True, exist_ok=True)
    (path / "pom.xml").write_text(MAVEN.format("".join(f"<module>{m}</module>" for m in modules)))
    for module in modules:
        (path / module).mkdir(parents=True, exist_ok=True)
        (path / module / "pom.xml").write_text("<project/>")
    return path


def test_init_finds_the_modules_and_takes_three_as_many(tmp_path):
    assert init.MANY_MODULES == 3
    three = reactor(tmp_path / "three", "core", "app", "web")
    (three / "mvnw").write_text("")
    assert init.modules(three) == ["core", "app", "web"]
    found = init.detect(three)
    assert found.modules == ["core", "app", "web"]
    assert found.verify == ["bash ./mvnw -B -pl {modules} -am verify"]
    two = reactor(tmp_path / "two", "core", "app")
    assert init.modules(two) == ["core", "app"] and init.detect(two).verify == []
    assert init.modules(tmp_path / "none") == []


def test_a_maven_command_becomes_the_one_by_modules():
    assert init.scoped("./mvnw -B verify") == "./mvnw -B -pl {modules} -am verify"
    assert init.scoped("bash ./mvnw -B verify") == "bash ./mvnw -B -pl {modules} -am verify"
    assert init.scoped("mvn clean verify") == "mvn -pl {modules} -am clean verify"
    assert init.scoped("mvn -B -pl {modules} -am verify") == "mvn -B -pl {modules} -am verify"
    assert init.scoped("make test") is None, "no build tool it knows how to scope"


def test_the_verification_dialog_turns_the_command_into_the_one_by_modules(env):
    name = fresh_project(env, "shop")
    reactor(env / name, "core", "app")
    settings_file = env / "config" / "projects" / "shop.toml"
    settings_file.write_text(settings_file.read_text().replace("verify = []", 'verify = ["mvn -B verify"]'))

    async def scenario(app, pilot):
        app.reload()
        app.table.move_cursor(row=rows(app).index("shop"))
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, settings.ProjectSettings)
        await pilot.press("down", "enter")  # the verification
        await pilot.pause()
        assert isinstance(app.screen, AskVerify)
        box = app.screen.query_one("#modules", Checkbox)
        assert "2 modules" in str(box.label) and not box.value
        box.value = True
        await pilot.pause()
        assert app.screen.query_one(Input).value == "mvn -B -pl {modules} -am verify"
        preview = str(app.screen.query_one("#modules-preview", Label).render())
        assert "mvn -B -pl core -am verify" in preview, "what a task changing core runs"
        box.value = False
        await pilot.pause()
        assert app.screen.query_one(Input).value == "mvn -B verify", "back to the whole build"
        box.value = True
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert load_project("shop").verify == ["mvn -B -pl {modules} -am verify"]
        assert load_project("shop").by_module

    run(scenario)
