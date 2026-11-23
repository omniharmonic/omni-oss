"""The owner's calendar, through gog: read one event, add Omni to its guest list.

Runs inside the gateway process (launchd, the login session), where gog can reach its Keychain
tokens. Only two commands are ever built here, both fixed in shape:

    gog calendar event  <cal> <id> --account <owner> --json --results-only
    gog calendar update <cal> <id> --account <owner> --add-attendee <omni> --send-updates none --gmail-no-send

``--send-updates none``: Google sends nobody an email about the change — the other guests just see
Omni on the guest list, and Omni's own calendar gets the event (so its signed-in browser is admitted
directly instead of knocking). ``--add-attendee`` preserves every existing guest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import rules

#: The owner's Google account (whose calendar holds the meetings). Config: OMNI_OWNER_EMAIL.
#: Empty = no calendar is read or changed (fails closed; see _require_accounts).
OWNER_EMAIL = os.environ.get("OMNI_OWNER_EMAIL", "").strip().lower()
_ID_RE = re.compile(r"^[A-Za-z0-9_@.\-]{3,256}$")


class CalendarError(RuntimeError):
    pass


def gog() -> str:
    for c in (shutil.which("gog"), "/opt/homebrew/bin/gog", "/usr/local/bin/gog"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise CalendarError("gog is not installed where the gateway can find it")


def _run(args: List[str], timeout: float = 60) -> str:
    try:
        out = subprocess.run([gog(), *args], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise CalendarError("gog timed out") from exc
    if out.returncode != 0:
        raise CalendarError("gog failed: %s" % (out.stderr or out.stdout).strip()[-300:])
    return out.stdout


def _check_ids(calendar_id: str, event_id: str) -> None:
    if not _ID_RE.match(calendar_id or "") or not _ID_RE.match(event_id or ""):
        raise CalendarError("calendar_id / event_id look wrong")


def _ts(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_event(raw: Any) -> Dict[str, Any]:
    """The fields we use, from a Google Calendar event resource (gog may wrap it)."""
    ev = raw
    for key in ("event", "result", "item"):
        if isinstance(ev, dict) and isinstance(ev.get(key), dict) and "id" not in ev:
            ev = ev[key]
    if not isinstance(ev, dict):
        raise CalendarError("unexpected event JSON")
    url = ev.get("hangoutLink") or ""
    if not url:
        for ep in ((ev.get("conferenceData") or {}).get("entryPoints") or []):
            if isinstance(ep, dict) and ep.get("entryPointType") == "video" and "meet.google.com" in str(ep.get("uri")):
                url = ep["uri"]
                break
    attendees = [a for a in (ev.get("attendees") or []) if isinstance(a, dict)]
    organizer = ev.get("organizer") or {}
    return {
        "event_id": ev.get("id"),
        "title": (ev.get("summary") or "").strip()[:200],
        "start": _ts(ev.get("start")),
        "end": _ts(ev.get("end")),
        "url": url.split("?")[0] if url else "",
        "status": ev.get("status"),
        "attendees": [str(a.get("email") or "").lower() for a in attendees],
        # Display names where the calendar has them (speaker naming links caption names to these).
        "attendee_names": {str(a.get("email") or "").lower(): str(a.get("displayName")).strip()[:120]
                           for a in attendees if a.get("email") and str(a.get("displayName") or "").strip()},
        "omni_invited": any(str(a.get("email") or "").lower() == rules.AGENT_EMAIL for a in attendees),
        "organizer_is_owner": bool(organizer.get("self")) or (bool(OWNER_EMAIL) and str(organizer.get("email") or "").lower() == OWNER_EMAIL),
        "guests_can_invite": ev.get("guestsCanInviteOthers", True) is not False,
    }


def _require_accounts(agent: bool = False) -> None:
    """Fail closed: no configured owner (or agent) account, no calendar call at all."""
    if not OWNER_EMAIL:
        raise CalendarError("OMNI_OWNER_EMAIL is not set: no calendar is read or changed")
    if agent and not rules.AGENT_EMAIL:
        raise CalendarError("OMNI_AGENT_EMAIL is not set: Omni has no account to add to the invite")


def get_event(event_id: str, calendar_id: str = "primary") -> Dict[str, Any]:
    _require_accounts()
    _check_ids(calendar_id, event_id)
    text = _run(["calendar", "event", calendar_id, event_id, "--account", OWNER_EMAIL, "--json", "--results-only"])
    try:
        return parse_event(json.loads(text))
    except ValueError as exc:
        raise CalendarError("gog returned no JSON") from exc


#: Google answers a burst of calendar writes with 403 rateLimitExceeded / 429 / 5xx; those pass.
_TRANSIENT = ("rateLimitExceeded", "userRateLimitExceeded", "429", "backendError", "503", "500")
RETRY_DELAYS = (2.0, 5.0, 12.0)


def invite_omni(event_id: str, calendar_id: str = "primary", sleep=time.sleep) -> None:
    _require_accounts(agent=True)
    _check_ids(calendar_id, event_id)
    args = ["calendar", "update", calendar_id, event_id, "--account", OWNER_EMAIL,
            "--add-attendee", rules.AGENT_EMAIL, "--send-updates", "none", "--gmail-no-send"]
    for delay in (*RETRY_DELAYS, None):
        try:
            _run(args)
            return
        except CalendarError as exc:
            if delay is None or not any(t in str(exc) for t in _TRANSIENT):
                raise
            sleep(delay)
