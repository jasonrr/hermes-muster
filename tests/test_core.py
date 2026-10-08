"""core: the approver's newest agent-ready label → one ledger card on the muster board → one herdr pane."""

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
import types
from pathlib import Path

import pytest

import muster.core as core
from muster import config
from tests.fake_herdr import World

REPO = "Radical-Candor-LLC/radicalcandorai"
JASON = {"login": "jasonrr", "id": 108170}
OTHER = {"login": "someone", "id": 1}


def tick(dry_run=False):
    return core.tick(argparse.Namespace(dry_run=dry_run))


def recover(card, resend=False, adopt=False):
    return core.recover(argparse.Namespace(card=card, resend=resend, adopt=adopt))


def labeled(actor, event_id, at, name="agent-ready"):
    return {"id": event_id, "event": "labeled", "actor": actor, "label": {"name": name}, "created_at": at}


def test_approval_is_the_newest_agent_ready_label_event_by_the_approver():
    assert core.approval([labeled(JASON, 10, "2026-09-16T10:00:00Z")])["id"] == 10


def test_a_label_by_anyone_else_is_refused():
    assert core.approval([labeled(OTHER, 11, "2026-09-16T10:00:00Z")]) is None


def test_a_relabel_by_someone_else_after_the_approver_is_refused():
    events = [
        labeled(JASON, 10, "2026-09-16T10:00:00Z"),
        {"id": 12, "event": "unlabeled", "actor": OTHER, "label": {"name": "agent-ready"},
         "created_at": "2026-09-16T11:00:00Z"},
        labeled(OTHER, 13, "2026-09-16T12:00:00Z"),
    ]
    assert core.approval(events) is None


def test_another_label_name_or_no_label_event_is_refused():
    assert core.approval([labeled(JASON, 14, "2026-09-16T10:00:00Z", name="bug")]) is None
    assert core.approval([]) is None


def test_matching_login_with_a_different_id_is_refused():
    assert core.approval([labeled({"login": "jasonrr", "id": 2}, 15, "2026-09-16T10:00:00Z")]) is None


def test_approver_login_case_insensitive():
    assert core.approval([labeled({"login": "JasonRR", "id": 108170}, 16, "2026-09-16T10:00:00Z")])["id"] == 16
    assert core.approval([labeled({"login": "JASONRR", "id": 2}, 17, "2026-09-16T10:00:00Z")]) is None


def test_card_argv_is_an_unassigned_card_on_the_board_keyed_by_the_label_event():
    argv = core.card_argv(REPO, {"number": 397, "title": "-Add a unit test"}, {"id": 10})
    assert argv[:5] == ["hermes", "kanban", "--board", "muster", "create"]
    assert argv[-2:] == ["--", "-Add a unit test"]
    assert argv[argv.index("--idempotency-key") + 1] == f"{REPO}#397@10"
    assert "--json" in argv
    for gone in ("--assignee", "--skill", "--max-runtime", "--max-retries"):
        assert gone not in argv
    body = argv[argv.index("--body") + 1]
    assert f"https://github.com/{REPO}/issues/397" in body
    assert body.endswith(core.PROVENANCE)
    assert argv[argv.index("--created-by") + 1] == core.CREATED_BY


def test_the_brief_names_bug_or_feature_and_never_carries_issue_text():
    bug = core.brief(REPO, 402, True)
    other = core.brief(REPO, 403, False)
    assert "This issue is a bug." in bug and "This issue is a feature." in other
    assert f'gh issue comment 402 -R {REPO} --body "Picked up by muster."' in bug
    assert f"gh issue view 402 -R {REPO} --json title,body,comments" in bug
    assert "`muster/402`" in bug and "Closes #402" in bug and "jasonrr approved" in bug
    assert "never push to main" in bug.lower()
    assert f"{config.hermes_bin()} muster hook done" in bug
    assert "AskUserQuestion" in bug
    assert "act as the login configured" in bug


def test_the_brief_without_a_bot_names_no_login(monkeypatch):
    monkeypatch.setitem(config.settings, "gh_config_dir", "")
    assert "act as the login" not in core.brief(REPO, 1, False)


def test_brief_has_workflow_and_no_tally():
    text = core.brief(REPO, 1, False)
    rule = next(line for line in config.workflow_path().read_text().splitlines() if line.startswith("1. "))
    assert "## How to work" in text and rule[3:] in text
    assert "tally" not in text.lower()


def test_brief_keeps_forbidden_list():
    text = core.brief(REPO, 7, False)
    for needle in (".github/", "lockfiles", "force-push", "attribution trailer", "Closes #7"):
        assert needle in text


