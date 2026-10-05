"""The reviewer changed with m while its task runs. A review's round is made of one role, read
once at its start: the round's container gets that role's provider, its configuration and its
key and no other, and the round's harness asks that provider for that role's model. What you
choose while a round runs is the next round's, whole; nothing of a round is ever half the old
choice and half the new one."""

import json
import subprocess
from functools import partial

import pytest
from conftest import as_verified, networks_up
from test_secrets import held, start, use

from vivibox import actions, opencode, reviewing, supervise
from vivibox import pod as pods
from vivibox.config import load_config, load_project

OC = "opencode"
ROLES = {"planner": (OC, "deepseek/plan"), "writer": (OC, "deepseek/write")}


def supervised(env, monkeypatch, **roles):
    """A task of a flow with a reviewer of its own, started, its work verified, and the command
    line's supervisor of it, on the task's own pod with a Docker that runs nothing."""
    use(env, **{**ROLES, **roles})
    task = actions.create("demo", "Goal")
    start(env, monkeypatch)
    asked: list[list[str]] = []

    def runner(command):
        asked.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(actions, "Pod", partial(pods.Pod, runner=runner))
    pod = actions.task_pod(task.id)
    pod.asked = asked  # what Docker was asked to run, in order
    networks_up(pod)
    as_verified(task)
    sup = supervise.make_supervisor(task, load_project("demo"), pod, load_config())
    return task, sup, pod


def round_of(task, sup, pod) -> dict:
    """What a round that starts now is made of: the model its harness asks for, the provider its
    container's opencode.json configures and the model it names, and the keys its container
    holds."""
    sup.ports.review_up()
    written = json.loads((pod.review_dir / "harness" / "opencode.json").read_text())
    return {
        "asks": sup.reviewer.model,
        "configured": (sorted(written["provider"]), written["model"]),
        "holds": held(f"{task.id}-review"),
    }


def as_one(provider: str, model: str) -> dict:
    return {"asks": model, "configured": ([provider], model), "holds": [provider]}


def choose(task, role: str, model: str) -> None:
    actions.choose_role(task, role, (OC, model), load_config())  # as m does


CHANGES = {
    "another model of the same provider": ("anthropic/read", "anthropic/reads-better"),
    "another provider": ("anthropic/read", "openai/read"),
    "back to config.toml's own": ("anthropic/read", "openai/read", "anthropic/read"),
}


@pytest.mark.real_start
@pytest.mark.parametrize("change", list(CHANGES))
def test_the_next_round_is_made_of_the_reviewer_you_chose(env, monkeypatch, change):
    first, *later = CHANGES[change]
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, first))
    assert round_of(task, sup, pod) == as_one(opencode.provider_of(first), first)
    for model in later:
        choose(task, "reviewer", model)
        made = round_of(task, sup, pod)
        assert made == as_one(opencode.provider_of(model), model), "one role: container, key and model"
        gone = {opencode.provider_of(m) for m in CHANGES[change]} - {opencode.provider_of(model)}
        assert not gone & set(made["holds"]), "no key of a provider you switched away from"


@pytest.mark.real_start
def test_a_change_made_while_a_round_runs_is_the_next_rounds(env, monkeypatch):
    """Between a round's container and its turns, and between its turn and its repair turn: the
    round goes on as it began, harness and key alike, and the round after it is the new choice's."""
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    began = round_of(task, sup, pod)
    harness = sup.reviewer.harness
    choose(task, "reviewer", "openai/read")
    assert sup.reviewer.harness is harness and sup.reviewer.model == "anthropic/read"
    assert held(f"{task.id}-review") == ["anthropic"] == began["holds"], "the round's key, untouched"
    written = json.loads((pod.review_dir / "harness" / "opencode.json").read_text())
    assert written["model"] == "anthropic/read", "and its configuration"
    assert round_of(task, sup, pod) == as_one("openai", "openai/read")


@pytest.mark.real_start
def test_the_writers_model_reviews_on_what_the_writer_was_put_on(env, monkeypatch):
    """No reviewer in config.toml: the mode has the writer's model read, in the review container,
    which then holds the writer's provider's key, the one the mode asks for, and no other."""
    task, sup, pod = supervised(env, monkeypatch)
    assert round_of(task, sup, pod) == as_one("deepseek", "deepseek/write")
    choose(task, "writer", "anthropic/write")
    assert round_of(task, sup, pod) == as_one("anthropic", "anthropic/write")
    assert sup.reviewer.port == opencode.REVIEW_PORT and sup.reviewer.pod.agent == pod.review


