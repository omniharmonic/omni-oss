"""
Active-context section queries.

One function per section of the materialized active context packet
(see .claude/planning/executive-assistant-agent.md §6.4 for the spec).

Each query:
  - Takes a Parachute client
  - Returns a structured dict
  - Caches per-call where TTL helps (orgs change slowly; new emails fast)
  - Never raises; degrades to empty + a note if data is missing

The assembler in active_packet.py composes these into a markdown packet.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from parachute_client import ParachuteClient
from standing_agent import people_resolver
from standing_agent.people_resolver import build_email_index
from zoneinfo import ZoneInfo
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: The owner's IANA timezone (identity owner.timezone)

logger = logging.getLogger("standing_agent.queries")

MST = ZoneInfo(OWNER_TZ)  # The owner's zone (name kept from the original)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Normalize naive datetimes (e.g. bare dates like "2026-06-10") to UTC so
    # callers can compare against tz-aware cutoffs without crashing.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _today_local() -> datetime:
    return datetime.now(MST)


# ---------------------------------------------------------------------------
# Tiny TTL cache decorator (per-call-site, in-process)
# ---------------------------------------------------------------------------

_cache: dict = {}


def _cached(key: str, ttl: int):
    def decorator(fn):
        def wrapped(*args, **kwargs):
            now = time.monotonic()
            hit = _cache.get(key)
            if hit and (now - hit["t"] < ttl):
                return hit["v"]
            v = fn(*args, **kwargs)
            _cache[key] = {"t": now, "v": v}
            return v
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Section 1 — Identity & current state
# ---------------------------------------------------------------------------

@_cached("identity", ttl=3600)
def query_identity(client: ParachuteClient) -> dict:
    """The owner's basic state. Mostly static; reads from CLAUDE.md
    semantics. Dynamic bits (current focus, energy) come from observation
    layer in later phases."""
    return {
        "name": os.environ.get("OMNI_OWNER_NAME", "").strip() or "the owner",
        "location": os.environ.get("OMNI_OWNER_LOCATION", "").strip(),
        "timezone": os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC",
        "date_local": _today_local().isoformat(),
        # These are seeded by future observation passes
        "current_focus_modes": [],
        "energy_proxy": "unobserved",
        "active_constraints": [],
    }


# ---------------------------------------------------------------------------
# Section 2 — Active concerns
# ---------------------------------------------------------------------------

def query_active_concerns(client: ParachuteClient) -> list[dict]:
    """All non-terminal concerns, sorted by heat (hot first)."""
    notes = client.query_notes(
        tag="concern", limit=200, include_content=False, newest_first=True,
    )
    out = []
    for n in notes:
        m = n.metadata or {}
        if m.get("status") not in ("active", None):
            continue
        out.append({
            "slug": m.get("slug") or n.path.split("/")[-1],
            "name": m.get("name") or m.get("title") or "(unnamed)",
            "type": m.get("concern_type", "?"),
            "heat": m.get("heat", "warm"),
            "deadline": m.get("deadline"),
            "last_touched": m.get("last_touched"),
            "attached_org": m.get("attached_org"),
            "attached_project": m.get("attached_project"),
            "attached_people": m.get("attached_people", []) or [],
            "path": n.path,
        })
    # Sort by heat (hot > warm > standby > stalled), then by last_touched desc
    heat_rank = {"hot": 0, "warm": 1, "standby": 2, "stalled": 3}
    out.sort(key=lambda c: (heat_rank.get(c["heat"], 4),
                            -(_parse_iso(c.get("last_touched") or "")
                              or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
    return out


# ---------------------------------------------------------------------------
# Section 3 — People in active rotation
# ---------------------------------------------------------------------------

def query_people_in_rotation(
    client: ParachuteClient, days_back: int = 30,
) -> list[dict]:
    """EA-managed people, filtered to those active in last N days."""
    build_email_index(client, force_refresh=False)
    by_path = people_resolver._INDEX_CACHE["by_path"]

    cutoff = _now() - timedelta(days=days_back)
    out = []
    for rp in by_path.values():
        meta = rp.raw_metadata or {}
        # Skip tombstones (any with status=merged_into_canonical pointing
        # elsewhere; self-references already filtered by resolver)
        if meta.get("status") == "merged_into_canonical":
            mi = meta.get("merged_into") or ""
            if mi and mi != rp.path:
                continue
        if not rp.ea_managed:
            continue
        last = _parse_iso(rp.last_meaningful_contact or "")
        is_active_window = last and last >= cutoff
        # Always include high-priority people even if they're outside window
        # (because that's a flag — drifting relationships are signal)
        if rp.priority_weight < 0.5 and not is_active_window:
            continue
        out.append({
            "path": rp.path,
            "name": rp.name,
            "priority": rp.priority_weight,
            "cadence_days": rp.cadence_baseline_days,
            "status": rp.relationship_status or "unknown",
            "last_meaningful_contact": rp.last_meaningful_contact,
            "drift_days": (_now() - last).days if last else None,
            "in_active_window": bool(is_active_window),
            "emails": rp.emails,
        })
    # Sort: priority desc, then most-recently-touched first
    out.sort(key=lambda p: (-p["priority"],
                            -(_parse_iso(p.get("last_meaningful_contact") or "")
                              or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
    return out


# ---------------------------------------------------------------------------
# Section 4 — Topics on watch
# ---------------------------------------------------------------------------

@_cached("topics", ttl=3600)
def query_topic_watches(client: ParachuteClient) -> list[dict]:
    notes = client.query_notes(
        tag="topic-watch", limit=50, include_content=False, newest_first=True,
    )
    out = []
    for n in notes:
        m = n.metadata or {}
        if m.get("status") not in ("active", None):
            continue
        out.append({
            "slug": m.get("slug") or n.path.split("/")[-1],
            "name": m.get("name") or m.get("title") or "(unnamed)",
            "priority": m.get("priority", "medium"),
            "keywords": m.get("keywords", []) or [],
            "synthesis_cadence": m.get("synthesis_cadence", "weekly"),
            "last_synthesis": m.get("last_synthesis"),
            "path": n.path,
        })
    return out


# ---------------------------------------------------------------------------
# Section 5 — Standing rhythms (date nights, sacred time, etc.)
# ---------------------------------------------------------------------------

@_cached("rhythms", ttl=3600 * 24)
def query_rhythms(client: ParachuteClient) -> list[dict]:
    notes = client.query_notes(
        tag="rhythm", limit=50, include_content=False, newest_first=True,
    )
    out = []
    today_dow = _today_local().strftime("%A").lower()
    for n in notes:
        m = n.metadata or {}
        dow = (m.get("day_of_week") or "").lower()
        out.append({
            "slug": m.get("slug"),
            "name": m.get("name"),
            "day_of_week": dow,
            "time_of_day": m.get("time_of_day"),
            "sacred": bool(m.get("sacred")),
            "agent_can_propose": bool(m.get("agent_can_propose")),
            "linked_people": m.get("linked_people", []) or [],
            "matches_today": dow == today_dow or dow in ("any", ""),
            "path": n.path,
        })
    return out


# ---------------------------------------------------------------------------
# Section 6a — Commitments mine to others
# ---------------------------------------------------------------------------

def query_commitments_mine(client: ParachuteClient) -> list[dict]:
    """Pull from tag:promise notes plus tag:commitment when it exists.

    Phase 3 wrote promises as tag:promise + status. We unify here so
    the packet sees both new (concerns + commitments) and old shapes.
    """
    out = []
    # Newer tag:commitment notes
    for n in client.query_notes(tag="commitment", limit=200,
                                include_content=False, newest_first=True):
        m = n.metadata or {}
        if m.get("direction") != "owner_to_other":
            continue
        if m.get("status") in ("done", "killed", "escaped"):
            continue
        out.append(_commitment_to_dict(m, n.path))
    # Phase 3 tag:promise notes
    # Ledger promises (scripts/ledger.py). Filter status in code: Parachute
    # ignores every tag after the first, so the old ["promise","confirmed"]
    # query returned all promises, archived ones included (2026-09-30).
    for n in client.query_notes(tag="promise", limit=400,
                                include_content=False, newest_first=True):
        m = n.metadata or {}
        if m.get("direction") != "owner_to_other" or m.get("status") not in ("tracked", "confirmed"):
            continue
        out.append(_commitment_to_dict(m, n.path))
    return out


def query_commitments_theirs(client: ParachuteClient) -> list[dict]:
    out = []
    for n in client.query_notes(tag="commitment", limit=200,
                                include_content=False, newest_first=True):
        m = n.metadata or {}
        if m.get("direction") != "other_to_owner":
            continue
        if m.get("status") in ("done", "killed", "escaped"):
            continue
        out.append(_commitment_to_dict(m, n.path))
    # Ledger promises (scripts/ledger.py). Filter status in code: Parachute
    # ignores every tag after the first, so the old ["promise","confirmed"]
    # query returned all promises, archived ones included (2026-09-30).
    for n in client.query_notes(tag="promise", limit=400,
                                include_content=False, newest_first=True):
        m = n.metadata or {}
        if m.get("direction") != "other_to_owner" or m.get("status") not in ("tracked", "confirmed"):
            continue
        out.append(_commitment_to_dict(m, n.path))
    return out


def _commitment_to_dict(m: dict, path: str) -> dict:
    deadline = m.get("deadline")
    age_days = None
    promised = _parse_iso(m.get("promised") or m.get("created") or "")
    if promised:
        age_days = (_now() - promised).days
    overdue_days = None
    d_parsed = _parse_iso(deadline or "")
    if d_parsed:
        overdue_days = (_now() - d_parsed).days
    return {
        "text": (m.get("text") or "")[:200],
        "owner": m.get("owner"),
        "recipient": m.get("recipient"),
        "deadline": deadline,
        "age_days": age_days,
        "overdue_days": overdue_days,
        "status": m.get("status", "pending"),
        "source_transcript": m.get("source_transcript"),
        "linked_concern": m.get("linked_concern"),
        "path": path,
    }


# ---------------------------------------------------------------------------
# Section 7 — Recent decisions
# ---------------------------------------------------------------------------

@_cached("decisions", ttl=3600)
def query_recent_decisions(client: ParachuteClient, days_back: int = 14) -> list[dict]:
    notes = client.query_notes(tag="decision", limit=100,
                               include_content=False, newest_first=True)
    cutoff = _today_local() - timedelta(days=days_back)
    out = []
    for n in notes:
        m = n.metadata or {}
        d = _parse_iso(m.get("decided_on") or "")
        if d and d >= cutoff:
            out.append({
                "decided_on": (m.get("decided_on") or "")[:10],
                "text": m.get("text") or m.get("title") or "(unnamed)",
                "decision": m.get("decision"),
                "rationale": (m.get("rationale") or "")[:200],
                "linked_concerns": m.get("linked_concerns", []) or [],
                "path": n.path,
            })
    return out


# ---------------------------------------------------------------------------
# Section 8 — Emerging patterns
# ---------------------------------------------------------------------------

@_cached("patterns", ttl=3600)
def query_recent_patterns(client: ParachuteClient, days_back: int = 7) -> list[dict]:
    notes = client.query_notes(tag="pattern", limit=50,
                               include_content=False, newest_first=True)
    cutoff = _now() - timedelta(days=days_back)
    out = []
    for n in notes:
        m = n.metadata or {}
        if m.get("status") == "dismissed":
            continue
        d = _parse_iso(m.get("detected_on") or n.created_at or "")
        if d and d >= cutoff:
            out.append({
                "detected_on": (m.get("detected_on") or n.created_at or "")[:10],
                "name": m.get("name") or m.get("title") or "(unnamed)",
                "description": (m.get("description") or "")[:300],
                "proposed_action": m.get("proposed_action"),
                "status": m.get("status", "detected"),
                "path": n.path,
            })
    return out


# ---------------------------------------------------------------------------
# Section 9 — Trips
# ---------------------------------------------------------------------------

@_cached("trips", ttl=3600)
def query_upcoming_trips(client: ParachuteClient, days_ahead: int = 90) -> list[dict]:
    notes = client.query_notes(tag="trip", limit=50,
                               include_content=False, newest_first=True)
    horizon = _now() + timedelta(days=days_ahead)
    out = []
    for n in notes:
        m = n.metadata or {}
        if m.get("status") in ("completed", "cancelled"):
            continue
        start = _parse_iso(m.get("start_date") or "")
        if start and start <= horizon:
            missing = [p for p in (m.get("missing_pieces") or [])
                       if (p.get("status") if isinstance(p, dict) else "open") == "open"]
            out.append({
                "name": m.get("name"),
                "destination": m.get("destination"),
                "start_date": (m.get("start_date") or "")[:10],
                "end_date": (m.get("end_date") or "")[:10],
                "purpose": m.get("purpose"),
                "missing_count": len(missing),
                "missing_pieces": missing,
                "path": n.path,
            })
    out.sort(key=lambda t: t["start_date"] or "")
    return out


# ---------------------------------------------------------------------------
# Section 10 — Active workflow Missions
# ---------------------------------------------------------------------------

def query_active_missions() -> list[dict]:
    """Returns the EA-relevant active Workflow Missions (not the EA
    Mission itself, when that exists)."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from missions import list_active
        from missions.persistence import list_watchers_for_mission
    except Exception as e:
        logger.debug("missions package unavailable: %s", e)
        return []

    out = []
    for m in list_active():
        if m.template == "executive_assistant":
            continue
        watchers = list_watchers_for_mission(m.id)
        out.append({
            "id": m.id,
            "template": m.template,
            "title": m.title,
            "state": m.state,
            "current_step": m.current_step,
            "parent_concern": (m.goal or {}).get("parent_concern_id"),
            "watcher_count": len(watchers),
        })
    return out


