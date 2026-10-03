"""The bench of the behavioural suite (tests/behavioural), checked without a model or a pod: a
mistake there is found only after a paid scenario has spent its money."""

import ast
import importlib.util
import stat
from pathlib import Path

import pytest

from vivibox.config import ConfigError, load_project

SOURCE = Path(__file__).parent / "behavioural" / "test_prompts.py"
# What takes a project's or a task's name as vivibox knows it.
BY_NAME = {"load_project", "load", "create", "find_task", "task_pod"}


@pytest.fixture(scope="module")
def bench_module():
    spec = importlib.util.spec_from_file_location("behavioural_bench", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def called(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def test_no_scenario_names_a_project_or_a_task_by_a_literal():
    """A scenario's projects are the run's own (bh<run>-<name>): vivibox is asked for one through
    the bench (the_project, begin), never by the name the scenario wrote, which is no project's."""
    tree = ast.parse(SOURCE.read_text())
    wrong = [
        f"line {node.lineno}: {called(node)}({literal(node.args[0])!r})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and called(node) in BY_NAME and node.args and literal(node.args[0])
    ]
    assert wrong == [], f"asked of vivibox by a literal name: {wrong}"


def test_every_scenario_begins_a_task_of_a_project_it_made():
    """begin(name, …) and the_project(name) take the name project(bench, name, …) was given, in
    the same scenario: a name without a project fails after the pod and the model were paid for."""
    tree = ast.parse(SOURCE.read_text())
    scenarios = [f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name.startswith("test_")]
    assert len(scenarios) >= 15
    for scenario in scenarios:
        calls = [n for n in ast.walk(scenario) if isinstance(n, ast.Call)]
        made = {literal(c.args[1]) for c in calls if called(c) == "project" and len(c.args) > 1}
        used = {literal(c.args[0]) for c in calls if called(c) in ("begin", "the_project") and c.args}
        assert None not in made | used, f"{scenario.name}: a name that is not a literal"
        assert used and used <= made, f"{scenario.name}: {sorted(used - made)} has no project"


def test_a_project_is_loaded_by_the_runs_name_for_it(bench_module, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setenv("VIVIBOX_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(bench_module, "NAMES", {})
    bench = {"tmp": tmp_path, "cfg": cfg, "run": "abc123"}
    bench_module.project(bench, "propose", {"calc.py": "x = 1\n"}, ["true"])
    assert bench_module.NAMES == {"propose": "bhabc123-propose"}
    assert bench_module.the_project("propose").verify == ["true"]
    assert load_project("bhabc123-propose").repo == tmp_path / "propose"
    with pytest.raises(ConfigError):
        load_project("propose")
    with pytest.raises(KeyError):
        bench_module.the_project("never-made")


def test_the_run_copies_only_the_keys_its_models_need_and_removes_them(bench_module, tmp_path, monkeypatch):
    """Your key store is read, never moved: the run's copy holds the planner's, the writer's and
    the reviewer's providers alone, private as yours, and is gone when the run ends."""
    real = tmp_path / "yours"
    real.mkdir(mode=0o700)
    for name in ("deepseek", "anthropic", "openai", "ntfy", "mcp.tools.token"):
        (real / name).write_text(f"key-{name}")
        (real / name).chmod(0o600)
    monkeypatch.setattr(bench_module, "PLANNER", "deepseek/pro")
    monkeypatch.setattr(bench_module, "WRITER", "deepseek/flash")
    monkeypatch.setattr(bench_module, "REVIEWER", "anthropic/reads")
    assert bench_module.needed_keys("deepseek/pro", "deepseek/flash", "anthropic/reads") == [
        "anthropic",
        "deepseek",
    ]
    copy = tmp_path / "run" / "data" / "vivibox" / "keys"
    bench_module.copy_keys(real, copy)
    assert sorted(p.name for p in copy.iterdir()) == ["anthropic", "deepseek"]
    assert stat.S_IMODE((copy / "deepseek").stat().st_mode) == 0o600
    assert stat.S_IMODE(copy.stat().st_mode) == 0o700
    bench_module.forget_keys(tmp_path / "run")
    assert not copy.exists() and sorted(p.name for p in real.iterdir()) == sorted(
        ["deepseek", "anthropic", "openai", "ntfy", "mcp.tools.token"]
    ), "yours are as they were"


def test_the_bench_forgets_the_keys_when_it_ends_whatever_it_keeps():
    """In the fixture's finally, outside the choice to keep the tasks."""
    tree = ast.parse(SOURCE.read_text())
    fixture = next(f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "bench")
    ending = next(n for n in ast.walk(fixture) if isinstance(n, ast.Try)).finalbody
    assert any(
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and called(s.value) == "forget_keys"
        for s in ending
    ), "forget_keys(tmp) is a statement of the finally itself"
