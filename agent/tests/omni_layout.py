"""A throwaway agent-repo layout for the omni-bridge tests — built the way the
agent host's is, so a test cannot pass on one machine and fail on another.

What the agent host has that a laptop worktree does not, and what this reproduces:

* the repo sits in a directory with its own name (`omni_agent` there;
  any name here — `OMNI_TEST_REPO_NAME`);
* Hermes reaches the plugin through a SYMLINK
  (`<HERMES_HOME>/plugins/omni-bridge -> <repo>/hermes/plugins/omni-bridge`);
* the repo has a venv made from the system Python: `venv/bin/python` is a
  symlink chain that ends OUTSIDE the repo;
* the repo's `.env` and Hermes' `.env` exist and hold ordinary configuration
  (an email address, a chat id, hosts, ports, paths, `true`) next to the
  credentials.

The tests load the plugin from this layout, never from the checkout they sit
in, and never read the machine's real `~/.hermes` or the real repo's `.env`.

Also here: a watchdog every test file arms, so no test run can hang.
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: OMNI_TEST_PLUGIN_SOURCE: test another copy of the plugin (an older release, to see a test catch what it should).
SOURCE = Path(os.environ.get("OMNI_TEST_PLUGIN_SOURCE") or ROOT / "hermes/plugins/omni-bridge")

#: Ordinary configuration, as an env file holds it. None of these is a secret.
ORDINARY = {
    "AGENT_GMAIL": "agent@example.com",
    "OWNER_EMAIL": "owner@example.com",
    "GOG_ACCOUNT": "owner@example.com",
    "TELEGRAM_CHAT_ID": "1234567890",
    "API_SERVER_HOST": "127.0.0.1",
    "API_SERVER_PORT": "18642",
    "OMNI_GATEWAY_URL": "http://127.0.0.1:8787",
    "PARACHUTE_URL": "https://vault.example.com/vault/default",
    "MATRIX_HOMESERVER": "matrix.example.org",
    "OMNI_KB": "/Users/someone/Documents/knowledge-base",
    "TOKEN_FILE": "/Users/someone/.config/app/token.json",
    "KEYCHAIN_SERVICE": "omni-agent-wallet",
    "SAFE_ADDRESS": "0x52908400098527886E0F7030069857D2E4169EE7",
    "DEPLOY_SHA": "57a254a0c1d2e3f4a5b6c7d8e9f00112233445566",
    "AUTH_HEADER": "Authorization",
    "ACTIONS_EMAIL_ENABLED": "true",
    "PASSWORD_MIN_LENGTH": "12",
    "TZ": "America/Edmonton",
    "MODEL_NAME": "claude-opus-latest",
}
#: Credentials, by name or by shape. All of these are secrets.
CREDENTIALS = {
    "API_SERVER_KEY": "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928",
    "OMNI_SERVICE_TOKEN": "svc-token-2f4b6d8a0c1e3f5a7b9d",
    "PROTON_BRIDGE_PASSWORD": "hunter2-hunter2!",
    "TELEGRAM_BOT_TOKEN": "7234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    "AGENT_WALLET_PRIVATE_KEY": "0x" + "ab12cd34" * 8,
    "ANTHROPIC_API_KEY": "sk-ant-api03-notreal-abcdefghijklmnop",
}
#: A value that is a secret only by its shape (the name says nothing).
SHAPED = {"DATABASE_URL": "postgres://app:s3cretPassw0rd@db.example.com:5432/app"}
SHAPED_SECRET = "s3cretPassw0rd"

OMNI_CLI = '''#!/usr/bin/env python3
"""A stand-in for scripts/omni_cli.py: says which interpreter ran it and whether the
repo's .env was readable (it is, for a trusted reader)."""
import os, sys
env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
try:
    n = sum(1 for line in open(env) if "=" in line)
    print("omni-cli stub: %s ok; repo .env readable (%d lines)" % (" ".join(sys.argv[1:]), n))
except OSError as e:
    print("omni-cli stub: repo .env NOT readable (%s)" % type(e).__name__)
    sys.exit(3)