# ---------------------------------------------------------------------------
# Section 11 — Calibration learnings
# ---------------------------------------------------------------------------

@_cached("calibration", ttl=3600)
def query_recent_calibration(client: ParachuteClient,
                              days_back: int = 30) -> list[dict]:
    notes = client.query_notes(tag="calibration-note", limit=50,
                               include_content=False, newest_first=True)
    cutoff = _now() - timedelta(days=days_back)
    out = []
    for n in notes:
        m = n.metadata or {}
        d = _parse_iso(m.get("detected_on") or n.created_at or "")
        if d and d >= cutoff:
            out.append({
                "detected_on": (m.get("detected_on") or n.created_at or "")[:10],
                "miss_description": m.get("miss_description"),
                "calibration_rule": m.get("calibration_rule"),
                "path": n.path,
            })
    return out


# ---------------------------------------------------------------------------
# Section 12 — New since last cycle (ephemeral)
# ---------------------------------------------------------------------------

def query_new_since(client: ParachuteClient, since: datetime) -> dict:
    """What's landed in Parachute since the given timestamp."""
    out = {
        "new_emails": [], "new_transcripts": [], "new_messages": [],
        "since": since.isoformat(),
    }
    try:
        for n in client.query_notes(tag="email", limit=30,
                                    include_content=False, newest_first=True):
            ts = _parse_iso(n.created_at or "")
            if ts and ts > since:
                m = n.metadata or {}
                out["new_emails"].append({
                    "subject": (m.get("subject") or "")[:80],
                    "from": (m.get("from") or "")[:60],
                    "ts": (n.created_at or "")[:19],
                    "path": n.path,
                })
            elif ts and ts < since:
                break  # newest-first; subsequent are older
    except Exception as e:
        logger.debug("new_emails query failed: %s", e)

    try:
        for n in client.query_notes(tag="transcript", limit=10,
                                    include_content=False, newest_first=True):
            ts = _parse_iso(n.created_at or "")
            if ts and ts > since:
                m = n.metadata or {}
                out["new_transcripts"].append({
                    "title": m.get("title") or (n.path or "").split("/")[-1],
                    "ts": (n.created_at or "")[:19],
                    "path": n.path,
                })
            elif ts and ts < since:
                break
    except Exception as e:
        logger.debug("new_transcripts query failed: %s", e)

    return out
