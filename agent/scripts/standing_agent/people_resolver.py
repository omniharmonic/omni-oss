"""
People resolver — the single source of truth for "find me the person
note for this email / name / reference."

Why this exists: prior EA code scattered path-prefix heuristics across
gather_person_context, _backfill_collaborators, validate, etc. Each
one was independently brittle (gets wrong person when first names
collide, prefers stubs over named profiles inconsistently, doesn't
look at channels.email). This module centralizes resolution so every
EA touchpoint gets identical, defensible behavior.

Public API:
    resolve_by_email(email)           → ResolvedPerson | None
    resolve_by_name(name)             → list[ResolvedPerson]
    resolve_from_text(text)           → list[ResolvedPerson]
    build_email_index()               → dict[email → path]   (cached)
    find_merge_candidates()           → list of fragmentation cases

The email index is built once and cached for 5 minutes — single
Parachute scan covers ~all subsequent EA lookups in a deliberation
cycle.

Schema-canonical email location: `metadata.channels.email` (Prism's
default). The resolver ALSO reads `metadata.emails` and `metadata.email`
for compatibility, but writes should prefer `channels.email`.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("standing_agent.people_resolver")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ResolvedPerson:
    """The canonical person resolution result.

    Always contains a path, the canonical name (best guess), priority
    weight, EA metadata, and the kind (named profile / stub / external).
    """
    path: str
    name: str
    emails: list[str]
    priority_weight: float
    cadence_baseline_days: Optional[int]
    relationship_status: Optional[str]
    last_meaningful_contact: Optional[str]
    is_named_profile: bool         # True if vault/people/{First Last} form
    is_stub: bool                  # True if dash-case/auto-stub
    ea_managed: bool
    is_non_human: bool = False     # tagged non-human (vendor/bot/org/role)
    raw_metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict = {
    "email_to_paths": {},
    "by_path": {},
    "built_at": 0.0,
}
_INDEX_TTL_SEC = 300  # 5 minutes


def _is_titlecase_named(path: str) -> bool:
    base = (path or "").split("/")[-1]
    if not base or " " not in base:
        return False
    return base[:1].isupper()


def _is_dashcase_stub(path: str) -> bool:
    base = (path or "").split("/")[-1].lower()
    if not base or " " in base:
        return False
    return ("-" in base or "_" in base)


def _extract_emails(m: dict) -> set[str]:
    """Pull every email reference from a person note's metadata."""
    emails: set[str] = set()
    ch = m.get("channels")
    if isinstance(ch, dict):
        ce = ch.get("email")
        if isinstance(ce, list):
            emails.update(e.lower().strip() for e in ce if e)
        elif isinstance(ce, str) and ce:
            emails.add(ce.lower().strip())
    plural = m.get("emails")
    if isinstance(plural, list):
        emails.update(e.lower().strip() for e in plural if e)
    elif isinstance(plural, str) and plural:
        emails.add(plural.lower().strip())
    singular = m.get("email")
    if isinstance(singular, str) and singular and "@" in singular:
        emails.add(singular.lower().strip())
    contact = m.get("contact", "")
    if isinstance(contact, str):
        for e in EMAIL_RE.findall(contact):
            emails.add(e.lower())
    # CRITICAL: never let a role/automated/bot mailbox (noreply@github.com,
    # no-reply@otter.ai, team@acme.example.org, ...) enter the index. Such an
    # address is shared by every person Prism ever ingested a notification
    # from, so using it as a join key collapses unrelated people into one
    # profile. This is the root cause of the 2026-06-03 people-merge
    # incident (and three earlier
    # collapses). See is_automated_email().
    return {e for e in emails if e and not is_automated_email(e)}


# Tokens that are never evidence two notes are the same person: bot/routing
# markers, joining words, and common email-TLD fragments that leak into
# email-derived stub paths (jane-acme-network → {acme, network}).
_NAME_STOPWORDS = {
    "via", "the", "of", "and", "from", "at", "team", "admin", "support",
    "insights", "assistant", "notifications", "notification", "noreply",
    "com", "net", "org", "io", "co", "ai", "us", "uk", "earth", "fund",
    "app", "dev", "tech", "xyz", "gmail", "social", "network", "foundation",
    "read", "otter", "miro", "notion", "github", "calendly", "tally",
}