def test_the_brief_carries_the_production_note_or_says_there_is_none(tmp_path, monkeypatch):
    monkeypatch.setitem(config.settings, "notes_dir", str(tmp_path / "repos"))
    assert "No production note for this repository" in core.brief(REPO, 1, False)
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos" / "Radical-Candor-LLC__radicalcandorai.md").write_text("# note\nCloud Run, us-west1\n")
    assert "# note\nCloud Run, us-west1" in core.brief(REPO, 1, False)


def fake_world(tmp_path, calls, failing_repo=None, card_age=0, fail_on=None, branches=(),
               subs=None, card=None, world=None, boards=("muster",)):
    """Stubs gh and hermes; herdr, git and `gh pr list` are fake_herdr's World.
    `fail_on(argv)` true → that call raises. `branches` are checkouts made earlier by nobody we know."""
    world = world or World(tmp_path)
    clone = config.repos()[REPO][0]
    for b in branches:
        world.worktree(clone, b, path=tmp_path / "elsewhere" / b.replace("/", "-"))
    worktree = core.worktrees_dir() / clone.name / "muster-397"
    git_dir = tmp_path / "gitdirs" / "muster-397"

    def fake_run(argv):
        if fail_on and fail_on(argv):
            calls.append(argv)
            raise core.CommandError(f"{' '.join(argv[:3])}: exit 1\nboom")
        out = world.handle(argv)
        if out is not None:
            calls.append(argv)
            return out
        calls.append(argv)
        if argv[:2] == ["gh", "api"] and "/issues?" in argv[-1]:
            repo = argv[-1].split("repos/")[1].split("/issues?")[0]
            if repo == failing_repo:
                raise core.CommandError("gh api: exit 4")
            if repo != REPO:
                return ""
            assert "labels=agent-ready" in argv[-1] and "state=open" in argv[-1] and "--paginate" in argv
            return "\n".join(json.dumps(i) for i in [
                {"number": 397, "title": "Add a unit test", "labels": []},
                {"number": 5, "title": "Not approved", "labels": []},
                {"number": 6, "title": "A PR with the label", "labels": [], "pull_request": {"url": "x"}},
            ]) + "\n"
        if argv[:2] == ["gh", "api"]:
            number = int(argv[-1].split("/issues/")[1].split("/")[0])
            actor = JASON if number == 397 else OTHER
            return json.dumps(labeled(actor, 10 + number, "2026-09-16T10:00:00Z")) + "\n"
        if argv[:4] == ["hermes", "kanban", "boards", "list"]:
            return json.dumps([{"slug": b} for b in boards])
        if argv[:2] == ["hermes", "kanban"]:
            verb = argv[4]
            if verb == "create":
                return json.dumps({"id": "t_abc123", "status": "ready", "assignee": None,
                                   "created_at": int(time.time()) - card_age}) + "\n"
            if verb == "show":
                return json.dumps({"task": card or {"id": argv[5], "status": "ready", "assignee": None}})
            if verb == "notify-list":
                return json.dumps(subs if subs is not None else [{"chat_id": "4242", "user_id": "4242", "chat_type": "dm", "notifier_profile": "default", "delivery_mode": "notify+wake"}])
            if verb in ("notify-subscribe", "comment", "block"):
                return ""
        raise AssertionError(argv)
    fake_run.world = world
    return fake_run, worktree, git_dir


def blocks(calls):
    return [c for c in calls if c[:2] == ["hermes", "kanban"] and c[4] == "block"]


