import os
import shlex

import pytest

from vivibox import prompts
from vivibox.cli import main


def test_new_and_status(env, capsys):
    assert main(["new", "demo", "Add health endpoint", "--draft"]) == 0
    assert "Created demo-1" in capsys.readouterr().out

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    # 0/2: the placeholder criterion, plus the standing one about seeing each test fail first.
    assert "demo-1" in out and "not started" in out and "0/2" in out and "Add health endpoint" in out

    assert main(["status", "demo-1"]) == 0
    assert "created" in capsys.readouterr().out


def test_version_names_the_installed_build(capsys):
    """What a bug report starts with: the version from git, a tag or the commit past it."""
    from vivibox import version

    with pytest.raises(SystemExit) as left:
        main(["--version"])
    assert left.value.code == 0
    assert capsys.readouterr().out.strip() == f"vivibox {version.current()}"
    assert version.current() and version.current() != "0+unknown"


def test_the_window_on_a_verification_says_when_it_is_over(env):
    """A window on a log nobody writes to any more looked like a verification that hangs: once
    the task leaves verifying, the log's last lines are shown and a word says it is over, and
    where the logs stay."""
    import io

    from test_tui import implementing

    from vivibox import logs
    from vivibox.states import State

    task = implementing()
    task.transition(State.VERIFY)
    log = task.meta / "log" / "verify-1-120000.log"
    log.write_text("# fresh clone of commit abc\n\n$ npm test\n")
    out = io.StringIO()

    def a_second(seconds: float) -> None:
        # The verification ends while the window is up: the summary, then the state.
        if task.read_state().state is State.VERIFY:
            log.write_text(log.read_text() + "[exit 0 after 3 s]\n# summary\n# npm test: ok after 3 s\n")
            task.transition(State.CHECKPOINT_FINAL)

    logs.follow_verification(task, log, out, sleep=a_second)
    shown = out.getvalue()
    assert "$ npm test" in shown and "# npm test: ok after 3 s" in shown, "the log, to its summary"
    assert shown.rstrip().endswith(logs.VERIFICATION_OVER)
    assert "nothing runs in it" in logs.VERIFICATION_OVER and "`l`" not in logs.VERIFICATION_OVER


def test_unknown_project_fails_cleanly(env, capsys):
    assert main(["new", "missing", "x", "--draft"]) == 1
    assert "missing.toml" in capsys.readouterr().err


def test_project_repo_must_be_git(env, capsys):
    (env / "config" / "projects" / "bad.toml").write_text(f'repo = "{env}"\nverify = ["true"]\n')
    assert main(["new", "bad", "x", "--draft"]) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_status_without_tasks(env, capsys):
    assert main(["status"]) == 0
    assert "No tasks." in capsys.readouterr().out


def test_risky_review_and_approval_move_the_task_on(env, capsys):
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Change the build", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    (task.repo / "pom.xml").write_text("<project/>\n")
    capsys.readouterr()

    assert main(["risky", "demo-1"]) == 0
    assert "+<project/>" in capsys.readouterr().out

    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.APPROVAL_RISKY):
        task.transition(state)
    assert main(["approve-risky", "demo-1"]) == 0
    assert task.read_state().state is State.CHECKPOINT_FINAL
    assert main(["risky", "demo-1"]) == 0
    assert "No changes" in capsys.readouterr().out


def test_accept_and_reply_follow_the_checkpoints(env, capsys):
    from vivibox import gate, supervisor
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Add health endpoint", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    assert main(["accept", "demo-1"]) == 1, "nothing to accept while planning"

    task.transition(State.CHECKPOINT_PLAN)
    assert main(["accept", "demo-1"]) == 1, "the template placeholder is not a criterion"
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "endpoint returns 200"))
    (task.meta / "handoff" / supervisor.QUESTION).write_text("unused")
    assert main(["reply", "demo-1", "Use Postgres, not H2"]) == 0
    assert task.read_state().state is State.PLAN
    assert "Use Postgres, not H2" in (task.meta / "handoff" / "comments.md").read_text()
    assert not (task.meta / "handoff" / supervisor.QUESTION).exists(), "answered questions are archived"
    assert supervisor.next_prompt(task, "") == prompts.PLAN_COMMENT_PROMPT

    task.transition(State.CHECKPOINT_PLAN)
    assert main(["accept", "demo-1"]) == 0
    assert task.read_state().state is State.IMPLEMENT
    assert (task.meta / gate.ACCEPTED_PLAN).exists()

    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)
    (task.repo / "pom.xml").write_text("<project/>")
    assert main(["accept", "demo-1"]) == 1, "unapproved risky files block done"
    assert main(["approve-risky", "demo-1"]) == 0
    assert main(["accept", "demo-1"]) == 0
    assert not task.root.exists(), "accepting the work removes the task"
    assert "is done" in capsys.readouterr().out


def agent_commit(task, name):
    import subprocess

    (task.repo / name).write_text("x\n")
    for args in (
        ["add", name],
        ["-c", "user.name=A", "-c", "user.email=a@b", "commit", "-q", "-m", f"Add {name}"],
    ):
        subprocess.run(["git", *args], cwd=task.repo, check=True, capture_output=True)


