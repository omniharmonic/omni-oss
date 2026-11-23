"""omni-meet — Omni attends Google Meet meetings the owner approves, and speaks only where he approves it.

Owner decision 2026-11-12 (docs/omni/meetings.md):
  * attend only meetings the owner approves, from Omni's own Google account (its address is added to
    the invite from the owner's calendar);
  * speak only in meetings where he separately approves speaking, only when addressed by name, under
    strict rules — and he sees every line it says;
  * it is not a substitute for meeting transcripts (captions are lossy) — it is participation.

Pieces: rules (pure), store (grants), gcal (invite), meetbot (Hermes' google_meet bot as a library),
bot_launcher (bot-side: no conversational voice), speech (verbatim TTS → bot microphone),
conductor (the gateway loop), tools (what the agent calls; the guard gates them).
"""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import threading

from . import tools

logger = logging.getLogger(__name__)

_conductor = None
_lock_file = None


def _in_gateway() -> bool:
    argv = [a.lower() for a in sys.argv[1:4]]
    return "gateway" in argv and "run" in argv


def _start_conductor() -> bool:
    """One conductor per machine: the gateway's, held by a lock file."""
    global _conductor, _lock_file
    from . import conductor, store
    try:
        f = open(store.state_dir() / ".conductor.lock", "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _lock_file = f
    _conductor = conductor.Conductor()
    threading.Thread(target=_conductor.run, name="omni-meet-conductor", daemon=True).start()
    return True


def _cli_setup(parser) -> None:
    subs = parser.add_subparsers(dest="omni_meet_command")
    subs.add_parser("status", help="Approved meetings, the bot, and whether this machine is ready")
    auth = subs.add_parser("auth", help="Sign Omni's own Google account in for meetings (run at the agent host's screen)")
    auth.add_argument("--refresh", action="store_true",
                      help="Skip the sign-in window: only check that Omni's Chrome profile is still signed in")


def _cmd_auth(refresh: bool = False) -> int:
    from . import googleauth, meetbot
    auth = meetbot.auth_state_path()
    if not refresh:
        print("Opening Google Chrome on Omni's own profile (not your usual Chrome).\n"
              "  1. Sign in as %s — only that account.\n"
              "  2. When Google shows you are signed in, QUIT that Chrome window (Cmd-Q).\n"
              "Waiting for Chrome to quit ..." % googleauth.rules.AGENT_EMAIL)
        try:
            googleauth.open_for_sign_in(auth)
        except googleauth.AuthError as exc:
            print("Sign-in did not finish: %s" % exc)
            return 1
    print("Checking with Google which account Omni's Chrome profile is signed in to ...")
    try:
        account = googleauth.verify(auth)
    except googleauth.AuthError as exc:
        print("Not signed in: %s" % exc)
        return 1
    except Exception as exc:  # noqa: BLE001 — Playwright/Chrome trouble (is a meeting using the profile?)
        print("Could not check Omni's Chrome profile (%s: %s). If Omni is in a meeting right now, try again "
              "after it leaves." % (type(exc).__name__, str(exc)[:200]))
        return 1
    print("Signed in: Omni joins meetings as %s, in its own Chrome profile." % account)
    return 0


def _cli(args) -> int:
    import json
    cmd = getattr(args, "omni_meet_command", None)
    if cmd == "status":
        print(json.dumps(json.loads(tools.status({})), indent=2))
        return 0
    if cmd == "auth":
        return _cmd_auth(refresh=bool(getattr(args, "refresh", False)))
    print("usage: hermes omni-meet {status,auth}")
    return 2


def register(ctx) -> None:
    if os.environ.get("OMNI_HERMES_GUARD", "").lower() == "buzz":
        return  # a Buzz relay agent: no meetings there
    for schema, handler, emoji in tools.TOOLS:
        ctx.register_tool(name=schema["name"], toolset=tools.TOOLSET, schema=schema, handler=handler, emoji=emoji)
    try:
        ctx.register_cli_command(name="omni-meet", help="Omni in Google Meet (status, auth)",
                                 setup_fn=_cli_setup, handler_fn=_cli,
                                 description="Meetings Omni is approved to attend; sign its Google account in.")
    except Exception as exc:  # noqa: BLE001 — an older Hermes without plugin CLI commands
        logger.info("omni-meet: no CLI (%s)", exc)
    if os.environ.get("OMNI_MEET", "").lower() in ("off", "0", "false"):
        logger.warning("omni-meet: OFF (OMNI_MEET=off) — tools registered, nothing joins")
        return
    if _in_gateway():
        if _start_conductor():
            logger.warning("omni-meet: ACTIVE (conductor running; %d tools)", len(tools.TOOLS))
        else:
            logger.warning("omni-meet: tools only (another process holds the conductor)")
