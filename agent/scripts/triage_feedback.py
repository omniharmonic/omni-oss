#!/usr/bin/env python3
"""
triage_feedback — Learn from the owner's "Relevant / Noise" verdicts on
borderline triaged items so the system gets smarter over time.

State lives in state/triage-feedback.json:
  {
    "verdicts": {<note_id>: {"verdict": "relevant"|"noise", "ts": "...", "sender": "...", "subject": "..."}},
    "noise_senders": {<sender_lc>: {"count": N, "last": "..."}},
    "relevant_senders": {<sender_lc>: {"count": N, "last": "..."}}
  }

Two modes of use:
  1. record_verdict(note_id, verdict, sender, subject) — store user's decision
  2. is_likely_noise(sender) — used by the relay to filter known-noise senders
     (3+ "noise" verdicts and zero "relevant" verdicts → filter out entirely)

The bar to mark a sender as "always noise" is intentionally high — we want
suppression to be earned, not over-eager. Otherwise we re-create the original
problem from a different angle.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "state" / "triage-feedback.json"

NOISE_THRESHOLD = 3   # verdicts marked noise before sender auto-filters
COOL_OFF_DAYS = 30    # if user once said "relevant", give the sender that long before noise can win again


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_sender(sender: str) -> str:
    """Strip RFC-2822 angle brackets, quotes, and lowercase for stable key."""
    if not sender:
        return ""
    s = sender.strip()
    if "<" in s and ">" in s:
        # "Name <addr>" → addr
        s = s[s.index("<") + 1 : s.index(">")]
    s = s.strip().strip('"').lower()
    return s


def _load() -> dict:
    if not STATE_FILE.exists():
        return {"verdicts": {}, "noise_senders": {}, "relevant_senders": {}}
    try:
        d = json.loads(STATE_FILE.read_text())
        d.setdefault("verdicts", {})
        d.setdefault("noise_senders", {})
        d.setdefault("relevant_senders", {})
        return d
    except (json.JSONDecodeError, OSError):
        return {"verdicts": {}, "noise_senders": {}, "relevant_senders": {}}


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATE_FILE)


def record_verdict(note_id: str, verdict: str, sender: str = "", subject: str = "") -> None:
    """Store a user verdict ('relevant' or 'noise')."""
    if verdict not in ("relevant", "noise"):
        raise ValueError(f"verdict must be 'relevant' or 'noise', got {verdict!r}")
    data = _load()
    data["verdicts"][note_id] = {
        "verdict": verdict,
        "ts": _now_iso(),
        "sender": sender,
        "subject": subject,
    }
    sender_key = _normalize_sender(sender)
    if sender_key:
        bucket = "noise_senders" if verdict == "noise" else "relevant_senders"
        entry = data[bucket].get(sender_key, {"count": 0})
        entry["count"] = entry.get("count", 0) + 1
        entry["last"] = _now_iso()
        data[bucket][sender_key] = entry
    _save(data)


def is_likely_noise(sender: str) -> bool:
    """Return True if this sender has been marked noise enough times AND not
    recently rescued by a 'relevant' verdict."""
    sender_key = _normalize_sender(sender)
    if not sender_key:
        return False
    data = _load()
    noise = data["noise_senders"].get(sender_key, {})
    rel = data["relevant_senders"].get(sender_key, {})

    if noise.get("count", 0) < NOISE_THRESHOLD:
        return False

    # If user ever said "relevant" recently, give them benefit of doubt
    if rel:
        last_rel = rel.get("last", "")
        try:
            last = datetime.fromisoformat(last_rel.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - last).days
            if age_days < COOL_OFF_DAYS:
                return False
        except (ValueError, TypeError):
            pass

    return True


def learned_adjustment(sender: str) -> float:
    """Score delta (≈ -0.4..+0.4) for the relevance scorer, from the owner's
    past Relevant/Noise verdicts on this sender. Positive = he's said he wants
    more from them; negative = he's repeatedly called them noise. This is how
    the EA learns his preferences rather than re-asking forever."""
    key = _normalize_sender(sender)
    if not key:
        return 0.0
    data = _load()
    noise = data["noise_senders"].get(key, {}).get("count", 0)
    rel = data["relevant_senders"].get(key, {}).get("count", 0)
    net = rel - noise
    if net == 0:
        return 0.0
    return max(-0.4, min(0.4, 0.13 * net))


def has_signal_conflict(tags: list[str]) -> bool:
    """Detect items with mixed/conflicting triage signals — these are the
    candidates for the ask-don't-dump prompt instead of silent inclusion."""
    tagset = set(t.lower() for t in tags)
    has_priority = bool(tagset & {"urgent", "action-required"})
    has_low = bool(tagset & {"low", "social", "informational"})
    return has_priority and has_low


def stats() -> dict:
    data = _load()
    return {
        "total_verdicts": len(data["verdicts"]),
        "noise_senders": len(data["noise_senders"]),
        "relevant_senders": len(data["relevant_senders"]),
    }


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2))
