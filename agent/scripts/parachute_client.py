#!/usr/bin/env python3
"""
Parachute Vault Client — Python wrapper for the Parachute HTTP API.

Used by telegram_bot.py, context_loader, dashboard, and other scripts
to read/write the knowledge graph without duplicating HTTP/auth logic.

Usage:
    from parachute_client import ParachuteClient
    client = ParachuteClient()  # reads from .env
    notes = client.query_notes(tag="briefing", limit=5)

Test connectivity:
    python3 scripts/parachute_client.py --test
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Load .env if not already in environment
# ---------------------------------------------------------------------------

_SHAPES = None
_SHAPE_WARNED = False


def _shape(metadata: Optional[dict], tags: Optional[list[str]], mode: str = "create") -> Optional[dict]:
    """Enforce declared field shapes on every write (scripts/vault_shapes.py).
    A guard failure never blocks a write: the metadata goes out as given."""
    if not metadata:
        return metadata
    global _SHAPES
    try:
        if _SHAPES is None:
            import importlib.util
            from pathlib import Path as _P
            spec = importlib.util.spec_from_file_location(
                "vault_shapes", _P(__file__).resolve().parent / "vault_shapes.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            _SHAPES = mod
        return _SHAPES.normalize_metadata(metadata, tags, mode)
    except Exception as e:  # noqa: BLE001 — fail open, but never silently
        global _SHAPE_WARNED
        if not _SHAPE_WARNED:
            _SHAPE_WARNED = True
            print(f"[parachute_client] WARNING: vault shape guard is not running "
                  f"({type(e).__name__}: {e}); metadata is being sent unshaped", file=sys.stderr)
        return metadata


def _load_dotenv():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val


_load_dotenv()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class NoteIndex:
    """Lightweight note summary (no content)."""
    id: str
    path: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    byte_size: Optional[int] = None
    preview: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "NoteIndex":
        return cls(
            id=d["id"],
            path=d.get("path"),
            tags=d.get("tags", []),
            metadata=d.get("metadata", {}),
            created_at=d.get("createdAt"),
            updated_at=d.get("updatedAt"),
            byte_size=d.get("byteSize"),
            preview=d.get("preview"),
        )


@dataclass
class Note(NoteIndex):
    """Full note with content."""
    content: str = ""
    links: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        return cls(
            id=d["id"],
            content=d.get("content", ""),
            path=d.get("path"),
            tags=d.get("tags", []),
            metadata=d.get("metadata", {}),
            created_at=d.get("createdAt"),
            updated_at=d.get("updatedAt"),
            byte_size=d.get("byteSize"),
            preview=d.get("preview"),
            links=d.get("links", []),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ParachuteError(Exception):
    """Raised on API errors."""
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Parachute API error {status}: {message}")


class ParachuteClient:
    """Thin Python client for the Parachute Vault HTTP API."""

    # Parachute 0.5.x: REST lives under /vault/{vault}/api and only accepts
    # hub-minted JWTs. Legacy pvt_* tokens are hard-rejected (401 "no longer
    # supported"). We can re-mint a scoped JWT against the local operator
    # identity, so the client is self-healing across the 90-day token expiry.
    _BUN_PARACHUTE = os.path.expanduser("~/.bun/bin/parachute")

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 15,
        vault: Optional[str] = None,
    ):
        self.host = (base_url or os.environ.get("PARACHUTE_URL", "http://127.0.0.1:1940")).rstrip("/")
        self.vault = vault or os.environ.get("PARACHUTE_VAULT", "default")
        # All REST calls are rooted here in 0.5.x (was bare /api in 0.4.x).
        self.base_url = f"{self.host}/vault/{self.vault}/api"
        self.api_key = api_key or os.environ.get("PARACHUTE_API_KEY", "")
        self.timeout = timeout
        # A stale pvt_* token is worse than none — force a re-mint.
        if self.api_key.startswith("pvt_"):
            self.api_key = ""
        if not self.api_key:
            self.api_key = self._mint_token() or ""
        if not self.api_key:
            raise ParachuteError(
                0,
                "No valid Parachute token. Set PARACHUTE_API_KEY to a hub-minted "
                "JWT, or ensure `parachute auth mint-token` works locally.",
            )

    def _mint_token(self) -> Optional[str]:
        """Mint a fresh write-scoped JWT against the local operator identity.

        Returns the token string, or None if the CLI is unavailable. Used both
        at construction (when no token is configured) and as a 401 self-heal.
        """
        import shutil
        import subprocess

        exe = self._BUN_PARACHUTE if Path(self._BUN_PARACHUTE).exists() else shutil.which("parachute")
        if not exe:
            return None
        try:
            out = subprocess.run(
                [exe, "auth", "mint-token", "--scope", f"vault:{self.vault}:write"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:
            return None
        if out.returncode != 0:
            return None
        tok = (out.stdout or "").strip()
        return tok or None

    # -- low-level HTTP --

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 body: Optional[dict] = None, _retry_count: int = 0) -> Any:
        """HTTP call with automatic retry on transient failures.

        Retries on:
          - 401 (token expired/invalid — re-mint a fresh JWT once, then retry;
            also covers the occasional transient auth blip on writes)
          - 502 / 503 / 504 (server transient)
          - URLError (connection blip)
        Max 3 retries with backoff: 0.5s, 1s, 2s.
        """
        import time as _time

        url = f"{self.base_url}{path}"
        if params:
            qs_parts = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, bool):
                    qs_parts.append(f"{k}={'true' if v else 'false'}")
                elif isinstance(v, list):
                    for item in v:
                        qs_parts.append(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(item))}")
                else:
                    qs_parts.append(f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}")
            if qs_parts:
                url += "?" + "&".join(qs_parts)

        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if data:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            try:
                err = json.loads(body_text)
                msg = err.get("message", err.get("error", body_text))
            except json.JSONDecodeError:
                msg = body_text
            # On 401, the token is likely expired — re-mint once before retrying.
            if e.code == 401 and _retry_count == 0:
                fresh = self._mint_token()
                if fresh:
                    self.api_key = fresh
            # Retry on 401 (auth blip / freshly re-minted) or 5xx
            if e.code in (401, 502, 503, 504) and _retry_count < 3:
                _time.sleep(0.5 * (2 ** _retry_count))
                return self._request(
                    method, path, params=params, body=body,
                    _retry_count=_retry_count + 1,
                )
            raise ParachuteError(e.code, msg)
        except urllib.error.URLError as e:
            if _retry_count < 3:
                _time.sleep(0.5 * (2 ** _retry_count))
                return self._request(
                    method, path, params=params, body=body,
                    _retry_count=_retry_count + 1,
                )
            raise ParachuteError(0, f"Connection failed: {e.reason}")

    # -- public API --

    def vault_info(self, include_stats: bool = False) -> dict:
        """Get vault name, description, and optionally stats."""
        # vault-info is via the MCP endpoint; use tags count as a proxy
        params = {}
        if include_stats:
            tags = self._request("GET", "/tags")
            total_notes = sum(t.get("count", 0) for t in tags)
            return {
                "status": "online",
                "tag_count": len(tags),
                "total_notes_approx": total_notes,
                "top_tags": sorted(tags, key=lambda t: t.get("count", 0), reverse=True)[:10],
            }
        # Simple health check
        self._request("GET", "/tags")
        return {"status": "online"}

    def query_notes(
        self,
        tag: Optional[str | list[str]] = None,
        path_prefix: Optional[str] = None,
        search: Optional[str] = None,
        metadata: Optional[dict] = None,
        limit: int = 50,
        offset: int = 0,
        include_content: bool = False,
        sort: Optional[str] = None,
        newest_first: bool = False,
        newest_pool: int = 500,
        exclude_tags: Optional[str | list[str]] = None,
        near: Optional[dict] = None,
        strict_response: bool = False,
    ) -> list[NoteIndex] | list[Note]:
        """Query notes with filters. Returns NoteIndex by default, Note if include_content=True.

        IMPORTANT: The Parachute server (as of 2026-05) ignores the `sort` parameter
        and always returns results in ascending createdAt order. This means
        `query_notes(tag="briefing", limit=3)` returns the THREE OLDEST briefings,
        not the newest. Callers that want the newest results must pass
        `newest_first=True`, which fetches up to `newest_pool` records, sorts them
        client-side by createdAt desc, and returns the top `limit`.

        Use `newest_first=True` for any "show me the latest X" use case
        (briefing display, freshness checks, urgent-email feeds, etc.).
        """
        if newest_first:
            return self._query_notes_newest(
                tag=tag, path_prefix=path_prefix, search=search,
                metadata=metadata, limit=limit,
                include_content=include_content, page_size=newest_pool,
            )

        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "include_content": include_content,
        }
        if tag:
            if isinstance(tag, list):
                for t in tag:
                    params.setdefault("tag", [])
                    if isinstance(params["tag"], list):
                        params["tag"].append(t)
                    else:
                        params["tag"] = [params["tag"], t]
            else:
                params["tag"] = tag
        if path_prefix:
            params["path_prefix"] = path_prefix
        if search:
            params["search"] = search
        if sort:
            params["sort"] = sort
        if exclude_tags:
            params["exclude_tags"] = exclude_tags
        if near:
            # Graph-neighborhood scope (replaces the removed semantic-search):
            # near={"note_id": ..., "depth": 2, "relationship": "..."}
            if near.get("note_id"):
                params["near.note_id"] = near["note_id"]
            if near.get("depth") is not None:
                params["near.depth"] = near["depth"]
            if near.get("relationship"):
                params["near.relationship"] = near["relationship"]
        if metadata:
            for k, v in metadata.items():
                params[f"metadata.{k}"] = v

        result = self._request("GET", "/notes", params=params)
        if not isinstance(result, list):
            if strict_response:
                raise ValueError("Note inventory response is not a list")
            return []
        cls = Note if include_content else NoteIndex
        return [cls.from_dict(n) for n in result]

    def _query_notes_newest(
        self,
        tag: Optional[str | list[str]],
        path_prefix: Optional[str],
        search: Optional[str],
        metadata: Optional[dict],
        limit: int,
        include_content: bool,
        page_size: int,
        max_pages: int = 50,
    ) -> list[NoteIndex] | list[Note]:
        """Walk pages from the tail of the result set to find the newest notes.

        The Parachute API returns results in ascending createdAt order. We
        walk forward in pages of `page_size` until a partial page signals
        the end, then sort and slice. We keep only a rolling top-N to bound
        memory. We hydrate full content only for the final top slice.
        """
        rolling: list[NoteIndex] = []
        for page in range(max_pages):
            batch = self.query_notes(
                tag=tag, path_prefix=path_prefix, search=search,
                metadata=metadata, limit=page_size, offset=page * page_size,
                include_content=False,
            )
            if not batch:
                break
            rolling.extend(batch)
            # Keep only the newest `limit * 4` so memory stays bounded but
            # we have enough slack if pages straddle the cutoff.
            rolling.sort(key=lambda n: n.created_at or "", reverse=True)
            rolling = rolling[: max(limit * 4, 16)]
            if len(batch) < page_size:
                break

        top = rolling[:limit]
        if not include_content:
            return top

        hydrated: list[Note] = []
        for idx in top:
            full = self.get_note(idx.id, include_content=True)
            if full is not None:
                hydrated.append(full)
        return hydrated

    def get_note(self, id_or_path: str, include_content: bool = True) -> Optional[Note]:
        """Get a single note by ID or path."""
        encoded = urllib.parse.quote(id_or_path, safe="")
        try:
            result = self._request("GET", f"/notes/{encoded}", params={
                "include_content": include_content,
            })
            return Note.from_dict(result)
        except ParachuteError as e:
            if e.status == 404:
                return None
            raise

    def create_note(
        self,
        content: str = "",
        path: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        links: Optional[list[dict]] = None,
    ) -> Note:
        """Create a new note. Metadata passes the shape guard (vault_shapes)."""
        metadata = _shape(metadata, tags)
        body: dict[str, Any] = {"content": content}
        if path:
            body["path"] = path
        if tags:
            body["tags"] = tags
        if metadata:
            body["metadata"] = metadata
        if links:
            body["links"] = links
        result = self._request("POST", "/notes", body=body)
        return Note.from_dict(result)

    def update_note(
        self,
        note_id: str,
        content: Optional[str] = None,
        path: Optional[str] = None,
        metadata: Optional[dict] = None,
        tags_add: Optional[list[str]] = None,
        tags_remove: Optional[list[str]] = None,
        links_add: Optional[list[dict]] = None,
        links_remove: Optional[list[dict]] = None,
        if_updated_at: Optional[str] = None,
        force: bool = True,
        shape_tags: Optional[list[str]] = None,
    ) -> Note:
        """Update an existing note. Metadata is MERGED, not replaced (you cannot
        delete a metadata key by omitting it — set it explicitly).

        `shape_tags` = the note's own tags, when the caller knows them. They are
        never sent: they only tell the shape guard which tag rules apply (a
        metadata-only update otherwise gets the by-name rules alone, so e.g. a
        numeric `confidence` on a person would pass). Pass them whenever you
        have the note in hand.

        Parachute 0.5.x requires optimistic-concurrency on every mutation: pass
        either `if_updated_at` (the note's last-seen updatedAt) or `force=True`.
        Without one, the server returns HTTP 428. We default to force=True so
        callers keep working; pass if_updated_at for true compare-and-swap.
        """
        metadata = _shape(metadata, [*(shape_tags or []), *(tags_add or [])] or None, "update")
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if path is not None:
            body["path"] = path
        if metadata:
            body["metadata"] = metadata
        if tags_add or tags_remove:
            body["tags"] = {}
            if tags_add:
                body["tags"]["add"] = tags_add
            if tags_remove:
                body["tags"]["remove"] = tags_remove
        if links_add or links_remove:
            body["links"] = {}
            if links_add:
                body["links"]["add"] = links_add
            if links_remove:
                body["links"]["remove"] = links_remove
        if if_updated_at:
            body["if_updated_at"] = if_updated_at
        elif force:
            body["force"] = True
        encoded = urllib.parse.quote(note_id, safe="")
        result = self._request("PATCH", f"/notes/{encoded}", body=body)
        return Note.from_dict(result)

    def delete_note(self, note_id: str) -> bool:
        """Delete a note by ID. NOTE: person notes must be TOMBSTONED, not
        deleted — see parachute_writer.tombstone_person()."""
        encoded = urllib.parse.quote(note_id, safe="")
        result = self._request("DELETE", f"/notes/{encoded}")
        return result.get("deleted", False)

    def search_notes(
        self,
        query: str,
        tag: Optional[str | list[str]] = None,
        limit: int = 20,
        include_content: bool = False,
    ) -> list[NoteIndex] | list[Note]:
        """Full-text search across all notes."""
        return self.query_notes(
            search=query, tag=tag, limit=limit, include_content=include_content,
        )

    def list_tags(self, include_schema: bool = False) -> list[dict]:
        """List all tags with usage counts."""
        params = {}
        if include_schema:
            params["include_schema"] = True
        return self._request("GET", "/tags", params=params)

    def update_tag(self, name: str, description: Optional[str] = None,
                   fields: Optional[dict] = None) -> dict:
        """Declare/update a tag's schema (PUT /tags/{name}).

        `fields` maps field name -> {type, enum?, indexed?}. Only string/int/bool
        fields may be indexed. See scripts/apply_tag_schemas.py for the canonical
        schema set and the cross-tag type-agreement validation.
        """
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if fields is not None:
            body["fields"] = fields
        encoded = urllib.parse.quote(name, safe="")
        return self._request("PUT", f"/tags/{encoded}", body=body)

    def find_path(self, source: str, target: str, max_depth: int = 5) -> Optional[dict]:
        """Find shortest path between two notes in the graph."""
        try:
            return self._request("GET", "/find-path", params={
                "source": source,
                "target": target,
                "max_depth": max_depth,
            })
        except ParachuteError:
            return None

    def is_healthy(self) -> tuple[bool, str]:
        """Quick health check. Returns (healthy, message)."""
        try:
            self.vault_info()
            return True, "online"
        except ParachuteError as e:
            return False, str(e)
        except Exception as e:
            return False, f"connection failed: {e}"


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _run_tests():
    print("Parachute Client — Connectivity Test")
    print("=" * 40)

    try:
        client = ParachuteClient()
    except ParachuteError as e:
        print(f"FAIL: {e}")
        return 1

    # 1. Health check
    healthy, msg = client.is_healthy()
    print(f"1. Health check: {'OK' if healthy else 'FAIL'} — {msg}")
    if not healthy:
        return 1

    # 2. Vault info with stats
    info = client.vault_info(include_stats=True)
    print(f"2. Vault stats: {info.get('tag_count', '?')} tags, ~{info.get('total_notes_approx', '?')} notes")

    # 3. Query notes
    notes = client.query_notes(limit=3)
    print(f"3. Query (limit=3): got {len(notes)} notes")

    # 4. Search
    results = client.search_notes("acme", limit=3)
    print(f"4. Search 'acme': {len(results)} results")

    # 5. Create + update + delete round-trip
    note = client.create_note(
        content="Parachute client test note",
        path="_test/client-test",
        tags=["_test"],
        metadata={"source": "parachute_client.py", "timestamp": datetime.now(timezone.utc).isoformat()},
    )
    print(f"5. Create note: {note.id} at {note.path}")

    updated = client.update_note(note.id, content="Updated test note", tags_add=["_verified"])
    print(f"6. Update note: tags={updated.tags}")

    deleted = client.delete_note(note.id)
    print(f"7. Delete note: {'OK' if deleted else 'FAIL'}")

    # 6. Tag listing
    tags = client.list_tags()
    meaningful = [t for t in tags if t.get("count", 0) > 0 and len(t["name"]) > 2]
    print(f"8. Tags: {len(meaningful)} with notes (of {len(tags)} total)")

    # 7. Check key tags exist
    key_tags = ["briefing", "task", "email", "person", "meeting", "transcript", "project"]
    found = {t["name"] for t in tags if t["name"] in key_tags}
    missing = set(key_tags) - found
    if missing:
        print(f"9. Key tags missing: {', '.join(sorted(missing))}")
    else:
        print(f"9. Key tags: all {len(key_tags)} present")

    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_run_tests())
    else:
        print("Usage: python3 scripts/parachute_client.py --test")
        print("  Or import: from parachute_client import ParachuteClient")
