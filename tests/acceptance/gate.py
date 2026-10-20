"""The install gate (design §6): run the acceptance suite against the muster to install, then check that production's
display and muster settings are what the suite tested. One verdict; exit 0 only on PASS.

  PASS             every test passed and production's settings match
  FAIL             anything failed
  BLOCKED          a skip, xfail or xpass, each with its reason: a skip never counts as acceptance
  CONFIG MISMATCH  the differing key names only, never their values

Usage: $PY tests/acceptance/gate.py [--muster <path>]   (HERMES_AGENT_DIR must be set)
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

DISPLAY = {"tool_progress": "all", "tool_progress_grouping": "accumulate", "interim_assistant_messages": True,
           "cleanup_progress": False, "platforms": {"slack": {"cleanup_progress": True}}}
MUSTER = {"notify_platform": "slack", "notify_topics": True}
REPO = Path(__file__).resolve().parents[2]
SECRET = re.compile(r"xox[a-z]-\S+|xapp-\S+|gh[pousr]_\w+|github_pat_\w+")


def tests(junit, report):
    """(failed, blocked): test ids, and "id: reason" lines, from pytest's junit XML and its -rA report."""
    failed, blocked, ran = [], [], 0
    for case in ET.parse(junit).iter("testcase"):
        ran += "test_lifecycle" in (case.get("classname") or "")
        name = f"{case.get('classname')}::{case.get('name')}"
        if (bad := case.find("failure") if case.find("failure") is not None else case.find("error")) is not None:
            why = (bad.get("message") or "").strip().split("\n")[0][:160]  # an expected red reads apart from a crash
            failed.append(f"{name}: {SECRET.sub('[redacted]', why)}")
        elif (skipped := case.find("skipped")) is not None:
            kind = "xfail" if skipped.get("type") == "pytest.xfail" else "skipped"
            blocked.append(f"{name}: {kind}: {skipped.get('message')}")
    blocked += [f"{line.split(' ', 1)[1]}: xpass" for line in report.splitlines() if line.startswith("XPASS ")]
    if not ran:  # no lifecycle test ran (exit 5, or the file stopped being collected): nothing was accepted
        failed.append("no lifecycle test ran")
    return failed, blocked


def mismatches(text, load):
    """Key names where production differs from what the suite tests. Only these keys are read; the rest of the
    file is dropped unread, and an unparseable file is reported without its error text (it may quote a secret)."""
    try:
        data = load(text) or {}
        display = data.get("display") or {}
        settings = (((data.get("plugins") or {}).get("entries") or {}).get("muster") or {}).get("settings") or {}
        seen = {"display.tool_progress": display.get("tool_progress"),
                "display.cleanup_progress": display.get("cleanup_progress", False),
                "display.platforms.slack": (display.get("platforms") or {}).get("slack") or {},
                **{f"plugins.entries.muster.settings.{k}": settings.get(k) for k in MUSTER}}
    except Exception:  # noqa: BLE001 - never echo what the parser saw
        return ["config: unreadable"]
    want = {"display.tool_progress": DISPLAY["tool_progress"], "display.cleanup_progress": DISPLAY["cleanup_progress"],
            "display.platforms.slack": DISPLAY["platforms"]["slack"],
            **{f"plugins.entries.muster.settings.{k}": v for k, v in MUSTER.items()}}
    return [k for k in want if seen[k] != want[k]]


def verdict(failed, blocked, config):
    if failed:
        return "FAIL", [f"failed: {n}" for n in failed]
    if blocked:
        return "BLOCKED", blocked
    if config:
        return "CONFIG MISMATCH", config
    return "PASS", []


def main(argv=None):
    parser = argparse.ArgumentParser(description="The install gate: only PASS proceeds.")
    parser.add_argument("--muster", default=str(REPO), help="the muster checkout to install (default: this one)")
    args = parser.parse_args(argv)
    if not os.environ.get("HERMES_AGENT_DIR"):
        print("BLOCKED: HERMES_AGENT_DIR unset")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        env = {**os.environ, "MUSTER_ACCEPTANCE": "1", "MUSTER_UNDER_TEST": str(Path(args.muster).resolve()),
               "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/acceptance", "-q",
                               "-rA", f"--junitxml={junit}"], cwd=REPO, env=env, capture_output=True, text=True,
                              check=False)
        if not junit.exists():
            print("FAIL\n  pytest wrote no report:\n" + re.sub(r"(?m)^", "  ", done.stdout[-2000:]))
            return 1
        failed, blocked = tests(junit, done.stdout)
    import hermes_yaml  # Hermes's own YAML loader, from the test interpreter

    path = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "config.yaml"
    try:
        text = path.read_text()
    except OSError:
        text = None
    config = mismatches(text, hermes_yaml.safe_load) if text is not None else ["config: unreadable"]
    name, lines = verdict(failed, blocked, config)
    print(name)
    for line in lines:
        print(f"  {line}")
    return 0 if name == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
