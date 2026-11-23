"""omni-resource-guard — keep Hermes from starving the Mac it runs on.

Why this exists (incident 2026-11-08)
-------------------------------------
A trip-research turn ("compare flights and holiday rentals across several sites")
called the ``agent-browser`` CLI straight from the ``terminal`` tool, with a new
self-chosen ``--session`` name per site and never ``close``. Each session is a
daemon plus a headless Chrome (~10 processes). In 11 minutes the agent host had
60 daemons and ~600 Chrome processes, swap was full (18 of 19 GB), the load
average hit 450, and Prism Server, the vault and the tunnel stopped answering.

Hermes' own browser tool cleans up idle sessions — but only the ones IT made
(``h_<uuid>`` names). Sessions the model starts through the shell are invisible
to it, and agent-browser's own daemon idle timeout defaults to one hour.

What this plugin does
---------------------
1. ``AGENT_BROWSER_IDLE_TIMEOUT_MS`` defaults to 5 minutes for this process and
   every shell it starts (terminal subprocesses inherit ``os.environ``), so a
   forgotten session goes away on its own.
2. ``pre_tool_call`` on ``terminal``: an ``agent-browser`` command that would
   start a NEW session is refused when ``OMNI_BROWSER_MAX_SESSIONS`` (3) are
   already live, or when macOS reports critical memory pressure. Commands that
   start browsers in parallel or in a loop (``&`` backgrounding, ``xargs -P``,
   ``parallel``, a ``$`` in the session name, more than one new session in one
   command) are refused outright: one session per command, sites one after
   another. ``close``, ``session list``, ``--help`` always pass.
3. ``pre_tool_call`` on Hermes' own ``browser_navigate``: refused only at the
   hard cap or under critical pressure (each task owns one such session).
4. A reaper thread (one per user, via a lock file) runs every 60 s and kills
   agent-browser daemons — with their Chrome — that are older than
   ``OMNI_BROWSER_MAX_AGE_S`` (1 h), beyond ``OMNI_BROWSER_HARD_CAP`` (6,
   oldest first), or all but the newest one under critical memory pressure.
   That catches anything that got past the hook (a full binary path, npx, a
   script).

Scope and failure policy
------------------------
Active in every Hermes process that loads it (gateway, desktop, CLI, Buzz
agents) unless ``OMNI_RESOURCE_GUARD=off``. Unlike omni-buzz-guard (a security
boundary, fails closed) this guard FAILS OPEN: a bug in a resource limiter must
never stop every shell command. The startup log line
``omni-resource-guard: ACTIVE`` proves it loaded.

The reaper treats every agent-browser daemon of this user as fair game,
including ones started outside Hermes. On the agent host that is intended.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(int(os.environ.get(name, default)), floor)
    except (TypeError, ValueError):
        return default


ACTIVE = os.environ.get("OMNI_RESOURCE_GUARD", "").strip().lower() not in ("off", "0", "false")
MAX_SESSIONS = _env_int("OMNI_BROWSER_MAX_SESSIONS", 3)
HARD_CAP = max(_env_int("OMNI_BROWSER_HARD_CAP", 6), MAX_SESSIONS)
MAX_AGE_S = _env_int("OMNI_BROWSER_MAX_AGE_S", 3600, floor=300)
IDLE_TIMEOUT_MS = str(_env_int("OMNI_BROWSER_IDLE_MS", 300_000, floor=30_000))
REAP_INTERVAL_S = 60

#: agent-browser's daemon binary (homebrew: agent-browser-darwin-arm64; npm: agent-browser-<os>-<arch>).
_DAEMON_RE = re.compile(r"agent-browser-(darwin|linux)-(arm64|x64)\b")
#: Subcommands that never start a daemon.
_NO_SESSION_CMDS = {"close", "session", "install", "skills", "profiles", "auth", "plugin", "help"}
_NO_SESSION_FLAGS = {"--help", "-h", "--version", "-V"}
#: Shell shapes that start browsers in parallel or in bulk.
_FANOUT_RE = re.compile(r"(^|[^&>|])&(\s|$)|\bxargs\b[^\n;|]*\s-P\b|\bparallel\b")

#: macOS kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical.
PRESSURE_CRITICAL = 4


# ── facts about the machine (patched in tests) ───────────────────────────────

def socket_dir() -> Path:
    return Path(os.environ.get("AGENT_BROWSER_SOCKET_DIR") or Path.home() / ".agent-browser")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_sessions() -> list[str]:
    """Names of agent-browser sessions whose daemon is running (``<name>.pid`` + live pid)."""
    out = []
    try:
        for f in socket_dir().glob("*.pid"):
            try:
                if _pid_alive(int(f.read_text().strip())):
                    out.append(f.stem)
            except (ValueError, OSError):
                continue
    except OSError:
        return []
    return sorted(out)


def memory_pressure() -> int:
    """macOS memory pressure level (1/2/4); 1 when unknown or not macOS."""
    try:
        r = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                           capture_output=True, text=True, timeout=2)
        return int(r.stdout.strip() or 1)
    except Exception:
        return 1


# ── the decision (pure; tested directly) ─────────────────────────────────────

def _invocations(command: str) -> list[list[str]]:
    """Argument lists of every ``agent-browser`` invocation in a shell command (best effort)."""
    out = []
    for segment in re.split(r"&&|\|\||[;\n|]", command):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        for i, w in enumerate(words):
            if os.path.basename(w) == "agent-browser" or _DAEMON_RE.search(w):
                env_session = None
                for pre in words[:i]:
                    if pre.startswith("AGENT_BROWSER_SESSION="):
                        env_session = pre.split("=", 1)[1]
                args = words[i + 1:]
                if env_session is not None:
                    args = ["--session", env_session] + args
                out.append(args)
                break
    return out


def _session_of(args: list[str]) -> str | None:
    """Session an invocation uses, or None when it starts no daemon."""
    if any(a in _NO_SESSION_FLAGS for a in args):
        return None
    session = os.environ.get("AGENT_BROWSER_SESSION") or "default"
    sub = None
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--session" and i + 1 < len(args):
            session = args[i + 1]
            skip = True
        elif a.startswith("--session="):
            session = a.split("=", 1)[1]
        elif a.startswith("-"):
            continue
        elif sub is None:
            sub = a
    if sub in _NO_SESSION_CMDS:
        return None
    return session


def _howto(live: list[str]) -> str:
    names = ", ".join(live) if live else "none"
    return (
        f"Live browser sessions ({len(live)}/{MAX_SESSIONS}): {names}. "
        "Reuse one of them (`agent-browser --session <name> open <url>`), or close one first "
        "(`agent-browser --session <name> close`). Work through sites ONE AT A TIME in a single "
        "session and close it when the step is done. When you only need a page's text, use the "
        "web_search / web_extract tools instead of a browser."
    )


def decide_terminal(command: str, live: list[str], pressure: int) -> dict | None:
    if "agent-browser" not in command:
        return None
    invocations = _invocations(command)
    sessions = [s for s in (_session_of(a) for a in invocations) if s is not None]
    if not sessions:
        return None
    if _FANOUT_RE.search(command) or any("$" in s or "`" in s for s in sessions):
        return _block(
            "Refused: this command would start browser sessions in parallel or in a loop. "
            "Run one agent-browser session per command and visit sites one after another. "
            + _howto(live))
    new = sorted({s for s in sessions if s not in live})
    if not new:
        return None
    if len(new) > 1:
        return _block("Refused: one command may start at most one new browser session. " + _howto(live))
    if pressure >= PRESSURE_CRITICAL:
        return _block(
            "Refused: the machine is under critical memory pressure, so no new browser may start. "
            "Close browser sessions you no longer need, or continue with web_search / web_extract. "
            + _howto(live))
    if len(live) >= MAX_SESSIONS:
        return _block(f"Refused: the browser session limit ({MAX_SESSIONS}) is reached. " + _howto(live))
    return None


def decide_browser_tool(live: list[str], pressure: int) -> dict | None:
    if pressure >= PRESSURE_CRITICAL:
        return _block("Refused: the machine is under critical memory pressure, so no new browser "
                      "may start. Continue with web_search / web_extract. " + _howto(live))
    if len(live) >= HARD_CAP:
        return _block(f"Refused: {len(live)} browser sessions are open (hard limit {HARD_CAP}). "
                      + _howto(live))
    return None


def _block(msg: str) -> dict:
    return {"action": "block", "message": msg}


# ── reaper ───────────────────────────────────────────────────────────────────

def _etime_seconds(etime: str) -> int:
    """ps etime ``[[dd-]hh:]mm:ss`` → seconds."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def daemons() -> list[tuple[int, int]]:
    """(pid, age seconds) of every agent-browser daemon of this user, oldest first."""
    try:
        r = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,etime=,command="],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and _DAEMON_RE.search(parts[2].split(" ", 1)[0]):
            try:
                out.append((int(parts[0]), _etime_seconds(parts[1])))
            except ValueError:
                continue
    return sorted(out, key=lambda d: -d[1])


