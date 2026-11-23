#!/usr/bin/env python3
"""
apply_tag_schemas — the schema-bridge (handoff 2026-06-02 §4).

Parses the canonical tag schemas out of `docs/tag-schemas.md` and declares them
in Parachute via `update-tag` (PUT /vault/{vault}/api/tags/{name}). Enforces the
two Parachute 0.5.x schema rules before writing anything:

  1. Only `string` / `integer` / `boolean` fields may be `indexed: true`.
  2. Every tag sharing a field NAME must agree on that field's `type` + `indexed`
     flag (a field can't be an indexed string under one tag and an array under
     another — the index is keyed by name across tags).

Usage:
    python3 scripts/apply_tag_schemas.py             # validate + apply
    python3 scripts/apply_tag_schemas.py --dry-run   # validate + print, no writes
    python3 scripts/apply_tag_schemas.py --file docs/tag-schemas.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from parachute_client import ParachuteClient, ParachuteError

INDEXABLE_TYPES = {"string", "integer", "boolean"}
DEFAULT_DOC = _THIS_DIR.parent / "docs" / "tag-schemas.md"

# A tag section is a `## tagname` header followed (eventually) by a ```json block.
_HEADER_RE = re.compile(r"^##\s+([a-z0-9][a-z0-9\-_/]*)\s*$", re.MULTILINE)
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_schemas(doc_path: Path) -> dict[str, dict]:
    """Return {tag_name: schema_dict} parsed from the markdown doc."""
    text = doc_path.read_text()
    schemas: dict[str, dict] = {}
    headers = list(_HEADER_RE.finditer(text))
    for i, h in enumerate(headers):
        tag = h.group(1)
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = _JSON_BLOCK_RE.search(text, start, end)
        if not block:
            continue
        try:
            schemas[tag] = json.loads(block.group(1))
        except json.JSONDecodeError as e:
            raise SystemExit(f"Invalid JSON in schema for #{tag}: {e}")
    return schemas


def validate(schemas: dict[str, dict]) -> list[str]:
    """Return a list of rule violations (empty == valid)."""
    errors: list[str] = []
    # Rule 2 accumulator: field name -> (tag, type, indexed)
    seen: dict[str, tuple[str, str, bool]] = {}

    for tag, schema in sorted(schemas.items()):
        fields = schema.get("fields", {})
        if not isinstance(fields, dict):
            errors.append(f"#{tag}: 'fields' must be an object")
            continue
        for fname, fdef in fields.items():
            if not isinstance(fdef, dict):
                errors.append(f"#{tag}.{fname}: field def must be an object")
                continue
            ftype = fdef.get("type", "string")
            indexed = bool(fdef.get("indexed", False))

            # Rule 1: indexability
            if indexed and ftype not in INDEXABLE_TYPES:
                errors.append(
                    f"#{tag}.{fname}: indexed=true but type='{ftype}' "
                    f"(only {sorted(INDEXABLE_TYPES)} are indexable)"
                )

            # Rule 2: cross-tag agreement on type + indexed
            if fname in seen:
                otag, otype, oindexed = seen[fname]
                if otype != ftype or oindexed != indexed:
                    errors.append(
                        f"field '{fname}' disagrees across tags: "
                        f"#{otag}=({otype}, indexed={oindexed}) vs "
                        f"#{tag}=({ftype}, indexed={indexed})"
                    )
            else:
                seen[fname] = (tag, ftype, indexed)
    return errors


def _declared_fields(client: ParachuteClient) -> dict[str, dict]:
    """Map tag -> currently-declared fields dict, from the live vault."""
    out: dict[str, dict] = {}
    try:
        for t in client.list_tags(include_schema=True):
            sch = t.get("schema") or t.get("fields") or {}
            fields = sch.get("fields") if isinstance(sch, dict) and "fields" in sch else sch
            if isinstance(fields, dict):
                out[t["name"]] = fields
    except ParachuteError:
        pass
    return out


def apply(schemas: dict[str, dict], *, dry_run: bool, replace: bool) -> int:
    """Apply schemas. By default MERGES doc fields over the vault's existing
    declarations (never drops a field Prism declared); --replace overwrites."""
    client = ParachuteClient()
    existing = {} if replace else _declared_fields(client)
    applied = 0
    for tag, schema in sorted(schemas.items()):
        desc = schema.get("description", "")
        doc_fields = schema.get("fields", {})
        merged = dict(existing.get(tag, {}))  # vault first
        merged.update(doc_fields)             # doc wins per-field
        added = [f for f in doc_fields if f not in existing.get(tag, {})]
        idx = [f for f, d in merged.items() if d.get("indexed")]
        idx_note = f"  [indexed: {', '.join(idx)}]" if idx else ""
        add_note = f"  (+{len(added)} new: {', '.join(added)})" if added else "  (no new fields)"
        if dry_run:
            print(f"  would apply #{tag}: {len(merged)} fields{idx_note}{add_note}")
            continue
        try:
            client.update_tag(tag, description=desc, fields=merged)
            print(f"  ✓ #{tag}: {len(merged)} fields{idx_note}{add_note}")
            applied += 1
        except ParachuteError as e:
            print(f"  ✗ #{tag}: {e}")
    return applied


def contract_conflicts(schemas: dict[str, dict]) -> list[str]:
    """Where the doc would re-declare a field AGAINST the field-shape contract
    (config/vault-shapes.json, the approved schema since 2026-10-08).

    `docs/tag-schemas.md` predates the schema clean-up: it still types
    `project.keywords` and `message-thread.participants` as strings and carries
    the old `task.status` list. Because this script lets the doc win per field,
    running it would quietly undo the approved corrections on the live vault.
    So a doc that disagrees with the contract is refused, not applied.
    """
    import vault_shapes
    c = vault_shapes.CONTRACT
    out: list[str] = []

    def field(tag: str, name: str) -> dict | None:
        f = (schemas.get(tag, {}).get("fields") or {}).get(name)
        return f if isinstance(f, dict) else None

    for tag, names in c["listFields"].items():
        for name in names:
            f = field(tag, name)
            if f and f.get("type") != "array":
                out.append(f"#{tag}.{name}: doc says {f.get('type')}, the contract says array (a list)")
    for tag in c["confidence"]["tags"]:
        f = field(tag, "confidence")
        if f and (f.get("type") != "string" or (f.get("enum") and set(f["enum"]) != set(c["confidence"]["labels"]))):
            out.append(f"#{tag}.confidence: the contract says one of {', '.join(c['confidence']['labels'])}")
    f = field("task", "status")
    if f and f.get("enum") and set(f["enum"]) != set(c["taskStatus"]["values"]):
        out.append("#task.status: the doc's allowed values are not the contract's "
                   f"({', '.join(c['taskStatus']['values'])})")
    f = field("message-thread", "platform")
    if f and f.get("enum") and set(f["enum"]) != set(c["threadPlatforms"]):
        out.append("#message-thread.platform: the doc's allowed values are not the contract's")
    for tag, names in c["integerFields"].items():
        for name in names:
            f = field(tag, name)
            if f and f.get("type") != "integer":
                out.append(f"#{tag}.{name}: doc says {f.get('type')}, the contract says integer")
    for tag, names in c["textFields"].items():
        for name in names:
            for t in (schemas if tag == "*" else [tag]):
                f = field(t, name)
                if f and f.get("type") != "string":
                    out.append(f"#{t}.{name}: doc says {f.get('type')}, the contract says text")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply tag schemas to Parachute.")
    ap.add_argument("--file", default=str(DEFAULT_DOC), help="schema markdown doc")
    ap.add_argument("--dry-run", action="store_true", help="validate + print only")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite each tag's fields instead of merging over the "
                         "vault's existing declarations (destructive)")
    args = ap.parse_args()

    doc = Path(args.file)
    if not doc.exists():
        print(f"Schema doc not found: {doc}")
        return 1

    schemas = parse_schemas(doc)
    print(f"Parsed {len(schemas)} tag schemas from {doc.name}: {', '.join(sorted(schemas))}")

    errors = validate(schemas)
    if errors:
        print("\nVALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 2
    print("Validation passed (indexability + cross-tag field agreement).\n")

    conflicts = contract_conflicts(schemas)
    if conflicts:
        print("REFUSED — this doc disagrees with the approved field-shape contract "
              "(config/vault-shapes.json). Applying it would undo the 2026-10-08 schema "
              "corrections on the vault. Nothing was read from or written to the vault.")
        for line in conflicts:
            print(f"  - {line}")
        print("Schema changes now go through Prism (docs/vault-schema-change.md there): "
              "change the contract, then bring this doc in line.")
        return 3

    mode = "replace" if args.replace else "merge"
    print("Dry run — no writes." if args.dry_run else f"Applying schemas ({mode})...")
    apply(schemas, dry_run=args.dry_run, replace=args.replace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
