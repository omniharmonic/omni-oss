"""
Morning brief synthesizer — the agent's own judgment layer over the data.

The audit found the relayed Prism briefing leads every day with a wall of
"EMERGENCY — 40 days overdue" items: ~290 lines, the same overdue list four
times, sorted by age instead of consequence. A sharp human EA opens with the
3 things that matter today and handles month-old zombies once, not daily.

build_brief(client) -> str produces that short version, deterministically
(no LLM spawn — those fail ~14% of the time). It draws on the same
world-model the relay uses: relationship priority, active-concern heat, real
stakes, today's calendar, and — crucially — handles stale work intelligently
(one line: "N overdue >30d, not happening as written, renegotiate?") instead
of re-screaming each item.

The relay leads the morning message with this; Prism's full briefing follows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from parachute_client import ParachuteClient
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: The owner's IANA timezone (identity owner.timezone)

MST = ZoneInfo(OWNER_TZ)


def _today() -> datetime:
    return datetime.now(MST)


def _parse_date(s) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=MST)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

#: Config: OMNI_OWNER_EMAIL (The owner's Google calendar account). Empty = no calendar read.
CALENDAR_ACCOUNT = os.environ.get("OMNI_OWNER_EMAIL", "").strip()


def calendar_events(start: datetime, end: datetime, *, timeout: int = 20) -> list[dict]:
    """The owner's primary-calendar events between start and end (read-only
    `gog calendar events`). Cancelled events are dropped. Raises on any
    failure — callers decide whether that is quiet (the brief) or an error
    (`omni_cli calendar`). Each item: {start, all_day, summary, raw}.
    Calendar details are for the owner only; never relay them elsewhere."""
    if not CALENDAR_ACCOUNT:
        raise RuntimeError("OMNI_OWNER_EMAIL is not set: no calendar account to read")
    deadline = time.monotonic() + timeout
    events = []
    page = None
    seen_pages = set()
    for _ in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("calendar pagination deadline exceeded; agenda incomplete")
        command = ["gog", "calendar", "events", "primary",
                   "--from", start.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "--to", end.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "--json", "--no-input", "--max", "100", "--account", CALENDAR_ACCOUNT]
        if page:
            command += ["--page", page]
        try:
            r = subprocess.run(command, capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired:
            raise RuntimeError("calendar pagination deadline exceeded; agenda incomplete") from None
        if r.returncode != 0:
            raise RuntimeError(f"gog calendar events exited {r.returncode}; agenda incomplete")
        data = json.loads(r.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise RuntimeError("calendar pagination response invalid; agenda completeness unknown")
        rows = data["events"]
        if any(not isinstance(event, dict) for event in rows):
            raise RuntimeError("calendar pagination events invalid")
        events.extend(rows)
        if len(events) > 1000:
            raise RuntimeError("calendar pagination event cap exceeded; agenda incomplete")
        page = data.get("nextPageToken") or data.get("next_page_token")
        if not page:
            break
        if not isinstance(page, str) or page in seen_pages:
            raise RuntimeError("calendar pagination cursor invalid; agenda incomplete")
        seen_pages.add(page)
    else:
        raise RuntimeError("calendar pagination page cap exceeded; agenda incomplete")
    out = []
    for e in events or []:
        # Skip declined events
        if e.get("status") == "cancelled":
            continue
        start_obj = e.get("start", {})
        dt_raw = start_obj.get("dateTime") or start_obj.get("date") or ""
        out.append({
            "start": _parse_date(dt_raw),
            "all_day": "T" not in dt_raw,
            "summary": (e.get("summary") or "(busy)").strip(),
            "raw": e,
        })
    return out


def _today_meetings() -> list[str]:
    """Today's calendar (The owner's own — fine to show him). Best-effort."""
    now = _today()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    try:
        out = []
        for ev in calendar_events(start, end):
            d = ev["start"]
            tm = d.astimezone(MST).strftime("%-I:%M%p").lower() if (d and not ev["all_day"]) else "all-day"
            out.append(f"{tm} {ev['summary']}")
        return out
    except Exception:
        return []


def _owe_reply(client) -> list[str]:
    """Top consequence-ranked emails that genuinely need the owner to reply."""
    try:
        from standing_agent.relevance import build_context, score_inbound
        ctx = build_context(client)
    except Exception:
        return []
    pool = client.query_notes(tag=["email", "action-required"], limit=60,
                              include_content=False, newest_first=True)
    scored = []
    for n in pool:
        try:
            s = score_inbound(n, ctx)
        except Exception:
            continue
        if s.tier != "surface":
            continue
        m = n.metadata or {}
        sender = (m.get("from") or "").split("<")[0].strip().strip('"') or "someone"
        subj = (m.get("subject") or "").strip()[:48]
        # Connect the dot: which active concern this reply belongs to.
        link = f"  → {s.concern}" if getattr(s, "concern", "") else ""
        scored.append((s.score, f"{sender} — {subj}{link}"))
    scored.sort(key=lambda x: -x[0])
    # Dedupe by line, keep top 3
    seen, out = set(), []
    for _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= 3:
            break
    return out


def _hot_deadlines(client) -> list[str]:
    """Active concerns with a near deadline, hottest first."""
    try:
        from standing_agent.queries import query_active_concerns
        concerns = query_active_concerns(client)
    except Exception:
        return []
    now = _today()
    soon = []
    for c in concerns:
        d = _parse_date(c.get("deadline"))
        if not d:
            continue
        days = (d.astimezone(MST).date() - now.date()).days
        if days < -3 or days > 21:           # within a sensible window
            continue
        when = ("today" if days == 0 else "tomorrow" if days == 1
                else f"in {days}d" if days > 0 else f"{-days}d ago")
        soon.append((days, f"{c.get('name','(concern)')} — due {when} [{c.get('heat','warm')}]"))
    soon.sort(key=lambda x: x[0])
    return [s for _, s in soon[:3]]


def _task_due(now=None) -> list[str]:
    """The owner's tasks that are overdue or due by tomorrow — REAL deadlines
    only (tasks_store.is_overdue ignores guessed ones). Until 2026-09-30 the
    brief showed no tasks at all; its "hot deadlines" come from concerns."""
    try:
        import tasks_store as ts
        tasks = ts.read_active_tasks()
    except Exception:
        return []
    now = now or _today()
    out = []
    for t in tasks:
        d = ts.deadline_dt(t)
        if d is None or t.get("deadline_source") == "guessed":
            continue
        days = (d.astimezone(MST).date() - now.date()).days
        if days > 1 or days < -7:
            continue   # >7d overdue is summarised by _stale_summary instead
        when = ("today" if days == 0 else "tomorrow" if days == 1 else f"{-days}d overdue")
        out.append((days, f"{t['title'][:80]} — {when}"))
    out.sort(key=lambda x: x[0])
    return [s for _, s in out[:4]]


def _stale_summary(client) -> Optional[str]:
    """ONE line about month-old overdue work instead of re-screaming each item."""
    try:
        import tasks_store as ts
        tasks = ts.read_active_tasks()
    except Exception:
        return None
    now = _today()
    very_stale = []   # overdue > 7 days, real deadlines only
    for t in tasks:
        d = ts.deadline_dt(t)
        if d is None or t.get("deadline_source") == "guessed":
            continue
        if (now.date() - d.astimezone(MST).date()).days > 7:
            very_stale.append(t.get("project") or "unfiled")
    if len(very_stale) < 3:
        return None
    from collections import Counter
    named = Counter(p for p in very_stale if p and p != "unfiled").most_common(2)
    where = (", ".join(p for p, _ in named) if named else "several areas")
    return (f"{len(very_stale)} tasks are overdue >7 days (mostly {where}) — "
            f"they're not moving as written. /tasks overdue to drop or re-date them.")


def _headline(meetings, owe, hot, stale) -> str:
    """The single most important thing today."""
    if hot:
        # nearest hot deadline wins
        return f"🎯 {hot[0]}"
    if owe:
        return f"🎯 Reply to {owe[0]}"
    if meetings:
        return f"🎯 {len(meetings)} on the calendar today — first up: {meetings[0]}"
    return "🎯 Clear runway today — good morning for deep work."


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_brief(client: Optional[ParachuteClient] = None) -> str:
    client = client or ParachuteClient(timeout=20)
    meetings = _today_meetings()
    owe = _owe_reply(client)
    hot = _hot_deadlines(client)
    stale = _stale_summary(client)
    due = _task_due()

    day = _today().strftime("%A, %B %-d")
    lines = [f"☀️ {day} — the short version", "", _headline(meetings, owe, hot, stale), ""]

    if meetings:
        head = meetings[0]
        more = f" (+{len(meetings)-1} more)" if len(meetings) > 1 else ""
        lines.append(f"📅 Today: {len(meetings)} meeting(s) — next: {head}{more}")
    else:
        lines.append("📅 Today: no meetings.")

    if owe:
        lines.append("✉️ Owe a reply:")
        for o in owe:
            lines.append(f"   • {o}")

    # Headline already led with the nearest hot deadline (hot[0]); show the rest.
    rest_hot = hot[1:] if hot else []
    if rest_hot:
        lines.append("⏰ Also time-sensitive:")
        for h in rest_hot:
            lines.append(f"   • {h}")

    if due:
        lines.append("✅ Tasks due:")
        for d in due:
            lines.append(f"   • {d}")

    # Meeting commitments are not listed here: the ledger (scripts/ledger.py)
    # turns them into tasks, and the bot sends an "Open loops" message with
    # buttons right after this brief.

    if stale:
        lines.append(f"🧹 {stale}")

    return "\n".join(lines)


if __name__ == "__main__":
    print(build_brief())
