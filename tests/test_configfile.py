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
    assert load_config().max_iterations == 7
