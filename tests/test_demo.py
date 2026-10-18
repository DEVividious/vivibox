"""Running the project for you to look at, and the line between that and the task's own work."""

import pytest

from vivibox import actions, harness, opencode, supervisor
from vivibox.cli import main
from vivibox.states import State
from vivibox.task import find_task


class FakePod:
    """Enough of a pod to answer the questions actions.demo asks of one."""

    def __init__(self, listening=()):
        self.started: list[list[str]] = []
        self.listening = list(listening)

    def up(self):
        pass

    def address(self):
        return "198.51.100.2"

    def demo_start(self, commands, workdir="", wait=40):
        self.started.append(list(commands))
        return self.listening

    def demo_log(self, lines=20):
        return ""

    def demo_running(self):
        return getattr(self, "running", False)

    def stop_leftovers(self):
        return getattr(self, "leftovers", [])


def a_task(goal="Goal"):
    assert main(["new", "demo", goal, "--draft"]) == 0
    from vivibox.config import load_config

    tasks = load_config().tasks_dir
    return find_task(tasks, sorted(p.name for p in tasks.iterdir())[-1])


def answering(monkeypatch, write):
    """An agent turn that writes what `write` says, instead of talking to a model."""

    def turn(self, prompt, session="", title="", on_step=None):
        write(prompt, session)
        return harness.Turn("ses_demo", True, 0.01, 10, "done")

    monkeypatch.setattr(opencode.OpenCode, "turn", turn)


def back_with_you(task):
    """The work reviewed: the state the app is run in."""
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_FINAL):
        task.transition(state)
    return task


def test_the_app_does_not_run_while_the_agent_works_in_the_pod(env, monkeypatch, capsys):
    """The agent builds and tests in the same tree and pod: a second build there, or a server on a
    port its tests want, would get in its way."""
    from types import SimpleNamespace

    from vivibox import gate

    task = a_task()
    task.transition(State.CHECKPOINT_PLAN)
    task.transition(State.IMPLEMENT)
    actions.write_instruction(task, "```bash\nnpm run dev\n```\n")
    pod = FakePod()
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)
    with pytest.raises(gate.GateError, match="at work in the pod"):
        actions.demo(task.id, ask=False)
    assert not pod.started
    assert main(["demo", task.id, "--no-ask"]) == 1
    assert "could not run the app" in capsys.readouterr().err
    task.transition(State.VERIFY)
    with pytest.raises(gate.GateError):
        actions.demo(task.id, ask=False)
    task.transition(State.CHECKPOINT_FINAL)
    assert actions.demo(task.id, ask=False).commands == ["npm run dev"]
    assert actions.demo_allowed(SimpleNamespace(box=True, state=State.IMPLEMENT)), "a box is yours"


def test_a_question_about_running_it_cannot_stop_the_task(env, monkeypatch):
    """The supervisor watches handoff/question.md to decide when a task stops for you. A question
    about how to start the project must never land there: a side errand must not halt the work."""
    task = back_with_you(a_task())
    pod = FakePod()
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)
    answering(
        monkeypatch,
        lambda prompt, session: (task.meta / "handoff" / actions.DEMO_QUESTION).write_text(
            "Which profile should it run with?"
        ),
    )

    result = actions.demo(task.id)

    assert "Which profile" in result.question
    assert actions.DEMO_QUESTION != supervisor.QUESTION, "its own file, or it would be a checkpoint"
    assert supervisor.question(task) is None, "the task is not asking you anything"
    assert task.read_state().state is State.CHECKPOINT_FINAL, "and it has not moved"
    assert not pod.started, "nothing ran, because nothing is known yet"


def test_answering_carries_the_same_conversation_on(env, monkeypatch):
    task = back_with_you(a_task())
    pod = FakePod()
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)
    seen: list[tuple[str, str]] = []

    def write(prompt, session):
        seen.append((prompt, session))
        (task.meta / "handoff" / actions.DEMO_QUESTION).write_text("Which profile?")

    answering(monkeypatch, write)
    actions.demo(task.id)
    assert seen[0][1] == "", "the first turn starts the conversation"

    def answer(prompt, session):
        seen.append((prompt, session))
        (task.meta / "handoff" / actions.DEMO_QUESTION).unlink(missing_ok=True)
        actions.write_instruction(task, "```bash\nnpm run dev\n```\n")

    answering(monkeypatch, answer)
    result = actions.demo(task.id, reply="the local profile")

    assert seen[1] == ("the local profile", "ses_demo"), "your answer, in the same session"
    assert result.commands == ["npm run dev"] and pod.started == [["npm run dev"]]