def plan_reap(found: list[tuple[int, int]], pressure: int) -> list[int]:
    """Daemon pids to kill. ``found`` is oldest first."""
    kill = [pid for pid, age in found if age > MAX_AGE_S]
    rest = [d for d in found if d[0] not in kill]
    keep = 1 if pressure >= PRESSURE_CRITICAL else HARD_CAP
    if len(rest) > keep:
        kill += [pid for pid, _ in rest[: len(rest) - keep]]
    return kill


def _descendants(pid: int) -> list[int]:
    try:
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    children: dict[int, list[int]] = {}
    for line in r.stdout.splitlines():
        try:
            p, pp = (int(x) for x in line.split())
        except ValueError:
            continue
        children.setdefault(pp, []).append(p)
    out, stack = [], [pid]
    while stack:
        for c in children.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def _kill_tree(pid: int) -> None:
    tree = _descendants(pid) + [pid]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in tree:
            try:
                os.kill(p, sig)
            except (ProcessLookupError, PermissionError):
                pass
        if sig == signal.SIGTERM:
            time.sleep(3)


def reap_once() -> list[int]:
    victims = plan_reap(daemons(), memory_pressure())
    for pid in victims:
        logger.warning("omni-resource-guard: reaping agent-browser daemon %s", pid)
        _kill_tree(pid)
    return victims


