"""Meeting grants — the only record of what the owner approved.

Also the busy hint ``<HERMES_HOME>/omni-meet/busy.json`` (0600, ``{"since", "meeting_keys"}``): present
while a meeting is joined or Minutes is processing one of ours, absent when idle. The memory guard
(lms-guard) reads it so it never evicts local models mid-call. Only the conductor writes it.

``<HERMES_HOME>/omni-meet/meetings.json`` (0600). Everything under Hermes' home is a guard path:
no Omni turn can write it (``paths.guard_paths``), so a grant exists only because an approved
``omni_meet_attend`` / ``omni_meet_allow_speech`` call wrote it in-process. One file lock serializes
the gateway's conductor thread and tool calls.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def state_dir() -> Path:
    d = home() / "omni-meet"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return state_dir() / "meetings.json"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    with open(state_dir() / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read() -> Dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meetings": {}}
    if not isinstance(data, dict) or not isinstance(data.get("meetings"), dict):
        return {"meetings": {}}
    return data


def _write(data: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(state_dir()), prefix=".meetings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load() -> Dict[str, Dict[str, Any]]:
    with _locked():
        return _read()["meetings"]


def update(key: str, change: Callable[[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Read-modify-write one meeting under the lock. ``change`` gets the current record (or None)
    and returns the new one (None deletes)."""
    with _locked():
        data = _read()
        new = change(data["meetings"].get(key))
        if new is None:
            data["meetings"].pop(key, None)
        else:
            new["updated_at"] = time.time()
            data["meetings"][key] = new
        _write(data)
        return new


def prune(older_than_days: float = 30) -> None:
    cutoff = time.time() - older_than_days * 86400
    with _locked():
        data = _read()
        data["meetings"] = {k: m for k, m in data["meetings"].items() if float(m.get("end") or 0) > cutoff}
        _write(data)


def find(ref: str) -> Optional[str]:
    """A meeting key from what an agent would say: the key, the calendar event id, or the Meet code."""
    ref = (ref or "").strip()
    if not ref:
        return None
    meetings = load()
    if ref in meetings:
        return ref
    for k, m in meetings.items():
        if ref in (m.get("event_id"), m.get("code"), m.get("url")):
            return k
    return None


# ── the busy hint ───────────────────────────────────────────────────────────

def busy_path() -> Path:
    return state_dir() / "busy.json"


def write_busy(keys, now: Optional[float] = None) -> None:
    """Keep busy.json naming ``keys`` (``since`` = when it became busy); remove it when ``keys`` is empty."""
    p = busy_path()
    keys = sorted(set(keys or ()))
    if not keys:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
        return
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if isinstance(old, dict) and old.get("meeting_keys") == keys:
        return
    since = old.get("since") if isinstance(old, dict) and old.get("since") else (now if now is not None else time.time())
    fd, tmp = tempfile.mkstemp(dir=str(state_dir()), prefix=".busy-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"since": since, "meeting_keys": keys}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