def _name_tokens(*sources: str) -> set[str]:
    """Lowercased name-like tokens drawn from any of the given strings
    (display name, path basename). Bot/routing markers and TLD fragments
    are dropped; everything after a 'via' marker is discarded."""
    toks: set[str] = set()
    for s in sources:
        if not s or "@" in s:
            continue
        base = s.split("/")[-1]
        raw = [t.lower() for t in re.split(r"[\s\-_.]+", base) if t]
        if "via" in raw:                       # strip "X via Bot" suffix
            raw = raw[: raw.index("via")]
        for t in raw:
            if t.isdigit() or len(t) < 3 or t in _NAME_STOPWORDS:
                continue
            toks.add(t)
    return toks


def names_compatible(stub, keeper) -> bool:
    """True only when the stub and keeper plausibly name the SAME person.

    The auto-merger's email join is no longer enough on its own — two
    distinct people can legitimately share a personal address, and (before
    the index filter) shared a bot address. This requires at least one
    overlapping name token between the two notes' names/paths before any
    automatic merge. If there is zero overlap (e.g. 'acme-community-group' vs
    'Jane Doe'), the pair is surfaced for human review
    instead of merged.
    """
    stub_toks = _name_tokens(
        (stub.raw_metadata or {}).get("name", ""),
        (stub.raw_metadata or {}).get("title", ""),
        stub.path,
    )
    keeper_toks = _name_tokens(
        (keeper.raw_metadata or {}).get("name", ""),
        (keeper.raw_metadata or {}).get("title", ""),
        keeper.path,
    )
    if not stub_toks or not keeper_toks:
        return False        # can't verify → don't auto-merge
    return bool(stub_toks & keeper_toks)


def _best_name(m: dict, path: str) -> str:
    """Pick the best display name from metadata, falling back to path."""
    for key in ("name", "title"):
        v = m.get(key)
        if v and isinstance(v, str) and v.strip() and "@" not in v:
            return v.strip()
    # Fall back to path basename, cleaned up
    base = (path or "").split("/")[-1].replace("-", " ").replace("_", " ")
    return base.strip()


def _make_resolved(note) -> ResolvedPerson:
    m = note.metadata or {}
    emails = sorted(_extract_emails(m))
    return ResolvedPerson(
        path=note.path or note.id,
        name=_best_name(m, note.path or ""),
        emails=emails,
        priority_weight=float(m.get("priority_weight") or 0.2),
        cadence_baseline_days=m.get("cadence_baseline_days"),
        relationship_status=m.get("relationship_status"),
        last_meaningful_contact=m.get("last_meaningful_contact"),
        is_named_profile=_is_titlecase_named(note.path or ""),
        is_stub=_is_dashcase_stub(note.path or ""),
        ea_managed=bool(m.get("ea_managed")),
        is_non_human="non-human" in (getattr(note, "tags", None) or []),
        raw_metadata=m,
    )


def build_email_index(client: Optional[ParachuteClient] = None,
                       force_refresh: bool = False) -> dict[str, list[str]]:
    """Build (or return cached) email → list[path] index.

    Returns: dict mapping each email to the list of person-note paths
    that reference it. Most emails map to exactly one path; multiple
    paths means fragmentation we should resolve via canonicalization.
    """
    global _INDEX_CACHE
    if (not force_refresh
            and _INDEX_CACHE["built_at"] > time.monotonic() - _INDEX_TTL_SEC
            and _INDEX_CACHE["email_to_paths"]):
        return _INDEX_CACHE["email_to_paths"]

    client = client or ParachuteClient(timeout=20)
    logger.info("Building email→person index (full scan)...")
    by_email: dict[str, list[str]] = {}
    by_path: dict[str, ResolvedPerson] = {}

    offset = 0
    while True:
        batch = client.query_notes(tag="person", limit=500, offset=offset,
                                   include_content=False)
        if not batch:
            break
        for n in batch:
            resolved = _make_resolved(n)
            # Always keep tombstones in by_path so callers can detect
            # them, but DON'T index their emails — the canonical they
            # were merged into owns those emails now.
            by_path[resolved.path] = resolved
            meta = resolved.raw_metadata or {}
            is_tombstone = meta.get("status") == "merged_into_canonical"
            merged_into = meta.get("merged_into") or ""
            # Self-referential tombstones (status set but merged_into
            # missing or pointing at self) are corruption — treat as
            # healthy notes. This protects against a class of partial-
            # write bugs we hit during retries.
            if is_tombstone and merged_into and merged_into != resolved.path:
                continue
            # Non-human notes (vendors/bots/orgs/role mailboxes) stay in
            # by_path for lookups but NEVER seed the email join index — they
            # must not be merge candidates with anyone.
            if resolved.is_non_human:
                continue
            for e in resolved.emails:
                by_email.setdefault(e, []).append(resolved.path)
        if len(batch) < 500:
            break
        offset += 500
        if offset > 8000:
            break

    # Mutate in place — DO NOT rebind. Other modules import this dict
    # by reference; rebinding orphans their references.
    _INDEX_CACHE["email_to_paths"] = by_email
    _INDEX_CACHE["by_path"] = by_path
    _INDEX_CACHE["built_at"] = time.monotonic()
    logger.info("Indexed %d emails across %d person notes",
                len(by_email), len(by_path))
    return by_email