def test_an_approved_issue_gets_one_card_one_subscription_and_one_agent_pane(tmp_path, monkeypatch, capsys):
    calls = []
    fake, worktree, git_dir = fake_world(tmp_path, calls)
    monkeypatch.setattr(core, "run", fake)
    assert tick() == 0
    clone = config.repos()[REPO][0]
    # one active path: never the Docker dispatcher, never an assignee
    assert not any("dispatch" in c or "--assignee" in c for c in calls)
    assert len([c for c in calls if c[:2] == ["hermes", "kanban"] and c[4] == "create"]) == 1
    sub = next(c for c in calls if c[:2] == ["hermes", "kanban"] and c[4] == "notify-subscribe")
    assert sub[5] == "t_abc123"
    for flag, value in (("--platform", "telegram"), ("--chat-id", "4242"), ("--user-id", "4242"),
                        ("--chat-type", "dm"), ("--notifier-profile", "default"), ("--delivery-mode", "notify+wake")):
        assert sub[sub.index(flag) + 1] == value
    create = next(c for c in calls if c[:3] == ["herdr", "worktree", "create"])
    for flag, value in (("--cwd", str(clone)), ("--branch", "muster/397"), ("--base", "origin/main"), ("--label", "radicalcandorai#397")):
        assert create[create.index(flag) + 1] == value
    assert "--no-focus" in create and "--trust-repository" in create
    tab = next(c for c in calls if c[:3] == ["herdr", "tab", "create"])
    assert tab[tab.index("--workspace") + 1] == "w1" and tab[tab.index("--cwd") + 1] == str(worktree)
    assert tab[tab.index("--env") + 1] == f"HERMES_HOME={tmp_path / 'hermes'}" and "--no-focus" in tab
    start = next(c for c in calls if c[:3] == ["herdr", "agent", "start"])
    assert start[3] == "muster-radicalcandorai-397" and start[start.index("--pane") + 1] == "w1:p2"
    assert start[start.index("--kind") + 1] == "claude"
    agent_args = start[start.index("--") + 1:]
    assert agent_args[agent_args.index("--model") + 1] == "opus"
    settings = core.intake_dir() / "t_abc123" / core.SETTINGS_FILE
    assert agent_args == ["--model", "opus", "--permission-mode", "auto", "--settings", str(settings)]
    assert_auto_mode(agent_args)
    # Two phases (herdr agent-automation.mdx): start carries no task; the brief is the first prompt,
    # submitted once Claude is idle-ready, as an explicit instruction plus one-line JSON.
    [submit] = [c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]
    assert calls.index(start) < calls.index(submit) and submit[3] == "w1:p2"
    prompt = submit[4]
    assert not any(ord(c) < 32 or ord(c) == 127 for c in prompt)
    assert prompt.startswith("Execute the authorized task")
    decoded = json.loads(prompt.split("Task brief (JSON): ", 1)[1])
    assert decoded == (git_dir / core.BRIEF_FILE).read_text()
    assert decoded.startswith(f"# muster: {REPO}#397") and "jasonrr approved" in decoded
    assert "How to work" in prompt and "Read " + str(git_dir) not in prompt
    assert "Add a unit test" not in prompt  # issue text never enters the startup prompt
    links = json.loads((git_dir / core.CARD_FILE).read_text())
    assert links == {"card": "t_abc123", "repo": REPO, "issue": 397, "title": "Add a unit test",
                     "pane": "w1:p2", "workspace": "w1",
                     "worktree": str(worktree), "base": "main", "branch": "muster/397", "launch_dir": str(core.intake_dir() / "t_abc123")}
    record = json.loads((core.intake_dir() / "t_abc123" / "launch.json").read_text())
    assert record["event"] == 407 and record["bug"] is False
    assert record["launch"]["prompt"]["state"] == "working" and record["launch"]["step"] == "done"
    hooks = json.loads(settings.read_text())["hooks"]
    assert set(hooks) == {"Notification", "UserPromptSubmit", "PostToolUse", "SessionEnd",
                          "PreToolUse", "PostToolUseFailure", "Stop"}
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("muster hook prompt")
    assert hooks["PostToolUse"][0]["hooks"][0]["command"].endswith("muster hook prompt")
    assert hooks["Notification"][0]["matcher"] == "permission_prompt|elicitation_dialog|elicitation_url_dialog|worker_permission_prompt"
    assert hooks["Notification"][0]["hooks"][0]["command"].endswith("muster hook notification")
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion"
    assert hooks["PreToolUse"][0]["hooks"][0]["command"].endswith("muster hook notification")
    assert hooks["PostToolUseFailure"][0]["hooks"][0]["command"].endswith("muster hook prompt")
    assert "matcher" not in hooks["PostToolUse"][0]
    comment = next(c for c in calls if c[:2] == ["hermes", "kanban"] and c[4] == "comment")
    assert comment[6].startswith(core.LINKS_PREFIX) and "w1:p2" in comment[6] and str(worktree) in comment[6]
    assert not blocks(calls)
    out = capsys.readouterr().out
    assert f"{REPO}#397 task t_abc123 pane w1:p2 (first prompt: working)" in out
    assert f"{REPO}#5 skipped" in out and "#6" not in out


def launched_base(tmp_path, monkeypatch, world):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, world=world)[0])
    assert tick() == 0
    create = next(c for c in calls if c[:3] == ["herdr", "worktree", "create"])
    base = create[create.index("--base") + 1]
    assert base.startswith("origin/") and ["git", "-C", str(config.repos()[REPO][0]), "fetch", "origin", base[7:]] in calls
    record = json.loads((core.intake_dir() / "t_abc123" / "launch.json").read_text())
    assert record["launch"]["base"] == base[7:]
    return base[7:], record, json.loads(world.submitted[0].split("(JSON): ", 1)[1])