def _reaper_loop(lock_file) -> None:  # lock_file kept referenced so the lock lives as long as the thread
    while True:
        try:
            reap_once()
        except Exception:
            logger.exception("omni-resource-guard: reap failed")
        time.sleep(REAP_INTERVAL_S)


def _start_reaper() -> bool:
    """Start the reaper unless another process of this user already runs one."""
    try:
        d = socket_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / ".omni-resource-guard.lock", "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    threading.Thread(target=_reaper_loop, args=(f,), name="omni-resource-guard-reaper",
                     daemon=True).start()
    return True


# ── Hermes wiring ────────────────────────────────────────────────────────────

def _pre_tool_call(tool_name: str = "", args: dict = None, **_kw):
    if not ACTIVE:
        return None
    try:
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            command = args.get("command")
            if isinstance(command, str) and "agent-browser" in command:
                decision = decide_terminal(command, live_sessions(), memory_pressure())
            else:
                return None
        elif tool_name == "browser_navigate":
            decision = decide_browser_tool(live_sessions(), memory_pressure())
        else:
            return None
    except Exception:  # fail OPEN: a limiter bug must never block every tool call
        logger.exception("omni-resource-guard: decision failed for %s", tool_name)
        return None
    if decision:
        logger.warning("omni-resource-guard: blocked %s", tool_name)
    return decision


def register(ctx):
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    if not ACTIVE:
        logger.warning("omni-resource-guard: OFF (OMNI_RESOURCE_GUARD=off)")
        return
    os.environ.setdefault("AGENT_BROWSER_IDLE_TIMEOUT_MS", IDLE_TIMEOUT_MS)
    reaper = _start_reaper()
    logger.warning("omni-resource-guard: ACTIVE (max %d browser sessions, hard cap %d, idle %ss%s)",
                   MAX_SESSIONS, HARD_CAP, int(IDLE_TIMEOUT_MS) // 1000,
                   ", reaper running" if reaper else "")