def final_checkpoint(task):
    from vivibox import gate
    from vivibox.states import State

    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "done"))
    assert main(["accept", task.id]) == 0
    task.transition(State.VERIFY)
    task.transition(State.CHECKPOINT_FINAL)


def test_accept_puts_the_work_in_your_checkout_and_offers_a_commit(env, capsys, monkeypatch):
    import subprocess

    from vivibox.config import load_config, load_project
    from vivibox.task import find_task

    source = load_project("demo").repo
    git = lambda *a: subprocess.run(["git", *a], cwd=source, capture_output=True, text=True).stdout  # noqa: E731
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Declined: the work waits in your checkout, uncommitted; no branch.
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    agent_commit(task, "one.txt")
    final_checkpoint(task)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    assert main(["accept", "demo-1"]) == 0
    assert (source / "one.txt").exists() and git("status", "--porcelain") == "A  one.txt\n"
    assert prompts[0].startswith("[Y]es"), "the message is shown above the question"
    shown = capsys.readouterr().out
    assert "with this message?\n\n  Goal\n\n  - Add one.txt" in shown, "the task, then every commit"
    assert not task.root.exists() and "vivibox/demo-1" not in git("branch", "--list")
    git("commit", "-q", "-m", "Mine")

    # Accepted with an edited message.
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-2")
    agent_commit(task, "two.txt")
    final_checkpoint(task)
    answers = iter(["e", "Add two", "1"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert main(["accept", "demo-2"]) == 0
    assert git("log", "-1", "--format=%s").strip() == "Add two" and git("status", "--porcelain") == ""
    assert git("log", "-1", "--format=%b").strip() == "- Add two.txt", "the commit listed"
    shown = capsys.readouterr().out
    assert "1  main, where the task started" in shown and "2  a new branch: feature/goal" in shown
    assert git("branch", "--show-current").strip() == "main"

    # --branch keeps the old way, for pull requests.
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-3")
    agent_commit(task, "three.txt")
    final_checkpoint(task)
    assert main(["accept", "demo-3", "--branch"]) == 0
    assert "vivibox/demo-3" in git("branch", "--list") and not (source / "three.txt").exists()

    # Where to commit is a question of its own; on a main branch a new branch is the default.
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-4")
    agent_commit(task, "four.txt")
    final_checkpoint(task)
    answers = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert main(["accept", "demo-4"]) == 0
    assert git("branch", "--show-current").strip() == "feature/goal"
    assert git("log", "-1", "--format=%s").strip() == "Goal"


def test_the_suggested_commit_message_is_a_subject_and_a_list(env):
    """Use actual changes, with further commit subjects as bullets and no acceptance checklist."""
    import subprocess

    from vivibox import actions
    from vivibox.config import load_project

    source = load_project("demo").repo
    git = lambda *a: subprocess.run(["git", *a], cwd=source, capture_output=True, text=True).stdout  # noqa: E731
    base = git("rev-parse", "HEAD").strip()
    for name in ("one.txt", "two.txt"):
        (source / name).write_text("x\n")
        git("add", name)
        git("-c", "user.name=A", "-c", "user.email=a@b", "commit", "-q", "-m", f"Add {name}")
    head = git("rev-parse", "HEAD").strip()
    goal = "Reject expired cards at checkout, so that a card past its date is refused before payment"
    criteria = ["An expired card is refused", "Co-Authored-By: a robot"]

    message = actions.suggested_message(source, base, head, goal, criteria)
    subject, blank, *points = message.splitlines()
    assert subject == "Add two.txt", "a goal too long for a subject: the last commit's"
    assert blank == "" and points == ["- Add one.txt"], "the other changes form the body"

    named = actions.suggested_message(source, base, head, goal, criteria, summary="Refuse expired cards")
    assert named == "Refuse expired cards\n\n- Add one.txt\n- Add two.txt", (
        "the plan's summary names the task, and every commit of the agent's is listed"
    )
    single = actions.suggested_message(source, git("rev-parse", "HEAD~1").strip(), head, goal, criteria)
    assert single == "Add two.txt", "one commit: its subject, without criteria or signature"
    assert actions.suggested_message(source, head, head, "Fix the build.", []) == "Fix the build"


def test_accept_refuses_over_your_staged_changes(env, capsys):
    import subprocess

    from vivibox.config import load_config, load_project
    from vivibox.task import find_task

    source = load_project("demo").repo
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    agent_commit(task, "one.txt")
    final_checkpoint(task)
    (source / "README.md").write_text("staged edit\n")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    assert main(["accept", "demo-1"]) == 1 and "staged changes" in capsys.readouterr().err
    assert task.root.exists(), "nothing is removed when accepting fails"


def test_stop_pauses_and_rm_removes_everything(env, capsys, monkeypatch, tmp_path):
    from vivibox.config import load_config
    from vivibox.task import find_task

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    assert main(["stop", "demo-1"]) == 0
    assert task.read_state().paused
    assert main(["delete", "demo-1", "--yes"]) == 0
    assert not task.root.exists() and "Deleted demo-1" in capsys.readouterr().out
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "No tasks." not in out and "demo-1" in out and "deleted" in out, "the history, as in the view"


def test_a_forced_stop_kills_the_supervisor_and_the_pod_and_says_so(env, capsys, monkeypatch, tmp_path):
    """s asks nicely and waits; --force (S in the view) kills the supervisor's process group and
    the containers, for when the container ignores the stop or the supervisor hangs in docker.
    The turn under way is lost; the task's files are on the host. The task says it was forced."""
    import signal

    from vivibox import actions, secrets
    from vivibox.config import load_config
    from vivibox.task import find_task

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    (task.meta / actions.SUPERVISOR_PID).write_text("4242")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    signals, killed, removed = [], [], []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(actions, "tmux", lambda *a, **k: None)
    monkeypatch.setattr(
        actions, "task_pod", lambda task_id: type("P", (), {"kill": lambda self: killed.append(task_id)})()
    )
    monkeypatch.setattr(secrets, "remove", lambda task_id: removed.append(task_id))

    assert main(["stop", "demo-1", "--force"]) == 0
    assert signals == [(4242, signal.SIGKILL)] and killed == ["demo-1"]
    assert removed == ["demo-1", "demo-1-review"], "the reviewer's key goes with the task's"
    st = task.read_state()
    assert st.paused and st.problem == "stopped by force"
    out = capsys.readouterr().out
    assert "by force" in out and "vivibox start demo-1" in out


def test_rm_asks_first(env, monkeypatch):
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert main(["rm", "demo-1"]) == 1
    assert find_task(load_config().tasks_dir, "demo-1").root.exists()


def test_rm_without_a_terminal_says_how_instead_of_a_traceback(env, monkeypatch, capsys):
    """Run from a script there is nobody to answer: the question ended in an EOFError's traceback.
    It says what happened, why, and the flag that goes without asking; the task stays."""
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    capsys.readouterr()

    def closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert main(["rm", "demo-1"]) == 1
    err = capsys.readouterr().err
    assert "not deleted" in err and "no terminal" in err and "--yes" in err
    assert find_task(load_config().tasks_dir, "demo-1").root.exists()


def test_new_without_the_image_says_so_before_it_creates_a_task(env, monkeypatch, capsys):
    """A task made without the agent image stood as 'could not start', and only then was the
    image named. Checked first, like the project's repository: nothing is created. A draft needs
    no image until it starts."""
    from vivibox import image
    from vivibox.config import load_config
    from vivibox.task import list_tasks

    monkeypatch.setattr(image, "exists", lambda ref, runner=None: False)
    assert main(["new", "demo", "Add health endpoint"]) == 1
    assert "vivibox image build" in capsys.readouterr().err
    assert list(list_tasks(load_config().tasks_dir)) == [], "no task stands as 'could not start'"
    assert main(["new", "demo", "Add health endpoint", "--draft"]) == 0


def test_review_worktree_follows_the_task_and_is_removed_with_it(env, capsys):
    import subprocess

    from vivibox.config import load_config, load_project
    from vivibox.repo import review_worktree_path
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    source = load_project("demo").repo
    agent_commit(task, "one.txt")
    (task.repo / ".idea").mkdir()
    agent_commit(task, ".idea/vcs.xml")
    assert main(["approve-risky", "demo-1"]) == 0
    assert main(["review", "demo-1"]) == 0
    copy = review_worktree_path(source, task.root)
    # The test repository is called "repo", like the agent's clone, so the copy gets another name.
    assert copy == task.root / "repo-review" and copy != task.repo
    assert (copy / "one.txt").exists()
    git = lambda *a: subprocess.run(["git", *a], cwd=copy, capture_output=True, text=True).stdout  # noqa: E731
    assert git("status", "--porcelain") == "A  .idea/vcs.xml\nA  one.txt\n", "the agent's work is uncommitted"
    assert git("rev-parse", "HEAD").strip() == task.read_state().base_commit
    assert "vivibox/demo-1" not in git("branch", "--list"), "no branch in your repository before accept"

    agent_commit(task, "two.txt")
    (copy / ".idea" / "workspace.xml").write_text("<project/>")
    (copy / ".idea" / "vcs.xml").write_text("rewritten by the IDE")
    assert "A  .idea/vcs.xml" in git("status", "--porcelain").splitlines(), "IDE rewrites stay hidden"
    assert main(["review", "demo-1"]) == 0, "a second review moves the copy; IDE files do not block it"
    assert (copy / "two.txt").exists()

    (copy / "Mine.java").write_text("class Mine {}")
    assert main(["review", "demo-1"]) == 1, "your own changes in the copy are never overwritten"
    (copy / "Mine.java").unlink()

    (task.repo / "pom.xml").write_text("<project/>")
    assert main(["review", "demo-1"]) == 1
    assert "not approved" in capsys.readouterr().err

    assert main(["rm", "demo-1", "--yes"]) == 0
    assert not copy.exists()
    worktrees = subprocess.run(["git", "worktree", "list"], cwd=source, capture_output=True, text=True).stdout
    assert str(copy) not in worktrees, "git forgets the review copy too"


def test_leaving_the_agent_view_says_nothing_to_your_scrollback(monkeypatch):
    """tmux prints '[detached (from session ...)]' after leaving its own screen, so the line lands
    on the normal one and is still there after vivibox closes. '-E true' replaces the exiting
    client with a command that does nothing, and tmux reports nothing."""
    from types import SimpleNamespace

    from vivibox import actions

    calls = fake_tmux(monkeypatch, shows="echo hi")
    task = SimpleNamespace(id="demo-1")

    monkeypatch.setattr(actions, "tmux_has", lambda target: False)
    actions.agent_view(task, ["echo", "hi"])
    binding = next(c for c in calls if c[0] == "bind-key")
    assert binding[-2:] == ["-E", "true"], binding

    # The keys belong to the server, which outlives any one session, so an old binding has to be
    # replaced even when there is nothing to create.
    calls.clear()
    monkeypatch.setattr(actions, "tmux_has", lambda target: True)
    actions.agent_view(task, ["echo", "hi"])
    assert [c for c in calls if c[0] != "show-environment"] == [actions.LEAVE_BINDING], calls


def fake_tmux(monkeypatch, shows: str) -> list[list[str]]:
    """Records tmux calls; the session, when asked, says it shows `shows`."""
    import subprocess

    from vivibox import actions

    calls: list[list[str]] = []

    def tmux(*a, **kw):
        calls.append(list(a))
        out = f"{actions.SHOWS}={shows}\n" if a[0] == "show-environment" else ""
        return subprocess.CompletedProcess(list(a), 0, out, "")

    monkeypatch.setattr(actions, "tmux", tmux)
    return calls


def test_the_agent_view_follows_the_agent_from_the_planner_to_the_writer(monkeypatch):
    """The planner's conversation and the writer's are two. A window opened while the planner
    worked would otherwise go on showing it for the whole task: w in implementation showed the
    plan being written, not the code."""
    from types import SimpleNamespace

    from vivibox import actions

    planner = ["opencode", "attach", "--session", "ses_planner"]
    writer = ["opencode", "attach", "--session", "ses_writer"]
    calls = fake_tmux(monkeypatch, shows=shlex.join(planner))
    monkeypatch.setattr(actions, "tmux_has", lambda target: True)
    actions.agent_view(SimpleNamespace(id="demo-1"), writer)
    kinds = [c[0] for c in calls]
    assert kinds.index("kill-session") < kinds.index("new-session"), "the planner's window makes way"
    created = next(c for c in calls if c[0] == "new-session")
    assert created[-1] == shlex.join(writer)
    assert ["set-environment", "-t", "vivibox-demo-1", actions.SHOWS, shlex.join(writer)] in calls


def test_w_shows_the_verification_while_it_runs_and_the_agent_otherwise(env, monkeypatch):
    """Pressing w during a verification landed in the writer's last conversation, where nothing
    was happening: the verification is not an agent's turn. Its log is what there is to see."""
    from test_tui import implementing

    from vivibox import actions
    from vivibox.pod import PodError
    from vivibox.states import State

    task = implementing()
    task.set_session("writer", "ses_writer")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    monkeypatch.setattr(actions, "tmux_has", lambda target: False)
    calls = fake_tmux(monkeypatch, shows="")
    task.transition(State.VERIFY)
    with pytest.raises(PodError, match="try again in a moment"):
        actions.attach_command(task.id)
    log = task.meta / "log" / "verify-1-120000.log"
    log.write_text("# fresh clone of commit abc\n\n$ npm test\n")
    assert actions.attach_command(task.id)[-2:] == ["-t", f"vivibox-{task.id}"]
    created = next(c for c in calls if c[0] == "new-session")
    assert created[-1] == shlex.join(["vivibox", "follow-verification", task.id, str(log)])
    # The agents' conversations stay a name away while the verification runs.
    task.set_session("planner", "ses_planner")
    assert "ses_planner" in actions.view_command(task, task.read_state(), "planner")[-1]
    calls.clear()
    task.transition(State.IMPLEMENT)
    actions.attach_command(task.id)
    created = next(c for c in calls if c[0] == "new-session")
    assert "opencode attach" in created[-1] and "ses_writer" in created[-1], "back to the agent"


def test_an_old_log_is_not_the_verification_under_way(env):
    """Verified again after a failure: until the gate opens this verification's log, w has the
    log of the failed one to show, which is not what is happening now."""
    from test_tui import implementing

    from vivibox import actions
    from vivibox.states import State

    task = implementing()
    task.transition(State.VERIFY)
    old = task.meta / "log" / "verify-1-120000.log"
    old.write_text("$ npm test\n[exit 1]\n")
    stale = old.stat().st_mtime - 60
    os.utime(old, (stale, stale))
    task.transition(State.CHECKPOINT_BLOCKED)
    task.transition(State.VERIFY)
    assert actions.verification_log(task) is None
    new = task.meta / "log" / "verify-1-120100.log"
    new.write_text("# fresh clone of commit abc\n")
    assert actions.verification_log(task) == new


def test_a_task_overrides_the_configured_model_everywhere_or_nowhere(env):
    """A model chosen for one task has to reach the harness and the opencode config alike. Applying
    in one place and not the other would run the turn on one model and bill the other."""
    from vivibox import actions
    from vivibox.config import load_config

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task, _ = actions.load("demo-1")
    config = load_config()

    assert actions.role_of(task, "writer", config).model == "m", "the config decides by default"
    task.set_model("writer", "deepseek/deepseek-v4-reasoner")
    assert actions.role_of(task, "writer", config).model == "deepseek/deepseek-v4-reasoner"
    assert actions.writer(config, task)[1] == "deepseek/deepseek-v4-reasoner", "and start uses it"
    assert actions.role_of(task, "planner", config).model == "m", "one role at a time"

    task.set_model("writer", "")
    assert actions.role_of(task, "writer", config).model == "m", "cleared gives it back"


def test_the_models_offered_are_the_ones_you_configured(env):
    """Asking a provider for its list means running a container on a keypress."""
    from vivibox import actions

    assert actions.models_offered() == ["m"], "both roles name the same model, offered once"


class FakeImage:
    """The image module as far as starting the view uses it."""

    def __init__(self, monkeypatch, built: bool, failing: tuple[str, ...] = ()):
        from vivibox import cli, image, tui

        self.built = built
        self.builds = 0
        self.opened = False
        self.cleaned: list[str] = []
        monkeypatch.setattr(image, "image_ref", lambda: "vivibox-agent:abc")
        # Never the real one: it would remove the agent images on the machine running the tests.
        monkeypatch.setattr(image, "remove_old", lambda ref: self.cleaned.append(ref) or [])
        monkeypatch.setattr(image, "exists", lambda ref: self.built)
        monkeypatch.setattr(image, "build", self.build)
        checks = [(image.Check(name, "", ""), name not in failing, "") for name in ("node", "java")]
        monkeypatch.setattr(image, "run_checks", lambda ref: checks)
        monkeypatch.setattr(tui, "run", self.open)
        self.main = cli.main

    def build(self):
        self.builds += 1
        self.built = True
        return "vivibox-agent:abc", True

    def open(self):
        self.opened = True
        return 0


def test_the_view_builds_the_image_it_needs_first(env, monkeypatch, capsys):
    fake = FakeImage(monkeypatch, built=False)
    assert fake.main([]) == 0
    assert fake.builds == 1 and fake.opened
    assert "Building the agent image vivibox-agent:abc" in capsys.readouterr().out


def test_the_view_opens_at_once_with_the_image_built(env, monkeypatch, capsys):
    fake = FakeImage(monkeypatch, built=True)
    assert fake.main([]) == 0
    assert fake.builds == 0 and fake.opened
    assert capsys.readouterr().out == ""
    assert fake.cleaned == ["vivibox-agent:abc"], "older images go, the current one is kept"


def test_the_view_does_not_open_on_an_image_that_fails_its_checks(env, monkeypatch, capsys):
    fake = FakeImage(monkeypatch, built=False, failing=("java",))
    assert fake.main([]) == 1
    assert not fake.opened
    assert "fails its checks (java); see: vivibox image check" in capsys.readouterr().err


@pytest.mark.real_start
def test_a_task_does_not_start_without_the_variables_its_project_passes(env, monkeypatch, tmp_path):
    from vivibox import actions, image
    from vivibox.pod import Pod, PodError

    project = env / "config" / "projects" / "demo.toml"
    project.write_text(project.read_text() + 'pass_env = ["REPO_TOKEN"]\n')
    monkeypatch.delenv("REPO_TOKEN", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(Pod, "up", lambda self: pytest.fail("the pod started without REPO_TOKEN"))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    with pytest.raises(PodError, match="REPO_TOKEN not set.*pass_env"):
        actions.start("demo-1")
    # The reason stays with the task: a toast that is gone in ten seconds left the row saying
    # "not started" with nothing to say why.
    from vivibox.config import load_config
    from vivibox.panel import detail
    from vivibox.task import find_task

    task = find_task(load_config().tasks_dir, "demo-1")
    shown = detail(task, task.read_state(), 3, running=False)
    assert "could not start" in shown.lower() and "REPO_TOKEN not set" in shown
    assert main(["status", "demo-1"]) == 0


def test_a_decision_starts_a_task_nobody_is_working_on(env, capsys):
    from vivibox import actions, gate
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))
    capsys.readouterr()
    assert main(["accept", "demo-1"]) == 0
    assert actions.started == ["demo-1"], "no supervisor was running, e.g. after a reboot"
    out = capsys.readouterr().out
    assert "Plan accepted" in out and "Started demo-1" in out


def test_a_decision_leaves_a_running_task_to_its_supervisor(env, capsys, monkeypatch):
    from vivibox import actions, gate
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    task.transition(State.CHECKPOINT_PLAN)
    task.plan_path.write_text(task.plan_path.read_text().replace(gate.PLACEHOLDER, "it works"))
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    assert main(["accept", "demo-1"]) == 0
    assert actions.started == []


def test_status_calls_a_task_what_the_view_calls_it(env, capsys):
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    assert "not started" in capsys.readouterr().out, "not 'planning': nobody is"
    task = find_task(load_config().tasks_dir, "demo-1")
    for state in (State.CHECKPOINT_PLAN, State.IMPLEMENT):
        task.transition(state)
    task.event("started", model="m")
    task.set_paused(True, problem="agent turn failed: 429 Too Many Requests")
    assert main(["status"]) == 0
    assert "agent turn failed" in capsys.readouterr().out
    assert main(["status", "demo-1"]) == 0
    out = capsys.readouterr().out
    assert "agent turn failed" in out and "429 Too Many Requests" in out
    assert "vivibox start demo-1" in out


def test_verify_again_from_the_shell_starts_a_task_nobody_runs(env, capsys):
    from vivibox import actions
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_BLOCKED):
        task.transition(s)
    capsys.readouterr()
    assert main(["verify-again", "demo-1"]) == 0
    out = capsys.readouterr().out
    assert task.read_state().state is State.VERIFY and actions.started == ["demo-1"]
    assert "Verifying demo-1 again" in out
    assert main(["status", "demo-1"]) == 0


