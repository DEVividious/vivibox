"""A flow's name is its title: planner_maker_checker was the one key that named its roles otherwise
(Planner → Writer → Reviewer), and it is read still, as planner_writer_reviewer."""

import json
import re

from vivibox import actions, config, orchestration
from vivibox.config import ORCHESTRATION_MODES, load_config
from vivibox.task import create_task


def test_every_flow_is_named_after_its_title():
    for name, mode in ORCHESTRATION_MODES.items():
        assert name == "_".join(re.findall(r"[a-z]+", mode.label.lower())), mode.label


def test_the_old_name_in_config_toml_is_read_as_the_new_one_and_said_once(env):
    cfg = env / "config" / "config.toml"
    cfg.write_text('agent_orchestration_mode = "planner_maker_checker"\n' + cfg.read_text())
    loaded = load_config()
    assert loaded.orchestration == "planner_writer_reviewer"
    assert "planner_maker_checker" in loaded.notice and "planner_writer_reviewer" in loaded.notice


def test_a_task_that_chose_the_old_name_runs_the_new_one(tmp_path, env):
    task = create_task(tmp_path / "tasks", "demo", "Goal", "+++\n+++\n")
    state = json.loads((task.meta / "state.json").read_text())
    state["orchestration"] = "planner_maker_checker"
    (task.meta / "state.json").write_text(json.dumps(state))
    assert task.read_state().orchestration == "planner_writer_reviewer"
    assert orchestration.mode_of(task, load_config()).name == "planner_writer_reviewer"


def test_new_takes_the_old_name_too(env):
    made = actions.create("demo", "Add health", orchestration="planner_maker_checker")
    assert orchestration.mode_of(made, load_config()).name == "planner_writer_reviewer"
    assert config.current_mode("planner_maker_checker") == "planner_writer_reviewer"
    assert config.current_mode("supervisor_worker") == "supervisor_worker"


def test_the_command_line_takes_the_old_name(env):
    from vivibox.cli import main

    assert main(["new", "demo", "Add health", "--flow", "planner_maker_checker", "--draft"]) == 0