def test_documentation_instead_of_an_instruction_is_refused(env, monkeypatch):
    task = back_with_you(a_task())
    monkeypatch.setattr(actions, "task_pod", lambda task_id: FakePod())
    answering(
        monkeypatch,
        lambda prompt, session: actions.write_instruction(task, "# About\n\n" + "prose. " * 600),
    )
    with pytest.raises(Exception, match="documentation, not a note"):
        actions.demo(task.id)


def test_a_slow_start_is_not_called_a_failure(env, monkeypatch):
    """An install or a compile can outlast the wait. The command is still alive, so reporting that
    nothing started sends you debugging an app that is merely still coming up."""
    task = back_with_you(a_task())
    actions.write_instruction(task, "Start it:\n\n```bash\nnpm ci && npm start\n```\n")
    pod = FakePod()  # nothing listening yet
    pod.running = True  # but the command has not exited
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)

    result = actions.demo(task.id, ask=False)
    assert result.starting, "still going, not failed"
    assert not result.listening


def test_a_command_that_exited_without_listening_is_a_failure(env, monkeypatch):
    task = back_with_you(a_task())
    actions.write_instruction(task, "Start it:\n\n```bash\nnpm start\n```\n")
    pod = FakePod()
    pod.running = False  # it exited
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)

    assert not actions.demo(task.id, ask=False).starting


def test_what_the_agent_left_on_the_port_is_stopped_and_said(env, monkeypatch):
    """The app is run once the agent rests, so a server still listening in its container is a
    leftover of its turn, not something to keep: it goes, the app starts from the current code,
    and you are told what went."""
    from vivibox.probe import Listener

    task = back_with_you(a_task())
    actions.write_instruction(task, "Start it:\n\n```bash\npython -m http.server 8000\n```\n")
    pod = FakePod(listening=[Listener(8000, True)])
    pod.leftovers = ["python -m http.server 8000 (port 8000)"]
    monkeypatch.setattr(actions, "task_pod", lambda task_id: pod)

    result = actions.demo(task.id, ask=False)
    assert result.stopped == ["python -m http.server 8000 (port 8000)"]
    assert result.urls == ["http://198.51.100.2:8000"] and pod.started == [["python -m http.server 8000"]]


def committing(task, message="Add a web target to run it in a browser"):
    """A demo turn that commits in the task's clone, as fairy-1's did, and then writes its note."""
    from vivibox import repo

    def write(prompt, session):
        (task.repo / "web.js").write_text(f"// {message}\n")
        repo.git("add", "web.js", cwd=task.repo)
        repo.git("commit", "-q", "-m", message, cwd=task.repo)
        actions.write_instruction(task, "```bash\nnpm run web\n```\n")

    return write


def test_a_commit_made_while_working_out_how_to_run_it_is_said(env, monkeypatch, capsys):
    """fairy-1: the turn that worked out how to run the app added a web target to the project and
    committed it in the task's clone; the writer went on from that commit, a review found the
    target outside the plan, and nobody had said where it came from. The commit is named to you,
    in the result, in the task's timeline and on the command line."""
    from vivibox import repo, timeline

    task = back_with_you(a_task())
    monkeypatch.setattr(actions, "task_pod", lambda task_id: FakePod())
    answering(monkeypatch, committing(task))
    before = repo.git("rev-parse", "HEAD", cwd=task.repo).stdout.strip()

    result = actions.demo(task.id)

    made = repo.git("rev-parse", "--short=7", "HEAD", cwd=task.repo).stdout.strip()
    assert made != before[:7] and result.committed == [made]
    assert f"the agent committed {made} while working out how to run the app" in [
        what for _, what in timeline.entries(task)
    ]
    answering(monkeypatch, committing(task, "Another"))
    (task.meta / "handoff" / actions.DEMO_FILE).unlink()
    assert main(["demo", task.id]) == 1  # nothing listens in the fake pod
    newer = repo.git("rev-parse", "--short=7", "HEAD", cwd=task.repo).stdout.strip()
    out = capsys.readouterr().out
    assert f"While working out how to run the app, the agent committed {newer} in the task's clone" in out
    assert "the work goes on from that commit: look at it before you accept" in out


