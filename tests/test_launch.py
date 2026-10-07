"""The shared launch (core.ensure): two-phase start, a record saved before every side effect, and
recovery that resumes, adopts or refuses without ever sending the brief twice on a guess."""

import copy
import json

import pytest

import muster.core as core
from tests.fake_herdr import World

DESTRUCTIVE = ("reset", "clean", "stash", "checkout", "branch", "worktree remove", "push")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)

    def run(argv):
        out = w.handle(argv)
        if out is None:
            raise AssertionError(argv)
        return out
    monkeypatch.setattr(core, "run", run)
    w.clone = tmp_path / "clone"
    w.clone.mkdir()
    w.dir = tmp_path / "launch"
    w.on_submit = lambda text: core.prompt_seen(w.dir, {"prompt": text, "session_id": "s1"})
    w.saved = []
    return w


def new_rec(w, **change):
    rec = core.plan("t_card", "o/r", w.clone, "fix/x", "main", "label", "run-x", "opus",
                         w.dir / "settings.json", "run", ["GH_TOKEN="])
    rec.update(change)
    return rec


def ensure(w, rec, brief="Do the thing.\n", known=None):
    def save(r):
        w.saved.append(copy.deepcopy(r))
        w.calls.append({"saved": copy.deepcopy(r)})
    save(rec)  # as both callers do: the record exists before ensure runs
    try:
        return core.ensure(rec, save, w.dir, lambda r, g: brief, known)
    except Exception:
        save(rec)  # as both callers do on a failure; a crash (KeyboardInterrupt) saves nothing
        raise


def last(w):
    """The record as the disk holds it: what a process started after a crash reads back."""
    return copy.deepcopy(w.saved[-1])


def herdr_calls(w, *verb):
    return [c for c in w.calls if isinstance(c, list) and c[1:1 + len(verb)] == list(verb) and c[0] == "herdr"]


def saved_before(w, call):
    """The record last saved before this command ran."""
    i = next(i for i, c in enumerate(w.calls) if c is call)
    return next(c["saved"] for c in reversed(w.calls[:i]) if isinstance(c, dict))


# --- the two phases ---------------------------------------------------------------------------


def test_start_without_a_task_waits_for_idle_then_submits_the_brief_and_sees_it_working(world):
    rec = new_rec(world)
    assert ensure(world, rec) == "working"
    [start] = herdr_calls(world, "agent", "start")
    tail = start[start.index("--") + 1:]
    assert tail == ["--model", "opus", "--permission-mode", "auto", "--settings", str(world.dir / "settings.json")]
    assert not any("bypass" in a or "dangerously" in a for a in start)
    [prompt] = herdr_calls(world, "agent", "prompt")
    assert prompt[3] == rec["pane"] and prompt[5:] == ["--wait", "--until", "working", "--until", "blocked",
                                                      "--timeout", "30000"]
    assert json.loads(prompt[4].split("Task brief (JSON): ", 1)[1]) == "Do the thing.\n"
    assert world.calls.index(start) < world.calls.index(prompt)
    assert last(world)["prompt"]["state"] == "working" and last(world)["step"] == "done"


def test_every_side_effect_is_preceded_by_a_saved_record_of_it(world):
    ensure(world, new_rec(world))
    assert saved_before(world, herdr_calls(world, "worktree", "create")[0])["step"] == "worktree"
    assert saved_before(world, herdr_calls(world, "tab", "create")[0])["step"] == "tab"
    assert saved_before(world, herdr_calls(world, "agent", "start")[0])["step"] == "agent"
    before = saved_before(world, herdr_calls(world, "agent", "prompt")[0])
    assert before["step"] == "prompt" and before["prompt"]["state"] == "sending"
    assert before["sha256"] and before["version"] == core.LAUNCH_VERSION


@pytest.mark.parametrize("text", ["line one\nline two\r\n", "tabs\tquotes ' \" ` $() ; \\ é →",
                                  "".join(chr(i) for i in range(128))])
def test_raw_control_characters_reach_claude_losslessly_on_one_line(world, text):
    ensure(world, new_rec(world), brief=text)
    sent = herdr_calls(world, "agent", "prompt")[0][4]
    assert not any(ord(c) < 32 or ord(c) == 127 for c in sent)
    assert sent.startswith("Execute the authorized task")
    assert json.loads(sent.split("Task brief (JSON): ", 1)[1]) == text


