#!/usr/bin/env python3
"""The one place tasks are read and written. Parachute is the only store.

There is no local file, no Notion mirror, no vault-markdown write-back, and no
fallback. That is deliberate and it is the point: for months this system had a
Parachute copy AND `state/active-tasks.json`, with the dashboard reading the
first and writing the second, agents writing the second because CLAUDE.md told
them to, and nothing reconciling the two. Tasks did not get lost outright, but
"the task list" meant different things depending on which surface you asked, and
the session summary read from a file whose shape had drifted until the whole
check silently threw.

A fallback would recreate exactly that. If Parachute is down, these functions
raise. A task system that fails loudly is honest; one that quietly serves a
stale local copy is how you lose a task while believing you never could.

Legacy task IDs (`task-84266712`, `notion-…`) live on as the `source_id`
metadata field, so old references keep resolving. `id()` prefers `source_id`
and falls back to the Parachute note id.

    from tasks_store import read_tasks, get_task, create_task, update_task
"""

from __future__ import annotations

import os
import re
import sys
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from parachute_client import ParachuteClient  # noqa: E402
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)

MST = ZoneInfo(OWNER_TZ)

TASK_TAG = "task"
TASK_PATH_PREFIX = "vault/tasks"

# Parachute has accumulated several vocabularies for the same lifecycle
# (Prism wrote `todo`, the old migration wrote `pending`, meeting extraction
# wrote `tracked`). Normalize on read so callers see one set; write the
# canonical spelling.
_STATUS_IN = {
    "todo": "pending",
    "tracked": "pending",
    "active": "in-progress",
    "doing": "in-progress",
    "done": "completed",
    "resolved": "completed",
    # These were read as *active* until 2026-09-30, so a task someone marked
    # duplicate/discarded stayed on the list.
    "duplicate": "cancelled",
    "discarded": "cancelled",
    "dropped": "cancelled",
    "waiting-on": "waiting",
}
ACTIVE_STATUSES = frozenset({"pending", "in-progress", "blocked"})
# Someone else owes it. Tracked, surfaced in its own section, never overdue
# on the owner's list.
WAITING_STATUSES = frozenset({"waiting"})
DONE_STATUSES = frozenset({"completed", "cancelled", "archived"})
CANONICAL_STATUSES = ACTIVE_STATUSES | WAITING_STATUSES | DONE_STATUSES

# Where a deadline came from. Only a deadline someone actually said can make a
# task overdue; the transcript job used to invent `due` dates from hints like
# "next week", and those made up most of the 34 "overdue" tasks on 2026-09-30.
DEADLINE_SOURCES = frozenset({"stated", "owner", "guessed", "none"})
_HARD_DEADLINE_SOURCES = frozenset({"stated", "owner"})

#: The owner as a task owner. Config: OMNI_OWNER_NAME (full name and first name both count).
OWNER_NAME = os.environ.get("OMNI_OWNER_NAME", "").strip()
SELF_OWNERS = frozenset({"", "owner", "me", "self"} | {n.lower() for n in (OWNER_NAME, OWNER_NAME.split(" ")[0]) if n})


def _client(timeout: int = 10) -> ParachuteClient:
    return ParachuteClient(timeout=timeout)


def normalize_status(status: Optional[str]) -> str:
    s = (status or "pending").strip().lower()
    return _STATUS_IN.get(s, s)


def is_active(task: dict) -> bool:
    return normalize_status(task.get("status")) in ACTIVE_STATUSES


