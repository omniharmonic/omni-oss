#!/usr/bin/env python3
"""Render Hermes' SOUL.md from config/identity.yaml — dry run by default.

    python3 hermes/scripts/render_soul.py                 # diff vs <home>/SOUL.md
    python3 hermes/scripts/render_soul.py --print         # the rendered file
    python3 hermes/scripts/render_soul.py --apply         # write it (old file backed up first)
    python3 hermes/scripts/render_soul.py --identity path/to/identity.yaml --hermes-home ~/.hermes-dev

config/identity.yaml (copy config/identity.example.yaml; it is git-ignored) is the one place that
says who the agent is and whom it works for. Nothing personal lives in code.

Fail closed on secrets: the rendered text is scanned for key and token shapes and a hit refuses
to render. Chat ids and wallet addresses belong in the environment (.env), not in the persona.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_IDENTITY = REPO / "config" / "identity.yaml"
EXAMPLE_IDENTITY = REPO / "config" / "identity.example.yaml"

SECRET_SHAPES = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),        # a Telegram bot token
    re.compile(r"\b0x[0-9a-fA-F]{64}\b"),                   # a private key
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # a JWT
]

DEFAULT_PRIVACY = [
    "Calendar event names, titles, descriptions or participants",
    "Email subject lines or content",
    "File names, project names or internal details",
    "Personal preferences, habits or schedule patterns",
]

DEFAULT_PRINCIPLES = [
    "**Be genuinely helpful, not performatively helpful.** Skip filler words. Just help.",
    "**Have opinions.** Disagree when warranted.",
    "**Be resourceful before asking.** Read the file. Check context. Search. THEN ask.",
    "**Earn trust through competence.** Be careful externally, bold internally.",
    "**Concise notifications, thorough briefings.** Real-time pings are one or two sentences.",
    "**Always provide options.** Never just report problems.",
    "**Never silent failure.** If something breaks, say so.",
    "**Nothing leaves on the owner's behalf without their approval of that exact draft.**",
]


def load_identity(path: Path | None = None) -> dict:
    import yaml

    p = Path(path) if path else DEFAULT_IDENTITY
    if not p.exists():
        if path is None and EXAMPLE_IDENTITY.exists():
            print(f"note: {p} not found; using {EXAMPLE_IDENTITY.name}", file=sys.stderr)
            p = EXAMPLE_IDENTITY
        else:
            raise SystemExit(f"render_soul: {p} not found (copy config/identity.example.yaml)")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"render_soul: {p} is not a mapping")
    return data


def _bullets(items) -> str:
    return "\n".join(f"- {i}" for i in items if str(i).strip())


def render(ident: dict) -> str:
    agent = ident.get("agent") or {}
    owner = ident.get("owner") or {}
    name = agent.get("name") or "Omni"
    owner_name = owner.get("name") or "the owner"
    call = owner.get("call_me") or owner_name

    out = [f"# {name}", ""]
    out.append(agent.get("summary") or f"You are {name}, {owner_name}'s personal AI executive assistant.")
    out += ["", "## Identity", ""]
    out.append(_bullets([
        f"**Name:** {name}",
        f"**Emoji:** {agent['emoji']}" if agent.get("emoji") else "",
        f"**Vibe:** {agent['vibe']}" if agent.get("vibe") else "",
    ]))

    out += ["", f"## {owner_name}'s context", ""]
    facts = [
        f"**Name:** {owner_name}" + (f" (always \"{call}\")" if call != owner_name else ""),
        f"**Pronouns:** {owner['pronouns']}" if owner.get("pronouns") else "",
        f"**Time zone:** {owner['timezone']}" if owner.get("timezone") else "",
        f"**Location:** {owner['location']}" if owner.get("location") else "",
    ]
    out.append(_bullets(facts))
    accounts = owner.get("accounts") or []
    if accounts:
        out += ["", "### Accounts", ""]
        out.append(_bullets(f"**{a.get('role', 'account')}:** {a.get('address', '')}" for a in accounts))
        out += ["", "These are the only accounts. Never send as, or list, any other address."]
    orgs = ident.get("organizations") or []
    if orgs:
        out += ["", "### Organizations", ""]
        out.append(_bullets(f"**{o.get('name')}**" + (f" ({o['role']})" if o.get("role") else "") for o in orgs))
    people = ident.get("collaborators") or []
    if people:
        out += ["", "### Key collaborators", ""]
        out.append(_bullets(f"{p.get('name')}" + (f" ({p['context']})" if p.get("context") else "") for p in people))

    privacy = ident.get("privacy") or {}
    out += ["", "## Privacy boundaries", "",
            f"**Core directive: protect {call}'s privacy above all else.**", "", "Never share externally:"]
    out.append(_bullets(privacy.get("never_share") or DEFAULT_PRIVACY))
    out += ["", "When coordinating with external contacts, share only available time slots, in generic "
            f"language (\"{call} has availability at…\"). If in doubt, ask {call} first."]

    out += ["", "## Operating principles", ""]
    out.append(_bullets(ident.get("principles") or DEFAULT_PRINCIPLES))
    extra = (ident.get("extra") or "").strip()
    if extra:
        out += ["", extra]
    text = "\n".join(out).rstrip() + "\n"
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    text = f"<!-- generated by hermes/scripts/render_soul.py from config/identity.yaml ({digest}); edit that file, not this one -->\n" + text
    for rx in SECRET_SHAPES:
        if rx.search(text):
            raise SystemExit("render_soul: refusing to render — the persona contains something shaped like a secret")
    return text


def install(home: Path, apply: bool, identity: Path | None = None) -> None:
    text = render(load_identity(identity))
    target = Path(home) / "SOUL.md"
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    if old == text:
        print(f"  ok   {target} is current")
        return
    for line in difflib.unified_diff(old.splitlines(), text.splitlines(), str(target), "rendered", lineterm=""):
        print("  " + line)
    if apply:
        if target.exists():
            backup = target.with_name(f"SOUL.md.bak-{datetime.now():%Y%m%d-%H%M%S}")
            shutil.copy2(target, backup)
            print(f"  backup {backup}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"  wrote {target}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", type=Path)
    ap.add_argument("--hermes-home", type=Path)
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.show:
        sys.stdout.write(render(load_identity(a.identity)))
        return 0
    home = a.hermes_home or Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    install(home, a.apply, a.identity)
    return 0


if __name__ == "__main__":
    sys.exit(main())
