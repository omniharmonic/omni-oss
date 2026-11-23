"""
Importance scorer for inbound items (emails / message-threads).

This is the bridge the audit (2026-06-03) said was missing: it wires the
standing-agent world-model — relationship priority_weight, active-concern
heat, and real stakes — into the channel the owner actually sees, so the
relay surfaces by CONSEQUENCE instead of tag-presence + recency.

A human EA doesn't forward you every email someone tagged "action-required."
They ask: who is this from (to you), does it touch something you care about
right now, and what's actually at stake — then they hand you the two that
matter and sit on the rest. That's what this does.

Public API:
    ctx = build_context(client)          # cached world-model snapshot
    s = score_inbound(note, ctx)         # -> ItemScore(score, tier, reasons)

Tiers: "surface" (≥ SURFACE_BAR) | "maybe" | "suppress".
The relay surfaces only "surface", ranked by score, with the lead reason —
and stays silent when nothing clears the bar.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from parachute_client import ParachuteClient
from parachute_writer import is_automated_email
from standing_agent import people_resolver
from standing_agent.people_resolver import build_email_index

# The owner's + the agent's own addresses: never "inbound that needs attention"
# (the agent's own outbound was surfacing as action-required).
# Config: OMNI_OWNER_EMAIL, OMNI_OWNER_EMAILS (comma list of the owner's other addresses), OMNI_AGENT_EMAIL.
SELF_ADDRESSES = {
    a.strip().lower()
    for a in ",".join(os.environ.get(k, "") for k in ("OMNI_OWNER_EMAIL", "OMNI_OWNER_EMAILS", "OMNI_AGENT_EMAIL")).split(",")
    if a.strip()
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Tier thresholds (tunable). Scores are normalized to 0..1.
SURFACE_BAR = 0.50
MAYBE_BAR = 0.32

# Subjects that are almost always notification/newsletter/recap noise even
# when a human-looking address sent them. Matched case-insensitively.
_NOISE_SUBJECT = re.compile(
    r"\b(recap|digest|newsletter|unsubscribe|double[- ]booked|"
    r"is now available|now available|your (meeting|recording|transcript)|"
    r"sign[- ]?in|verify your|confirm your (email|address)|receipt|"
    r"password|security alert|notification|reminder:|webinar|"
    r"join us|rsvp|early bird|last chance|don'?t miss|"
    r"out now|new episode|weekly|monthly update|"
    r"is starting|starting soon|happening (now|soon)|thanks for joining|"
    r"thank you for (joining|registering|attending)|"
    r"registration (confirmed|received|successful))\b",
    re.IGNORECASE,
)

# Google Calendar system mail: "Accepted:", "Invitation: <title> @ <time>",
# "Updated invitation", "Declined:", etc. These are calendar machinery (the
# calendar is synced separately) — never correspondence that needs the owner.
# The trailing colon is the signature; "Invitation to host a session…" (a real
# human invite) has no colon and is NOT matched.
_CALENDAR_NOISE = re.compile(
    r"^(re:\s*|fwd:\s*)?(accepted|declined|tentatively accepted|invitation|"
    r"updated invitation|canceled event|cancelled event|new event|"
    r"updated event)( with note)?:",
    re.IGNORECASE,
)

# Stakes signals in subject/snippet — things that genuinely need a person.
_DEADLINE_RE = re.compile(
    r"\b(deadline|due (today|tomorrow|by)|by (today|tomorrow|eod|cob)|"
    r"urgent|asap|time[- ]sensitive|expires?|closing|final (notice|reminder))\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(approve|approval|sign(-?off)?|decision|decide|confirm|review|"
    r"your (input|thoughts|take|call|go-ahead)|need(s|ed)? you|"
    r"can you|could you|would you|are you (able|free|available)|"
    r"invit(e|ation|ing)|propos(al|e)|offer|intro(duction)?)\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"(\binvoice\b|\bpayment\b|\bpay\b|\$[\d,]+|\bwire\b|\bcontract\b|"
    r"\bgrant\b|\bfunding\b|\bproposal\b)",
    re.IGNORECASE,
)
_QUESTION_TO_YOU = re.compile(r"\?\s*$|\byou\b.*\?", re.IGNORECASE)


@dataclass
class ScoreContext:
    """World-model snapshot used to score items. Built once per relay run."""
    email_to_priority: dict        # lowercased email -> priority_weight (float)
    email_to_name: dict            # lowercased email -> display name
    concern_terms: list            # [(heat, set_of_lowercased_terms)]
    built_at: float = 0.0


@dataclass
class ItemScore:
    score: float
    tier: str                      # "surface" | "maybe" | "suppress"
    reasons: list = field(default_factory=list)
    sender: str = ""
    priority: float = 0.0
    concern: str = ""              # name of the active concern this touches


_CTX_CACHE: dict = {"ctx": None, "built_at": 0.0}
_CTX_TTL = 120


def _concern_terms(client) -> list:
    """Lowercased term sets per active concern, tagged by heat. Terms come
    from the concern name + attached project/org slugs + attached people
    basenames, so a note mentioning any of them counts as on-topic."""
    try:
        from standing_agent.queries import query_active_concerns
        concerns = query_active_concerns(client)
    except Exception:
        return []
    out = []
    for c in concerns:
        terms = set()
        for raw in [c.get("name"), c.get("attached_project"), c.get("attached_org")]:
            if raw and isinstance(raw, str):
                for w in re.split(r"[\s/_\-]+", raw.lower()):
                    if len(w) >= 4:        # skip short noise words
                        terms.add(w)
        for p in (c.get("attached_people") or []):
            base = str(p).split("/")[-1].replace("-", " ").lower()
            for w in base.split():
                if len(w) >= 3:
                    terms.add(w)
        if terms:
            out.append((c.get("heat", "warm"), c.get("name", ""), terms))
    return out


def build_context(client: Optional[ParachuteClient] = None,
                  force: bool = False) -> ScoreContext:
    now = time.monotonic()
    if (not force and _CTX_CACHE["ctx"]
            and _CTX_CACHE["built_at"] > now - _CTX_TTL):
        return _CTX_CACHE["ctx"]
    client = client or ParachuteClient(timeout=20)
    build_email_index(client, force_refresh=False)
    by_path = people_resolver._INDEX_CACHE["by_path"]
    email_to_priority: dict = {}
    email_to_name: dict = {}
    for rp in by_path.values():
        if getattr(rp, "is_non_human", False):
            continue
        for e in rp.emails:
            el = e.lower()
            # Keep the highest priority if an email maps to multiple notes.
            if el not in email_to_priority or rp.priority_weight > email_to_priority[el]:
                email_to_priority[el] = rp.priority_weight
                email_to_name[el] = rp.name
    ctx = ScoreContext(
        email_to_priority=email_to_priority,
        email_to_name=email_to_name,
        concern_terms=_concern_terms(client),
        built_at=now,
    )
    _CTX_CACHE["ctx"] = ctx
    _CTX_CACHE["built_at"] = now
    return ctx


def _sender_email(raw: str) -> str:
    """Bare address from a 'Name <addr>' From header."""
    if not raw:
        return ""
    m = EMAIL_RE.search(raw)
    return m.group(0).lower() if m else raw.strip().lower()


def score_inbound(note, ctx: ScoreContext) -> ItemScore:
    """Score one email/message note 0..1 by consequence to the owner."""
    m = note.metadata or {}
    subject = str(m.get("subject") or m.get("title") or "").strip()
    sender_raw = str(m.get("from") or m.get("sender") or "")
    sender = _sender_email(sender_raw)
    # Text we scan for stakes — subject + preview (no body fetch needed).
    blob = f"{subject}\n{getattr(note, 'preview', '') or ''}"

    reasons: list[str] = []

    # --- Hard suppressors: bots/automated senders are never the EA's job. ---
    if sender in SELF_ADDRESSES:
        return ItemScore(0.02, "suppress",
                         ["agent/the owner's own outbound mail"], sender, 0.0)
    if sender and is_automated_email(sender):
        return ItemScore(0.05, "suppress",
                         [f"automated sender ({sender})"], sender, 0.0)
    # Google Calendar system mail — the calendar is synced separately.
    if _CALENDAR_NOISE.search(subject):
        return ItemScore(0.08, "suppress",
                         ["calendar notification (handled by calendar sync)"],
                         sender, ctx.email_to_priority.get(sender, 0.0))

    # --- Relationship: who is this from, to the owner? ---
    priority = ctx.email_to_priority.get(sender, 0.0)
    rel = 0.0
    if priority >= 0.9:
        rel = 0.50; reasons.append(f"from {ctx.email_to_name.get(sender, sender)} (top priority)")
    elif priority >= 0.7:
        rel = 0.38; reasons.append(f"from {ctx.email_to_name.get(sender, sender)} (inner circle)")
    elif priority >= 0.5:
        rel = 0.26; reasons.append(f"from {ctx.email_to_name.get(sender, sender)} (active contact)")
    elif priority > 0.0:
        rel = 0.12; reasons.append(f"from {ctx.email_to_name.get(sender, sender)}")

    # A real human we just don't have weighted yet still deserves a baseline —
    # the EA reads mail from real people, not just the top 25.
    is_known_human = priority > 0.0
    looks_human = bool(sender) and not is_automated_email(sender)
    base = 0.30 if looks_human else 0.10
    if not is_known_human and looks_human:
        reasons.append("from a person")

    # --- Concern/project relevance: does this touch something hot? ---
    rel_concern = 0.0
    matched_concern = ""
    blob_low = blob.lower()
    for heat, cname, terms in ctx.concern_terms:
        if any(t in blob_low for t in terms):
            bump = {"hot": 0.30, "warm": 0.20, "standby": 0.10}.get(heat, 0.10)
            if bump > rel_concern:
                rel_concern = bump
                matched_concern = cname
                label = f"touches {cname}" if cname else f"touches a {heat} concern"
                reasons.append(label)
    # --- Stakes signals (subject/preview). ---
    stakes = 0.0
    if _DEADLINE_RE.search(blob):
        stakes += 0.22; reasons.append("time-sensitive")
    if _DECISION_RE.search(blob):
        stakes += 0.16; reasons.append("asks for a decision/your input")
    if _MONEY_RE.search(blob):
        stakes += 0.12; reasons.append("money/contract")
    if _QUESTION_TO_YOU.search(subject):
        stakes += 0.08; reasons.append("direct question")
    stakes = min(stakes, 0.40)

    # --- Noise subjects damp the score (recaps, newsletters, etc.). ---
    noise_penalty = 0.0
    if _NOISE_SUBJECT.search(subject):
        noise_penalty = 0.28
        reasons.append("notification/newsletter-style subject")

    score = base + rel + rel_concern + stakes - noise_penalty
    # urgent tag from Prism is a weak positive nudge, not a decider.
    if "urgent" in (note.tags or []):
        score += 0.05
    # Learned preference: The owner's past Relevant/Noise verdicts on this sender.
    try:
        from triage_feedback import learned_adjustment
        adj = learned_adjustment(sender)
        if adj:
            score += adj
            reasons.append("you've called this sender "
                           + ("relevant" if adj > 0 else "noise") + " before")
    except Exception:
        pass
    score = max(0.0, min(1.0, score))

    tier = ("surface" if score >= SURFACE_BAR
            else "maybe" if score >= MAYBE_BAR
            else "suppress")
    return ItemScore(round(score, 3), tier, reasons, sender, priority,
                     concern=matched_concern)


# ---------------------------------------------------------------------------
# CLI: audit the current action-required pool through the scorer
# ---------------------------------------------------------------------------

def _audit(limit: int = 250):
    client = ParachuteClient(timeout=30)
    ctx = build_context(client, force=True)
    print(f"world-model: {len(ctx.email_to_priority)} weighted senders, "
          f"{len(ctx.concern_terms)} active concerns\n")
    pool = client.query_notes(tag=["email", "action-required"], limit=limit,
                              include_content=False, newest_first=True)
    scored = [(score_inbound(n, ctx), n) for n in pool]
    scored.sort(key=lambda x: -x[0].score)
    from collections import Counter
    tally = Counter(s.tier for s, _ in scored)
    print(f"Scored {len(scored)} action-required emails: "
          f"surface={tally['surface']}  maybe={tally['maybe']}  suppress={tally['suppress']}")
    print(f"→ would surface {tally['surface']}/{len(scored)} "
          f"({100*tally['surface']//max(1,len(scored))}%) vs the old flat dump of all of them\n")
    print("TOP 12 by consequence:")
    for s, n in scored[:12]:
        subj = (n.metadata or {}).get("subject", "")[:54]
        print(f"  [{s.score:.2f} {s.tier:8}] {subj!r:56} — {'; '.join(s.reasons[:2])}")
    print("\nBOTTOM 8 (correctly suppressed):")
    for s, n in scored[-8:]:
        subj = (n.metadata or {}).get("subject", "")[:54]
        print(f"  [{s.score:.2f} {s.tier:8}] {subj!r:56} — {'; '.join(s.reasons[:1])}")


if __name__ == "__main__":
    _audit()
