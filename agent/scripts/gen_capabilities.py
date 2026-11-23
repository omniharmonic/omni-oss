#!/usr/bin/env python3
"""Render what Omni can do on a surface from config/omni-capabilities.yaml.

    python3 scripts/gen_capabilities.py render omni-app    # print the block
    python3 scripts/gen_capabilities.py --check            # exit 1 when the manifest is malformed

`render(surface)` is pure: it reads the YAML and returns plain text. Consumers
(omni-bridge's system prompt; later Telegram, Buzz personas, proactive prompts)
call it so every surface describes its abilities the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "config" / "omni-capabilities.yaml"
SURFACES = ("omni-app", "telegram", "buzz")
# blocked = a guard rule enforces it; ask = an instruction only (nothing in code stops it), rendered as such.
GATES = ("none", "approval", "ask", "blocked")


def load(path: Path = MANIFEST) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check(data: dict) -> list:
    """Every problem with the manifest, as sentences (empty = valid)."""
    errs = []
    caps = data.get("capabilities") if isinstance(data, dict) else None
    if not isinstance(caps, list) or not caps:
        return ["capabilities must be a non-empty list"]
    seen = set()
    for i, c in enumerate(caps):
        cid = c.get("id") if isinstance(c, dict) else None
        if not cid or cid in seen:
            errs.append("capability %d: missing or duplicate id" % i)
            continue
        seen.add(cid)
        for k in ("label", "how"):
            if not isinstance(c.get(k), str) or not c[k].strip():
                errs.append("%s: %s must be a non-empty string" % (cid, k))
        if c.get("gate") not in GATES:
            errs.append("%s: gate must be one of %s" % (cid, "/".join(GATES)))
        s = c.get("surfaces")
        if not isinstance(s, list) or not s or any(x not in SURFACES for x in s):
            errs.append("%s: surfaces must be a non-empty subset of %s" % (cid, ", ".join(SURFACES)))
    lim = data.get("limits")
    if not isinstance(lim, list) or not all(isinstance(x, str) and x.strip() for x in lim):
        errs.append("limits must be a list of sentences")
    return errs


def render(surface: str, data: dict = None) -> str:
    """The plain-text capability block for one surface."""
    if surface not in SURFACES:
        raise ValueError("unknown surface %r" % surface)
    data = data if data is not None else load()
    mine = [c for c in data["capabilities"] if surface in c["surfaces"]]
    out = ["What you can do here:"]
    for c in mine:
        if c["gate"] == "none":
            out.append("- %s (%s)" % (c["label"], c["how"]))
    gated = [c for c in mine if c["gate"] == "approval"]
    if gated:
        out.append("Needs the owner's approval (drafted or proposed, never done directly):")
        out += ["- %s (%s)" % (c["label"], c["how"]) for c in gated]
    ask = [c for c in mine if c["gate"] == "ask"]
    if ask:
        out.append("Ask the owner first (an instruction; no guard enforces it):")
        out += ["- %s" % c["label"] for c in ask]
    blocked = [c for c in mine if c["gate"] == "blocked"]
    if blocked:
        out.append("Never, on any surface:")
        out += ["- %s" % c["label"] for c in blocked]
    out.append("Limits:")
    out += ["- %s" % x for x in data.get("limits", [])]
    return "\n".join(out)


def main(argv: list) -> int:
    if argv[:1] == ["--check"]:
        errs = check(load())
        for e in errs:
            print(e, file=sys.stderr)
        return 1 if errs else 0
    if len(argv) == 2 and argv[0] == "render":
        print(render(argv[1]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
