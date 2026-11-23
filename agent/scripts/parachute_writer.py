#!/usr/bin/env python3
"""
parachute_writer — Skill-friendly wrapper around ParachuteClient.

Encapsulates Parachute schema conventions (paths, tags, metadata, links) so
skills don't have to learn the full data model. The intent: a skill that
extracts entities from a meeting can write them with three lines of code,
and idempotently — re-running with the same data won't create duplicates.

Schema rules implemented here are documented in `.claude/PARACHUTE.md`.

Usage:
    from parachute_writer import ParachuteWriter
    w = ParachuteWriter()

    person_id = w.upsert_person("Sarah Chen", organization="ACME", email="s@example.com")
    project_id = w.upsert_project("acme", title="Acme",
                                  collaborators=["Jordan Lee", "Casey Kim"])
    meeting_id = w.upsert_meeting(
        title="Strategy session",
        date="2026-04-25",
        transcript="...",
        attendees=["Sarah Chen", "Sample Owner"],
        project_slug="acme",
        source="fathom",
    )

Test:
    python3 scripts/parachute_writer.py --test
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

# Ensure parachute_client is importable when this is invoked from skill bash
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from parachute_client import ParachuteClient, Note, NoteIndex, ParachuteError
from vault_shapes import canonical_task_status, strip_empty

PROJECTS_ROOT = "vault/projects"


def project_note_path(slug: str) -> str:
    """The canonical project NOTE: `vault/projects/<slug>/PROJECT`.

    `vault/projects/<slug>` is a folder, not a note; a link to it resolves to
    nothing (qa/vault-health.md §6: 28+ dangling folder links). Link the note.
    """
    return f"{PROJECTS_ROOT}/{slugify(slug)}/PROJECT"


# ---------------------------------------------------------------------------
# Slug + wikilink helpers
# ---------------------------------------------------------------------------

def slugify(value: str) -> str:
    """ASCII-fold, lowercase, hyphenate. Stable, idempotent."""
    if not value:
        return ""
    # Normalize accents away
    norm = unicodedata.normalize("NFKD", value)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    # Replace any non-alphanumeric run with a hyphen
    s = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    return s


# ---------------------------------------------------------------------------
# Person-emission guard (handoff §4: extract-entities is a sprawl source)
# ---------------------------------------------------------------------------

# Local-parts that denote a role/automated mailbox, never an individual human.
_ROLE_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "notifications", "notification", "notify", "support",
    "billing", "hello", "info", "team", "help", "admin", "contact", "sales",
    "marketing", "newsletter", "news", "updates", "alerts", "security",
    "accounts", "account", "service", "services", "feedback", "invites",
    "invite", "bounce", "bounces", "automated", "auto", "system", "root",
}


# Transcript/meeting-bot and notification vendors. ANY address at these
# domains is a routing artifact, never an individual's personal mailbox —
# e.g. executiveassistant@e.read.ai, no-reply@otter.ai. These slipped past
# the role-localpart check and seeded the 2026-06-03 merge corruption.
_AUTOMATED_DOMAINS = {
    "read.ai", "otter.ai", "fathom.video", "tldv.io", "fireflies.ai",
    "meetgeek.ai", "grain.com", "sembly.ai", "avoma.com", "substack.com",
    "academia.edu", "academia-mail.com", "medium.com", "eventbrite.com",
    "luma-mail.com", "lu.ma", "meetup.com",
}


def is_automated_email(email: str) -> bool:
    """True if the address is a role/automated/bot mailbox, not an individual.

    Filters before emitting a `person` note so notification artifacts like
    'Someone via Miro' (invites@notification.miro.com) never sprawl the
    person graph. See handoff 2026-06-02 §4.
    """
    if not email or "@" not in email:
        return False
    local, _, domain = email.strip().lower().partition("@")
    local_base = local.split("+", 1)[0]  # drop +tag suffix
    if local_base in _ROLE_LOCALPARTS:
        return True
    if local_base.endswith("bot") or local_base.endswith("-bot") or local_base.endswith(".bot"):
        return True
    # Bot-vendor domains (read.ai, otter.ai, ...) and their subdomains
    # (e.read.ai, mail.fireflies.ai, ...).
    if any(domain == d or domain.endswith("." + d) for d in _AUTOMATED_DOMAINS):
        return True
    # Notification subdomains (notification.miro.com, mail.notion.so, ...)
    if any(domain.startswith(p + ".") or f".{p}." in f".{domain}." for p in
           ("notification", "notifications", "notify", "mailer", "bounce", "email")):
        return True
    return False


def _project_slug_of(path: str) -> str:
    """`vault/projects/<slug>/PROJECT` or legacy `vault/projects/<slug>` -> slug."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "vault" and parts[1] == "projects":
        return parts[2]
    return parts[-1] if parts else ""


