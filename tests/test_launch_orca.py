import pytest

import muster.config as config
import muster.core as core
import muster.hosts.orca as orca
import muster.launcher as launcher
from tests.fake_orca import OrcaWorld
from tests.test_launch import ensure, last, new_rec


@pytest.fixture
def w(tmp_path, monkeypatch):
    """test_launch's world fixture on OrcaWorld, with host orca."""
    world = OrcaWorld(tmp_path)

    def run(argv, env=None):
        out = world.handle(argv)
        if out is None:
            raise AssertionError(argv)
        return out
    monkeypatch.setattr(core, "run", run)
    monkeypatch.setattr(orca, "sleep", lambda s: None)
    monkeypatch.setitem(config.settings, "host", "orca")
    world.clone = tmp_path / "clone"
    world.clone.mkdir()
    world.dir = tmp_path / "launch"
    world.on_submit = lambda text: launcher.prompt_seen(world.dir, {"prompt": text, "session_id": "s1"})
    world.saved = []
    return world


def calls(w, *verb):
    return [c for c in w.calls if isinstance(c, list) and c[:len(verb)] == list(verb)]


def test_a_fresh_orca_launch_types_claude_then_delivers_the_brief(w):
    rec = new_rec(w)
    assert ensure(w, rec) == "working"
    assert rec["host"] == "orca"
    assert rec["pane"].startswith("orca:")
    assert rec["workspace"] == f"{w.repos[str(w.clone)]}::{rec['path']}"
    assert "--permission-mode auto --settings" in w.typed[0]
    assert w.submitted == [rec["text"]]


def test_an_existing_branch_with_commits_launches_on_gits_checkout_with_adopt(w):
    w.branches[(str(w.clone), "fix/x")] = "sha_x"
    w.ahead["fix/x"] = 2
    rec = new_rec(w, adopt=True)
    ensure(w, rec)
    assert not calls(w, "orca", "worktree", "create")
    assert [c for c in calls(w, "git") if c[3:5] == ["worktree", "add"]]
    assert w.submitted == [rec["text"]]


def test_an_untrusted_checkout_stops_as_readiness(w):
    w.agent_start = "waiting"
    with pytest.raises(launcher.LaunchFailure) as e:
        ensure(w, new_rec(w))
    assert e.value.kind == "readiness"
    assert w.submitted == []


def test_a_blocked_prompt_is_not_sent(w):
    w.send_mode = "blocked"
    rec = new_rec(w)
    with pytest.raises(launcher.LaunchFailure) as e:
        ensure(w, rec)
    assert e.value.kind == "readiness"
    assert rec["prompt"]["state"] == "not-sent"
    assert w.submitted == []


def test_an_unproven_prompt_is_resolved_by_the_hook_evidence(w):
    w.send_mode = "accepted"
    rec = new_rec(w)
    assert ensure(w, rec) in ("working", "done")
    assert w.submitted == [rec["text"]]


def test_a_restart_during_launch_waits_then_resumes_on_the_same_terminal(w):
    w.crash_after = lambda argv: argv[:3] == ["orca", "terminal", "send"] and "--wait-submit" not in argv
    with pytest.raises(KeyboardInterrupt):
        ensure(w, new_rec(w))
    w.restart()
    with pytest.raises(core.CommandError, match="detached"):
        ensure(w, last(w))
    assert len(w.typed) == 1
    w.adopt()
    ensure(w, last(w))
    assert len(w.submitted) == 1
    assert len(w.typed) == 1
    made = calls(w, "orca", "terminal", "create")
    assert len(made) == 1 and made[0][made[0].index("--title") + 1] == "run-x"


def test_a_crash_after_worktree_create_recovers_it(w):
    w.crash_after = lambda argv: argv[:3] == ["orca", "worktree", "create"]
    rec = new_rec(w)
    with pytest.raises(KeyboardInterrupt):
        ensure(w, rec)
    again = last(w)
    assert (again["path"], again["step"], again["workspace"]) == (again["planned"], "worktree", None)
    ensure(w, again)
    assert again["workspace"] == f"{w.repos[str(w.clone)]}::{again['path']}"
    assert w.submitted == [again["text"]]
    assert len(calls(w, "orca", "worktree", "create")) == 1


def test_a_crash_after_the_checkout_recovers_it_visible_to_orca(w):
    w.branches[(str(w.clone), "fix/x")] = "sha_x"
    w.ahead["fix/x"] = 2
    w.crash_after = lambda argv: argv[:1] == ["git"] and argv[3:5] == ["worktree", "add"]
    rec = new_rec(w, adopt=True)
    with pytest.raises(KeyboardInterrupt):
        ensure(w, rec)
    assert not w.visible
    w.crash_after = None
    ensure(w, last(w))
    assert w.visible == {w.repos[str(w.clone)]}
    assert w.submitted == [last(w)["text"]]
