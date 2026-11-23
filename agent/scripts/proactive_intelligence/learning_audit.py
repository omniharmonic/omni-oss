"""The audit and revert trail for autonomous learning (owner decision 3, 2026-10-22).

Proactive runs may write Hermes memory (MEMORY.md / USER.md) and Hermes skills. Every such write, and every
write the guard refused, is one JSON line in `state/learning-audit.jsonl`:

    {"at": ISO-8601 UTC, "session": Hermes session id, "actor": "proactive" | "background-review",
     "phase": "intent" | "done", "tool": "memory" | "skill_manage",
     "action": add|replace|remove|batch|create|patch|edit|delete|write_file|remove_file,
     "target": "MEMORY.md" | "USER.md" | <skill name>, "ok": bool (null on an intent line), "summary": <=200 chars,
     "refused": why (only when refused)}

An "intent" line is appended BEFORE each write and a "done" line after it; a write whose intent line cannot be
appended does not happen (hermes_adapter). An intent with no done line is a write that was cut off (the worker was
stopped mid-write): check the store by hand.

The file is mode 0600 and only ever appended to. The summary passes through omni-bridge's secret scrubber
(hermes/plugins/omni-bridge/redact.py, the known secret VALUES) when it can be loaded; when it cannot, the
summary is metadata only (action, target and size, no content). Reverting is by hand: the line names the store
and the text. For skills, Hermes' own ledger (~/.hermes/skills/.curator_ledger.jsonl) holds before/after blobs of
every mutation; that ledger is the ONLY way back from a foreground `delete`, which removes the skill folder outright
(the background review's delete archives it under ~/.hermes/skills/.archive instead).
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO / 'state' / 'learning-audit.jsonl'
SUMMARY_CHARS = 200
_LOCK = threading.Lock()
_SCRUB = []


def _scrubber():
    """omni-bridge's scrub(text), loaded without running the plugin's __init__ (which registers hooks). None
    when it cannot be loaded: callers then write metadata only."""
    if _SCRUB:
        return _SCRUB[0]
    scrub = None
    try:
        import importlib.util
        import types
        folder = REPO / 'hermes' / 'plugins' / 'omni-bridge'
        package = '_omni_bridge_audit_redact'
        if package not in sys.modules:
            module = types.ModuleType(package)
            module.__path__ = [str(folder)]
            sys.modules[package] = module
        spec = importlib.util.spec_from_file_location(package + '.redact', folder / 'redact.py')
        redact = importlib.util.module_from_spec(spec)
        sys.modules[package + '.redact'] = redact
        spec.loader.exec_module(redact)
        redact.values()  # the secret list must load, or the scrubber protects nothing
        scrub = redact.scrub
    except Exception:
        scrub = None
    _SCRUB.append(scrub)
    return scrub


def summarize(text, scrub=None):
    """At most SUMMARY_CHARS of `text`, secrets replaced; None when no scrubber is available."""
    if text is None:
        return None
    scrub = scrub or _scrubber()
    if scrub is None:
        return None
    flat = ' '.join(str(text).split())
    # Scrub before cutting (a secret split at the cut would no longer match), and again after.
    return scrub(scrub(flat)[:SUMMARY_CHARS])[:SUMMARY_CHARS]


def actor():
    """Hermes runs its post-turn review in a thread named 'bg-review' (run_agent._spawn_background_review)."""
    if threading.current_thread().name == 'bg-review':
        return 'background-review'
    try:
        from tools.skill_provenance import is_background_review
        if is_background_review():
            return 'background-review'
    except Exception:
        pass
    return 'proactive'


def append(path, entry):
    """One JSON line, appended under a lock to a 0600 file. Never raises into the caller's write."""
    path = Path(path or DEFAULT_PATH)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + '\n'
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW: a symlink planted at the audit path must not redirect the appends (or the chmod).
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, line.encode('utf-8'))
            finally:
                os.close(fd)
        return True
    except Exception:
        return False


def check_writable(path):
    """True when the audit file can be opened for appending (created 0600 if missing), without writing a line."""
    path = Path(path or DEFAULT_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def entry(*, session, tool, action, target, ok, text=None, size=None, refused=None, scrub=None, after_turn=False, phase='done'):
    """`after_turn`: the foreground turn has returned, so any write now is the background review's (Hermes may run
    the review's tools on executor threads that do not carry the 'bg-review' name)."""
    record = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'session': session or None,
              'actor': 'background-review' if after_turn else actor(), 'phase': phase,
              'tool': tool, 'action': action or None, 'target': target or None, 'ok': None if ok is None else bool(ok)}
    summary = summarize(text, scrub) if text is not None else None
    if summary is None:
        bits = [str(action or '?'), str(target or '?')]
        if size is not None:
            bits.append('%d chars' % size)
        summary = ' '.join(bits)[:SUMMARY_CHARS]
    record['summary'] = summary
    if refused:
        record['refused'] = str(refused)[:SUMMARY_CHARS]
    return record