def test_a_repo_whose_default_branch_is_master_launches_from_master(tmp_path, monkeypatch):
    world = World(tmp_path)
    world.origin_head = "master"
    base, record, brief = launched_base(tmp_path, monkeypatch, world)
    assert base == "master" and "cut from origin/master" in brief and "against master" in brief
    git_dir = Path(world.worktrees[record["launch"]["path"]]["git_dir"])
    assert json.loads((git_dir / core.CARD_FILE).read_text())["base"] == "master"


def test_an_at_base_suffix_wins_over_origin_head(tmp_path, monkeypatch):
    monkeypatch.setitem(config.settings, "repos", [r + "@develop" if r.startswith(REPO + "=") else r
                                                   for r in config.settings["repos"]])
    world = World(tmp_path)
    world.origin_head = "master"
    assert launched_base(tmp_path, monkeypatch, world)[0] == "develop"
    assert world.set_heads == 0


def test_an_unset_origin_head_is_set_from_the_remote_once(tmp_path, monkeypatch):
    world = World(tmp_path)
    world.origin_head, world.remote_head = None, "master"
    assert launched_base(tmp_path, monkeypatch, world)[0] == "master"
    assert world.set_heads == 1


def test_no_origin_head_and_no_suffix_launches_from_main_and_records_why(tmp_path, monkeypatch):
    world = World(tmp_path)
    world.origin_head = None
    base, record, _ = launched_base(tmp_path, monkeypatch, world)
    assert base == "main" and world.set_heads == 1
    assert any("launched from main" in line for line in record["launch"]["evidence"])


def test_an_existing_card_launches_nothing(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, card_age=60)[0])
    assert tick() == 0
    assert not any(c[0] == "herdr" for c in calls)
    assert not any(c[:2] == ["hermes", "kanban"] and c[4] == "notify-subscribe" for c in calls)
    assert f"{REPO}#397 task t_abc123 (ready) card exists" in capsys.readouterr().out


def test_gate_skips_missing_clone(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    skipped = list(config.repos())[2]
    clone = config.repos()[skipped][0]
    shutil.rmtree(clone / ".git")
    assert tick() == 0
    assert not any(skipped in c[-1] for c in calls if c[:2] == ["gh", "api"])
    assert f"{skipped}: skipped, clone {clone} has no .git" in capsys.readouterr().out


def test_dry_run_prints_what_would_launch_and_writes_nothing(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    assert tick(dry_run=True) == 0
    assert all(c[:2] in (["gh", "api"], ["hermes", "kanban"]) for c in calls)
    assert not any(c[4:5] == ["create"] for c in calls)
    assert f"{REPO}#397 would launch: {REPO}#397@407 (feature)" in capsys.readouterr().out


def test_a_failed_launch_step_blocks_the_card_with_the_step_and_exits_one(tmp_path, monkeypatch, capsys):
    calls = []
    fake = fake_world(tmp_path, calls, fail_on=lambda a: a[:3] == ["herdr", "agent", "start"])[0]
    monkeypatch.setattr(core, "run", fake)
    assert tick() == 1
    [block] = blocks(calls)
    # capability, not needs_input: the ping says "Setup trouble", not that the human owes a decision.
    assert block[5:8] == ["--kind", "capability", "t_abc123"]
    assert block[8].startswith("The coding agent did not start (the agent step failed). This is setup trouble, "
                               "not a product decision.\nDetails: Claude did not start in pane w1:p2: herdr agent start: exit 1")
    assert f"{REPO}#397: launch failed at agent" in capsys.readouterr().out


def test_an_existing_worktree_for_the_branch_blocks_instead_of_launching_a_second_pane(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, branches=("muster/397",))[0])
    assert tick() == 1
    assert not any(c[:3] == ["herdr", "worktree", "create"] for c in calls)
    # A checkout of this branch that no launcher recorded is refused, never reused on a guess.
    assert "no launcher record" in blocks(calls)[0][8] and "--adopt" in blocks(calls)[0][8]
    assert "(the worktree step)" in blocks(calls)[0][8] and "did not start" not in blocks(calls)[0][8]
    assert not any(c[:3] in (["herdr", "tab", "create"], ["herdr", "agent", "start"]) for c in calls)


def test_a_subscription_that_does_not_read_back_blocks_the_card(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, subs=[])[0])
    assert tick() == 1
    assert "(the subscribe step failed)" in blocks(calls)[0][8]
    assert not any(c[0] == "herdr" for c in calls)


GROUP = {"chat_id": "-1004455490829", "user_id": "8768235002", "chat_type": "group"}


def use_group(monkeypatch):
    for key, value in GROUP.items():
        monkeypatch.setitem(config.settings, f"notify_{key}", value)


