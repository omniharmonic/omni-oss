"""Hermes' google_meet bot, used as a library — never as the stock plugin.

Why not enable the stock ``google_meet`` plugin: its ``on_session_end`` hook stops the active meeting
when ANY Hermes session ends, and the gateway ends sessions all the time (the calendar cron alone runs
every two minutes), so an attended meeting would be dropped within minutes; and its ``meet_say``
speaks through OpenAI's conversational Realtime model. We import its modules (process manager, bot,
auth) — they ship with Hermes whether the plugin is enabled or not — and start the bot through
``bot_launcher.py``.

The launch is redirected only for the spawn WE start (a context variable is set around our call to
``process_manager.start``): its ``subprocess`` module reference is replaced by a proxy whose ``Popen``
rewrites exactly ``[python, "-P", "-m", "plugins.google_meet.meet_bot"]``. Any other call passes through.
If google_meet's shape changes, ``check()`` says so and nothing is joined.

The same spawn — and only it — gets the conductor's per-meeting recording decision in its environment:
``OMNI_MEET_UNMUTE_AUDIO=1`` when Chrome may play the call (so Minutes can record it), and the variable
removed otherwise, whatever the gateway's own environment holds (bot_launcher.audio_unmuted).
"""

from __future__ import annotations

import contextvars
import os
import socket
import subprocess as _subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

LAUNCHER = str(Path(__file__).resolve().parent / "bot_launcher.py")
BOT_MODULE = "plugins.google_meet.meet_bot"
#: The bot refuses realtime without a key; ours never reaches OpenAI (bot_launcher), so a placeholder.
LOCAL_VOICE_KEY = "omni-meet-local-voice"

#: Kept in step with bot_launcher.UNMUTE_ENV (a test checks; bot_launcher runs as a bare script).
UNMUTE_ENV = "OMNI_MEET_UNMUTE_AUDIO"

_OURS: contextvars.ContextVar[bool] = contextvars.ContextVar("omni_meet_spawn", default=False)
_UNMUTE: contextvars.ContextVar[bool] = contextvars.ContextVar("omni_meet_unmute", default=False)


def _is_bot(argv: Any) -> bool:
    return isinstance(argv, (list, tuple)) and len(argv) >= 3 and list(argv[-2:]) == ["-m", BOT_MODULE]


def rewrite_argv(argv: Any) -> Any:
    """``[py, -P, -m, plugins.google_meet.meet_bot]`` → ``[py, -P, bot_launcher.py]``; else unchanged."""
    if _OURS.get() and _is_bot(argv):
        return [*list(argv[:-2]), LAUNCHER]
    return argv


def bot_env(env: Optional[Dict[str, str]], unmute: bool) -> Dict[str, str]:
    """The bot's environment with exactly our audio decision in it (a copy; ``None`` = this process's)."""
    out = dict(os.environ if env is None else env)
    if unmute:
        out[UNMUTE_ENV] = "1"
    else:
        out.pop(UNMUTE_ENV, None)
    return out


