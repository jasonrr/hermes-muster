"""The rig: a real `hermes gateway run` child with muster loaded from this checkout, the real Slack adapter talking
to Hermes's Slack stand-in, a scripted coordinator, and herdr/gh/git answered by the test's world (design option B).

Three isolation layers (design §5): the shim in every child, PATH scripts that refuse the real herdr and gh, and a
dead-port proxy. The preflight (design §5a) runs before any child starts, and again once the gateway is up; a
failed check raises Preflight naming itself.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

if os.environ.get("MUSTER_ACCEPTANCE") != "1":  # the ordinary suite never shows a green run that skipped these
    collect_ignore_glob = ["test_lifecycle.py", "test_preflight.py", "test_kit.py"]

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / "tests" / "acceptance" / "shim"
CHANNEL, APPROVER, OTHER = "C0ACCEPT", "U0000111", "U0000222"
ALLOWED = {CHANNEL}
DEAD = "http://127.0.0.1:9"
# Hermes's Slack adapter sets the proxy on its clients even for the loopback stand-in, and skips it only when
# NO_PROXY names Slack's hosts (plugins/platforms/slack/adapter.py:861-877). So Slack bypasses the dead proxy; its
# egress rests on the stand-in redirect and the stand-in's fake token (checked below). Jason chose this 2026-10-20.
NO_PROXY = "127.0.0.1,localhost,slack.com,files.slack.com,wss-primary.slack.com"
SCAN = 2  # muster/channels/shared.py:14, the gateway's scan period
DISPLAY = {"tool_progress": "all", "tool_progress_grouping": "accumulate", "interim_assistant_messages": True,
           "cleanup_progress": False, "platforms": {"slack": {"cleanup_progress": True}}}


class Preflight(Exception):
    """A preflight check failed: "<check>: <detail>". Nothing has run that could reach the real world."""


@dataclass
class Rig:
    root: Path
    user: Path  # the children's HOME: apart from the gateway home, so Hermes names no production service (hermes_kit)
    settings: dict
    env: dict  # what GatewayUnderTest passes to its children, before hermetic_env
    world: object
    sock: Path
    driver: object
    director: object
    llm: object
    gw: object = None
    server: object = None
    held: list = field(default_factory=list)  # the builder's held PermissionRequest hooks, killed at teardown
    started: float = field(default_factory=time.monotonic)

    @property
    def hermes(self):
        return self.root / "home" / ".hermes"

    @property
    def journal(self):
        return self.root / "journal.jsonl"

    @property
    def markers(self):
        return self.root / "markers"

    @property
    def standin(self):
        return self.driver.standin

    def child_env(self):
        from tests.acceptance import hermes_kit as kit

        return kit.hermetic_env(self.root / "home", dict(self.env))

    def gw_config(self):
        import hermes_yaml

        return hermes_yaml.safe_load((self.hermes / "config.yaml").read_text())

    def entries(self):
        return [json.loads(line) for line in self.journal.read_text().splitlines()] if self.journal.exists() else []

    def marks(self):
        return [json.loads(p.read_text()) for p in sorted(self.markers.glob("*.json"))] if self.markers.is_dir() else []

    def hermes_cli(self, *argv, payload=None, timeout=120):
        return subprocess.run([str(self.user / ".local/bin/hermes"), *argv], env=self.child_env(), text=True,
                              input=json.dumps(payload) if payload is not None else "", capture_output=True,
                              timeout=timeout, cwd=self.root, check=False)


KEEP = ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "HOME", "PATH", "TMPDIR", "TZ", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "no_proxy")


def scripts(rig, py):
    """$HOME/.local/bin, first on PATH: the hermes wrapper (the production launcher's -I would drop the shim), and
    herdr and gh stubs that journal and refuse."""
    bin_ = rig.user / ".local" / "bin"
    bin_.mkdir(parents=True, exist_ok=True)
    env = rig.child_env()
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in sorted(env.items())
                      if k.startswith(("MUSTER_", "HERMES_", "SLACK_", "XDG_")) or k in KEEP)
    (bin_ / "hermes").write_text(f"#!/bin/sh\n{exports}exec {shlex.quote(py)} -m hermes_cli.main \"$@\"\n")
    for name in ("herdr", "gh"):
        line = shlex.quote(json.dumps({"stub": name, "refused": True}))
        (bin_ / name).write_text(f"#!/bin/sh\necho {line} >> {shlex.quote(str(rig.journal))}\nexit 97\n")
    for name in ("hermes", "herdr", "gh"):
        (bin_ / name).chmod(0o755)


def build(factory, settings=None, display=None, prepend=None, plugin=None):
    """An unstarted rig; the keyword arguments are what the negative tests break."""
    from tests.acceptance import hermes_kit as kit
    from tests.acceptance import world as W

    root = factory.mktemp("acc")
    user, clone, hermes = root / "user", root / "clones" / "demo", root / "home" / ".hermes"
    for d in (user, clone, hermes / "plugins", root / "tmp"):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(clone)], check=True)  # the world serves its remote
    (hermes / "plugins" / "muster").symlink_to(plugin or os.environ.get("MUSTER_UNDER_TEST") or REPO)
    under = str(Path(os.environ.get("MUSTER_UNDER_TEST") or REPO).resolve())
    settings = {"approver_login": "tester", "approver_id": 1, "repos": [f"demo/demo={clone}"], "board": "muster-acc",
                "notify_platform": "slack", "notify_chat_id": CHANNEL, "notify_user_id": APPROVER,
                "notify_chat_type": "group", "notify_topics": True, "coordinator_name": "Ella",
                "worktrees": str(root / "worktrees"), **(settings or {})}
    driver, director = kit.SlackDriver(), kit.Director()
    llm = kit.FakeLLMServer(director)
    driver.start()
    llm.start()
    base = driver.gateway_env()
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if Path(p).expanduser() != Path.home() / ".local" / "bin")
    sock = W.short_sock()
    env = {**base, "SLACK_ALLOWED_USERS": base["SLACK_ALLOWED_USERS"] + f",{OTHER}",
           "PYTHONPATH_PREPEND": str(prepend or SHIM), "MUSTER_CHAIN_SITECUSTOMIZE": str(kit.SLACK_SHIM),
           "MUSTER_UNDER_TEST": under, "MUSTER_WORLD_SOCK": str(sock), "MUSTER_JOURNAL": str(root / "journal.jsonl"),
           "MUSTER_MARKERS": str(root / "markers"), "HERMES_KANBAN_HOME": str(hermes),
           "HTTPS_PROXY": DEAD, "HTTP_PROXY": DEAD, "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY, "PATH": f"{user / '.local/bin'}{os.pathsep}{path}",
           "HOME": str(user), "TMPDIR": str(root / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
    rig = Rig(root, user, settings, env, W.AcceptanceWorld(root, root / "journal.jsonl"), sock, driver, director, llm)
    config = {"plugins": {"enabled": ["muster"], "isolation": "in_process", "entries": {"muster": {"settings": settings}}},
              "display": display or DISPLAY, **driver.gateway_config()}
    config["platforms"]["slack"]["extra"]["allowed_channels"] = [CHANNEL]
    rig.gw = kit.GatewayUnderTest(root, llm_base_url=llm.base_url, config=config, env=env, ready=driver.connected)
    scripts(rig, sys.executable)
    return rig


def preflight_static(rig):
    """Before any child starts: every writable path under the root, the plugin's code the one pinned path outside,
    the channel allowlisted, the PATH scripts first, the stand-in's tokens, the dead proxy."""
    root, env, child = rig.root.resolve(), rig.env, rig.child_env()  # child: what the children actually get
    writable = {k: child[k] for k in ("HOME", "HERMES_HOME", "HERMES_KANBAN_HOME", "XDG_STATE_HOME", "TMPDIR",
                                      "MUSTER_JOURNAL", "MUSTER_MARKERS")}
    writable.update({"worktrees": rig.settings["worktrees"],
                     "data dir": Path(child["HERMES_HOME"]) / "plugin-data" / "muster",  # plugins/plugin_storage.py
                     **{f"repo {r}": r.split("=", 1)[1] for r in rig.settings["repos"]}})
    for what, p in writable.items():
        if not Path(p).expanduser().resolve().is_relative_to(root):
            raise Preflight(f"paths: {what} {p} is outside the test root")
    link, under = rig.hermes / "plugins" / "muster", Path(env["MUSTER_UNDER_TEST"]).resolve()
    if not link.is_symlink() or link.resolve() != under:
        raise Preflight(f"paths: the plugin {link} resolves to {link.resolve()}, not the muster under test {under}")
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(d) / name
            if p.is_symlink() and p != link and not p.resolve().is_relative_to(root):
                raise Preflight(f"paths: {p} links outside the test root")
    if rig.settings["notify_chat_id"] not in ALLOWED:
        raise Preflight(f"channel: {rig.settings['notify_chat_id']} is not allowlisted")
    slack = rig.gw_config()["platforms"]["slack"]["extra"]
    if slack.get("allowed_channels") != [rig.settings["notify_chat_id"]]:
        raise Preflight(f"channel: Hermes's Slack allowed_channels is {slack.get('allowed_channels')}")
    if rig.settings["notify_user_id"] != APPROVER:
        raise Preflight(f"identity: the approver is {rig.settings['notify_user_id']}, not the test user")
    for name in ("hermes", "herdr", "gh"):
        found = shutil.which(name, path=env["PATH"])
        if found != str(rig.user / ".local" / "bin" / name):
            raise Preflight(f"path-scripts: {name} resolves to {found}")
    s = rig.standin
    if (env.get("SLACK_BOT_TOKEN"), env.get("SLACK_APP_TOKEN")) != (s.bot_token, s.app_token) or (
            env.get("HERMES_STANDIN_SLACK_API") != s.api_base or not s.api_base.startswith("http://127.0.0.1:")):
        raise Preflight("tokens: the child's Slack tokens or API base are not the stand-in's")
    if (env.get("HTTPS_PROXY"), env.get("HTTP_PROXY"), env.get("NO_PROXY"), env.get("no_proxy")) != (
            DEAD, DEAD, NO_PROXY, NO_PROXY):
        raise Preflight("egress: the proxies are not the dead port, or more than loopback and Slack bypass it")


def preflight_dynamic(rig):
    """Once the gateway is up: its muster is shimmed, a canary CLI is too and journals nothing, the adapter talks
    to the stand-in's identity, and nothing named slack.com."""
    from tests.acceptance import hermes_kit as kit

    def gateway():
        return [m for m in rig.marks() if "gateway" in m["argv"] and m["patched"]]

    try:
        kit.wait_until(gateway, "the gateway's muster loaded through the shim", timeout=15)
    except AssertionError:
        raise Preflight("gateway-marker: the gateway's muster left no shim marker") from None
    before = {m["pid"] for m in rig.marks()}
    done = rig.hermes_cli("muster", "recover", "nosuch")
    if not any("recover" in m["argv"] and m["patched"] for m in rig.marks() if m["pid"] not in before):
        raise Preflight(f"canary: `hermes muster recover nosuch` left no marker (rc {done.returncode})")
    if rig.entries():
        raise Preflight(f"canary: the journal is not empty: {rig.entries()}")
    seen = [c.response for c in rig.standin.calls_of("auth.test")]
    pinned = {"team_id": "T0STANDIN", "user_id": "UBOT0001", "bot_id": "B0STANDIN"}
    if not seen or any({k: (r or {}).get(k) for k in pinned} != pinned for r in seen):
        raise Preflight(f"identity: auth.test answered {seen}")
    if re.search(r"slack\.com", rig.gw.log_path.read_text()):
        raise Preflight("no-real-slack: the gateway log names slack.com")


def start(rig):
    from tests.acceptance import world as W

    preflight_static(rig)
    rig.server = W.serve(rig.world, rig.sock)
    created = rig.gw.run_cli("kanban", "boards", "create", rig.settings["board"])
    assert created.returncode == 0, created.stdout + created.stderr
    board = rig.hermes / "kanban" / "boards" / rig.settings["board"] / "board.json"
    board.write_text(json.dumps({**json.loads(board.read_text()), "human_notices": True}))  # docs/setup.md step 3
    rig.started = time.monotonic()
    rig.gw.start()
    preflight_dynamic(rig)
    return rig


SECRET = re.compile(r"xox[a-z]-\S+|xapp-\S+")


def shown(text, blocks):
    """A message as text, its blocks flattened the way test_cards renders them."""
    from tests.test_cards import flat

    return (text or "") + ("\n" + flat(blocks) if blocks else "")


def timeline(rig):
    """root/timeline.md: every chat write in order, then what a human sees per thread, then the journal and the
    markers. Tokens are redacted. Returns the path."""
    out = ["# Timeline", "", "## chat.* calls"]
    for c in rig.standin.calls_of("chat.postMessage", "chat.update", "chat.delete"):
        p, r = c.params, c.response if isinstance(c.response, dict) else {}
        first = " ".join(shown(p.get("text"), p.get("blocks") or []).split())[:80]
        out.append(f"+{c.at - rig.started:7.2f}s {c.method:16} ts={p.get('ts') or r.get('ts')} "
                   f"thread={p.get('thread_ts')} | {first}")
    out += ["", f"## visible({CHANNEL})"]
    threads = {}
    for v in rig.standin.visible(CHANNEL):
        threads.setdefault(v.extra.get("thread_ts") or v.message_id, []).append(v)
    for parent, messages in threads.items():
        out.append(f"### thread {parent}")
        for v in messages:
            first = " ".join(shown(v.text, v.extra.get("blocks") or []).split())[:120]
            out.append(f"- {v.message_id} edits={v.edits}{' DELETED' if v.deleted else ''} | {first}")
    out += ["", "## journal", *(json.dumps(e) for e in rig.entries()), "", "## markers",
            *(json.dumps(m) for m in rig.marks())]
    path = rig.root / "timeline.md"
    path.write_text(SECRET.sub("[redacted]", "\n".join(out)) + "\n")
    return path


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if report.when == "call" and report.failed:
        for rig in getattr(item, "rigs", []):
            path = timeline(rig)
            tail = "\n".join(path.read_text().splitlines()[-60:])
            report.sections.append(("timeline", f"{path}\n{tail}"))


def stop(rig):
    for held in rig.held:
        held.kill()
        held.wait()
    if rig.gw:
        rig.gw.stop()  # SIGTERM, then SIGKILL, to the gateway's whole process group
    rig.llm.stop()
    rig.driver.stop()
    if rig.server:
        rig.server.shutdown()
        rig.server.server_close()
    shutil.rmtree(rig.sock.parent, ignore_errors=True)


@pytest.fixture
def make_rig(tmp_path_factory, request):
    """make_rig(**breaks) → an unstarted rig, stopped at teardown; start(rig) runs both preflights."""
    rigs = request.node.rigs = []

    def make(**kwargs):
        rigs.append(build(tmp_path_factory, **kwargs))
        return rigs[-1]

    yield make
    for rig in rigs:
        try:
            stop(rig)
        except Exception as caught:  # noqa: BLE001 - every rig's children are stopped, whatever one raised
            print(f"stopping {rig.root}: {caught!r}")
    for rig in rigs:
        assert not rig.world.unmodelled, f"muster reached a call the world does not model: {rig.world.unmodelled}"
        log = rig.gw.log_path.read_text() if rig.gw.log_path.exists() else ""
        assert "slack.com" not in log, "the gateway named slack.com during the run"  # the preflight's check, again


@pytest.fixture
def rig(make_rig):
    return start(make_rig())