@pytest.mark.real_start
def test_the_reviewers_container_never_holds_the_writers_key_when_a_reviewer_reviews(env, monkeypatch):
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    choose(task, "writer", "openai/write")  # the writer's choice is not the review container's
    assert round_of(task, sup, pod) == as_one("anthropic", "anthropic/read")
    choose(task, "reviewer", "openai/read")
    assert round_of(task, sup, pod)["holds"] == ["openai"], "the reviewer's own, now on that provider"
    assert "deepseek" not in held(f"{task.id}-review")


@pytest.mark.real_start
@pytest.mark.parametrize("edit", ["a reviewer added", "the reviewer removed"])
def test_a_reviewer_added_to_or_taken_out_of_config_toml_waits_for_the_next_start(env, monkeypatch, edit):
    """config.toml is read at the task's start, for the round's container and its harness alike:
    a role added or removed while the task runs changes neither until it starts again, so the
    two never disagree."""
    was = {} if edit == "a reviewer added" else {"reviewer": (OC, "anthropic/read")}
    task, sup, pod = supervised(env, monkeypatch, **was)
    before = round_of(task, sup, pod)
    use(env, **{**ROLES, **({"reviewer": (OC, "openai/read")} if edit == "a reviewer added" else {})})
    assert round_of(task, sup, pod) == before, "as the task started, whole"
    again = supervise.make_supervisor(task, load_project("demo"), pod, load_config())
    expected = (
        as_one("openai", "openai/read")
        if edit == "a reviewer added"
        else as_one("deepseek", "deepseek/write")
    )
    assert round_of(task, again, pod) == expected, "from the next start"


@pytest.mark.real_start
def test_a_round_whose_container_could_not_be_made_leaves_the_harness_as_it_was(env, monkeypatch):
    """A reviewer put on a provider with no key: the round's container is not made, the error is
    the task's, and no turn could ask the new model of a container made for the old one."""
    from vivibox import keys

    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    round_of(task, sup, pod)
    harness = sup.reviewer.harness
    choose(task, "reviewer", "mistral/read")
    with pytest.raises(keys.KeyStoreError):
        sup.ports.review_up()
    assert sup.reviewer.harness is harness and sup.reviewer.model == "anthropic/read"
    assert "mistral" not in held(f"{task.id}-review")


@pytest.mark.real_start
def test_the_turn_of_a_round_names_the_model_its_container_was_made_for(env, monkeypatch):
    """What the harness runs in the review container, as the command it hands to docker: the
    model is the round's, after the change."""
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    round_of(task, sup, pod)
    choose(task, "reviewer", "openai/read")
    round_of(task, sup, pod)
    del pod.asked[:]
    sup.reviewer.turn("Review the work.", title="round 2")
    turns = [command for command in pod.asked if "'opencode' 'run'" in command[-1]]
    assert len(turns) == 1 and turns[0][:3] == ["docker", "exec", pod.review], pod.asked
    assert "'--model' 'openai/read'" in turns[0][-1] and "anthropic" not in turns[0][-1]


def test_the_supervisors_reviewer_is_the_rounds_harness_and_nothing_else():
    """The wrapper hands on whatever is asked of it, and changes harness only at a round's start."""
    from vivibox.config import Role

    made = []

    class Harness:
        name, metered, manual = "opencode", True, False

        def __init__(self, role):
            self.model = role.model
            made.append(role.model)

    reviewer = reviewing.Reviewer(Harness, Role(OC, "a/one"))
    assert (reviewer.model, reviewer.name, reviewer.metered, reviewer.manual) == (
        "a/one",
        "opencode",
        True,
        False,
    )
    reviewer.begin(Role(OC, "b/two"))
    assert reviewer.model == "b/two" and made == ["a/one", "b/two"]
    with pytest.raises(AttributeError):
        _ = reviewer.nothing_a_harness_has


def test_m_says_when_each_choice_applies(env):
    """A reviewer's from its next review, whose container is made for it; the rest from the
    task's next start, as their containers are."""
    from test_tui import run as run_view

    use(env, **ROLES, reviewer=(OC, "anthropic/read"))
    task = actions.create("demo", "Goal")

    async def scenario(app, pilot):
        app.reload()
        app.set_choice(task, "reviewer", (OC, "openai/read"), load_config())
        app.set_choice(task, "writer", (OC, "openai/write"), load_config())
        await pilot.pause()
        assert [str(n.message) for n in app._notifications][-2:] == [
            "reviewer runs on openai/read from its next review.",
            "writer runs on openai/write from the next start.",
        ]

    run_view(scenario, notifications=True)