def test_a_blocked_task_offers_verify_again_next(env, capsys):
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    for s in (State.CHECKPOINT_PLAN, State.IMPLEMENT, State.VERIFY, State.CHECKPOINT_BLOCKED):
        task.transition(s)
    capsys.readouterr()
    assert main(["status", "demo-1"]) == 0
    assert "vivibox verify-again demo-1" in capsys.readouterr().out


def test_a_supervisor_leaves_its_pid_and_its_code_with_the_task(env):
    import os

    from vivibox import actions, code
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    actions.supervising(task)
    assert (task.meta / actions.SUPERVISOR_PID).read_text() == str(os.getpid())
    assert (task.meta / code.RECORD).read_text().strip() == code.signature()


def test_new_says_when_a_mentioned_file_is_not_what_the_agent_will_see(env, capsys, tmp_path):
    import subprocess

    repo = env / "repo"
    (repo / "a.txt").write_text("1\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Add a"], cwd=repo, check=True)
    (repo / "a.txt").write_text("2\n")
    assert main(["new", "demo", "Change @a.txt", "--draft"]) == 0
    out = capsys.readouterr().out
    assert "a.txt has uncommitted changes; the agent sees the committed version" in out


def test_an_accepted_task_leaves_its_plan_and_events_in_the_archive(env, capsys, monkeypatch):
    from vivibox import actions
    from vivibox.config import load_config
    from vivibox.task import find_task

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    agent_commit(task, "one.txt")
    final_checkpoint(task)
    (task.meta / "log" / "verify-1-120000.log").write_text("$ true\n[exit 0 after 1 s]\n")
    (task.meta / "log" / "supervisor.log").write_text("Supervising demo-1\n")
    (task.meta / "handoff" / "review-1.md").write_text("## Blocking\n\n## Not blocking\n")
    assert main(["accept", "demo-1"]) == 0
    kept = actions.archive_path("demo-1")
    assert not task.root.exists() and kept.is_dir()
    assert (kept / "plan.accepted.md").exists() and (kept / "events.jsonl").exists()
    assert (kept / "criteria.md").exists()
    assert not (kept / "repo").exists(), "the plan and the record, not the clone"
    assert '"type": "state"' in (kept / "events.jsonl").read_text()
    # Its logs and the reviews it got go with it, and the timeline as text: once the task is
    # done there was no way to read what its verifications and its reviewer said.
    assert (kept / "log" / "verify-1-120000.log").read_text() == "$ true\n[exit 0 after 1 s]\n"
    assert (kept / "log" / "supervisor.log").exists() and (kept / "review-1.md").exists()
    assert (kept / "timeline.txt").read_text().startswith("# demo-1: Goal\n")