class _SubprocessProxy:
    """Stands in for ``subprocess`` inside google_meet.process_manager only."""

    def __init__(self, real):
        self._real = real

    def Popen(self, argv, *args, **kwargs):  # noqa: N802 — mirrors subprocess.Popen
        if _OURS.get() and _is_bot(argv):
            kwargs["env"] = bot_env(kwargs.get("env"), _UNMUTE.get())
        return self._real.Popen(rewrite_argv(argv), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


_pm = None


def pm():
    """google_meet.process_manager with our spawn proxy installed (idempotent)."""
    global _pm
    if _pm is None:
        from plugins.google_meet import process_manager
        if not isinstance(process_manager.subprocess, _SubprocessProxy):
            process_manager.subprocess = _SubprocessProxy(process_manager.subprocess)
        _pm = process_manager
    return _pm


def check() -> List[str]:
    """What is missing for Omni to attend meetings on this machine (empty = ready)."""
    problems = []
    try:
        mod = pm()
        for name in ("start", "stop", "status"):
            if not callable(getattr(mod, name, None)):
                problems.append("google_meet.process_manager.%s is gone" % name)
        if "subprocess.Popen([sys.executable" not in Path(mod.__file__).read_text():
            problems.append("google_meet changed how it starts the bot")
    except Exception as exc:  # noqa: BLE001
        return ["google_meet is not importable: %s" % exc]
    try:
        from plugins.google_meet.tools import check_meet_requirements
        if not check_meet_requirements():
            problems.append("Playwright/Chromium for meetings is not installed (hermes pm install --extra google-meet)")
    except Exception as exc:  # noqa: BLE001
        problems.append("cannot check meeting requirements: %s" % exc)
    return problems


def auth_state_path() -> Path:
    from plugins.google_meet.cli import _auth_state_path
    return Path(_auth_state_path())


def signed_in_account() -> Optional[str]:
    """Omni's address when a verified, still-live Google session is saved (googleauth), else None."""
    from . import googleauth
    try:
        return googleauth.signed_in_account(auth_state_path())
    except Exception:  # noqa: BLE001
        return None


def verify_sign_in() -> Optional[str]:
    """Ask Google now (headless Chrome on Omni's profile; a few seconds) and record the answer. Only when no
    bot is using the profile — the conductor calls it just before starting one. None = not signed in."""
    from . import googleauth
    auth = auth_state_path()
    if not googleauth.profile_dir(auth).is_dir():
        return None
    try:
        return googleauth.verify(auth)
    except googleauth.AuthError:
        return None


def start(url: str, *, mode: str, duration_s: int, guest_name: str, unmute_audio: bool = False) -> Dict[str, Any]:
    auth = auth_state_path()
    # The auth path is passed only when Omni's profile was last found signed in: the bot launcher then runs
    # Chrome on that profile (no cookie copy). Otherwise the bot joins as a guest named Omni.
    signed_in = signed_in_account() is not None
    token, unmute = _OURS.set(True), _UNMUTE.set(bool(unmute_audio))
    try:
        return pm().start(url, guest_name=guest_name, duration="%ds" % max(60, int(duration_s)),
                          auth_state=str(auth) if signed_in else None, mode=mode,
                          realtime_api_key=LOCAL_VOICE_KEY if mode == "realtime" else None)
    finally:
        _UNMUTE.reset(unmute)
        _OURS.reset(token)


# ── Chrome's profile lock ───────────────────────────────────────────────────
#
# Chrome marks a profile in use with three symlinks; SingletonLock points at "<hostname>-<pid>". When the
# bot's Chrome is killed (launchd bootout, 2026-11-21) they stay behind. Chrome usually copes, but before
# a join they are cleared when — and only when — the lock names THIS host and a pid that is not running.

SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def stale_lock(target: str, hostname: str, pid_alive) -> bool:
    """Whether a SingletonLock target ``"<host>-<pid>"`` is stale: this host, and a pid that is not alive.
    Anything unreadable, another host, or a live pid is NOT stale (never touched)."""
    host, sep, pid = (target or "").rpartition("-")
    if not sep or not host or not pid.isdigit() or int(pid) <= 0:
        return False
    if host != hostname:
        return False
    return not pid_alive(int(pid))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def clear_stale_locks(profile: Path, hostname: Optional[str] = None, pid_alive=_pid_alive) -> List[str]:
    """Remove Chrome's Singleton* links from ``profile`` when its lock is stale; the names removed."""
    lock = Path(profile) / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return []
    if not stale_lock(target, hostname if hostname is not None else socket.gethostname(), pid_alive):
        return []
    removed = []
    for name in SINGLETON_FILES:
        p = Path(profile) / name
        if os.path.lexists(p):
            try:
                os.unlink(p)
                removed.append(name)
            except OSError:
                pass
    return removed


def clear_stale_profile_locks() -> List[str]:
    """clear_stale_locks on Omni's own Chrome profile (nothing when it cannot be found)."""
    from . import googleauth
    try:
        prof = googleauth.profile_dir(auth_state_path())
    except Exception:  # noqa: BLE001 — google_meet not importable: no profile to clear
        return []
    return clear_stale_locks(prof) if prof.is_dir() else []


def stop(reason: str) -> Dict[str, Any]:
    return pm().stop(reason=reason)


#: process_manager.status() merges the bot's status.json, whose keys are camelCase. One spelling here.
_STATUS_KEYS = {"ok": "ok", "alive": "alive", "url": "url", "outDir": "out_dir", "inCall": "in_call",
                "lobbyWaiting": "lobby_waiting", "realtime": "realtime", "realtimeReady": "realtime_ready",
                "error": "error", "leaveReason": "leave_reason", "exited": "exited",
                "transcriptLines": "transcript_lines", "micState": "mic_state"}


def normalize_status(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {ours: raw.get(theirs) for theirs, ours in _STATUS_KEYS.items() if theirs in raw}


def status() -> Dict[str, Any]:
    return normalize_status(pm().status())


def transcript_text(out_dir: Optional[str]) -> str:
    if not out_dir:
        return ""
    p = Path(out_dir) / "transcript.txt"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
