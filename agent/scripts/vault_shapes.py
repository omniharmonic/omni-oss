"""Shape guard for metadata written to the Parachute vault.

Every note this repo writes passes through `normalize_metadata()` (called by
`ParachuteClient.create_note` / `update_note`), so the field shapes the vault
schemas declare are produced at the source instead of being cleaned up later.

Why it exists (Prism `qa/vault-health.md`, 2026-10-08): ~90% of person and
organization notes carried `""` in list fields, `confidence` held labels the
schema typed as numbers, thread timestamps arrived as strings, and every one of
those came from a writer. The one-off migrations in Prism
`scripts/vault-hygiene/` heal the past; this module keeps the drift from coming
back. The rules mirror `scripts/vault-hygiene/schema-fixes.json` (S1-S13) and
Prism's `apps/server/src/vault-shapes.ts` — keep the three in step.

Two kinds of rule:

* By field NAME, always (tags unknown, e.g. a metadata-only update):
  a list field holding `""` is dropped on a create and sent as null (= remove
  the stored key) on an update, empty and duplicate list elements are removed,
  `recording_id` becomes text.
* By TAG, when the note's tags are known (creates; updates that add tags):
  a non-empty string in a list field becomes a one-element list; `confidence`
  on person/project/organization/concept becomes a label high|medium|low;
  task `status` synonyms map to the canonical vocabulary; message-thread
  `lastMessageAt` becomes epoch-ms integer, `platform` lower case,
  `participants` a list; meeting/transcript `source` lower case (`""` dropped);
  spec `version` becomes text.

Never raises, never mutates its input. `PARACHUTE_SHAPE_GUARD=0` turns it off.

    python3 scripts/vault_shapes.py --test
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# The rules' DATA is not written in this file. It is loaded from the field-shape
# CONTRACT, `config/vault-shapes.json` — a byte-identical vendored copy of Prism's
# `packages/core/src/lib/schemas/vault-shapes.json` (the single source of truth:
# Prism's guard and lint load the same file, and the "Field shapes" block in every
# routine prompt is generated from it by `scripts/gen_field_shapes.py`).
# To change a shape, change the contract in Prism and re-vendor it:
#     python3 scripts/gen_field_shapes.py --pull <prism checkout> --write
# ---------------------------------------------------------------------------
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "config" / "vault-shapes.json"


def load_contract(path: Optional[Path] = None) -> dict:
    return json.loads((path or CONTRACT_PATH).read_text(encoding="utf-8"))


CONTRACT = load_contract()

# Tag -> metadata keys that are lists.
LIST_FIELDS_BY_TAG: dict[str, frozenset[str]] = {
    tag: frozenset(fields) for tag, fields in CONTRACT["listFields"].items()
}
# Names that are lists wherever they appear: `""` there is never a value.
ALWAYS_LIST: frozenset[str] = frozenset().union(*LIST_FIELDS_BY_TAG.values())

CONFIDENCE_TAGS = frozenset(CONTRACT["confidence"]["tags"])
CONFIDENCE_LABELS = tuple(CONTRACT["confidence"]["labels"])

# task.status (schema-fixes S4): canonical tasks_store vocabulary + todo/done,
# which the ClickUp mirror and Prism's task UI still write.
TASK_STATUSES = frozenset(CONTRACT["taskStatus"]["values"])
TASK_STATUS_SYNONYMS: dict[str, str] = dict(CONTRACT["taskStatus"]["synonyms"])

THREAD_PLATFORMS = frozenset(CONTRACT["threadPlatforms"])
SOURCE_TAGS = frozenset(CONTRACT["source"]["tags"])
# Fields that are text wherever they appear / on one tag; integer (epoch ms) fields.
TEXT_ANYWHERE = frozenset(CONTRACT["textFields"].get("*", []))
TEXT_FIELDS_BY_TAG = {t: frozenset(f) for t, f in CONTRACT["textFields"].items() if t != "*"}
INTEGER_FIELDS_BY_TAG = {t: frozenset(f) for t, f in CONTRACT["integerFields"].items()}
LOWERCASE_STATUS_TAGS = tuple(CONTRACT["lowercaseStatusTags"])


def enabled() -> bool:
    return os.environ.get("PARACHUTE_SHAPE_GUARD", "1").strip().lower() not in ("0", "false", "off", "no")


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _clean_list(values: Iterable[Any]) -> list:
    out: list = []
    seen: set = set()
    for v in values:
        if _is_blank(v):
            continue
        if isinstance(v, str):
            v = v.strip()
        key = repr(v) if not isinstance(v, (str, int, float)) else v
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _split_list_string(s: str) -> list[str]:
    """A string in a list field. Split on commas only when every piece is a
    wikilink or an address ("Smith, John" stays one name)."""
    s = s.strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        if all(p and ((p.startswith("[[") and p.endswith("]]")) or "@" in p) for p in parts):
            return parts
    return [s]


def confidence_label(value: Any) -> Optional[str]:
    """Number / numeric string / label -> high|medium|low; anything else None."""
    if isinstance(value, bool):
        return None
    num: Optional[float] = None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        s = value.strip().lower()
        if s in CONFIDENCE_LABELS:
            return s
        try:
            num = float(s)
        except ValueError:
            return None
    if num is None or math.isnan(num) or math.isinf(num):
        return None
    if num > 1:  # a percentage
        num = num / 100.0
    if num >= 0.8:
        return "high"
    if num >= 0.5:
        return "medium"
    return "low"


def canonical_task_status(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    s = value.strip().lower()
    if s in TASK_STATUSES:
        return s
    return TASK_STATUS_SYNONYMS.get(s, value)


def epoch_ms(value: Any) -> Any:
    """Integer epoch ms from an int, a numeric string or an ISO-8601 string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value > 10_000_000_000 else value * 1000
    if isinstance(value, float) and math.isfinite(value):
        return epoch_ms(int(value))
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return epoch_ms(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return value


def _version_text(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return value


def normalize_metadata(metadata: Optional[dict], tags: Optional[Iterable[str]] = None,
                       mode: str = "create") -> Optional[dict]:
    """Return a copy of `metadata` with the known field shapes enforced.

    `tags` = the note's tags when known (creates), else None (name rules only).
    `mode` = "create" drops a list field holding `""`; "update" sends None (JSON
    null = remove the stored key): empty IS absent, never `""`. A real empty
    list `[]` is left as it is (that is how a caller clears a list).
    """
    if not metadata or not isinstance(metadata, dict) or not enabled():
        return metadata
    tagset = {str(t).strip().lower() for t in (tags or []) if t}
    # Governance notes carry an HMAC over their metadata (Prism): never rewrite.
    if "gov_sig" in metadata or any(t.startswith("governance-") for t in tagset):
        return metadata
    list_fields = set(ALWAYS_LIST if not tagset else ())
    for t in tagset:
        list_fields |= LIST_FIELDS_BY_TAG.get(t, frozenset())
    tagged_lists: set[str] = set()
    for t in tagset:
        tagged_lists |= LIST_FIELDS_BY_TAG.get(t, frozenset())

    out: dict = {}
    for key, value in metadata.items():
        if key in ALWAYS_LIST or key in list_fields:
            if isinstance(value, str) and not value.strip():
                if mode == "update":
                    out[key] = None  # remove the stored "" / clear the list
                continue  # "" is never a list value: absent instead
            if isinstance(value, (list, tuple)):
                value = _clean_list(value)
            elif isinstance(value, str) and key in tagged_lists:
                value = _clean_list(_split_list_string(value))
        if key in TEXT_ANYWHERE and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = _version_text(value)
        out[key] = value

    if not tagset:
        return out

    if tagset & CONFIDENCE_TAGS and "confidence" in out and out["confidence"] is not None:
        label = confidence_label(out["confidence"])
        if label is None:
            if mode == "update":
                out["confidence"] = None
            else:
                del out["confidence"]
        else:
            out["confidence"] = label

    if "task" in tagset and "status" in out:
        out["status"] = canonical_task_status(out["status"])

    for t in tagset:
        for f in INTEGER_FIELDS_BY_TAG.get(t, ()):
            if out.get(f) is not None:
                out[f] = epoch_ms(out[f])

    if "message-thread" in tagset:
        if isinstance(out.get("platform"), str):
            p = out["platform"].strip().lower()
            if p:
                out["platform"] = p
            else:
                del out["platform"]

    if tagset & SOURCE_TAGS and isinstance(out.get("source"), str):
        s = out["source"].strip().lower()
        if s:
            out["source"] = s
        else:
            del out["source"]

    for t in tagset:
        for f in TEXT_FIELDS_BY_TAG.get(t, ()):
            if f in out:
                out[f] = _version_text(out[f])

    for t in LOWERCASE_STATUS_TAGS:
        if t in tagset and isinstance(out.get("status"), str) and "task" not in tagset:
            out["status"] = out["status"].strip().lower()

    return out


def strip_empty(metadata: Optional[dict]) -> dict:
    """For a CREATE: drop keys whose value carries nothing (None, "", [], {})."""
    return {k: v for k, v in (metadata or {}).items()
            if not (v is None or v == "" or v == [] or v == {})}


# ---------------------------------------------------------------------------

def _selftest() -> None:
    import unittest

    class T(unittest.TestCase):
        def test_empty_list_placeholders_dropped(self):
            md = normalize_metadata({"organizations": "", "aliases": "", "role": "x"}, ["person"])
            self.assertEqual(md, {"role": "x"})

        def test_update_mode_clears_with_null(self):
            md = normalize_metadata({"projects": "", "title": "T"}, None, mode="update")
            self.assertEqual(md, {"projects": None, "title": "T"})

        def test_governance_untouched(self):
            src = {"people": "", "gov_sig": "v1.x"}
            self.assertEqual(normalize_metadata(src, ["governance-role"]), src)

        def test_name_rule_without_tags(self):
            md = normalize_metadata({"projects": "", "aliases": ["a", "", "a"], "confidence": 0.9})
            self.assertEqual(md, {"aliases": ["a"], "confidence": 0.9})

        def test_string_to_list_only_when_tagged(self):
            self.assertEqual(normalize_metadata({"aliases": "Sam"}, ["person"])["aliases"], ["Sam"])
            self.assertEqual(normalize_metadata({"aliases": "Sam"})["aliases"], "Sam")
            self.assertEqual(
                normalize_metadata({"attendees": "[[a]], b@x.org"}, ["meeting"])["attendees"],
                ["[[a]]", "b@x.org"])
            self.assertEqual(
                normalize_metadata({"collaborators": "Smith, John"}, ["project"])["collaborators"],
                ["Smith, John"])

        def test_confidence(self):
            self.assertEqual(normalize_metadata({"confidence": 0.95}, ["person"])["confidence"], "high")
            self.assertEqual(normalize_metadata({"confidence": "0.6"}, ["organization"])["confidence"], "medium")
            self.assertEqual(normalize_metadata({"confidence": "High"}, ["project"])["confidence"], "high")
            self.assertNotIn("confidence", normalize_metadata({"confidence": ""}, ["person"]))
            self.assertNotIn("confidence", normalize_metadata({"confidence": "sure"}, ["person"]))
            # promises keep their numeric confidence
            self.assertEqual(normalize_metadata({"confidence": 0.8}, ["promise"])["confidence"], 0.8)

        def test_task_status(self):
            self.assertEqual(normalize_metadata({"status": "Tracked"}, ["task"])["status"], "pending")
            self.assertEqual(normalize_metadata({"status": "done"}, ["task"])["status"], "done")
            self.assertEqual(normalize_metadata({"status": "review"}, ["task"])["status"], "in-progress")
            self.assertEqual(normalize_metadata({"status": "processed"}, ["meeting"])["status"], "processed")

        def test_thread(self):
            md = normalize_metadata({"lastMessageAt": "2026-10-01T00:00:00Z", "platform": "Matrix",
                                     "participants": "Ann"}, ["message-thread"])
            self.assertEqual(md["lastMessageAt"], 1790812800000)
            self.assertEqual(md["platform"], "matrix")
            self.assertEqual(md["participants"], ["Ann"])
            self.assertEqual(normalize_metadata({"lastMessageAt": "1790812800000"}, ["message-thread"])["lastMessageAt"], 1790812800000)
            self.assertEqual(normalize_metadata({"lastMessageAt": 1790812800}, ["message-thread"])["lastMessageAt"], 1790812800000)

        def test_source_recording_version(self):
            md = normalize_metadata({"source": "", "recording_id": 12345}, ["meeting", "transcript"])
            self.assertEqual(md, {"recording_id": "12345"})
            self.assertEqual(normalize_metadata({"source": "Fireflies"}, ["transcript"])["source"], "fireflies")
            self.assertEqual(normalize_metadata({"version": 1.0}, ["spec"])["version"], "1")
            self.assertEqual(normalize_metadata({"version": 1.2}, ["spec"])["version"], "1.2")
            self.assertEqual(normalize_metadata({"recording_id": 7})["recording_id"], "7")

        def test_no_mutation_and_kill_switch(self):
            src = {"aliases": ""}
            normalize_metadata(src, ["person"])
            self.assertEqual(src, {"aliases": ""})
            os.environ["PARACHUTE_SHAPE_GUARD"] = "0"
            try:
                self.assertEqual(normalize_metadata(src, ["person"]), src)
            finally:
                del os.environ["PARACHUTE_SHAPE_GUARD"]

        def test_strip_empty(self):
            self.assertEqual(strip_empty({"a": "", "b": [], "c": None, "d": 0, "e": "x"}), {"d": 0, "e": "x"})

    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(T))
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        _selftest()
    else:
        print(__doc__)