def _get_by_path(client: ParachuteClient, path: str) -> Optional[ResolvedPerson]:
    """Cached path lookup using the index built by build_email_index."""
    if path in _INDEX_CACHE["by_path"]:
        return _INDEX_CACHE["by_path"][path]
    # Cache miss — fetch
    n = client.get_note(path, include_content=False)
    if not n:
        return None
    resolved = _make_resolved(n)
    _INDEX_CACHE["by_path"][path] = resolved
    return resolved


# ---------------------------------------------------------------------------
# Public resolution API
# ---------------------------------------------------------------------------

def resolve_by_email(
    email: str, client: Optional[ParachuteClient] = None,
) -> Optional[ResolvedPerson]:
    """Find the canonical person note for an email address.

    Resolution priority when multiple matches exist:
      1. ea_managed: True (we already curated this)
      2. is_named_profile: True (Title Case path, real profile)
      3. has the most populated metadata fields (richer = canonical)
      4. lexicographically first path (deterministic tiebreak)

    Returns None if no person note references this email anywhere.
    """
    if not email:
        return None
    email = email.lower().strip()
    client = client or ParachuteClient(timeout=15)
    index = build_email_index(client)
    paths = index.get(email)
    if not paths:
        return None

    candidates = []
    for p in paths:
        rp = _get_by_path(client, p)
        if rp:
            candidates.append(rp)
    if not candidates:
        return None

    def rank(rp: ResolvedPerson) -> tuple:
        # Higher tuple = better. Negate to use Python's ascending sort.
        return (
            int(rp.ea_managed),                      # ea_managed wins
            int(rp.is_named_profile),                # named profile wins
            int(not rp.is_stub),                     # not-stub wins
            len(rp.raw_metadata),                    # more metadata wins
            -ord(rp.path[0]) if rp.path else 0,      # earlier path wins
        )
    candidates.sort(key=rank, reverse=True)
    return candidates[0]


def resolve_by_name(
    name: str, client: Optional[ParachuteClient] = None,
) -> list[ResolvedPerson]:
    """Find all person notes whose name (or filename) matches `name`.

    Returns a list because names collide. The caller should disambiguate
    by other context (email, org, recent meeting).
    """
    if not name or len(name) < 2:
        return []
    name_lower = name.lower().strip()
    client = client or ParachuteClient(timeout=15)
    build_email_index(client)   # populate by_path cache

    matches: list[ResolvedPerson] = []
    for path, rp in _INDEX_CACHE["by_path"].items():
        if rp.name.lower() == name_lower:
            matches.append(rp)
            continue
        # Path basename match (handles stubs)
        base = path.split("/")[-1].replace("-", " ").replace("_", " ").lower()
        if base == name_lower:
            matches.append(rp)

    # Sort: ea_managed > named > stub > other
    matches.sort(key=lambda rp: (
        int(rp.ea_managed), int(rp.is_named_profile), int(not rp.is_stub)
    ), reverse=True)
    return matches


def resolve_from_text(
    text: str, client: Optional[ParachuteClient] = None,
) -> list[ResolvedPerson]:
    """Find all people referenced by email in arbitrary text.

    Useful for parsing email From: headers, meeting attendee strings,
    etc. — pulls every email and resolves each.
    """
    if not text:
        return []
    emails = set(e.lower() for e in EMAIL_RE.findall(text))
    if not emails:
        return []
    client = client or ParachuteClient(timeout=15)
    out: list[ResolvedPerson] = []
    seen_paths: set[str] = set()
    for e in emails:
        rp = resolve_by_email(e, client)
        if rp and rp.path not in seen_paths:
            out.append(rp)
            seen_paths.add(rp.path)
    return out


# ---------------------------------------------------------------------------
# Fragmentation surfacing (helper for dedup tools)
# ---------------------------------------------------------------------------