def test_a_turn_that_commits_nothing_names_no_commit(env, monkeypatch):
    task = back_with_you(a_task())
    monkeypatch.setattr(actions, "task_pod", lambda task_id: FakePod())
    answering(
        monkeypatch, lambda prompt, session: actions.write_instruction(task, "```bash\nnpm run dev\n```\n")
    )
    assert actions.demo(task.id).committed == []
    assert not [e for e in task.events() if e["type"] == "demo_commit"]


def test_a_long_run_of_commits_is_named_by_its_newest_ten_and_counted(env, monkeypatch):
    """The agent can make any number of commits in its turn; the view's notice stays a line."""
    from vivibox import repo

    task = back_with_you(a_task())
    monkeypatch.setattr(actions, "task_pod", lambda task_id: FakePod())

    def many(prompt, session):
        for n in range(13):
            repo.git("commit", "-q", "--allow-empty", "-m", f"Step {n}", cwd=task.repo)
        actions.write_instruction(task, "```bash\nnpm run dev\n```\n")

    answering(monkeypatch, many)
    committed = actions.demo(task.id).committed
    newest = repo.git("rev-list", "--abbrev-commit", "--abbrev=7", "-10", "HEAD", cwd=task.repo).stdout
    newest = newest.split()
    assert committed == [*newest, "and 3 more"]


def failing(task, how):
    """A demo turn that commits in the task's clone and then fails: the turn itself, or with an
    instruction too long to be one."""

    def turn(self, prompt, session="", title="", on_step=None):
        from vivibox import repo

        repo.git("commit", "-q", "--allow-empty", "-m", "Add a web target", cwd=task.repo)
        if how == "turn failed":
            return harness.Turn("ses_demo", False, 0.01, 10, "", error="the provider went away")
        actions.write_instruction(task, "```bash\nnpm run web\n```\n" + "Background. " * 300)
        return harness.Turn("ses_demo", True, 0.01, 10, "done")

    return turn


@pytest.mark.parametrize("how", ["turn failed", "instruction too long"])
def test_a_commit_made_before_the_turn_failed_is_still_said(env, monkeypatch, how):
    """The review of fairy-1's fix: a turn that committed and then failed said only its failure,
    and the work went on from the commit all the same. The commit is named in the error and kept
    in the timeline, once."""
    from vivibox import opencode, repo, timeline

    task = back_with_you(a_task())
    monkeypatch.setattr(actions, "task_pod", lambda task_id: FakePod())
    monkeypatch.setattr(opencode.OpenCode, "turn", failing(task, how))
    before = repo.git("rev-parse", "HEAD", cwd=task.repo).stdout.strip()

    with pytest.raises(actions.PodError) as raised:
        actions.demo(task.id)

    made = repo.git("rev-parse", "--short=7", "HEAD", cwd=task.repo).stdout.strip()
    assert made != before[:7]
    said = str(raised.value)
    assert said.startswith("the agent ")  # what failed comes first
    assert f"\nWhile working out how to run the app, the agent committed {made} in the task's clone" in said
    assert [e["data"]["commits"] for e in task.events() if e["type"] == "demo_commit"] == [[made]]
    assert f"the agent committed {made} while working out how to run the app" in [
        what for _, what in timeline.entries(task)
    ]


def test_a_failed_turn_that_committed_nothing_says_only_its_failure(env, monkeypatch):
    from vivibox import opencode

    task = back_with_you(a_task())
    monkeypatch.setattr(actions, "task_pod", lambda task_id: FakePod())

    def turn(self, prompt, session="", title="", on_step=None):
        return harness.Turn("ses_demo", False, 0.01, 10, "", error="the provider went away")

    monkeypatch.setattr(opencode.OpenCode, "turn", turn)
    with pytest.raises(actions.PodError) as raised:
        actions.demo(task.id)
    assert "committed" not in str(raised.value)
    assert not [e for e in task.events() if e["type"] == "demo_commit"]