def test_cards_subscribe_the_configured_group_with_the_configured_user(tmp_path, monkeypatch):
    (core.hermes_home() / ".env").write_text("OTHER=1\n")  # the group never needs the DM
    use_group(monkeypatch)
    calls = []
    sub = {**GROUP, "notifier_profile": "default", "delivery_mode": "notify+wake"}
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, subs=[sub])[0])
    assert tick() == 0
    argv = next(c for c in calls if c[:2] == ["hermes", "kanban"] and c[4] == "notify-subscribe")
    for flag, value in (("--chat-id", "-1004455490829"), ("--user-id", "8768235002"), ("--chat-type", "group"),
                        ("--notifier-profile", "default"), ("--delivery-mode", "notify+wake")):
        assert argv[argv.index(flag) + 1] == value


def test_a_group_subscription_that_reads_back_as_a_dm_blocks_the_card(tmp_path, monkeypatch):
    use_group(monkeypatch)
    calls = []
    sub = {**GROUP, "chat_type": "dm", "notifier_profile": "default", "delivery_mode": "notify+wake"}
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, subs=[sub])[0])
    assert tick() == 1
    assert "(the subscribe step failed)" in blocks(calls)[0][8]


def test_a_missing_telegram_home_blocks_the_card_without_printing_env(tmp_path, monkeypatch, capsys):
    (core.hermes_home() / ".env").write_text("OTHER=secret-value\n")
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    assert tick() == 1
    assert "TELEGRAM_HOME_CHANNEL" in blocks(calls)[0][8]
    assert "secret-value" not in capsys.readouterr().out


def test_an_assigned_card_on_readback_blocks_instead_of_launching(tmp_path, monkeypatch):
    calls = []
    card = {"id": "t_abc123", "status": "ready", "assignee": "rc-coder"}
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, card=card)[0])
    assert tick() == 1
    assert "(the card readback step failed)" in blocks(calls)[0][8]
    assert not any(c[0] == "herdr" for c in calls)


def test_a_missing_bot_gh_config_blocks_the_card(tmp_path, monkeypatch):
    (Path(config.settings["gh_config_dir"]) / "hosts.yml").unlink()
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    assert tick() == 1
    assert "gh-bot" in blocks(calls)[0][8]
    assert not any(c[0] == "herdr" for c in calls)


def test_a_held_lock_means_another_tick_is_running_and_this_one_does_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    core.lock_path().parent.mkdir(parents=True, exist_ok=True)
    with open(core.lock_path(), "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tick() == 0
    assert calls == []


def test_tick_keeps_the_hermes_home_and_leaves_the_kanban_home_to_hermes(tmp_path, monkeypatch):
    # Hermes shares one board root across profiles; pinning it to a profile's home would fork the board.
    monkeypatch.setattr(core, "run", fake_world(tmp_path, [])[0])
    monkeypatch.delenv("HERMES_KANBAN_HOME")
    tick(dry_run=True)
    assert os.environ["HERMES_HOME"] == str(tmp_path / "hermes")
    assert "HERMES_KANBAN_HOME" not in os.environ
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    tick(dry_run=True)
    assert os.environ["HERMES_KANBAN_HOME"] == str(tmp_path / "kanban")


def test_tick_refuses_missing_board(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, boards=())[0])
    with pytest.raises(config.ConfigError, match="muster"):
        tick()
    assert not any(c[4:5] == ["create"] for c in calls)


@pytest.mark.parametrize("out", ["boom", "not json", "5"])
def test_a_board_list_that_fails_or_is_not_json_is_a_config_error(tmp_path, monkeypatch, out):
    base = fake_world(tmp_path, [])[0]

    def run(argv):
        if argv[:4] == ["hermes", "kanban", "boards", "list"]:
            if out == "boom":
                raise core.CommandError("hermes kanban boards: exit 1\ndatabase is locked")
            return out
        return base(argv)
    monkeypatch.setattr(core, "run", run)
    with pytest.raises(config.ConfigError, match="cannot list kanban boards"):
        tick()


def test_a_failing_repository_is_reported_and_the_rest_still_run(tmp_path, monkeypatch, capsys):
    calls = []
    failing = list(config.repos())[1]
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, failing_repo=failing)[0])
    assert tick() == 1
    out = capsys.readouterr().out
    assert f"{failing}: gh api: exit 4" in out
    assert f"{REPO}#397 task t_abc123 pane w1:p2" in out


def test_unparseable_create_output_is_reported_and_nothing_launches(tmp_path, monkeypatch, capsys):
    calls = []
    base = fake_world(tmp_path, calls)[0]
    monkeypatch.setattr(core, "run",
                        lambda a: "warning: something\n" if a[:2] == ["hermes", "kanban"] and a[4] == "create" else base(a))
    assert tick() == 1
    assert not any(c[0] == "herdr" for c in calls)
    assert any(line.startswith(f"{REPO}#397: ") for line in capsys.readouterr().out.splitlines())