def test_a_start_that_times_out_with_claude_idle_still_proceeds_once(world):
    world.start = "timeout-idle"
    assert ensure(world, new_rec(world)) == "working"
    assert len(herdr_calls(world, "agent", "start")) == 1 and len(world.submitted) == 1
    assert any("agent start" in e for e in last(world)["evidence"])


def test_a_start_with_no_agent_is_a_readiness_failure(world):
    world.start = "none"
    with pytest.raises(core.LaunchFailure, match="Claude did not start") as caught:
        ensure(world, new_rec(world))
    assert caught.value.kind == "start" and world.submitted == []
    assert "did not start" in core.trouble("agent", caught.value)


def test_a_dialog_at_startup_is_not_called_did_not_start_and_resumes_after_it_is_answered(world):
    world.start = "blocked"
    with pytest.raises(core.LaunchFailure, match="shows a dialog") as caught:
        ensure(world, new_rec(world))
    assert caught.value.kind == "readiness"
    assert "did not start" not in core.trouble("agent", caught.value)
    assert "did not become ready" in core.trouble("agent", caught.value)
    world.agents[last(world)["pane"]]["agent_status"] = "idle"  # the human answered it in the pane
    assert ensure(world, last(world)) == "working"
    assert len(herdr_calls(world, "agent", "start")) == 1 and len(world.submitted) == 1


# --- the prompt's outcome ---------------------------------------------------------------------


def test_a_prompt_that_opens_a_dialog_is_delivered_and_blocked(world):
    world.prompt = "blocked"
    assert ensure(world, new_rec(world)) == "blocked"


def test_a_stalled_prompt_with_claude_working_is_delivered_not_resent(world):
    world.prompt = "stalled-working"
    assert ensure(world, new_rec(world)) == "working"
    assert len(world.submitted) == 1


def test_a_turn_that_finished_before_herdr_saw_it_work_is_done(world):
    world.prompt, world.on_submit = "stalled-done", None  # completion_seq is the only evidence
    assert ensure(world, new_rec(world)) == "done"


def test_a_timeout_with_the_hook_evidence_is_delivered(world):
    world.prompt = "timeout"
    assert ensure(world, new_rec(world)) == "done"  # the hook saw the exact bytes; Claude is idle again
    assert len(world.submitted) == 1


def test_an_unconfirmed_prompt_is_unknown_never_resent_without_a_person(world):
    world.prompt = "lost"  # no hook, no state change: herdr cannot say whether it arrived
    with pytest.raises(core.LaunchFailure, match="could not confirm the brief") as caught:
        ensure(world, new_rec(world))
    assert caught.value.kind == "delivery-unknown"
    assert "did not start" not in core.trouble("prompt", caught.value)
    for _ in range(2):
        with pytest.raises(core.LaunchFailure, match="may or may not"):
            ensure(world, last(world))
    assert len(world.submitted) == 1
    world.prompt = "working"
    assert ensure(world, {**last(world), "resend": True}) == "working"
    assert len(world.submitted) == 2 and "resend" not in last(world)


def test_a_prompt_refused_by_a_dialog_was_not_sent_and_is_sent_once_it_is_answered(world):
    world.prompt = "agent_blocked"
    with pytest.raises(core.LaunchFailure, match="was not sent") as caught:
        ensure(world, new_rec(world))
    assert caught.value.kind == "readiness" and last(world)["prompt"]["state"] == "not-sent"
    world.prompt = "working"
    assert ensure(world, last(world)) == "working" and len(world.submitted) == 1


def test_a_delivered_launch_does_nothing_more(world):
    ensure(world, new_rec(world))
    before = len(world.calls)
    assert ensure(world, last(world)) == "working"
    assert len(world.calls) == before + 1  # only the save-free return: no herdr call at all
    assert len(world.submitted) == 1


# --- crash at every boundary, then a retry from the saved record -----------------------------


BOUNDARIES = [("worktree", "create"), ("tab", "create"), ("agent", "start"), ("agent", "prompt")]