def _strip_wikilink(value) -> str:
    """Project fields arrive as bare slugs, `[[vault/projects/x]]`, or a list."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if not isinstance(value, str) or not value:
        return ""
    if value.startswith("[[") and value.endswith("]]"):
        inner = value[2:-2]
        return inner.split("projects/")[-1] if "projects/" in inner else inner
    return value


def _clean_preview(preview: str) -> str:
    """First line of a note preview without markdown heading/link syntax."""
    line = (preview or "").strip().splitlines()[0] if (preview or "").strip() else ""
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)?", "", line)
    return line.strip()


def _to_task(note) -> dict:
    """Map a Parachute note to the task dict shape callers already expect.

    `description` is the one-line headline every surface prints, so it is the
    TITLE when one exists. It used to prefer the metadata `description`, which
    writers fill with a context sentence ("Kevin forked the Ma Earth QF app…")
    while `title` holds the actual task ("Get added to technefoundation/qf-app").
    The context sentence is kept as `details`.
    """
    m = note.metadata if isinstance(note.metadata, dict) else {}
    title = (m.get("title") or "").strip() or _clean_preview(getattr(note, "preview", ""))
    desc = (m.get("description") or "").strip()
    deadline = m.get("deadline") or m.get("due") or "no-deadline"
    return {
        "id": m.get("source_id") or note.id,
        "parachute_id": note.id,
        "path": note.path,
        "type": m.get("type", ""),
        "title": title or desc or getattr(note, "preview", "") or "",
        "description": title or desc or getattr(note, "preview", "") or "",
        "details": desc if desc and desc != title else "",
        "owner": m.get("owner") or m.get("assigned") or "",
        "requester": m.get("requester") or m.get("assigned") or "",
        "dateReceived": m.get("dateReceived") or getattr(note, "created_at", "") or "",
        "deadline": deadline,
        "deadline_source": m.get("deadline_source") or ("none" if deadline == "no-deadline" else "legacy"),
        "source": m.get("source", ""),
        "source_ref": m.get("source_ref", ""),
        "status": normalize_status(m.get("status")),
        "context": m.get("context", ""),
        "lastUpdate": getattr(note, "updated_at", "") or "",
        "project": _strip_wikilink(m.get("project", "")),
        "priority": m.get("priority", "medium"),
        "tags": getattr(note, "tags", []) or [],
    }


def read_tasks(limit: int = 2000) -> list[dict]:
    """Every task. Raises if Parachute is unreachable — see module docstring."""
    notes = _client().query_notes(
        tag=TASK_TAG, limit=limit, include_content=False, newest_first=True
    )
    tasks = []
    for n in notes:
        try:
            tasks.append(_to_task(n))
        except Exception:
            # One malformed note must not blank the whole list. Skipping is
            # safe here because it cannot hide a task from a *write* — writes
            # resolve by id against Parachute directly.
            continue
    return tasks


def read_active_tasks() -> list[dict]:
    return [t for t in read_tasks() if is_active(t)]


def is_waiting(task: dict) -> bool:
    return normalize_status(task.get("status")) in WAITING_STATUSES


def deadline_dt(task: dict) -> Optional[datetime]:
    """The deadline as an aware datetime in MST, or None. Date-only values
    mean end of that day locally — not midnight UTC, which made a task due
    "today" read overdue from 5 PM the evening before."""
    dl = (task.get("deadline") or "").strip()
    if not dl or dl == "no-deadline" or dl.startswith("recurring"):
        return None
    try:
        if len(dl) == 10:
            d = datetime.fromisoformat(dl).replace(hour=23, minute=59, tzinfo=MST)
        else:
            d = datetime.fromisoformat(dl.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=MST)
        return d
    except ValueError:
        return None


def is_overdue(task: dict, now: Optional[datetime] = None) -> bool:
    """Active, past its deadline, and the deadline was not a guess."""
    if not is_active(task):
        return False
    if task.get("deadline_source") == "guessed":
        return False
    d = deadline_dt(task)
    return d is not None and d < (now or datetime.now(MST))


def get_task(task_id: str) -> Optional[dict]:
    """Resolve by source_id first, then Parachute note id, then path."""
    for t in read_tasks():
        if t["id"] == task_id or t["parachute_id"] == task_id:
            return t
    note = _client().get_note(task_id)
    return _to_task(note) if note else None


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower()))[:70].strip("-")


class TaskRejected(ValueError):
    """A proposed task failed the quality bar. The message says why."""


_PAST_TENSE_LEAD = re.compile(
    r"^[A-Z][a-z]+(?: [A-Z][a-z]+)? (agreed|proposed|said|forked|replied|asked|"
    r"mentioned|noted|shared|offered|committed|confirmed|suggested|wanted)\b"
)


def validate_task(task_data: dict) -> dict:
    """Apply the task quality bar; return normalized fields or raise.

    A task is something THE OWNER will do, phrased as an action, with an
    honest deadline. Everything else is a different kind of thing:
      - someone else's commitment  -> status "waiting" (waiting-on list)
      - an observation / alert     -> an agent-insight, not a task
      - a meeting commitment       -> a promise first (see missions/ledger)
    """
    title = (task_data.get("title") or task_data.get("description") or "").strip()
    if not title:
        raise TaskRejected("a task needs a title")
    if len(title) > 140:
        raise TaskRejected("title over 140 chars — that is context, not a task")
    if _PAST_TENSE_LEAD.match(title):
        raise TaskRejected(
            f"title reads as a report of what someone did, not an action: {title[:60]!r}"
        )
    status = normalize_status(task_data.get("status", "pending"))
    if status not in CANONICAL_STATUSES:
        raise TaskRejected(f"unknown status {status!r}")
    owner = (task_data.get("owner") or "").strip()
    if owner.lower() not in SELF_OWNERS and status not in WAITING_STATUSES | DONE_STATUSES:
        # Someone else's commitment is tracked, just not as the owner's to-do.
        status = "waiting"
    deadline = task_data.get("deadline") or "no-deadline"
    ds = task_data.get("deadline_source")
    if ds is None:
        ds = "none" if deadline == "no-deadline" else "owner"
    if ds not in DEADLINE_SOURCES:
        raise TaskRejected(f"deadline_source must be one of {sorted(DEADLINE_SOURCES)}")
    return {"title": title, "status": status, "owner": owner or OWNER_NAME or "me",
            "deadline": deadline, "deadline_source": ds}


def create_task(task_data: dict) -> dict:
    """Create a task note after validate_task(). Raises TaskRejected.

    Accepts `title` (preferred) or the legacy `description`; a separate
    `details` / `context` carries the why.
    """
    v = validate_task(task_data)
    desc = v["title"]
    status = v["status"]

    now = datetime.now(MST).isoformat()
    details = (task_data.get("details") or "").strip()
    metadata = {
        "title": desc,
        "description": details or desc,
        "owner": v["owner"],
        "deadline_source": v["deadline_source"],
        "type": task_data.get("type") or "task",
        "status": status,
        "priority": task_data.get("priority") or "medium",
        "requester": task_data.get("requester") or OWNER_NAME or "me",
        "deadline": v["deadline"],
        "dateReceived": task_data.get("dateReceived") or now,
        "context": task_data.get("context") or "",
    }
    if task_data.get("project"):
        metadata["project"] = task_data["project"]
    if task_data.get("source_id"):
        metadata["source_id"] = task_data["source_id"]
    for key in ("source", "source_ref"):
        if task_data.get(key):
            metadata[key] = task_data[key]

    folder = "done" if status in DONE_STATUSES else ""
    path = f"{TASK_PATH_PREFIX}/{folder + '/' if folder else ''}{_slug(desc)}"

    note = _client().create_note(
        path=path,
        content=task_data.get("context") or desc,
        tags=[TASK_TAG],
        metadata=metadata,
    )
    return _to_task(note)


def update_task(task_id: str, updates: dict) -> Optional[dict]:
    """Patch a task's metadata. Returns the updated task, or None if absent."""
    existing = get_task(task_id)
    if not existing:
        return None

    metadata = {}
    for key in ("status", "priority", "deadline", "project", "context",
                "type", "requester", "description", "title", "owner",
                "deadline_source", "source", "source_ref", "details"):
        if key in updates:
            metadata[key] = updates[key]
    if "status" in metadata:
        metadata["status"] = normalize_status(metadata["status"])
        if metadata["status"] in DONE_STATUSES and "completed" not in metadata:
            metadata["completed"] = datetime.now(MST).isoformat()
    if "details" in metadata:
        metadata["description"] = metadata.pop("details")
    elif "description" in metadata and "title" not in metadata:
        # Legacy callers pass `description` meaning the headline.
        metadata["title"] = metadata["description"]
    if not metadata:
        return existing

    # shape_tags: lets the shape guard apply the task rules (status vocabulary).
    note = _client().update_note(existing["parachute_id"], metadata=metadata, shape_tags=["task"])
    return _to_task(note)


def complete_task(task_id: str) -> Optional[dict]:
    return update_task(task_id, {"status": "completed"})


if __name__ == "__main__":
    tasks = read_tasks()
    active = [t for t in tasks if is_active(t)]
    waiting = [t for t in tasks if is_waiting(t)]
    overdue = sorted((t for t in active if is_overdue(t)), key=lambda t: deadline_dt(t))
    print(f"Tasks: {len(active)} active, {len(overdue)} overdue, {len(waiting)} waiting-on others")
    for t in overdue[:5]:
        print(f"  - {t['title'][:70]} (due: {t['deadline'][:10]})")
    if len(overdue) > 5:
        print(f"  ... and {len(overdue) - 5} more")