def test_one_issue_with_a_failing_timeline_is_named_and_the_next_issue_still_runs(tmp_path, monkeypatch, capsys):
    calls = []
    base = fake_world(tmp_path, calls)[0]
    monkeypatch.setattr(core, "run",
                        lambda a: "[]\n" if a[:2] == ["gh", "api"] and "/issues/5/timeline" in a[-1] else base(a))
    assert tick() == 1
    out = capsys.readouterr().out
    assert f"{REPO}#5: " in out and f"{REPO}#397 task t_abc123 pane w1:p2" in out


def test_the_card_is_subscribed_before_its_readback_so_a_readback_failure_still_pings(tmp_path, monkeypatch):
    calls = []
    card = {"id": "t_abc123", "status": "ready", "assignee": "rc-coder"}
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, card=card)[0])
    assert tick() == 1
    kanban_verbs = [c[4] for c in calls if c[:2] == ["hermes", "kanban"]]
    assert kanban_verbs.index("notify-subscribe") < kanban_verbs.index("show") < kanban_verbs.index("block")


def test_a_readback_with_no_task_blocks_the_card(tmp_path, monkeypatch):
    calls = []
    fake = fake_world(tmp_path, calls)[0]
    null_task = json.dumps({"task": None})
    monkeypatch.setattr(core, "run", lambda a: null_task if a[:2] == ["hermes", "kanban"] and a[4] == "show" else fake(a))
    assert tick() == 1
    assert "(the card readback step failed)" in blocks(calls)[0][8]


def test_a_block_that_itself_fails_is_printed_for_the_cron_log(tmp_path, monkeypatch, capsys):
    calls = []
    fake = fake_world(tmp_path, calls, fail_on=lambda a: a[:3] == ["herdr", "agent", "start"])[0]

    def run(argv):
        if argv[:2] == ["hermes", "kanban"] and argv[4] == "block":
            calls.append(argv)
            raise FileNotFoundError("hermes")
        return fake(argv)
    monkeypatch.setattr(core, "run", run)
    assert tick() == 1
    out = capsys.readouterr().out
    assert f"{REPO}#397: launch failed at agent" in out and f"{REPO}#397: could not block t_abc123" in out


def pane_envs(calls):
    tab = next(c for c in calls if c[:3] == ["herdr", "tab", "create"])
    return [tab[i + 1] for i, a in enumerate(tab) if a == "--env"]


def test_pane_env_with_bot(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    tick()
    # gh prefers a token env var over GH_CONFIG_DIR, so both are blanked.
    assert pane_envs(calls) == [f"HERMES_HOME={tmp_path / 'hermes'}", f"GH_CONFIG_DIR={tmp_path / 'gh-bot'}",
                                "GH_TOKEN=", "GITHUB_TOKEN="]


def test_pane_env_without_bot(tmp_path, monkeypatch):
    monkeypatch.setitem(config.settings, "gh_config_dir", "")
    calls = []
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls)[0])
    assert tick() == 0
    assert pane_envs(calls) == [f"HERMES_HOME={tmp_path / 'hermes'}"]


def assert_auto_mode(agent_args):
    """Claude's own auto mode: never a bypass of its permission checks."""
    assert agent_args[agent_args.index("--permission-mode") + 1] == "auto"
    assert agent_args.count("--permission-mode") == 1
    assert not any("bypass" in a or "dangerously" in a for a in agent_args)


# recover: a failed intake launch resumes under the issue's CURRENT approval, never on an old one