def find_merge_candidates(
    client: Optional[ParachuteClient] = None,
) -> list[dict]:
    """Return list of {email, canonical_path, merge_paths} for each
    email that maps to multiple person notes where one is a stub and
    one is a named profile."""
    client = client or ParachuteClient(timeout=15)
    index = build_email_index(client, force_refresh=True)

    out: list[dict] = []
    for email, paths in index.items():
        if len(paths) < 2:
            continue
        resolved = [_get_by_path(client, p) for p in paths]
        resolved = [r for r in resolved if r]
        # Find the "keeper" via the same ranking used in resolve_by_email
        named = [r for r in resolved if r.is_named_profile]
        if not named:
            # No named profile — leave as-is for now; merging stubs is harder
            continue
        keeper = max(named, key=lambda r: (int(r.ea_managed), len(r.raw_metadata)))
        merges = [r for r in resolved if r.path != keeper.path]
        out.append({
            "email": email,
            "canonical_path": keeper.path,
            "merge_paths": [r.path for r in merges],
            "merge_count": len(merges),
        })

    return out


def stub_top_collaborators(
    client: Optional[ParachuteClient] = None,
) -> list[dict]:
    """Surface EA-managed people whose current path is a dash-case stub.
    These probably have a canonical named profile elsewhere that should
    be the resolver target — manual review needed."""
    client = client or ParachuteClient(timeout=15)
    build_email_index(client, force_refresh=True)
    out: list[dict] = []
    for path, rp in _INDEX_CACHE["by_path"].items():
        if rp.ea_managed and rp.is_stub:
            out.append({
                "path": rp.path,
                "name": rp.name,
                "emails": rp.emails,
                "priority_weight": rp.priority_weight,
            })
    return out


# ---------------------------------------------------------------------------
# CLI for inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="People resolver inspector")
    ap.add_argument("--email", help="Resolve a single email")
    ap.add_argument("--name", help="Resolve by name")
    ap.add_argument("--from-text", help="Find people in arbitrary text")
    ap.add_argument("--merge-candidates", action="store_true",
                    help="List fragmentation cases")
    ap.add_argument("--ea-stubs", action="store_true",
                    help="EA-managed people pointing at stub notes")
    ap.add_argument("--stats", action="store_true",
                    help="Index size + coverage")
    args = ap.parse_args()

    client = ParachuteClient(timeout=20)

    if args.email:
        r = resolve_by_email(args.email, client)
        if r:
            print(f"Resolved: {r.path}")
            print(f"  Name: {r.name}")
            print(f"  Emails: {r.emails}")
            print(f"  Priority: {r.priority_weight}")
            print(f"  Status: {r.relationship_status}")
            print(f"  Cadence: {r.cadence_baseline_days}d")
            print(f"  Kind: {'named' if r.is_named_profile else 'stub' if r.is_stub else 'other'}")
        else:
            print(f"No person note found for {args.email}")

    if args.name:
        rs = resolve_by_name(args.name, client)
        print(f"{len(rs)} matches for '{args.name}':")
        for r in rs:
            tag = "named" if r.is_named_profile else "stub" if r.is_stub else "—"
            print(f"  [{tag}] {r.path}  prio={r.priority_weight}")

    if args.from_text:
        rs = resolve_from_text(args.from_text, client)
        print(f"{len(rs)} people resolved from text:")
        for r in rs:
            print(f"  {r.name} → {r.path}")

    if args.merge_candidates:
        cands = find_merge_candidates(client)
        print(f"{len(cands)} merge candidates:")
        for c in cands:
            print(f"  {c['email']}: keep {c['canonical_path']}, merge {c['merge_count']}")
            for p in c["merge_paths"]:
                print(f"    → {p}")

    if args.ea_stubs:
        stubs = stub_top_collaborators(client)
        print(f"{len(stubs)} EA-managed people on stub notes:")
        for s in stubs:
            print(f"  {s['path']}  prio={s['priority_weight']}  emails={s['emails']}")

    if args.stats:
        idx = build_email_index(client, force_refresh=True)
        print(f"Index size: {len(idx)} emails → {sum(len(v) for v in idx.values())} note refs")
        ambiguous = [(e, len(p)) for e, p in idx.items() if len(p) > 1]
        print(f"Ambiguous (>1 path per email): {len(ambiguous)}")
        for e, c in sorted(ambiguous, key=lambda x: -x[1])[:10]:
            print(f"  {e}: {c} notes")