def test_a_deleted_task_is_archived_too_and_forgetting_it_removes_the_archive(env, capsys):
    from vivibox import actions

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    assert main(["rm", "demo-1", "--yes"]) == 0
    kept = actions.archive_path("demo-1")
    assert (kept / "plan.md").exists() and (kept / "events.jsonl").exists()
    actions.forget("demo-1")
    assert not kept.exists() and actions.history() == []


def test_start_carries_a_started_task_on_and_resume_is_its_other_name(env, capsys, monkeypatch):
    from vivibox import actions
    from vivibox.config import load_config
    from vivibox.task import find_task

    calls = []
    monkeypatch.setattr(actions, "start", lambda task_id, resume=False: calls.append(resume) or "m")
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    assert main(["start", "demo-1"]) == 0
    task.event("started", model="m")
    assert main(["start", "demo-1"]) == 0
    assert main(["resume", "demo-1"]) == 0
    assert calls == [False, True, True], "a task started before goes on from where it was"


def test_stop_points_at_start(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    capsys.readouterr()
    assert main(["stop", "demo-1"]) == 0
    out = capsys.readouterr().out
    assert "vivibox start demo-1" in out and "resume" not in out


def test_status_lists_finished_tasks_under_the_live_ones(env, capsys):
    from vivibox import actions
    from vivibox.task import now

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    actions.history_path().parent.mkdir(parents=True, exist_ok=True)
    actions.history_path().write_text(
        '{"id": "demo-0", "project": "demo", "title": "Old one", "cost": 0.1, "commit": "abc", "branch": "",'
        ' "conflicts": [], "finished": "' + now() + '"}\n'
    )
    capsys.readouterr()
    assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[1:]] == ["demo-1", "demo-0"]
    assert "done" in lines[2] and "Old one" in lines[2] and "$0.10" in lines[2]