class Recovery:
    """One intake card whose first launch failed (no agent appeared), on a board that keeps state."""

    def __init__(self, tmp_path, monkeypatch):
        self.calls, self.world = [], World(tmp_path)
        self.world.start = "none"
        self.cards, self.kinds = {"t_abc123": "ready"}, {}
        self.issue = {"number": 397, "title": "Add a unit test", "state": "open", "labels": [{"name": "agent-ready"}]}
        self.events = [labeled(JASON, 407, "2026-09-16T10:00:00Z")]
        base = fake_world(tmp_path, self.calls, world=self.world)[0]

        def run(argv):
            if argv[:2] == ["hermes", "kanban"] and argv[4] in ("show", "block", "unblock"):
                self.calls.append(argv)
                card = argv[7] if argv[4] == "block" else argv[5]
                if argv[4] == "show":
                    return json.dumps({"task": {"id": card, "status": self.cards[card], "assignee": None}})
                if argv[4] == "block":
                    assert self.cards[card] == "ready", "hermes refuses a second block"
                    self.kinds[card] = argv[6]
                self.cards[card] = "blocked" if argv[4] == "block" else "ready"
                return ""
            if argv[:2] == ["gh", "api"] and argv[-1] == f"repos/{REPO}/issues/397":
                self.calls.append(argv)
                return json.dumps(self.issue)
            if argv[:2] == ["gh", "api"] and "/issues/397/timeline" in argv[-1]:
                self.calls.append(argv)
                return "\n".join(json.dumps(e) for e in self.events) + "\n"
            return base(argv)
        monkeypatch.setattr(core, "run", run)
        monkeypatch.setattr(core, "block_kind", lambda card: self.kinds.get(card))
        assert tick() == 1
        assert self.cards["t_abc123"] == "blocked" and self.kinds["t_abc123"] == "capability"
        self.world.start = "ready"
        self.mark = len(self.calls)

    def since(self, *prefix):
        return [c for c in self.calls[self.mark:] if c[:len(prefix)] == list(prefix)]

    def comments(self):
        return [c[6] for c in self.since("hermes", "kanban") if c[4] == "comment"]


@pytest.fixture
def failed(tmp_path, monkeypatch):
    return Recovery(tmp_path, monkeypatch)


def test_recover_resumes_under_the_same_approval_and_unblocks_once(failed, capsys):
    assert recover("t_abc123") == 0
    assert len(failed.world.submitted) == 1 and len(failed.world.agents) == 1
    assert [c[4] for c in failed.since("hermes", "kanban") if c[4] in ("block", "unblock")] == ["unblock"]
    assert failed.cards["t_abc123"] == "ready"
    assert any(c.startswith(core.LINKS_PREFIX) for c in failed.comments())
    assert len(failed.since("herdr", "worktree", "create")) == 0  # the first attempt's checkout is reused
    assert "recovered (first prompt: working)" in capsys.readouterr().out
    assert recover("t_abc123") == 0  # ready now and delivered: sends nothing more
    assert len(failed.world.submitted) == 1


@pytest.mark.parametrize("change", ["relabeled-by-someone-else", "relabeled-by-approver", "closed", "unlabeled"])
def test_recover_refuses_a_revoked_or_superseded_manual_approval(failed, capsys, change):
    if change == "relabeled-by-someone-else":
        failed.events.append(labeled(OTHER, 999, "2026-09-17T10:00:00Z"))
    elif change == "relabeled-by-approver":
        failed.events.append(labeled(JASON, 999, "2026-09-17T10:00:00Z"))  # that event makes its own card
    elif change == "closed":
        failed.issue["state"] = "closed"
    else:
        failed.issue["labels"] = []
    assert recover("t_abc123") == 1
    assert not failed.since("herdr") and failed.world.submitted == []
    assert failed.cards["t_abc123"] == "blocked"
    # Already blocked: the failure is a comment (a second same-kind block after an unblock is triage).
    assert failed.comments()[-1].startswith("Recovery failed again. The launch was refused")
    assert "approval" in capsys.readouterr().err or change in ("closed", "unlabeled")


def test_recover_refuses_an_issue_whose_branch_already_has_a_pull_request(failed, capsys):
    failed.world.prs = [{"url": "https://github.com/o/r/pull/9", "state": "OPEN"}]
    assert recover("t_abc123") == 1
    assert "pull/9 is already open or merged" in capsys.readouterr().err
    assert failed.world.submitted == [] and failed.cards["t_abc123"] == "blocked"


def test_recover_refuses_a_session_that_ended_after_its_launch(failed, capsys):
    failed.kinds["t_abc123"] = "needs_input"
    assert recover("t_abc123") == 1
    assert "only a ready card or one blocked by a launch failure" in capsys.readouterr().err
    assert not failed.since("herdr") and not failed.comments()


def test_recover_waits_behind_a_concurrent_one_and_sends_nothing_twice(failed, capsys):
    with core.launch_lock(core.intake_dir() / "t_abc123"):
        assert recover("t_abc123") == 1
    assert "another launch or recover" in capsys.readouterr().err
    assert not failed.since("herdr") and not failed.comments()


def test_recover_of_a_card_without_a_launch_record_says_how_to_relaunch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "run", fake_world(tmp_path, [])[0])
    assert recover("t_nope") == 1
    assert "re-apply agent-ready" in capsys.readouterr().err


@pytest.mark.parametrize("record", [{"card": "t_x", "repo": REPO, "issue": 397}, {"card": "t_x", "launch": []}])
def test_recover_of_a_record_without_a_launch_says_so_without_a_traceback(tmp_path, monkeypatch, capsys, record):
    monkeypatch.setattr(core, "run", fake_world(tmp_path, [])[0])
    core.save_json(core.intake_dir() / "t_x" / "launch.json", record)
    assert recover("t_x") == 1
    assert "has no launch record; remove and re-apply agent-ready" in capsys.readouterr().err


