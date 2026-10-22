import tomllib

from vivibox import configfile
from vivibox.config import load_config, load_project

TEMPLATE = """# The manual, kept.
tasks_dir = "/srv/vivibox"

[limits]
# How many.
max_iterations = 3

[roles.writer]
harness = "opencode"
model = ""                           # picked in the view

[review]
# ide = "idea {path}"
"""


def test_a_key_is_replaced_in_its_table_and_the_comments_stay(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(TEMPLATE)
    configfile.set_value(path, "max_iterations", 5, "limits")
    configfile.set_value(path, "model", "deepseek/deepseek-v4", "roles.writer")
    configfile.set_value(path, "ide", "code {path}", "review")
    configfile.set_value(path, "tasks_dir", "/data/vivibox")
    text = path.read_text()
    assert "# The manual, kept." in text and "# How many." in text
    assert "max_iterations = 5" in text and text.count("max_iterations") == 1
    assert 'model = "deepseek/deepseek-v4"\n' in text, "the placeholder's note goes with the placeholder"
    assert 'ide = "code {path}"' in text and "# ide" not in text, "a commented-out key is the key's line"
    assert 'tasks_dir = "/data/vivibox"' in text and text.index("tasks_dir") < text.index("[limits]")


def test_a_key_or_a_table_the_file_lacks_is_added(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(TEMPLATE)
    configfile.set_value(path, "verify_timeout", 600, "limits")
    configfile.set_value(path, "desktop", False, "notifications")
    text = path.read_text()
    assert "[limits]\nverify_timeout = 600\n# How many.\nmax_iterations = 3\n" in text, "under the header"
    assert text.endswith("[notifications]\ndesktop = false\n")


def test_a_project_files_lists_are_written_on_one_line(env):
    path = env / "config" / "projects" / "demo.toml"
    path.write_text('repo = "/r"\nverify = ["true"]\ndemo = []\njava = ""\npass_env = []\n')
    configfile.set_value(path, "demo", ["npm install", "npm start"])
    configfile.set_value(path, "pass_env", ["NPM_TOKEN"])
    configfile.set_value(path, "java", "17")
    assert path.read_text() == (
        'repo = "/r"\nverify = ["true"]\ndemo = ["npm install", "npm start"]\njava = "17"\n'
        'pass_env = ["NPM_TOKEN"]\n'
    )
    project = load_project("demo")
    assert project.demo == ["npm install", "npm start"] and project.java == "17"
    configfile.set_value(env / "config" / "config.toml", "max_iterations", 7, "limits")
    assert load_config().max_rounds == 7, "the old key carries over as the round limit"


def test_a_header_with_spaces_or_a_note_is_the_tables_own_and_is_not_written_twice(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[limits]\nmax_iterations = 3\n\n  [ review ]   # where the diff opens\n# ide = "idea {path}"\n'
        'auto_open = true\n\n[ roles . writer ]\t# the writer\nharness = "opencode"\n'
    )
    configfile.set_value(path, "ide", "code {path}", "review")
    configfile.set_value(path, "harness", "claude", "roles.writer")
    configfile.set_value(path, "max_iterations", 5, "limits")
    text = path.read_text()
    assert text == (
        '[limits]\nmax_iterations = 5\n\n  [ review ]   # where the diff opens\nide = "code {path}"\n'
        'auto_open = true\n\n[ roles . writer ]\t# the writer\nharness = "claude"\n'
    ), "each key in its own table, the headers and their notes as they were"
    data = tomllib.loads(text)
    assert data["review"] == {"ide": "code {path}", "auto_open": True}
    assert data["roles"]["writer"] == {"harness": "claude"} and data["limits"]["max_iterations"] == 5


def test_a_key_the_table_lacks_goes_under_a_header_with_a_note(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[review] # where the diff opens\nauto_open = true\n")
    configfile.set_value(path, "ide", "code {path}", "review")
    text = path.read_text()
    assert text == '[review] # where the diff opens\nide = "code {path}"\nauto_open = true\n'
    assert tomllib.loads(text)["review"] == {"ide": "code {path}", "auto_open": True}


def test_a_header_in_a_comment_is_none_and_an_array_of_tables_ends_the_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# [review]\n[limits]\nmax_iterations = 3\n[[plugins]]\nide = "idea {path}"\n')
    configfile.set_value(path, "ide", "code {path}", "limits")
    configfile.set_value(path, "ide", "vim {path}", "review")
    text = path.read_text()
    assert text == (
        '# [review]\n[limits]\nide = "code {path}"\nmax_iterations = 3\n[[plugins]]\nide = "idea {path}"\n'
        '\n[review]\nide = "vim {path}"\n'
    )
    data = tomllib.loads(text)
    assert data["plugins"] == [{"ide": "idea {path}"}] and data["limits"]["ide"] == "code {path}"


def test_a_line_of_an_array_is_no_header_and_the_key_after_it_is_still_the_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[review]\nitems = [\n  ["a", "b"]\n]\nnames = [\n  ["c"],\n["d"]\n]\n'
        'note = """\n[review]\n"""\nide = "old"\n'
    )
    configfile.set_value(path, "ide", "code {path}", "review")
    text = path.read_text()
    assert text == (
        '[review]\nitems = [\n  ["a", "b"]\n]\nnames = [\n  ["c"],\n["d"]\n]\n'
        'note = """\n[review]\n"""\nide = "code {path}"\n'
    ), "the arrays and the string as they were, the key replaced in its own line"
    assert tomllib.loads(text)["review"] == {
        "items": [["a", "b"]],
        "names": [["c"], ["d"]],
        "note": "[review]\n",
        "ide": "code {path}",
    }
