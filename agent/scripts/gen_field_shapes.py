#!/usr/bin/env python3
"""One "Field shapes" block, generated — never hand-copied.

The vault's metadata shape rules live in ONE file, the field-shape contract:
Prism `packages/core/src/lib/schemas/vault-shapes.json`, vendored here
byte-for-byte as `config/vault-shapes.json`. From it:

  scripts/vault_shapes.py        loads its rule data (the write guard)
  config/vault-field-shapes.md   the rendered prompt block (same bytes as
                                 Prism's docs/vault-field-shapes.md)
  every file in PROMPT_FILES     carries that block between its markers
                                 (all routines, plus EXTRA_PROMPT_FILES)

    python3 scripts/gen_field_shapes.py               # --check: exit 1 when anything is stale
    python3 scripts/gen_field_shapes.py --write       # re-render the block and re-inject it
    python3 scripts/gen_field_shapes.py --prism DIR   # also compare the vendored files with a Prism checkout
    python3 scripts/gen_field_shapes.py --pull DIR --write   # re-vendor the contract from Prism, then regenerate

Local files only: no vault, no network, no routine is synced. After a --write that
changes a routine, the cloud copy is stale until the owner re-syncs it
(`scripts/sync_routines.py --emit-update <name> --dump <list>`).

This renderer is the twin of Prism's `renderFieldShapesBlock`
(packages/core/src/lib/schemas/vault-shapes.ts). Both must produce the committed
block byte-for-byte; `--prism` (and tests/test_vault_shapes.py, when a Prism
checkout is present) compare the two.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO / "config" / "vault-shapes.json"
BLOCK_PATH = REPO / "config" / "vault-field-shapes.md"
ROUTINES = REPO / "routines"

# Where the two files live in a Prism checkout.
PRISM_CONTRACT = Path("packages/core/src/lib/schemas/vault-shapes.json")
PRISM_BLOCK = Path("docs/vault-field-shapes.md")

BEGIN = "<!-- field-shapes:begin"
END = "<!-- field-shapes:end -->"
BLOCK_RE = re.compile(r"<!-- field-shapes:begin[^\n]*-->.*?<!-- field-shapes:end -->", re.S)

# Prompt files besides the routines that tell an agent how to write notes.
#: (The original install also kept the block in its persona file and a vault-writing guide; add your own
#: agent-facing prompt files here so they are checked too.)
EXTRA_PROMPT_FILES: tuple = ()


def prompt_files() -> list[Path]:
    """Every file that must carry the current block: all routines + the extras."""
    return sorted(ROUTINES.glob("*/SKILL.md")) + [REPO / p for p in EXTRA_PROMPT_FILES]


def _code(s: str) -> str:
    return f"`{s}`"


def _code_list(items: list[str], last: str) -> str:
    c = [_code(i) for i in items]
    if len(c) <= 1:
        return "".join(c)
    return f"{', '.join(c[:-1])} {last} {c[-1]}"


def placeholders(c: dict) -> dict[str, str]:
    mirror = set(c["taskStatus"]["mirrorOnly"])
    return {
        "listFieldsByTag": "; ".join(
            f"{tag}: {', '.join(_code(f) for f in fields)}" for tag, fields in c["listFields"].items()),
        "projectFolder": c["projectLink"]["folder"],
        "projectNote": c["projectLink"]["noteName"],
        "confidenceTags": " / ".join(c["confidence"]["tags"]),
        "confidenceLabels": _code_list(c["confidence"]["labels"], "or"),
        "agentTaskStatuses": ", ".join(_code(s) for s in c["taskStatus"]["values"] if s not in mirror),
        "mirrorTaskStatuses": "/".join(_code(s) for s in c["taskStatus"]["mirrorOnly"]),
        "sourceTags": " / ".join(c["source"]["tags"]),
        "sources": _code_list(c["source"]["known"], "or"),
    }


def render_body(c: dict) -> str:
    values = placeholders(c)

    def fill(s: str) -> str:
        return re.sub(r"\{([A-Za-z]+)\}", lambda m: values.get(m.group(1), m.group(0)), s)

    lines = [c["prompt"]["heading"], "", fill(c["prompt"]["intro"]), ""]
    lines += [f"- {fill(r)}" for r in c["prompt"]["rules"]]
    return "\n".join(lines)


def body_sha256(path: Path = CONTRACT_PATH) -> str:
    """SHA-256 of the block's TEXT (not of the contract file): the block, and with it
    every routine prompt, changes only when a rule an agent reads changes — never
    because an unrelated contract key (a lint tag) moved."""
    c = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(render_body(c).encode("utf-8")).hexdigest()


def render_block(path: Path = CONTRACT_PATH) -> str:
    """The block exactly as it is pasted into a prompt (markers included, no trailing newline)."""
    c = json.loads(path.read_text(encoding="utf-8"))
    begin = (f"{BEGIN} contract=v{c['version']} sha256={body_sha256(path)[:12]} "
             "· GENERATED from vault-shapes.json — do not edit by hand -->")
    return f"{begin}\n{render_body(c)}\n{END}"


def with_block(text: str, block: str) -> Optional[str]:
    """`text` with its field-shapes block replaced; None when it has no markers."""
    if not BLOCK_RE.search(text):
        return None
    return BLOCK_RE.sub(lambda _m: block, text, count=1)


def stale(block: Optional[str] = None) -> list[str]:
    """What is out of date, as human-readable lines (empty = everything current)."""
    block = block or render_block()
    out: list[str] = []
    have = BLOCK_PATH.read_text(encoding="utf-8") if BLOCK_PATH.exists() else ""
    if have != block + "\n":
        out.append(f"{BLOCK_PATH.relative_to(REPO)}: not the block the contract renders")
    for p in prompt_files():
        rel = p.relative_to(REPO)
        if not p.exists():
            out.append(f"{rel}: missing")
            continue
        text = p.read_text(encoding="utf-8")
        if BEGIN not in text:
            out.append(f"{rel}: has no field-shapes block")
        elif block not in text:
            out.append(f"{rel}: field-shapes block is stale")
        elif text.count(BEGIN) != 1:
            out.append(f"{rel}: more than one field-shapes block")
    return out


def compare_with_prism(prism: Path) -> list[str]:
    out: list[str] = []
    for mine, theirs, what in ((CONTRACT_PATH, prism / PRISM_CONTRACT, "contract"),
                               (BLOCK_PATH, prism / PRISM_BLOCK, "rendered block")):
        if not theirs.exists():
            out.append(f"{what}: {theirs} not found in the Prism checkout")
        elif mine.read_bytes() != theirs.read_bytes():
            out.append(f"{what}: {mine.relative_to(REPO)} differs from Prism's {theirs.relative_to(prism)}")
    return out


def write() -> list[str]:
    block = render_block()
    changed: list[str] = []
    if not BLOCK_PATH.exists() or BLOCK_PATH.read_text(encoding="utf-8") != block + "\n":
        BLOCK_PATH.write_text(block + "\n", encoding="utf-8")
        changed.append(str(BLOCK_PATH.relative_to(REPO)))
    for p in prompt_files():
        text = p.read_text(encoding="utf-8")
        new = with_block(text, block)
        if new is None:
            sys.exit(f"{p.relative_to(REPO)}: no field-shapes markers — add\n  {BEGIN} -->\n  {END}\nwhere the block belongs, then re-run --write")
        if new != text:
            p.write_text(new, encoding="utf-8")
            changed.append(str(p.relative_to(REPO)))
    return changed


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="the default: exit 1 when anything is stale")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--print", action="store_true", dest="print_block")
    ap.add_argument("--prism", help="a Prism checkout to compare the vendored files with")
    ap.add_argument("--pull", help="a Prism checkout to copy the contract from (use with --write)")
    a = ap.parse_args(argv)

    if a.pull:
        src = Path(a.pull).expanduser() / PRISM_CONTRACT
        if not src.exists():
            sys.exit(f"no contract at {src}")
        shutil.copyfile(src, CONTRACT_PATH)
        print(f"vendored {src} -> {CONTRACT_PATH.relative_to(REPO)}")
        if not a.write:
            print("now run with --write to regenerate the block and the prompts")
    if a.print_block:
        print(render_block())
        return 0
    if a.write:
        for c in write():
            print(f"updated {c}")
    problems = stale()
    if a.prism:
        problems += compare_with_prism(Path(a.prism).expanduser())
    for line in problems:
        print(f"STALE  {line}")
    if problems:
        print("fix: python3 scripts/gen_field_shapes.py --write   (routines then need a re-sync to claude.ai)")
        return 1
    print(f"field shapes: {len(prompt_files())} prompt files carry the current block (block sha256 {body_sha256()[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
