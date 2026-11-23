#!/usr/bin/env python3
"""What would the Omni tool policy do with this shell command? (and, with --run, do it)

For the owner at the agent host's Terminal — to check a command before relying on it,
or to see why one failed inside the sandbox. It uses the very code Hermes runs
(hermes/plugins/omni-bridge), with the settings of the Hermes home it is
pointed at. It never contacts the Omni gateway and never prints a setting's value.

    scripts/omni_policy_check.py 'gog calendar events primary --from 2026-10-09T00:00:00Z'
    scripts/omni_policy_check.py --run 'gog calendar events primary --from 2026-10-09T00:00:00Z'
    scripts/omni_policy_check.py --run --approve 'git push origin main'    # you are the approval
    scripts/omni_policy_check.py --selftest                                 # is the sandbox working here?
    scripts/omni_policy_check.py --explain-secrets                          # which values are scrubbed? (names only)

Exit code: with --run, the command's own; otherwise 0 = runs freely, 3 = would
ask, 4 = refused, 5 = the sandbox does not work on this machine.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "hermes/plugins/omni-bridge"


def load(plugin_dir=None):
    name = "omni_bridge_check"
    plugin = Path(plugin_dir) if plugin_dir else PLUGIN
    spec = importlib.util.spec_from_file_location(name, plugin / "__init__.py", submodule_search_locations=[str(plugin)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def read_settings(home: str) -> None:
    """The OMNI_BRIDGE_* settings Hermes would have, from its .env (names only are used
    here; nothing is printed). Anything already in this shell's environment wins."""
    try:
        text = Path(home, ".env").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    for line in text.splitlines():
        key, eq, value = line.strip().partition("=")
        if eq and key.startswith("OMNI_BRIDGE_") and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", help="the shell command line, quoted")
    ap.add_argument("--cwd", default=os.getcwd(), help="where it would run (default: here)")
    ap.add_argument("--run", action="store_true", help="run it the way Hermes would, inside the sandbox")
    ap.add_argument("--approve", action="store_true", help="with --run: you are approving a command that would ask")
    ap.add_argument("--selftest", action="store_true", help="check that the sandbox denies what it should on this machine")
    ap.add_argument("--explain-secrets", action="store_true",
                    help="per env file: which NAMES hold a value treated as a secret, and which do not (never a value)")
    ap.add_argument("--plugin-dir", default="", help=argparse.SUPPRESS)
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    a = ap.parse_args()

    os.environ["HERMES_HOME"] = a.hermes_home
    read_settings(a.hermes_home)
    b = load(a.plugin_dir)
    policy, sandbox, paths = b.policy, b.sandbox, b.paths

    if a.explain_secrets:
        print("Values scrubbed from tool results and refused in commands — by NAME (values are never shown).")
        print("A name is a secret when it says credential (…_KEY, …_TOKEN, …_SECRET, …_PASSWORD, …_HASH …) and its value")
        print("is at least 8 characters; ordinary configuration names are left alone. Also scrubbed: a value with the shape")
        print("of a credential. Settle a wrong call with OMNI_BRIDGE_SECRET_KEYS / OMNI_BRIDGE_NOT_SECRET_KEYS.\n")
        total = 0
        for r in b.redact.explain():
            src = str(r["source"])
            if not r.get("exists"):
                print("%s\n    (no such file)\n" % src)
                continue
            if r.get("readable") is False:
                print("%s\n    (not readable)\n" % src)
                continue
            names = list(r["secrets"])
            total += len(names)
            print("%s\n    %d of %d values are treated as secrets" % (src, len(names), r["assignments"]))
            if names:
                print("    secret:     %s" % ", ".join(names))
            if src.endswith(".env") or "/.env" in src:
                try:
                    every = [n for n, _v in b.redact._pairs(src, open(src, encoding="utf-8", errors="ignore").read(2_000_000))]
                    rest = sorted(set(every) - set(names))
                    if rest:
                        print("    not secret: %s" % ", ".join(rest))
                except OSError:
                    pass
            print()
        print("%d names in all." % total)
        return 0

    if a.selftest:
        if not sandbox.available():
            print("sandbox: NOT WORKING on this machine — every Omni shell command would ask for approval")
            return 5
        probes = (
            ("a secret does not open", "cat %s/.env" % a.hermes_home, False),
            ("the network is closed", "python3 -c \"import socket; socket.create_connection(('192.0.2.1', 443), timeout=3)\"", False),
            ("a launchd job cannot be made", "/bin/launchctl list", False),
            ("another app cannot be driven", "/usr/bin/osascript -e 'tell application \"Finder\" to get name of startup disk'", False),
            ("outside the workspace is read-only", "touch %s/omni-policy-selftest" % os.path.expanduser("~"), False),
            ("the workspace is writable", "mkdir -p {0} && touch {0}/.omni-selftest && rm {0}/.omni-selftest".format(paths.default_workspace()), True),
            ("ordinary work runs", "python3 -c 'print(6*7)' && git --version", True),
        )
        os.makedirs(paths.default_workspace(), exist_ok=True)
        bad = 0
        for label, cmd, should_work in probes:
            line = sandbox.wrap(cmd, sandbox.profile("contained"), tier="contained", cwd=paths.default_workspace())
            ok = subprocess.run(["/bin/bash", "-c", line], capture_output=True, timeout=60).returncode == 0
            good = ok == should_work
            bad += 0 if good else 1
            print("%s  %s" % ("ok  " if good else "FAIL", label))
        print("sandbox: %s" % ("working" if not bad else "%d check(s) FAILED — do not switch Omni to full tools" % bad))
        return 0 if not bad else 5

    if not a.command:
        ap.error("give a command, or --selftest")
    cwd = os.path.realpath(a.cwd)
    d = policy.decide_shell(a.command, cwd)
    grants = ", ".join(n for n in (("network" if d.network else ""), ("Keychain" if d.keychain else ""),
                                   ("write anywhere" if d.write_anywhere else ""), ("automation" if d.automation else "")) if n)
    print("decision: %s%s" % ({"allow": "runs without asking", "approve": "ASKS the owner first", "block": "REFUSED"}[d.action],
                              (" — %s" % d.rule) if d.rule else ""))
    if d.title:
        print("what:     %s" % d.title)
    if d.message:
        print("why:      %s" % d.message)
    if d.action != policy.BLOCK:
        print("sandbox:  %s%s%s" % (d.tier, (" + " + grants) if grants else "",
                                    (" + reads " + ", ".join(d.secrets)) if d.secrets else ""))
    if not a.run:
        return {"allow": 0, "approve": 3, "block": 4}[d.action]
    if d.action == policy.BLOCK:
        print("not run: a refused action cannot be approved.")
        return 4
    if d.action == policy.APPROVE and not a.approve:
        print("not run: this would ask. Add --approve to run it as an approved command.")
        return 3
    if not sandbox.available():
        print("not run: the sandbox does not work on this machine.")
        return 5
    prof = sandbox.profile(d.tier, network=d.network, keychain=d.keychain, read_secrets=d.grants(),
                           write_anywhere=d.write_anywhere, automation=d.automation)
    line = sandbox.wrap(d.command if d.command is not None else a.command, prof, tier=d.tier, cwd=d.cwd or cwd, trusted_env=d.trusted_env())
    print("─" * 60, flush=True)
    return subprocess.run(["/bin/bash", "-c", line]).returncode


if __name__ == "__main__":
    sys.exit(main())
