import json
import re
from pathlib import Path

from conftest import make_repo

from vivibox import about
from vivibox.cli import main
from vivibox.config import ORCHESTRATION_MODES

README = Path(__file__).parent.parent / "README.md"


def info(capsys, *args):
    assert main(["info", "--json", *map(str, args)]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_project_is_found_from_anywhere_in_its_repository(env, capsys):
    (env / "repo" / "src" / "main").mkdir(parents=True)
    shown = info(capsys, env / "repo" / "src" / "main")
    assert shown["version"] == about.JSON_VERSION and shown["vivibox"]
    assert shown["project"] == {"name": "demo", "repo": str(env / "repo"), "verify": ["true"]}
    assert shown["hint"] == ""


def test_a_repository_with_no_project_is_told_how_to_make_one(env, capsys):
    other = make_repo(env / "other")
    (other / "docs").mkdir()
    shown = info(capsys, other / "docs")
    assert shown["project"] is None and shown["hint"] == f"vivibox init {other}"


def test_a_folder_outside_git_has_no_project(env, capsys):
    (env / "plain").mkdir()
    shown = info(capsys, env / "plain")
    assert shown["project"] is None and "not in a git repository" in shown["hint"]


def test_the_flows_say_which_a_planner_in_your_cli_can_run(env, capsys):
    flows = {flow["name"]: flow for flow in info(capsys, env / "repo")["flows"]}
    assert list(flows) == list(ORCHESTRATION_MODES)
    for name in ("single_agent", "supervisor_worker"):
        assert not flows[name]["plan_in_cli"] and flows[name]["why_not"]
    for name in ("planner_executor", "planner_maker_checker"):
        assert flows[name]["plan_in_cli"] and flows[name]["why_not"] == ""
    mode = ORCHESTRATION_MODES["planner_executor"]
    assert flows["planner_executor"]["title"] == mode.label
    assert flows["planner_executor"]["best_for"] == mode.best_for
    assert flows["planner_executor"]["summary"] == mode.summary


def test_the_roles_and_the_skill_are_there(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    shown = info(capsys, env / "repo")
    assert shown["roles"]["planner"] == {"harness": "opencode", "model": "m"}
    assert shown["default_flow"] in ORCHESTRATION_MODES
    assert shown["skill"] == [], "no copy installed under the test's home"


def test_the_readmes_flows_are_best_for_what_the_code_says():
    """One source for what a flow is best for: info tells an agent's CLI what the README tells you."""
    rows = re.findall(r"^\| \*\*(.+?)\*\*[^|]*\|[^|]*\| (.+?) \|$", README.read_text(), re.MULTILINE)
    assert {title: best for title, best in rows} == {
        mode.label: mode.best_for.capitalize() for mode in ORCHESTRATION_MODES.values()
    }


def test_without_json_the_same_for_you(env, capsys):
    assert main(["info", str(env / "repo")]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "Single agent" in out and "not with a planner in your CLI" in out
