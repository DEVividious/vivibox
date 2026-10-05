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


def said_by_m(env, flow: str, started_with: dict, then: dict, role: str, model: str) -> str:
    """What m says of a choice for role, in a task of flow started with those roles in config.toml."""
    from test_tui import run as run_view

    use(env, flow, **{**ROLES, **started_with})
    task = actions.create("demo", "Goal")
    actions.record_settings(task, load_project("demo"), load_config())  # as a start records them
    use(env, flow, **{**ROLES, **then})
    said: list[str] = []

    async def scenario(app, pilot):
        app.reload()
        app.set_choice(task, role, (OC, model), load_config())
        await pilot.pause()
        said.append([str(n.message) for n in app._notifications][-1])

    run_view(scenario, notifications=True)
    return said[0]


REVIEWER = {"reviewer": (OC, "anthropic/read")}
START = "from the next start"


@pytest.mark.parametrize(
    "flow,started_with,then,role,when",
    [
        ("planner_writer_reviewer", REVIEWER, REVIEWER, "reviewer", "from its next review"),
        ("planner_writer_reviewer", REVIEWER, REVIEWER, "writer", START),
        # The role added to config.toml since the start: this task's rounds are the writer's model's.
        ("planner_writer_reviewer", {}, REVIEWER, "reviewer", START),
        ("planner_writer_reviewer", {}, {}, "writer", f"{START}; its model reviews from the next review"),
        # Flows with no reviewer of their own: the role's model is read at a start, if at all.
        ("single_agent", REVIEWER, REVIEWER, "reviewer", START),
        ("supervisor_worker", REVIEWER, REVIEWER, "reviewer", START),
        ("planner_writer_reviewer", REVIEWER, REVIEWER, "planner", START),
    ],
)
def test_m_says_when_each_choice_applies(env, flow, started_with, then, role, when):
    """A reviewer's from its next review only where a round's container is made for it: a flow
    with a reviewer of its own, in a task that started with one. Where the writer's model
    reviews, m on the writer says both: its conversation at the next start, the review sooner."""
    assert said_by_m(env, flow, started_with, then, role, "openai/x") == f"{role} runs on openai/x {when}."


@pytest.mark.real_start
def test_a_rounds_key_is_written_only_once_the_container_before_it_is_gone(env, monkeypatch):
    """A container that outlived its round (a supervisor ended in the middle of one) mounts the
    directory the next round's key is written to: it is removed first, so it never sees a key
    that is not its round's."""
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    round_of(task, sup, pod)  # and no review_down: the round's container is still there
    choose(task, "reviewer", "openai/read")
    seen: list[list[str]] = []
    down = pod.review_down

    def removed():
        seen.append(held(f"{task.id}-review"))  # what the container being removed can read
        down()

    monkeypatch.setattr(pod, "review_down", removed)
    del pod.asked[:]
    assert round_of(task, sup, pod) == as_one("openai", "openai/read")
    assert seen and all(keys == ["anthropic"] for keys in seen), seen
    gone = next(i for i, c in enumerate(pod.asked) if c[:3] == ["docker", "rm", "-f"] and pod.review in c)
    made = next(i for i, c in enumerate(pod.asked) if c[:2] == ["docker", "run"] and pod.review in c)
    assert gone < made, "removed, then made anew"


@pytest.mark.real_start
def test_m_between_a_rounds_container_and_its_harness_changes_nothing_of_the_round(env, monkeypatch):
    """The interleaving, by hand: the round's container is made for A, and before its harness
    is, you choose B. The role was read once: the round is A's whole, the next one B's."""
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    up = reviewing.up

    def up_and_then_m(*args, **kwargs):
        out = up(*args, **kwargs)
        choose(task, "reviewer", "openai/read")
        return out

    monkeypatch.setattr(reviewing, "up", up_and_then_m)
    assert round_of(task, sup, pod) == as_one("anthropic", "anthropic/read")
    monkeypatch.setattr(reviewing, "up", up)
    assert round_of(task, sup, pod) == as_one("openai", "openai/read")


@pytest.mark.real_start
def test_a_rounds_container_is_made_for_the_role_it_is_handed(env, monkeypatch):
    """reviewing.up with the role its caller read: the container's configuration and key are
    that role's, whatever the task's own choice says by then."""
    from vivibox.config import Role

    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    reviewing.up(task, pod, load_config(), (), Role(OC, "openai/read"))
    written = json.loads((pod.review_dir / "harness" / "opencode.json").read_text())
    assert (sorted(written["provider"]), written["model"]) == (["openai"], "openai/read")
    assert held(f"{task.id}-review") == ["openai"]


def docker_that(pod, said: str, code: int = 1):
    """The pod's Docker, answering the removal of the review container with said and code."""
    run = pod.runner

    def runner(command, **how):
        if list(command[:3]) == ["docker", "rm", "-f"] and pod.review in command:
            pod.asked.append(list(command))
            return subprocess.CompletedProcess(command, code, "", said)
        return run(command, **how)

    pod.runner = runner


@pytest.mark.real_start
def test_a_round_is_not_started_while_the_container_before_it_cannot_be_removed(env, monkeypatch):
    """Docker refuses to remove the container of the round before, which mounts the directory
    the next round's key would be written to: no key is written and no container made; the
    error is the task's, with what Docker said and what to do."""
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    round_of(task, sup, pod)
    harness = sup.reviewer.harness
    choose(task, "reviewer", "openai/read")
    docker_that(pod, "Error response from daemon: cannot remove container: device or resource busy")
    del pod.asked[:]
    with pytest.raises(pods.PodError) as refused:
        sup.ports.review_up()
    assert str(refused.value) == (
        f"the review could not start: the container of the round before ({pod.review}) could not be "
        "removed, so no key of the next round is written where it reads: Error response from daemon: "
        f"cannot remove container: device or resource busy; stop the task (vivibox stop {task.id}), "
        f"then start it (vivibox start {task.id})"
    )
    assert held(f"{task.id}-review") == ["anthropic"], "the next round's key is not written"
    assert not [c for c in pod.asked if c[:2] == ["docker", "run"]], "and no container is made"
    assert sup.reviewer.harness is harness


@pytest.mark.real_start
def test_a_container_that_is_not_there_is_no_reason_to_stop_a_round(env, monkeypatch):
    """Docker says so with an error on some versions: there is nothing to remove, and the round starts."""
    task, sup, pod = supervised(env, monkeypatch, reviewer=(OC, "anthropic/read"))
    docker_that(pod, f"Error response from daemon: No such container: {pod.review}")
    assert round_of(task, sup, pod) == as_one("anthropic", "anthropic/read")