def test_a_new_label_reuses_the_checkout_an_earlier_card_of_the_issue_left(tmp_path, monkeypatch):
    """An earlier card's launch failed and left muster/<n>; the approver labels again."""
    calls, world = [], World(tmp_path)
    clone = config.repos()[REPO][0]
    path, git_dir = world.worktree(clone, "muster/397", path=tmp_path / "old-default" / "muster-397")
    (git_dir / core.CARD_FILE).write_text(json.dumps({"card": "t_old", "repo": REPO, "issue": 397}))
    world.dirty[path], world.ahead["muster/397"] = ["half-done.py"], 1
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, world=world)[0])
    assert tick() == 0
    assert not [c for c in calls if c[:3] == ["herdr", "worktree", "create"]]
    brief = json.loads(world.submitted[0].split("(JSON): ", 1)[1])
    assert "1 commits not on origin and 1 uncommitted or untracked files" in brief
    owner = json.loads((git_dir / core.OWNER_FILE).read_text())
    assert owner["owner"] == "t_abc123" and owner["previous"] == "muster card t_old"
    assert json.loads((git_dir / core.CARD_FILE).read_text())["card"] == "t_abc123"


def test_a_left_over_branch_with_no_commits_relaunches_from_current_main(tmp_path, monkeypatch):
    calls, world = [], World(tmp_path)
    world.branches[(str(config.repos()[REPO][0]), "muster/397")] = "sha_old"
    monkeypatch.setattr(core, "run", fake_world(tmp_path, calls, world=world)[0])
    assert tick() == 0
    assert any("update-ref" in c for c in calls) and len(world.submitted) == 1


def test_pane_env_expands_a_tilde_gh_config_dir(monkeypatch):
    monkeypatch.setitem(config.settings, "gh_config_dir", "~/.config/gh-bot")
    expected = str(Path("~/.config/gh-bot").expanduser())
    assert f"GH_CONFIG_DIR={expected}" in core.pane_env()
    assert f"`{expected}`" in core.brief(REPO, 1, False)


def test_pane_env_carries_a_kanban_home_that_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    assert core.pane_env()[:2] == [f"HERMES_HOME={tmp_path / 'hermes'}", f"HERMES_KANBAN_HOME={tmp_path / 'kanban'}"]


def test_notify_target_stringifies_ids_yaml_parsed_as_ints(monkeypatch):
    monkeypatch.setitem(config.settings, "notify_chat_id", -1004455490829)
    monkeypatch.setitem(config.settings, "notify_user_id", 8768235002)
    assert core.notify_target() == {"chat_id": "-1004455490829", "user_id": "8768235002", "chat_type": "group"}
    monkeypatch.setitem(config.settings, "notify_user_id", "")
    assert core.notify_target()["user_id"] == "-1004455490829"


def test_board_db_follows_the_kanban_home_and_the_default_board_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kb"))
    assert core.board_db() == tmp_path / "kb" / "kanban" / "boards" / "muster" / "kanban.db"
    monkeypatch.setitem(config.settings, "board", "default")
    assert core.board_db() == tmp_path / "kb" / "kanban.db"


def test_board_db_asks_hermes_when_it_runs_inside_hermes(monkeypatch):
    fake = types.ModuleType("hermes_cli.kanban_db")
    fake.kanban_db_path = lambda board: Path("/root") / f"{board}.db"
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", fake)
    assert core.board_db() == Path("/root/muster.db")


def test_approval_label_name_is_case_insensitive():
    assert core.approval([labeled(JASON, 18, "2026-09-16T10:00:00Z", name="Agent-Ready")])["id"] == 18


def test_the_issue_query_url_encodes_the_label(tmp_path, monkeypatch):
    monkeypatch.setitem(config.settings, "label", "ready & go")
    calls = []
    monkeypatch.setattr(core, "run", lambda argv: calls.append(argv) or "")
    core.intake(REPO)
    assert "labels=ready%20%26%20go&" in calls[0][-1]


def test_a_command_error_never_carries_a_token():
    """CommandError text reaches card bodies (setup_trouble) and logs: a token gh or git echoes is cut first."""
    with pytest.raises(core.CommandError) as e:
        core.run(["sh", "-c", "echo ghp_ABCDEF0123 github_pat_11AB_cd >&2; exit 1", "ghs_inargv9"])
    assert "ghp_" not in str(e.value) and "github_pat_" not in str(e.value) and "[redacted]" in str(e.value)
