#!/usr/bin/env python3
"""
Active context packet assembler.

Materializes the structured markdown document the EA reads at the start
of every deliberation cycle. Spec lives in
.claude/planning/executive-assistant-agent.md §6.

Public API:
    assemble_active_context() → str    # full packet
    assemble_active_context(since=dt)  # also populate "new since" section
    materialize_to_disk()              # write to state/standing-agent/active-packet.md

Surface count is bounded:
  - Concerns: up to 12
  - People: up to 15 (highest priority first)
  - Topics: up to 10
  - Rhythms: today's matches first, then all sacred
  - Commitments: oldest-overdue first, up to 8 each direction
  - Decisions: last 14d, up to 8
  - Patterns: last 7d, up to 5
  - Trips: next 90d, all
  - Missions: all active
  - Calibration: last 30d, up to 3

Token budget target: ~3000 tokens (~12k chars). Counted; if exceeded,
the assembler trims tail sections.

Usage as CLI:
    python3 scripts/standing_agent/active_packet.py             # print
    python3 scripts/standing_agent/active_packet.py --write     # write to disk
    python3 scripts/standing_agent/active_packet.py --json      # structured
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from parachute_client import ParachuteClient
from standing_agent import queries
import os
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: The owner's IANA timezone (identity owner.timezone)

OUTPUT_PATH = PROJECT_ROOT / "state" / "standing-agent" / "active-packet.md"
TOKEN_BUDGET_CHARS = 12000  # ~3000 tokens


def _today_local_str() -> str:
    return datetime.now(ZoneInfo(OWNER_TZ)).strftime("%Y-%m-%d %A %H:%M %Z")


def _fmt_concerns(rows: list[dict]) -> str:
    if not rows:
        return "_(none configured yet — write `tag:concern` notes to populate)_"
    L = ["| Concern | Type | Heat | Last touched | Deadline | Attached |",
         "|---|---|---|---|---|---|"]
    for c in rows[:12]:
        last = (c.get("last_touched") or "")[:10] or "—"
        ddl = (c.get("deadline") or "")[:10] or "—"
        attached = []
        if c.get("attached_org"): attached.append(f"org:{c['attached_org']}")
        if c.get("attached_project"): attached.append(f"proj:{c['attached_project']}")
        if c.get("attached_people"):
            people = c["attached_people"]
            attached.append(f"people:{len(people) if isinstance(people, list) else 1}")
        L.append(f"| {c['name'][:40]} | {c['type']} | {c['heat']} | {last} | {ddl} | {', '.join(attached) or '—'} |")
    if len(rows) > 12:
        L.append(f"_(+{len(rows) - 12} more not shown)_")
    return "\n".join(L)


def _fmt_people(rows: list[dict]) -> str:
    if not rows:
        return "_(no EA-managed people yet — run Phase 7 backfill first)_"
    L = ["| Person | Priority | Cadence | Status | Last contact | Drift |",
         "|---|---|---|---|---|---|"]
    for p in rows[:15]:
        last = (p.get("last_meaningful_contact") or "")[:10] or "—"
        drift = p.get("drift_days")
        cad = p.get("cadence_days")
        drift_str = "—"
        if drift is not None and cad:
            drift_str = f"{drift}d"
            if drift > (cad or 0) * 2:
                drift_str = f"⚠️ {drift}d (~{cad}d expected)"
        L.append(f"| {p['name'][:35]} | {p['priority']:.2f} | "
                 f"{cad or '?'}d | {p['status']} | {last} | {drift_str} |")
    if len(rows) > 15:
        L.append(f"_(+{len(rows) - 15} more)_")
    return "\n".join(L)


def _fmt_topics(rows: list[dict]) -> str:
    if not rows:
        return "_(no topic-watches configured yet — these are critical for surfacing the right inbound. Write some `tag:topic-watch` notes covering your standing interests.)_"
    L = []
    for t in rows[:10]:
        kw = ", ".join((t.get("keywords") or [])[:5])
        last = (t.get("last_synthesis") or "")[:10] or "never"
        L.append(f"- **{t['name'][:50]}** ({t['priority']}) — synth {last}, cadence {t['synthesis_cadence']}")
        if kw:
            L.append(f"    keywords: {kw}")
    return "\n".join(L)


def _fmt_rhythms(rows: list[dict]) -> str:
    if not rows:
        return "_(no rhythms configured — these protect personal time like date nights. Phase 13 territory.)_"
    today = [r for r in rows if r.get("matches_today")]
    L = []
    if today:
        L.append("**Today:**")
        for r in today:
            sacred = " 🕯 sacred" if r.get("sacred") else ""
            people = ", ".join(r.get("linked_people", []) or [])
            L.append(f"  - {r['name']} ({r.get('time_of_day', '')}){sacred}"
                     + (f" — {people}" if people else ""))
        L.append("")
    sacred_others = [r for r in rows if r.get("sacred") and not r.get("matches_today")]
    if sacred_others:
        L.append("**Sacred (other days):**")
        for r in sacred_others[:5]:
            L.append(f"  - {r['name']} ({r.get('day_of_week', '')})")
    return "\n".join(L) or "_(no rhythms matching today)_"


def _fmt_one_commitment(c: dict, direction_label: str) -> str:
    ddl = (c.get("deadline") or "")[:10] or "no deadline"
    age = c.get("age_days")
    age_str = f"age {age}d" if age is not None else "age unknown"
    overdue = ""
    if c.get("overdue_days") and c["overdue_days"] > 0:
        overdue = f" ⚠️ {c['overdue_days']}d overdue"
    counterparty = c.get("recipient") if direction_label == "to" else c.get("owner")
    text = c.get("text") or "_(no text)_"
    return f"  - {direction_label} {counterparty or '?'}: {text} (by {ddl}, {age_str}{overdue})"


def _fmt_commitments(mine: list[dict], theirs: list[dict]) -> str:
    L = []
    L.append("**Mine to others** (oldest first):")
    if not mine:
        L.append("  _(none tracked — Phase 3 promise extraction should fill this)_")
    else:
        # Filter out garbage commitments (empty text + unknown age)
        usable_mine = [c for c in mine if c.get("text") or c.get("age_days")]
        if not usable_mine:
            L.append("  _(promise notes exist but lack text — needs cleanup)_")
        else:
            for c in sorted(usable_mine,
                            key=lambda x: x.get("age_days") or 0,
                            reverse=True)[:8]:
                L.append(_fmt_one_commitment(c, "to"))
    L.append("")
    L.append("**Theirs to me** (oldest first):")
    if not theirs:
        L.append("  _(none tracked)_")
    else:
        usable_theirs = [c for c in theirs if c.get("text") or c.get("age_days")]
        if not usable_theirs:
            L.append("  _(promise notes exist but lack text — needs cleanup)_")
        else:
            for c in sorted(usable_theirs,
                            key=lambda x: x.get("age_days") or 0,
                            reverse=True)[:8]:
                L.append(_fmt_one_commitment(c, "from"))
    return "\n".join(L)


def _fmt_decisions(rows: list[dict]) -> str:
    if not rows:
        return "_(no decisions captured in last 14d — the deliberation step will start writing these in Phase 9)_"
    L = []
    for d in rows[:8]:
        L.append(f"- {d['decided_on']} **{d['decision']}** — {d['text'][:80]}")
        if d.get("rationale"):
            L.append(f"    _why: {d['rationale'][:120]}_")
    return "\n".join(L)


def _fmt_patterns(rows: list[dict]) -> str:
    if not rows:
        return "_(none surfaced in last 7d)_"
    L = []
    for p in rows[:5]:
        L.append(f"- **{p['name']}** ({p['detected_on']})")
        L.append(f"    {p['description'][:200]}")
        if p.get("proposed_action"):
            L.append(f"    → proposed: {p['proposed_action']}")
    return "\n".join(L)


def _fmt_trips(rows: list[dict]) -> str:
    if not rows:
        return "_(no trips in next 90d)_"
    L = []
    for t in rows[:6]:
        missing = ""
        if t.get("missing_count"):
            missing = f" — {t['missing_count']} open prep items"
        L.append(f"- **{t['name']}** {t.get('start_date')} → {t.get('end_date')} "
                 f"({t.get('destination', '?')}){missing}")
    return "\n".join(L)


def _fmt_missions(rows: list[dict]) -> str:
    if not rows:
        return "_(no active workflow Missions)_"
    L = []
    for m in rows[:10]:
        L.append(f"- `{m['id'][:18]}` {m['template']} — {m['state']} ({m['title'][:50]})")
    return "\n".join(L)


def _fmt_calibration(rows: list[dict]) -> str:
    if not rows:
        return "_(none recently)_"
    L = []
    for c in rows[:3]:
        L.append(f"- {c['detected_on']}: {c['miss_description'][:120]}")
        if c.get("calibration_rule"):
            L.append(f"    rule: {c['calibration_rule'][:200]}")
    return "\n".join(L)


def _fmt_new_since(new: dict) -> str:
    L = []
    n_emails = len(new.get("new_emails", []))
    n_msgs = len(new.get("new_messages", []))
    n_trans = len(new.get("new_transcripts", []))
    L.append(f"_Since {new.get('since', '')[:19]}:_")
    L.append(f"  Emails: {n_emails}   Messages: {n_msgs}   Transcripts: {n_trans}")
    for e in new.get("new_emails", [])[:5]:
        L.append(f"  📧 {e['ts']} — {e['from']}: {e['subject']}")
    for t in new.get("new_transcripts", [])[:5]:
        L.append(f"  🎙 {t['ts']} — {t['title']}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------

def assemble_active_context(
    client: Optional[ParachuteClient] = None,
    since: Optional[datetime] = None,
) -> str:
    """Materialize the active context packet."""
    client = client or ParachuteClient(timeout=15)

    t0 = time.time()

    identity = queries.query_identity(client)
    concerns = queries.query_active_concerns(client)
    people = queries.query_people_in_rotation(client)
    topics = queries.query_topic_watches(client)
    rhythms = queries.query_rhythms(client)
    commits_mine = queries.query_commitments_mine(client)
    commits_theirs = queries.query_commitments_theirs(client)
    decisions = queries.query_recent_decisions(client)
    patterns = queries.query_recent_patterns(client)
    trips = queries.query_upcoming_trips(client)
    missions = queries.query_active_missions()
    calibration = queries.query_recent_calibration(client)
    new_since = queries.query_new_since(client, since) if since else None

    assembly_ms = round((time.time() - t0) * 1000)

    L = []
    L.append(f"# The owner's Active Context — {_today_local_str()}")
    L.append(f"_Assembled in {assembly_ms}ms_\n")

    L.append("## 1. Who I am right now\n")
    L.append(f"- **Name:** {identity['name']}")
    L.append(f"- **Location:** {identity['location']} ({identity['timezone']})")
    if identity.get("current_focus_modes"):
        L.append(f"- **Focus modes:** {', '.join(identity['current_focus_modes'])}")
    if identity.get("active_constraints"):
        L.append(f"- **Active constraints:** {'; '.join(identity['active_constraints'])}")
    L.append("")

    L.append(f"## 2. Active concerns ({len(concerns)})\n")
    L.append(_fmt_concerns(concerns)); L.append("")

    L.append(f"## 3. People in active rotation ({len(people)})\n")
    L.append(_fmt_people(people)); L.append("")

    L.append(f"## 4. Topics on watch ({len(topics)})\n")
    L.append(_fmt_topics(topics)); L.append("")

    L.append(f"## 5. Standing rhythms\n")
    L.append(_fmt_rhythms(rhythms)); L.append("")

    L.append(f"## 6. Open commitments\n")
    L.append(_fmt_commitments(commits_mine, commits_theirs)); L.append("")

    L.append(f"## 7. Recent decisions ({len(decisions)})\n")
    L.append(_fmt_decisions(decisions)); L.append("")

    L.append(f"## 8. Emerging patterns ({len(patterns)})\n")
    L.append(_fmt_patterns(patterns)); L.append("")

    L.append(f"## 9. Trips & events ({len(trips)})\n")
    L.append(_fmt_trips(trips)); L.append("")

    L.append(f"## 10. Active workflow missions ({len(missions)})\n")
    L.append(_fmt_missions(missions)); L.append("")

    L.append(f"## 11. Calibration learnings (recent {len(calibration)})\n")
    L.append(_fmt_calibration(calibration)); L.append("")

    if new_since is not None:
        L.append(f"## 12. New since last cycle\n")
        L.append(_fmt_new_since(new_since)); L.append("")

    packet = "\n".join(L)

    # Trim if over budget — drop sections from the tail
    while len(packet) > TOKEN_BUDGET_CHARS and "## 11." in packet:
        # Drop calibration first (least frequently useful for deliberation)
        packet = packet.split("## 11.")[0] + "\n_(calibration trimmed to fit token budget)_\n"
    while len(packet) > TOKEN_BUDGET_CHARS and "## 10." in packet:
        packet = packet.split("## 10.")[0] + "\n_(missions trimmed)_\n"

    return packet


def materialize_to_disk(packet: str) -> Path:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(packet)
    return OUTPUT_PATH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="Write to state/standing-agent/active-packet.md")
    ap.add_argument("--json", action="store_true",
                    help="Output the raw structured data instead of markdown")
    ap.add_argument("--since-hours", type=int, default=None,
                    help="Populate 'new since' section (last N hours)")
    args = ap.parse_args()

    since = None
    if args.since_hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)

    if args.json:
        client = ParachuteClient(timeout=15)
        result = {
            "identity": queries.query_identity(client),
            "concerns": queries.query_active_concerns(client),
            "people": queries.query_people_in_rotation(client),
            "topics": queries.query_topic_watches(client),
            "rhythms": queries.query_rhythms(client),
            "commitments_mine": queries.query_commitments_mine(client),
            "commitments_theirs": queries.query_commitments_theirs(client),
            "decisions": queries.query_recent_decisions(client),
            "patterns": queries.query_recent_patterns(client),
            "trips": queries.query_upcoming_trips(client),
            "missions": queries.query_active_missions(),
            "calibration": queries.query_recent_calibration(client),
        }
        if since:
            result["new_since"] = queries.query_new_since(client, since)
        print(json.dumps(result, indent=2, default=str))
        return

    packet = assemble_active_context(since=since)
    print(packet)
    if args.write:
        p = materialize_to_disk(packet)
        print(f"\n---\nWritten to {p} ({len(packet)} chars, ~{len(packet)//4} tokens)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