def test_accepting_a_plan_that_names_a_command_settles_nothing_for_the_project(env, capsys):
    from vivibox import actions
    from vivibox.states import State

    actions.setup_project(env / "clicker", "clicker", [], create=True)
    task = actions.create("clicker", "A click counter page")
    plan = '+++\nverify = ["npm test"]\n+++\n\n# Goal\n\n## Acceptance criteria\n\n- [ ] it counts\n'
    task.plan_path.write_text(plan)
    task.transition(State.CHECKPOINT_PLAN)
    assert main(["accept", task.id]) == 0
    out = capsys.readouterr().out
    assert "Plan accepted" in out and "from now on" not in out
    from vivibox.config import load_project

    assert load_project("clicker").verify == [], "the writer proposes it, for you to accept after the work"


@pytest.mark.real_start
def test_a_start_says_which_step_it_is_at(env, monkeypatch, tmp_path):
    """Starting the pod, installing a JDK, starting opencode: minutes on a slow day, and a row that
    only said "starting…" looked stuck."""
    from vivibox import actions, image, opencode, secrets
    from vivibox.pod import Pod, PodError

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(secrets, "prepare", lambda task_id, keys: None)
    monkeypatch.setattr(opencode, "prepare", lambda task, model, verify, used: False)
    monkeypatch.setattr(opencode, "provider_of", lambda model: "p")

    def away(self):
        raise PodError("docker is away")

    monkeypatch.setattr(Pod, "up", away)
    assert main(["new", "demo", "Goal", "--draft"]) == 0
    steps = []
    with pytest.raises(PodError, match="docker is away"):
        actions.start("demo-1", on_step=steps.append)
    assert steps == ["starting the pod…"], "said before the step, which is the one that took long"