@pytest.mark.parametrize("verb", BOUNDARIES)
@pytest.mark.parametrize("after", [False, True])
def test_a_crash_at_each_boundary_resumes_to_one_worktree_one_agent_one_brief(world, verb, after):
    def at(argv):
        return isinstance(argv, list) and argv[:3] == ["herdr", *verb]
    if after:
        world.crash_after = at
    else:
        world.crash = at
    with pytest.raises(KeyboardInterrupt):
        ensure(world, new_rec(world))
    world.crash = None
    if verb == ("agent", "prompt") and not after:
        # Killed after the record said "sending": nothing shows whether the brief went out, so a person
        # decides. (Had it gone out, the hook's sha256 or Claude's state would show it.)
        with pytest.raises(core.LaunchFailure) as caught:
            ensure(world, last(world))
        assert caught.value.kind == "delivery-unknown" and world.submitted == []
        last(world)["resend"] = True
        world.saved[-1]["resend"] = True
    state = ensure(world, last(world))
    assert state in ("working", "done")
    assert len(herdr_calls(world, "worktree", "create")) == 1
    assert len(world.agents) == 1 and len(herdr_calls(world, "agent", "start")) == 1
    assert len(world.submitted) == 1
    # ponytail's known corner: a crash right after `tab create` leaves one empty tab behind.
    assert len(herdr_calls(world, "tab", "create")) == (2 if verb == ("tab", "create") and after else 1)


def test_a_crash_after_sending_with_no_evidence_waits_for_a_person(world):
    world.on_submit, world.crash_after = None, lambda a: a[:3] == ["herdr", "agent", "prompt"]
    with pytest.raises(KeyboardInterrupt):
        ensure(world, new_rec(world))
    world.agents[last(world)["pane"]]["agent_status"] = "idle"  # and no hook, no completion: no evidence
    with pytest.raises(core.LaunchFailure) as caught:
        ensure(world, last(world))
    assert caught.value.kind == "delivery-unknown" and len(world.submitted) == 1


def test_a_concurrent_retry_is_refused_not_queued(world):
    with core.launch_lock(world.dir):
        with pytest.raises(core.LaunchFailure, match="another launch") as caught:
            with core.launch_lock(world.dir):
                pass
    assert caught.value.kind == "busy"
    with core.launch_lock(world.dir):  # released: the next one runs
        pass


# --- existing checkouts and agents -------------------------------------------------------------


def test_an_owned_clean_worktree_is_reused_not_recreated(world):
    world.worktree(world.clone, "fix/x", owner="t_card")
    assert ensure(world, new_rec(world)) == "working"
    assert herdr_calls(world, "worktree", "create") == []


def test_dirty_files_and_local_commits_of_an_adopted_checkout_are_kept_and_named(world):
    path, git_dir = world.worktree(world.clone, "fix/x", owner="t_old")
    world.dirty[path], world.ahead["fix/x"] = ["notes.md", "src/a.py"], 3
    with pytest.raises(core.LaunchFailure, match="belongs to launch t_old") as caught:
        ensure(world, new_rec(world))
    assert caught.value.kind == "refused" and herdr_calls(world, "tab", "create") == []
    assert ensure(world, {**last(world), "adopt": True}) == "working"
    brief = json.loads(world.submitted[0].split("Task brief (JSON): ", 1)[1])
    assert "3 commits not on origin and 2 uncommitted or untracked files" in brief and "never reset" in brief
    assert json.loads((git_dir / core.OWNER_FILE).read_text())["previous"]["owner"] == "t_old"
    assert not any(isinstance(c, list) and c[0] == "git" and any(d in " ".join(c) for d in DESTRUCTIVE)
                   for c in world.calls)
    assert not any(isinstance(c, list) and "remove" in c for c in world.calls)


def test_an_unrecorded_checkout_is_refused_unless_the_caller_knows_its_owner(world):
    path, _ = world.worktree(world.clone, "fix/x", path=world.tmp / "elsewhere" / "x")
    with pytest.raises(core.LaunchFailure, match="no launcher record"):
        ensure(world, new_rec(world))
    assert herdr_calls(world, "agent", "start") == []
    assert ensure(world, last(world), known=lambda git_dir: "muster card t_old") == "working"
    assert last(world)["path"] == path


def test_a_checkout_with_a_live_agent_is_not_reused(world):
    path, _ = world.worktree(world.clone, "fix/x")
    workspace = world.worktrees[path]["workspace"]
    world.live_agent(f"{workspace}:p9", workspace, "someone", path, "working")
    with pytest.raises(core.LaunchFailure, match="live agent in pane"):
        ensure(world, new_rec(world, adopt=True))
    assert world.submitted == []


