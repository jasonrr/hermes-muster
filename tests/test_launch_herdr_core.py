import pytest

import muster.config as config
import muster.core as core
import muster.hosts.herdr_core as herdr_core
import muster.launcher as launcher
from tests.fake_herdr_core import HcWorld
from tests.test_herdr_core import calls, jump
from tests.test_launch import ensure, last, new_rec

STEPS = {
    "worktree": lambda argv: argv[:1] == ["git"] and argv[3:5] == ["worktree", "add"],
    "tab": lambda argv: argv[:2] == ["hc", "run"],
    "typing": lambda argv: argv[:2] == ["hc", "send"] and "--wait" not in argv,
    "brief": lambda argv: argv[:2] == ["hc", "send"] and "--wait" in argv,
}


@pytest.fixture
def w(tmp_path, monkeypatch):
    """test_launch's world fixture on HcWorld, with host herdr-core."""
    world = HcWorld(tmp_path)

    def run(argv, env=None):
        out = world.handle(argv)
        if out is None:
            raise AssertionError(argv)
        return out
    monkeypatch.setattr(core, "run", run)
    monkeypatch.setattr(herdr_core, "sleep", lambda s: None)
    monkeypatch.setitem(config.settings, "host", "herdr-core")
    world.clone = tmp_path / "clone"
    world.clone.mkdir()
    world.dir = tmp_path / "launch"
    world.on_submit = lambda text: launcher.prompt_seen(world.dir, {"prompt": text, "session_id": "s1"})
    world.saved = []
    return world


def test_a_fresh_launch_types_claude_then_delivers_the_brief(w):
    rec = new_rec(w)
    assert ensure(w, rec) == "working"
    assert rec["host"] == "herdr-core"
    assert rec["pane"] == rec["name"]
    assert rec["workspace"] == rec["path"]
    assert "--permission-mode auto --settings" in w.typed[0]
    assert w.submitted == [rec["text"]]


def crash_then_recover(w, step):
    w.crash_after = step
    with pytest.raises(KeyboardInterrupt):
        ensure(w, new_rec(w))
    ensure(w, last(w))
    assert len(calls(w, "hc", "run")) == 1
    assert len(w.typed) == 1
    assert len(w.submitted) == 1


def test_a_crash_after_worktree_recovers_it(w):
    crash_then_recover(w, STEPS["worktree"])


def test_a_crash_after_tab_recovers_it(w):
    crash_then_recover(w, STEPS["tab"])


def test_a_crash_after_typing_recovers_it(w):
    w.hc_late = 3  # recovery meets a Claude still booting
    crash_then_recover(w, STEPS["typing"])


def test_a_crash_after_brief_recovers_it(w):
    crash_then_recover(w, STEPS["brief"])


def test_a_launch_with_no_window_runs_hidden(w, monkeypatch):
    w.window = w.opens = False
    jump(monkeypatch, 0, 31)
    rec = new_rec(w)
    ensure(w, rec)
    assert w.submitted == [rec["text"]]
    assert "herdr.space" not in w.find(rec["name"])["labels"]


def test_a_crash_after_hc_run_with_no_window_then_a_window_gets_a_space(w, monkeypatch):
    w.window = w.opens = False
    jump(monkeypatch, 0, 31)
    w.crash_after = STEPS["tab"]
    with pytest.raises(KeyboardInterrupt):
        ensure(w, new_rec(w))
    w.window = True
    ensure(w, last(w))
    assert len(calls(w, "hc", "run")) == 1
    assert [c for c in calls(w, "hc", "label") if any(a.startswith("herdr.space=") for a in c)]
    assert len(w.submitted) == 1


def test_an_untrusted_checkout_stops_as_readiness(w):
    w.hc_start = "blocked"
    with pytest.raises(launcher.LaunchFailure) as e:
        ensure(w, new_rec(w))
    assert e.value.kind == "readiness"
    assert w.submitted == []


def test_an_existing_branch_with_commits_launches_on_gits_checkout_with_adopt(w):
    w.branches[(str(w.clone), "fix/x")] = "sha_x"
    w.ahead["fix/x"] = 2
    rec = new_rec(w, adopt=True)
    ensure(w, rec)
    adds = [c for c in calls(w, "git") if c[3:5] == ["worktree", "add"]]
    assert len(adds) == 1 and adds[0][5:] == [rec["path"], "fix/x"]
    assert w.submitted == [rec["text"]]


def test_a_core_restart_after_delivery_reads_as_agent_gone(w):
    rec = new_rec(w)
    ensure(w, rec)
    w.hc_restart()
    assert herdr_core.agent(rec["pane"]) is None
    assert herdr_core.pane(rec["pane"]) is not None


def test_a_blocked_prompt_is_not_sent(w):
    w.hc_send = "blocked"
    rec = new_rec(w)
    with pytest.raises(launcher.LaunchFailure) as e:
        ensure(w, rec)
    assert e.value.kind == "readiness"
    assert rec["prompt"]["state"] == "not-sent"
    assert w.submitted == []


def test_an_unproven_prompt_is_resolved_by_the_hook_evidence(w):
    w.hc_send = "not_started"
    rec = new_rec(w)
    assert ensure(w, rec) in ("working", "done")
    assert w.submitted == [rec["text"]]