def test_attach_shows_the_conversation_of_the_role_you_ask_for(env, monkeypatch):
    from test_tui import implementing

    from vivibox import actions
    from vivibox.states import State

    task = implementing()
    task.set_session("planner", "ses_planner")
    task.set_session("writer", "ses_writer")
    monkeypatch.setattr(actions, "supervisor_running", lambda t: True)
    monkeypatch.setattr(actions, "tmux_has", lambda target: False)
    calls = fake_tmux(monkeypatch, shows="")
    assert actions.watchable_sessions(task) == [("writer", "ses_writer"), ("planner", "ses_planner")]
    actions.attach_command(task.id)
    created = next(c for c in calls if c[0] == "new-session")
    assert "ses_writer" in created[-1], "the writer's, at work now, unless you say otherwise"
    calls.clear()
    actions.attach_command(task.id, role="planner")
    created = next(c for c in calls if c[0] == "new-session")
    assert "ses_planner" in created[-1]
    task.transition(State.VERIFY)
    (task.meta / "log" / "verify-1-120000.log").write_text("# fresh clone of commit abc\n")
    calls.clear()
    actions.attach_command(task.id, role="planner")
    created = next(c for c in calls if c[0] == "new-session")
    assert "ses_planner" in created[-1], "asked for by name, even while the verification runs"