def test_a_live_agent_of_this_launch_is_adopted_not_restarted(world):
    """A crash after `agent start` returned but before the save: the agent is ours by name and workspace."""
    path, _ = world.worktree(world.clone, "fix/x", owner="t_card")
    workspace = world.worktrees[path]["workspace"]
    world.live_agent(f"{workspace}:p1", workspace, "run-x", path)
    assert ensure(world, new_rec(world)) == "working"
    assert herdr_calls(world, "agent", "start") == [] and herdr_calls(world, "tab", "create") == []
    assert len(world.submitted) == 1


def test_a_busy_live_agent_that_never_got_the_brief_is_not_prompted(world):
    path, _ = world.worktree(world.clone, "fix/x", owner="t_card")
    workspace = world.worktrees[path]["workspace"]
    world.live_agent(f"{workspace}:p1", workspace, "run-x", path, "working")
    with pytest.raises(core.LaunchFailure, match="not ready") as caught:
        ensure(world, new_rec(world))
    assert caught.value.kind == "busy" and world.submitted == []


def test_an_agent_name_already_live_elsewhere_is_refused(world):
    world.live_agent("w99:p1", "w99", "run-x", "/somewhere")
    with pytest.raises(core.LaunchFailure, match="already runs in pane w99:p1"):
        ensure(world, new_rec(world))
    assert herdr_calls(world, "tab", "create") == [] and world.submitted == []


def test_a_left_over_branch_without_commits_is_fast_forwarded_and_reused(world):
    world.branches[(str(world.clone), "fix/x")] = "sha_old"
    assert ensure(world, new_rec(world)) == "working"
    update = next(c for c in world.calls if isinstance(c, list) and "update-ref" in c)
    assert update[3:] == ["update-ref", "refs/heads/fix/x", "sha_main", "sha_old"]  # old value: git refuses a race


def test_a_left_over_branch_with_commits_needs_adopt_and_is_kept(world):
    world.branches[(str(world.clone), "fix/x")] = "sha_work"
    world.ahead["fix/x"] = 2
    with pytest.raises(core.LaunchFailure, match="2 commits not on origin/main"):
        ensure(world, new_rec(world))
    assert herdr_calls(world, "worktree", "create") == []
    assert ensure(world, {**last(world), "adopt": True}) == "working"
    assert not any(isinstance(c, list) and "update-ref" in c for c in world.calls)
    assert "2 commits not on origin" in json.loads(world.submitted[0].split("(JSON): ", 1)[1])


def test_a_stray_directory_at_the_planned_path_is_refused_not_deleted(world):
    rec = new_rec(world)
    (world.tmp / "worktrees").mkdir()
    stray = world.tmp / "worktrees" / "clone" / "fix-x"
    stray.mkdir(parents=True)
    (stray / "keep.txt").write_text("mine")
    with pytest.raises(core.LaunchFailure, match="is not a worktree"):
        ensure(world, rec)
    assert (stray / "keep.txt").read_text() == "mine"


# --- names and inputs --------------------------------------------------------------------------


def test_agent_names_fit_herdr_and_stay_deterministic_and_distinct():
    long_a = core.agent_name("run", "a-very-long-branch-name-that-goes-on-and-on-one")
    long_b = core.agent_name("run", "a-very-long-branch-name-that-goes-on-and-on-two")
    dotted = core.agent_name("run", "v1.2_Fix.UP")
    for name in (long_a, long_b, dotted, core.agent_name("rc", "website-123456")):
        assert core.AGENT_NAME.fullmatch(name), name
    assert long_a != long_b and long_a == core.agent_name("run", "a-very-long-branch-name-that-goes-on-and-on-one")
    assert core.agent_name("muster", "rcai-397") == "muster-rcai-397"


@pytest.mark.parametrize("branch", ["fix/a..b", "fix/x.lock", "fix/ x", "fix/x~1"])
def test_a_branch_git_would_refuse_is_refused_before_any_side_effect(world, branch):
    with pytest.raises(core.LaunchFailure, match="not a valid branch name"):
        core.plan("t", "o/r", world.clone, branch, "main", "l", "run-x", "opus", "/s", "run")
    assert world.calls == []