def wikilink(path: str) -> str:
    """Render a Parachute wikilink string from a path. Existing notes use the
    `[[vault/...]]` convention in metadata values for backward compat."""
    return f"[[{path}]]"


def _wikilinks_to_paths(values: Optional[list]) -> list[str]:
    """Strip `[[...]]` wrapping from a list of wikilink strings, if present."""
    if not values:
        return []
    out: list[str] = []
    for v in values:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("[[") and v.endswith("]]"):
                v = v[2:-2]
                if "|" in v:  # alias form
                    v = v.split("|", 1)[0]
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# ParachuteWriter
# ---------------------------------------------------------------------------

class ParachuteWriter:
    """High-level upsert API for skills.

    All upsert_* methods are idempotent: they update an existing note matching
    the canonical path, or create a new note if none exists. They merge
    metadata (don't overwrite) and union tags (don't replace).
    """

    def __init__(self, client: Optional[ParachuteClient] = None, timeout: int = 15):
        self.client = client or ParachuteClient(timeout=timeout)
        self._project_paths: dict[str, str] = {}

    # ----- Project resolution ------------------------------------------------

    def project_path(self, slug_or_name: str) -> str:
        """Path of the note that IS the project for a slug.

        `vault/projects/<slug>/PROJECT` when it exists (or nothing exists yet:
        new projects are created there); a legacy note sitting at the folder
        path itself is honoured so its links keep resolving. Cached per writer.
        """
        slug = slugify(slug_or_name)
        if slug in self._project_paths:
            return self._project_paths[slug]
        canonical = project_note_path(slug)
        path = canonical
        if self._get(canonical) is None and self._get(f"{PROJECTS_ROOT}/{slug}") is not None:
            path = f"{PROJECTS_ROOT}/{slug}"
        self._project_paths[slug] = path
        return path

    def project_link(self, slug_or_name: str) -> str:
        """`[[vault/projects/<slug>/PROJECT]]` — never a link to the folder."""
        return wikilink(self.project_path(slug_or_name))

    # ----- Internal helpers --------------------------------------------------

    def _get(self, path: str) -> Optional[Note]:
        try:
            return self.client.get_note(path, include_content=True)
        except ParachuteError as e:
            if e.status == 404:
                return None
            raise
        except Exception:
            return None

    def _upsert(
        self,
        path: str,
        *,
        content: str = "",
        tags: Iterable[str] = (),
        metadata: Optional[dict] = None,
        replace_content: bool = False,
    ) -> str:
        """Generic upsert. Returns the note id."""
        existing = self._get(path)
        type_tags = list(tags)

        if existing is None:
            # A new note gets no placeholder keys: "" in a list field (or an
            # empty scalar) is absent, never written (vault-health §2).
            note = self.client.create_note(
                content=content,
                path=path,
                tags=type_tags,
                metadata=strip_empty(metadata),
            )
            return note.id

        # Merge metadata (don't clobber existing fields with empty values)
        merged_metadata = {}
        if metadata:
            for k, v in metadata.items():
                # Skip empty values so we don't blank out existing fields
                if v in (None, "", [], {}):
                    continue
                merged_metadata[k] = v

        # Tag union — only ADD missing tags, don't remove
        existing_tags = set(existing.tags or [])
        tags_to_add = [t for t in type_tags if t not in existing_tags]

        update_kwargs: dict[str, Any] = {}
        if replace_content and content:
            update_kwargs["content"] = content
        elif content and not (existing.content or "").strip():
            # Existing note had no content, we have new content — fill it in
            update_kwargs["content"] = content
        if merged_metadata:
            update_kwargs["metadata"] = merged_metadata
            # Tell the shape guard what kind of note this is (never sent to the
            # vault): without it an update only gets the by-name rules.
            update_kwargs["shape_tags"] = sorted(existing_tags | set(type_tags))
        if tags_to_add:
            update_kwargs["tags_add"] = tags_to_add

        if update_kwargs:
            self.client.update_note(existing.id, **update_kwargs)
        return existing.id

    # ----- Person ------------------------------------------------------------

    def upsert_person(
        self,
        name: str,
        *,
        slug: Optional[str] = None,
        role: str = "",
        organization: str = "",
        organizations: Optional[list[str]] = None,
        email: str = "",
        projects: Optional[list[str]] = None,
        first_met: str = "",
        aliases: Optional[list[str]] = None,
        source: str = "",
        confidence: str = "",
        extra_tags: Optional[list[str]] = None,
        body: Optional[str] = None,
    ) -> str:
        if not name:
            raise ValueError("upsert_person requires a non-empty name")
        # Sprawl guard: never mint a person note for a role/automated mailbox.
        # Callers that genuinely want such an entity should write it directly
        # with a `non-human` tag instead of going through upsert_person.
        if email and is_automated_email(email):
            raise ValueError(
                f"refusing to create person from automated/role email: {email} "
                f"(name={name!r}). Tag it `non-human` if it must exist."
            )
        slug = slug or slugify(name)
        path = f"vault/people/{slug}"

        orgs = list(organizations or [])
        if organization and organization not in orgs:
            orgs.insert(0, organization)
        org_links = [wikilink(f"vault/organizations/{slugify(o)}") for o in orgs if o]

        proj_links = [self.project_link(p) for p in (projects or []) if p]

        metadata = {
            "title": name,
            "type": "person",
            "role": role,
            "organizations": org_links,
            "projects": proj_links,
            "contact": email,
            "first-met": first_met,
            "aliases": aliases or [],
            "source": source,
            # A label (high|medium|low); the shape guard maps a number.
            "confidence": confidence,
        }

        tags = ["person"] + (extra_tags or [])
        if body is None:
            body = f"# {name}\n\n## Context\n\n"

        return self._upsert(path, content=body, tags=tags, metadata=metadata)

    @staticmethod
    def _is_resolvable_person(note: NoteIndex | Note) -> bool:
        """Skip tombstoned merges and non-human notes when resolving a person.

        Handoff §3: a merged secondary keeps the `person` tag but carries
        metadata.status == 'merged_into_canonical'; `non-human` notes are
        intentionally not people.
        """
        tags = set(note.tags or [])
        if "non-human" in tags:
            return False
        if (note.metadata or {}).get("status") == "merged_into_canonical":
            return False
        return True

    def find_person(self, name: str) -> Optional[Note]:
        """Lookup a person by exact path, then full-text-narrowed title/alias scan.

        Resolves through tombstones: never returns a merged secondary or a
        `non-human` note. Paginates the (small) search result set fully rather
        than capping at 200 — the cap was a known dedup blind spot.
        """
        slug = slugify(name)
        n = self._get(f"vault/people/{slug}")
        if n and self._is_resolvable_person(n):
            return n

        name_lower = name.strip().lower()
        # Full-text search narrows ~4000 people to a handful before scanning,
        # so we can afford to page through all matches without a hard cap.
        offset, page = 0, 200
        for _ in range(50):  # safety bound: 10k candidates
            try:
                results = self.client.query_notes(
                    tag="person", search=name, exclude_tags="non-human",
                    limit=page, offset=offset, include_content=False,
                )
            except Exception:
                return None
            if not results:
                break
            for r in results:
                if not self._is_resolvable_person(r):
                    continue
                md = r.metadata or {}
                title = (md.get("title") or "").lower()
                aliases = md.get("aliases") or []
                alias_hit = isinstance(aliases, list) and any(
                    a and a.lower() == name_lower for a in aliases
                )
                if title == name_lower or alias_hit:
                    full = self._get(r.path) if r.path else None
                    if full and self._is_resolvable_person(full):
                        return full
            if len(results) < page:
                break
            offset += page
        return None

    def tombstone_person(
        self,
        secondary_id_or_path: str,
        canonical_path: str,
        *,
        merged_at: Optional[str] = None,
    ) -> Optional[str]:
        """Merge a duplicate person into a canonical note as a TOMBSTONE.

        Handoff §3 — person merges are tombstones, NEVER deletes:
          - secondary content becomes `# (merged) …\\n\\nSee [[canonical]]`
          - metadata: status='merged_into_canonical', merged_into, merged_at
          - keep the `person` tag (so historical links still resolve)
          - transfer outbound links to the canonical note
          - union channels.email / emails onto the canonical

        Returns the canonical note id, or None if either note is missing.
        Idempotent: a note already tombstoned to the same canonical is a no-op.
        """
        from datetime import datetime, timezone

        secondary = self._get(secondary_id_or_path)
        canonical = self._get(canonical_path)
        if secondary is None or canonical is None:
            return None
        if secondary.id == canonical.id:
            return canonical.id

        sec_md = secondary.metadata or {}
        if sec_md.get("status") == "merged_into_canonical" and \
                sec_md.get("merged_into") == f"[[{canonical_path}]]":
            return canonical.id  # already merged here

        stamp = merged_at or datetime.now(timezone.utc).isoformat()

        # 1) Union email channels onto the canonical before we rewrite the secondary.
        canon_md = canonical.metadata or {}

        def _emails(md: dict) -> list[str]:
            out: list[str] = []
            for key in ("emails", "channels.email", "contact", "email"):
                v = md.get(key)
                if isinstance(v, list):
                    out.extend(x for x in v if x)
                elif isinstance(v, str) and v:
                    out.append(v)
            ch = md.get("channels")
            if isinstance(ch, dict):
                e = ch.get("email")
                if isinstance(e, list):
                    out.extend(x for x in e if x)
                elif isinstance(e, str) and e:
                    out.append(e)
            return out

        merged_emails = []
        for e in _emails(canon_md) + _emails(sec_md):
            if e not in merged_emails:
                merged_emails.append(e)
        if merged_emails:
            self.client.update_note(canonical.id, metadata={"emails": merged_emails})

        # 2) Transfer the secondary's outbound links to the canonical.
        sec_links = secondary.links or []
        links_add = [
            {"target": l.get("target") or l.get("target_id"),
             "relationship": l.get("relationship") or l.get("kind") or "related"}
            for l in sec_links
            if (l.get("target") or l.get("target_id"))
            and (l.get("target") or l.get("target_id")) != canonical_path
        ]
        if links_add:
            try:
                self.client.update_note(canonical.id, links_add=links_add)
            except ParachuteError:
                pass  # link transfer is best-effort; tombstone still proceeds

        # 3) Rewrite the secondary as a tombstone (keep the `person` tag).
        title = (sec_md.get("title") or secondary.path or "").split("/")[-1]
        tombstone_body = (
            f"# (merged) {title}\n\n"
            f"This note was merged into [[{canonical_path}]] on {stamp[:10]}.\n"
        )
        self.client.update_note(
            secondary.id,
            content=tombstone_body,
            metadata={
                "status": "merged_into_canonical",
                "merged_into": f"[[{canonical_path}]]",
                "merged_at": stamp,
            },
        )
        return canonical.id

    # ----- Organization ------------------------------------------------------

    def upsert_organization(
        self,
        name: str,
        *,
        slug: Optional[str] = None,
        url: str = "",
        description: str = "",
        extra_tags: Optional[list[str]] = None,
    ) -> str:
        slug = slug or slugify(name)
        path = f"vault/organizations/{slug}"
        metadata = {
            "title": name,
            "type": "organization",
            "url": url,
        }
        body = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
        return self._upsert(path, content=body, tags=["organization"] + (extra_tags or []),
                             metadata=metadata)

    # ----- Concept -----------------------------------------------------------

    def upsert_concept(
        self,
        name: str,
        *,
        slug: Optional[str] = None,
        kind: str = "concept",  # concept | pattern | protocol | primitive
        description: str = "",
        sectors: Optional[list[str]] = None,
        scales: Optional[list[str]] = None,
        extra_tags: Optional[list[str]] = None,
    ) -> str:
        slug = slug or slugify(name)
        path = f"vault/concepts/{slug}"
        metadata = {
            "title": name,
            "type": kind,
            "sectors": sectors or [],
            "scales": scales or [],
        }
        body = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
        tags = ["concept"]
        if kind != "concept":
            tags.append(kind)
        tags.extend(extra_tags or [])
        return self._upsert(path, content=body, tags=tags, metadata=metadata)

    # ----- Project -----------------------------------------------------------

    def upsert_project(
        self,
        slug: str,
        *,
        title: Optional[str] = None,
        description: str = "",
        collaborators: Optional[list[str]] = None,
        organization: Optional[str] = None,
        url: str = "",
        status: str = "active",
        aliases: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        body: Optional[str] = None,
    ) -> str:
        slug = slugify(slug)
        path = self.project_path(slug)
        title = title or slug.replace("-", " ").title()

        collab_links = [wikilink(f"vault/people/{slugify(c)}") for c in (collaborators or []) if c]
        org_link = wikilink(f"vault/organizations/{slugify(organization)}") if organization else ""

        metadata = {
            "title": title,
            "type": "project",
            "status": status,
            "org": org_link,
            "collaborators": collab_links,
            "url": url,
            "aliases": aliases or [],
            "keywords": keywords or [],
            "slug": slug,
        }

        tags = ["project", slug]
        if body is None:
            # The title and collaborators are properties, not duplicate prose.
            body = description or ""

        return self._upsert(path, content=body, tags=tags, metadata=metadata)

    def find_project(self, slug_or_name: str) -> Optional[Note]:
        return self._get(self.project_path(slug_or_name))

    def list_projects(self) -> list[Note]:
        """Return all project notes with content (for keyword matching)."""
        try:
            results = self.client.query_notes(tag="project", limit=200, include_content=True)
        except Exception:
            return []
        # Filter out non-project pages that happen to have project tag
        return [r for r in results if r.path and r.path.startswith("vault/projects/")]

    def find_project_by_keywords(
        self,
        content: str,
        attendees: Optional[list[str]] = None,
        title: str = "",
    ) -> tuple[Optional[str], int, list[tuple[str, int]]]:
        """Score content against each known project. Returns (best_slug, score, all_scores).

        Scoring (matches OPAL meeting-processor weights):
          +5  project title in meeting title
          +4  project alias mentioned
          +3  project name in content; team member is an attendee
          +2  team member name in content; project keyword
          +1  project tag found
        """
        attendees_lc = [a.lower() for a in (attendees or [])]
        title_lc = title.lower()
        content_lc = content.lower()
        scores: list[tuple[str, int]] = []

        for proj in self.list_projects():
            md = proj.metadata or {}
            slug = (md.get("slug") or _project_slug_of(proj.path or "")).lower()
            proj_title = (md.get("title") or "").lower()
            aliases = [a.lower() for a in (md.get("aliases") or []) if a]
            collaborators_raw = md.get("collaborators") or []
            collaborator_names = [
                p.split("/")[-1].replace("-", " ")
                for p in _wikilinks_to_paths(collaborators_raw)
            ]
            keywords = [k.lower() for k in (md.get("keywords") or []) if k]

            score = 0
            if proj_title and proj_title in title_lc:
                score += 5
            if proj_title and proj_title in content_lc:
                score += 3
            for a in aliases:
                if a and (a in title_lc or a in content_lc):
                    score += 4
                    break  # at most one alias bonus
            for cn in collaborator_names:
                cn_lc = cn.lower()
                if cn_lc and cn_lc in attendees_lc:
                    score += 3
                elif cn_lc and cn_lc in content_lc:
                    score += 2
            for kw in keywords:
                if kw and kw in content_lc:
                    score += 2
            if slug and slug in content_lc:
                score += 1

            if score > 0:
                scores.append((slug, score))

        scores.sort(key=lambda x: -x[1])
        if not scores:
            return None, 0, []
        return scores[0][0], scores[0][1], scores

    # ----- Meeting -----------------------------------------------------------

    def upsert_meeting(
        self,
        *,
        title: str,
        date: str,                              # YYYY-MM-DD
        transcript: str = "",
        summary: str = "",
        attendees: Optional[list[str]] = None,
        project_slug: Optional[str] = None,
        concepts: Optional[list[str]] = None,
        organizations: Optional[list[str]] = None,
        action_items: Optional[list[str]] = None,
        decisions: Optional[list[str]] = None,
        topics: Optional[dict[str, str]] = None,
        source: str = "",                       # fathom | meetily | manual
        recording_id: str = "",
        recording_url: str = "",
        duration_minutes: Optional[int] = None,
        status: str = "processed",
        topic_tags: Optional[list[str]] = None,
        slug_override: Optional[str] = None,
        body: Optional[str] = None,
    ) -> str:
        if not title:
            raise ValueError("upsert_meeting requires a title")
        slug = slug_override or slugify(title)
        if project_slug:
            path = f"vault/projects/{slugify(project_slug)}/meetings/{date}-{slug}"
        else:
            path = f"vault/meetings/{date}/{slug}"

        attendee_links = [wikilink(f"vault/people/{slugify(p)}") for p in (attendees or []) if p]
        concept_links = [wikilink(f"vault/concepts/{slugify(c)}") for c in (concepts or []) if c]
        org_links = [wikilink(f"vault/organizations/{slugify(o)}") for o in (organizations or []) if o]
        project_link = self.project_link(project_slug) if project_slug else ""

        metadata = {
            "title": title,
            "type": "meeting",
            "date": date,
            "duration_minutes": duration_minutes,
            "projects": [project_link] if project_link else [],
            "attendees": attendee_links,
            "concepts": concept_links,
            "organizations": org_links,
            "source": source,
            "status": status,
            "recording_id": str(recording_id) if recording_id not in (None, "") else "",
            "fathom_url": recording_url if source == "fathom" else "",
            "url": recording_url,
        }
        # Drop empty values to avoid clutter (but keep arrays as-is so they replace cleanly)
        metadata = {k: v for k, v in metadata.items() if v not in (None, "")}

        tags = ["meeting"]
        if transcript:
            tags.append("transcript")
        if project_slug:
            tags.append(slugify(project_slug))
        if status == "processed":
            tags.append("processed")
        tags.extend(topic_tags or [])

        if body is None:
            body = self._render_meeting_body(
                title=title, date=date, project_slug=project_slug, project_link=project_link,
                attendees=attendees or [], summary=summary, action_items=action_items or [],
                decisions=decisions or [], topics=topics or {}, transcript=transcript,
            )

        return self._upsert(path, content=body, tags=tags, metadata=metadata)

    @staticmethod
    def _render_meeting_body(*, title, date, project_slug, project_link, attendees,
                              summary, action_items, decisions, topics, transcript) -> str:
        lines = [f"# {title}", ""]
        if project_link:
            lines.append(f"> **Project:** {project_link}")
        lines.append(f"> **Date:** {date}")
        if attendees:
            lines.append("> **Attendees:** " + ", ".join(
                wikilink(f"vault/people/{slugify(a)}") for a in attendees
            ))
        lines.append("")
        if summary:
            lines.extend(["## Summary", "", summary, ""])
        if action_items:
            lines.append("## Action Items")
            lines.extend(f"- [ ] {item}" for item in action_items)
            lines.append("")
        if decisions:
            lines.append("## Decisions")
            lines.extend(f"- {d}" for d in decisions)
            lines.append("")
        if topics:
            lines.append("## Topics")
            for topic, notes in topics.items():
                lines.append(f"### {topic}")
                lines.append(notes)
                lines.append("")
        if transcript:
            lines.extend(["---", "", "## Transcript", "", transcript, ""])
        return "\n".join(lines)

    # ----- Task --------------------------------------------------------------

    def upsert_task(
        self,
        title: str,
        *,
        slug: Optional[str] = None,
        status: str = "pending",   # tasks_store vocabulary: pending | in-progress | blocked | waiting | completed | cancelled | archived
        priority: str = "medium",   # low | medium | high | critical
        project_slug: Optional[str] = None,
        assigned: str = (os.environ.get("OMNI_OWNER_NAME", "").strip() or "me"),
        due: str = "",
        context: str = "",
        completed: str = "",        # ISO date set when status == done
        source_meeting_path: Optional[str] = None,
        extra_tags: Optional[list[str]] = None,
    ) -> str:
        slug = slug or slugify(title)[:80]
        # One vocabulary (tasks_store): todo -> pending, done -> completed.
        status = {"todo": "pending", "done": "completed"}.get(
            canonical_task_status(status), canonical_task_status(status))
        bucket = "done" if status in ("completed", "cancelled", "archived") else "active"
        path = f"vault/tasks/{bucket}/{slug}"

        project_link = self.project_link(project_slug) if project_slug else ""
        meeting_link = wikilink(source_meeting_path) if source_meeting_path else ""

        if status == "completed" and not completed:
            from datetime import datetime, timezone
            completed = datetime.now(timezone.utc).date().isoformat()

        # Declared task fields (handoff §2): status/due/assigned/priority/
        # completed/context — NOT pending/deadline/requester.
        metadata = {
            "title": title,
            "type": "task",
            "status": status,
            "priority": priority,
            "project": project_link,
            "assigned": assigned,
            "due": due,
            "completed": completed,
            "context": context,
            "source_meeting": meeting_link,
        }
        # Status lives in metadata only; a status word as a TAG drifts from it
        # (vault-health §1: todo/done/archived… used as both).
        tags = ["task"]
        if priority in ("high", "critical"):
            tags.append("urgent")
        if project_slug:
            tags.append(slugify(project_slug))
        tags.extend(extra_tags or [])

        body_lines = [f"# {title}", ""]
        if context:
            body_lines.extend(["## Context", context, ""])
        if meeting_link:
            body_lines.append(f"From: {meeting_link}")
        body = "\n".join(body_lines)

        return self._upsert(path, content=body, tags=tags, metadata=metadata)

    # ----- Generic note ------------------------------------------------------

    def upsert_note(
        self,
        path: str,
        *,
        content: str = "",
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        replace_content: bool = True,
    ) -> str:
        """Escape hatch for non-standard note types (briefings, insights, etc.)."""
        return self._upsert(path, content=content, tags=tags or [],
                            metadata=metadata or {}, replace_content=replace_content)

    # ----- Linking -----------------------------------------------------------

    def add_meeting_attendee(self, meeting_path: str, person_name: str) -> None:
        """Append a person link to an existing meeting's attendees metadata."""
        meeting = self._get(meeting_path)
        if meeting is None:
            return
        link = wikilink(f"vault/people/{slugify(person_name)}")
        attendees = list((meeting.metadata or {}).get("attendees") or [])
        if link in attendees:
            return
        attendees.append(link)
        self.client.update_note(meeting.id, metadata={"attendees": attendees})


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def _selftest() -> None:
    """End-to-end roundtrip on test entities. Cleans up after."""
    w = ParachuteWriter()

    print("Creating test person...")
    pid = w.upsert_person(
        "Test Person Selfcheck",
        organization="Test Org Selfcheck",
        email="test@example.com",
    )
    print(f"  person id: {pid}")

    print("Creating test project...")
    proj_id = w.upsert_project(
        "test-project-selfcheck",
        title="Test Project Selfcheck",
        collaborators=["Test Person Selfcheck"],
        keywords=["testing", "verification"],
    )
    print(f"  project id: {proj_id}")

    print("Creating test meeting (linked to project + person)...")
    meeting_id = w.upsert_meeting(
        title="Test Meeting Selfcheck",
        date="2026-04-25",
        summary="A test meeting for verifying the writer.",
        attendees=["Test Person Selfcheck", "Sample Owner"],
        project_slug="test-project-selfcheck",
        action_items=["Verify the writer works end-to-end"],
        source="manual",
        topic_tags=["testing"],
    )
    print(f"  meeting id: {meeting_id}")

    print("Round-trip read...")
    fetched = w.client.get_note(f"vault/projects/test-project-selfcheck/meetings/2026-04-25-test-meeting-selfcheck")
    assert fetched is not None, "Round-trip failed"
    assert "meeting" in fetched.tags
    assert "test-project-selfcheck" in fetched.tags
    assert "transcript" not in fetched.tags  # we didn't pass transcript
    md = fetched.metadata or {}
    assert md.get("source") == "manual"
    assert "[[vault/projects/test-project-selfcheck/PROJECT]]" in md.get("projects", [])
    print(f"  ✓ tags={fetched.tags[:5]} attendees={md.get('attendees')}")

    print("Idempotency check (re-upsert same meeting)...")
    meeting_id_2 = w.upsert_meeting(
        title="Test Meeting Selfcheck",
        date="2026-04-25",
        summary="Updated summary.",
        attendees=["Test Person Selfcheck", "Sample Owner"],
        project_slug="test-project-selfcheck",
        source="manual",
    )
    assert meeting_id_2 == meeting_id, f"Idempotency broken: {meeting_id} != {meeting_id_2}"
    print(f"  ✓ same id returned")

    print("Project keyword matching...")
    slug, score, all_scores = w.find_project_by_keywords(
        "We discussed testing and verification with the testing team.",
        attendees=["Test Person Selfcheck"],
        title="testing huddle",
    )
    print(f"  best={slug} score={score} (top: {all_scores[:3]})")
    assert slug == "test-project-selfcheck", f"Expected test-project-selfcheck, got {slug}"

    print("Cleaning up test entities...")
    for path in [
        f"vault/projects/test-project-selfcheck/meetings/2026-04-25-test-meeting-selfcheck",
        f"vault/projects/test-project-selfcheck/PROJECT",
        f"vault/people/test-person-selfcheck",
        f"vault/organizations/test-org-selfcheck",
    ]:
        n = w._get(path)
        if n:
            try:
                w.client.delete_note(n.id)
                print(f"  deleted {path}")
            except Exception as e:
                print(f"  WARNING: failed to delete {path}: {e}")

    print("\n✓ parachute_writer self-test passed")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _selftest()
    else:
        print("parachute_writer — wrapper around ParachuteClient.")
        print("Use --test to run the self-check.")