def test_timeline_prints_what_happened_one_line_each(env, capsys):
    from vivibox.config import load_config
    from vivibox.states import State
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    task.event("turn_started", state="plan", role="planner")
    task.event("turn", state="plan", role="planner", ok=True, cost=0.1234, tokens=1200)
    task.transition(State.CHECKPOINT_PLAN, reason="plan ready")
    capsys.readouterr()
    assert main(["timeline", "demo-1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# demo-1: Goal" and out[2].endswith("created: Goal")
    assert any(line.endswith("planner turn: $0.12, 1200 tokens, 0 s") for line in out)
    assert out[-1].endswith("→ review the plan (plan ready)")


@pytest.mark.real_start
def test_one_start_or_stop_of_a_task_at_a_time_from_however_many_views(env, monkeypatch):
    """Two views on one machine: a start from each would make the same pod twice, and Docker
    refuses the second container by its name. The second is told, and leaves the task as it is;
    a stop by force is for exactly a start or a stop that hangs, so it is never held back."""
    import fcntl

    from vivibox import actions
    from vivibox.config import load_config
    from vivibox.pod import PodError
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    killed = []
    monkeypatch.setattr(actions, "task_pod", lambda task_id: type("P", (), {
        "kill": lambda self: killed.append(task_id), "down": lambda self: None})())  # fmt: skip
    monkeypatch.setattr(actions.secrets, "remove", lambda task_id: None)
    with (task.meta / actions.START_LOCK).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(PodError, match="under way elsewhere"):
            actions.start("demo-1")
        assert task.read_state().problem == "", "the other view's start is not a failed one"
        with pytest.raises(PodError, match="under way elsewhere"):
            actions.stop(task)
        actions.stop(task, force=True)
        assert killed == ["demo-1"]


def test_accept_keeps_the_writers_command_for_the_project_and_verifies(env, capsys):
    from test_tui import at_command_checkpoint

    from vivibox.config import load_project
    from vivibox.states import State

    task = at_command_checkpoint(env)
    assert main(["accept", task.id]) == 0
    out = capsys.readouterr().out
    assert "demo is verified with `npm ci && npm test` from now on" in out
    assert load_project("demo").verify == ["npm ci && npm test"] and task.read_state().state is State.VERIFY
    task = at_command_checkpoint(env, command="")
    assert main(["accept", task.id]) == 1
    err = capsys.readouterr().err
    assert "no command came from the writer" in err and "--verify" in err
    assert main(["accept", task.id, "--verify", "npm test"]) == 0
    assert load_project("demo").verify == ["npm test"] and task.read_state().state is State.VERIFY


def test_new_takes_a_task_with_nothing_to_build(env):
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Analyse PAY-123", "--no-build", "--draft"]) == 0
    assert "verify = false" in find_task(load_config().tasks_dir, "demo-1").plan_path.read_text()


def test_new_takes_the_tasks_orchestration_mode(env):
    """As the Flow row in n: this task's mode; the other tasks keep config.toml's."""
    from vivibox.config import load_config
    from vivibox.orchestration import mode_of
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--flow", "single_agent", "--draft"]) == 0
    assert main(["new", "demo", "Other", "--draft"]) == 0
    config = load_config()
    assert mode_of(find_task(config.tasks_dir, "demo-1"), config).name == "single_agent"
    assert mode_of(find_task(config.tasks_dir, "demo-2"), config).name == config.orchestration


def test_attach_opens_the_agents_window_and_closes_it_when_you_leave(env, monkeypatch):
    from vivibox import actions, cli
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    monkeypatch.setattr(
        actions, "attach_command", lambda task_id, role="": ["tmux", "-L", "vivibox", "attach"]
    )
    ran = {}
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kw: ran.update(command=command, **kw))
    calls = fake_tmux(monkeypatch, shows="")
    monkeypatch.setattr(cli, "on_a_terminal", lambda: True)
    assert main(["attach", task.id]) == 0
    assert ran["command"][0] == "tmux" and "TMUX" not in ran["env"]
    assert ["kill-session", "-t", f"vivibox-{task.id}"] in calls


def test_attach_without_a_terminal_refuses_before_it_opens_the_agents_window(env, monkeypatch, capsys):
    """Run from a script, tmux could not attach and left opencode's window running in the pod."""
    from vivibox import actions, cli
    from vivibox.config import load_config
    from vivibox.task import find_task

    assert main(["new", "demo", "Goal", "--draft"]) == 0
    task = find_task(load_config().tasks_dir, "demo-1")
    opened = []
    monkeypatch.setattr(actions, "attach_command", lambda task_id, role="": opened.append(task_id))
    monkeypatch.setattr(cli, "on_a_terminal", lambda: False)
    assert main(["attach", task.id]) == 1
    assert opened == []
    assert "needs a terminal" in capsys.readouterr().err


@pytest.mark.real_start
def test_a_start_names_the_model_that_writes_in_the_tasks_mode(env, monkeypatch, tmp_path, capsys):
    """single_agent writes on the planner's model (P+W+R): "Started … (the writer's model)" named
    one that never ran."""
    from vivibox import actions, image, opencode, secrets, supervisor
    from vivibox.pod import Pod

    config = env / "config" / "config.toml"
    config.write_text(
        config.read_text()
        .replace('model = "m"', 'model = "p/pro"', 1)
        .replace('model = "m"', 'model = "p/flash"', 1)
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(image, "exists", lambda ref: True)
    monkeypatch.setattr(secrets, "prepare", lambda task_id, keys: None)
    monkeypatch.setattr(opencode, "prepare", lambda task, model, verify, used: False)
    monkeypatch.setattr(actions, "model_missing", lambda *a: "")
    monkeypatch.setattr(actions, "available_models", lambda refresh=False: [])
    monkeypatch.setattr(Pod, "up", lambda self: None)
    monkeypatch.setattr(actions.toolchain, "ensure", lambda *a, **kw: None)
    monkeypatch.setattr(actions.prepare, "begin", lambda *a: None)
    monkeypatch.setattr(opencode.OpenCode, "ensure_server", lambda self: None)
    monkeypatch.setattr(opencode.OpenCode, "session_exists", lambda self, s: True)
    monkeypatch.setattr(supervisor, "set_next_prompt", lambda *a: None)
    assert main(["new", "demo", "Goal", "--flow", "single_agent", "--draft"]) == 0
    assert actions.start("demo-1", supervise=False) == "p/pro"
    assert main(["new", "demo", "Other", "--draft"]) == 0
    assert actions.start("demo-2", supervise=False) == "p/flash"