'''


def _system_python() -> str:
    """The real interpreter binary a system-Python venv links to (on the agent host:
    /Library/Developer/CommandLineTools/…/python3.9). Not /usr/bin/python3: that is
    Apple's launcher, which refuses to run under another name."""
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def build(name: str = "") -> dict:
    """Make the layout; return where things are. Removed at exit."""
    name = name or os.environ.get("OMNI_TEST_REPO_NAME") or "omni_agent"
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="omni-layout-")))
    atexit.register(shutil.rmtree, str(base), True)
    repo = base / name
    plugin = repo / "hermes/plugins/omni-bridge"
    plugin.parent.mkdir(parents=True)
    shutil.copytree(str(SOURCE), str(plugin), ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "scripts").mkdir()
    (repo / "scripts/omni_cli.py").write_text(OMNI_CLI)
    # omni_card validates with the agent's card rules, read from the repo next to the plugin.
    (repo / "scripts/proactive_intelligence").mkdir()
    shutil.copy(str(ROOT / "scripts/proactive_intelligence/cards.py"), str(repo / "scripts/proactive_intelligence/cards.py"))
    shutil.copy(str(ROOT / "scripts/gen_capabilities.py"), str(repo / "scripts/gen_capabilities.py"))
    (repo / "config").mkdir()
    shutil.copy(str(ROOT / "config/omni-capabilities.yaml"), str(repo / "config/omni-capabilities.yaml"))
    (repo / "outputs").mkdir()
    # A system-Python venv: python -> python3 -> the system's interpreter, outside the repo.
    bindir = repo / "venv/bin"
    bindir.mkdir(parents=True)
    os.symlink(_system_python(), str(bindir / "python3"))
    os.symlink("python3", str(bindir / "python"))
    (repo / "venv/pyvenv.cfg").write_text("home = %s\n" % os.path.dirname(_system_python()))
    lines = ["# agent repo .env (test layout)"] + ["%s=%s" % kv for kv in list(ORDINARY.items()) + list(CREDENTIALS.items()) + list(SHAPED.items())]
    (repo / ".env").write_text("\n".join(lines) + "\n")
    home = base / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    os.symlink(str(plugin), str(home / "plugins/omni-bridge"))
    (home / ".env").write_text("API_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT=18642\nGATEWAY_ALLOWED_USERS=1234567890\n"
                               "API_SERVER_KEY=%s\nOMNI_GATEWAY_URL=http://127.0.0.1:8787\n" % CREDENTIALS["API_SERVER_KEY"])
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-1\n  base_url: http://127.0.0.1:18661/v1\n"
        "mcp_servers:\n  parachute:\n    url: https://vault.example.com/vault/default/mcp\n    headers:\n"
        "      Authorization: Bearer vault-token-0123456789abcdefghij\n"
        "session:\n  key_prefix: agent:main:telegram\n  cache_key: sessions-by-chat-and-thread\n")
    return {"base": str(base), "repo": str(repo), "hermes_home": str(home),
            "plugin_init": str(home / "plugins/omni-bridge/__init__.py"), "plugin_dir": str(home / "plugins/omni-bridge"),
            "vault_token": "vault-token-0123456789abcdefghij"}


def other_pythons() -> list:
    """Interpreters other than the one running, to re-run a test file under (3.9 ↔ newer)."""
    here = os.path.realpath(sys.executable)
    seen, out = {here}, []
    for cand in ("/usr/bin/python3", "/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3.11",
                 "/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        # Not ~/.hermes/hermes-agent/venv: since Hermes v0.23 (2026-11-12) that venv is a dead leftover — it still
        # imports Hermes' modules but cannot run them (its dependencies moved to the package manager's env).
        if not os.path.exists(cand):
            continue
        real = os.path.realpath(cand)
        try:
            import subprocess
            ver = subprocess.run([cand, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            continue
        if real in seen or not ver or ver == "%d.%d" % sys.version_info[:2] or ver in [v for _c, v in out]:
            continue
        seen.add(real)
        out.append((cand, ver))
    return out


def watchdog(seconds: int) -> None:
    """No test run may hang: past `seconds` every thread's stack is printed and the process
    exits non-zero. (A wrong classification used to mean a ten-minute wait for an approval.)"""
    faulthandler.dump_traceback_later(int(os.environ.get("OMNI_TEST_WATCHDOG_S", "") or seconds), exit=True)
