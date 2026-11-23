#!/usr/bin/env python3
"""routines/ is the source of truth for Omni's scheduled routines.

Each `routines/<name>/SKILL.md` is a portable skill (agentskills/Hermes
frontmatter + the prompt) with its schedule, runtime and routine id under
`metadata.omni`. The same prompt runs in three places over time:

  desktop  Claude Desktop runs device-bound routines from
           ~/Documents/Claude/Scheduled/<name>/SKILL.md (verified 2026-09-30:
           the live routine prompt is byte-identical to that file, and the
           remote API refuses to edit device-bound routines).
  cloud    claude.ai routines, edited through the RemoteTrigger API
           (`--emit-update` prints the body; a Claude session sends it).
  hermes   later — the same SKILL.md loads as a Hermes skill.

    python3 scripts/sync_routines.py --check [--dump list.json]
    python3 scripts/sync_routines.py --to-desktop [name ...]
    python3 scripts/sync_routines.py --emit-update <name> --dump list.json
    python3 scripts/sync_routines.py --emit-create <name> --dump list.json [--like <other>] [--enabled]
    python3 scripts/sync_routines.py --record-create <name> --routine-id trig_...

`--emit-create` is the way out for a DEVICE-BOUND routine (the API refuses to
edit its prompt): it prints the body of a NEW cloud routine with the repo
prompt and the same schedule, environment, model, tool policy and connectors
as the live one (or as `--like <other routine>` when the live entry lacks
them). The new routine is created DISABLED unless `--enabled` is passed, so
the old and the new one never both run. A Claude session sends the body with
RemoteTrigger action=create; `--record-create` then writes the new id into the
SKILL.md front matter (`runtime: cloud`, `routine_id`, and the old id as
`replaces`). The old routine is disabled, never deleted.

`--check` also fails when a routine's generated "Field shapes" block is missing
or stale (scripts/gen_field_shapes.py — the block is rendered from the
field-shape contract, config/vault-shapes.json, never written by hand). So a
schema change that has not reached every routine prompt cannot pass as "ok".

`--dump` is the saved output of RemoteTrigger `list` (HTTP header line OK).
Desktop copies are written for EVERY routine, not just device-bound ones, so
the folder never again holds stale prompts that look authoritative.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUTINES = REPO / "routines"
DESKTOP = Path.home() / "Documents/Claude/Scheduled"


_IDENTITY = None


def fill(text: str) -> str:
    """Fill a routine template's {{owner.name}} / {{owner.call_me}} / {{owner.email}} / {{owner.timezone}} /
    {{agent.name}} from config/identity.yaml (hermes/scripts/render_soul.load_identity). A text without
    placeholders is returned unchanged; an unknown placeholder is an error, never left in a prompt."""
    global _IDENTITY
    if "{{" not in text:
        return text
    if _IDENTITY is None:
        sys.path.insert(0, str(REPO / "hermes/scripts"))
        import render_soul
        _IDENTITY = render_soul.load_identity()
    owner = _IDENTITY.get("owner") or {}
    agent = _IDENTITY.get("agent") or {}
    accounts = owner.get("accounts") or []
    values = {
        "owner.name": owner.get("name") or "",
        "owner.call_me": owner.get("call_me") or (owner.get("name") or "").split(" ")[0],
        "owner.email": (accounts[0] or {}).get("address", "") if accounts else "",
        "owner.timezone": owner.get("timezone") or "UTC",
        "agent.name": agent.get("name") or "Omni",
    }

    def one(m):
        key = m.group(1).strip()
        if not values.get(key):
            sys.exit(f"routine template: {{{{{key}}}}} has no value in config/identity.yaml")
        return values[key]

    return re.sub(r"\{\{([a-z_.]+)\}\}", one, text)


def parse(path: Path) -> dict:
    text = fill(path.read_text())
    _, fm, body = text.split("---", 2)
    meta = {}
    for key in ("name", "schedule", "timezone", "runtime", "routine_id", "description"):
        m = re.search(rf"^\s*{key}:\s*\"?(.*?)\"?\s*$", fm, re.M)
        meta[key] = m.group(1) if m else ""
    meta["body"] = body.lstrip("\n")
    return meta


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def load_dump(path: str) -> dict:
    t = Path(path).read_text()
    data = json.loads(t[t.index("{"):])
    return {r["id"]: r for r in data.get("data", [])}


def desktop_text(r: dict) -> str:
    desc = r["description"].split(" — ")[0]
    return f"---\nname: {r['name']}\ndescription: {desc}\n---\n\n{r['body']}"


def all_routines():
    return [parse(p) for p in sorted(ROUTINES.glob("*/SKILL.md"))]


def shapes_drift() -> list[str]:
    """Stale / missing field-shapes blocks, as printable lines (empty = current)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gen_field_shapes  # local module; reads files only
    return gen_field_shapes.stale()


def check(dump):
    drift = 0
    for r in all_routines():
        d = DESKTOP / r["name"] / "SKILL.md"
        dbody = d.read_text().split("---", 2)[2] if d.exists() else ""
        state = ["desktop:" + ("ok" if norm(dbody) == norm(r["body"]) else "DRIFT")]
        if dump is not None:
            live = dump.get(r["routine_id"])
            lp = (live or {}).get("derived_state", {}).get("prompt", "")
            state.append("live:" + ("ok" if live and norm(lp) == norm(r["body"]) else "DRIFT" if live else "MISSING"))
            if live and live["cron_expression"].split(" ", 1)[1] != r["schedule"]:
                state.append(f"cron:DRIFT({live['cron_expression']})")
            if live and live.get("enabled") is False:
                state.append("enabled:NO")  # shown, not counted: a routine may be off on purpose
        drift += any("DRIFT" in s or "MISSING" in s for s in state)
        print(f"{r['name']:32s} {r['runtime']:8s} " + "  ".join(state))
    stale = shapes_drift()
    for line in stale:
        print(f"field-shapes DRIFT  {line}")
    if stale:
        print("fix: python3 scripts/gen_field_shapes.py --write, then re-sync the changed routines")
    else:
        print("field-shapes: every prompt carries the current generated block")
    return drift + len(stale)


def to_desktop(names):
    stamp = datetime.now().strftime("%Y-%m-%d")
    for r in all_routines():
        if names and r["name"] not in names:
            continue
        d = DESKTOP / r["name"] / "SKILL.md"
        d.parent.mkdir(parents=True, exist_ok=True)
        new = desktop_text(r)
        if d.exists() and d.read_text() == new:
            continue
        if d.exists():
            shutil.copy2(d, d.with_name(f"SKILL.md.bak-{stamp}"))
        d.write_text(new)
        print(f"wrote {d}")


def emit_update(name, dump):
    r = next(x for x in all_routines() if x["name"] == name)
    if r["runtime"] != "cloud":
        sys.exit(f"{name} is device-bound — use --to-desktop; the API refuses it.")
    live = dump[r["routine_id"]]
    events = json.loads(json.dumps(live["job_config"]["ccr"]["events"]))
    msgs = [e for e in events if isinstance(e.get("data", {}).get("message", {}).get("content"), str)]
    max(msgs, key=lambda e: len(e["data"]["message"]["content"]))["data"]["message"]["content"] = r["body"]
    body = {"job_config": {"ccr": {"environment_id": live["job_config"]["ccr"]["environment_id"],
                                   "events": events}}}
    cron = f"CRON_TZ={r['timezone']} {r['schedule']}"
    if live["cron_expression"] != cron:
        body["cron_expression"] = cron
    print(json.dumps(body, ensure_ascii=False))


def _prompt_events(events: list) -> list:
    return [e for e in events if isinstance(e.get("data", {}).get("message", {}).get("content"), str)]


def create_body(name, dump, like=None, enabled=False, new_name=None) -> dict:
    """The RemoteTrigger `create` body for a NEW cloud routine built from the repo file.

    Everything that is not the prompt or the schedule is copied from a live
    routine in the dump: the routine itself (default) or `like`. Only known
    create fields are copied — never ids, timestamps or derived state.
    """
    routines = {x["name"]: x for x in all_routines()}
    if name not in routines:
        sys.exit(f"no routines/{name}/SKILL.md")
    r = routines[name]
    own = dump.get(r["routine_id"])
    src = own
    if like:
        if like not in routines:
            sys.exit(f"--like {like}: no routines/{like}/SKILL.md")
        src = dump.get(routines[like]["routine_id"])
        if not src:
            sys.exit(f"--like {like}: routine {routines[like]['routine_id']} is not in the dump")
    if not src:
        sys.exit(f"{name}: routine {r['routine_id'] or '(no id)'} is not in the dump — pass --like <a cloud routine> to copy its environment")
    ccr = (src.get("job_config") or {}).get("ccr") or {}
    if not ccr.get("environment_id"):
        sys.exit(f"the live entry for {like or name} has no job_config.ccr.environment_id — pass --like <a cloud routine>")
    msgs = _prompt_events(ccr.get("events") or [])
    template = json.loads(json.dumps(max(msgs, key=lambda e: len(e["data"]["message"]["content"])))) if msgs else {
        "data": {"session_id": "", "type": "user", "parent_tool_use_id": None, "message": {"role": "user"}}}
    template["data"]["uuid"] = str(uuid.uuid4())  # a new routine gets its own event id
    template["data"]["message"]["content"] = r["body"]
    new_ccr = {"environment_id": ccr["environment_id"], "events": [template]}
    if ccr.get("session_context"):
        new_ccr["session_context"] = json.loads(json.dumps(ccr["session_context"]))
    body = {
        "name": new_name or (own or {}).get("name") or r["name"],
        "cron_expression": f"CRON_TZ={r['timezone']} {r['schedule']}",
        "enabled": bool(enabled),
        "job_config": {"ccr": new_ccr},
    }
    # Connectors: the routine's own when it has any, else the template's.
    conns = (own or {}).get("mcp_connections") or src.get("mcp_connections")
    if conns:
        body["mcp_connections"] = json.loads(json.dumps(conns))
    return body


def record_create(name, routine_id, path=None) -> str:
    """Write a created cloud routine's id into the SKILL.md front matter. Returns the old id."""
    if not re.fullmatch(r"trig_[A-Za-z0-9]+", routine_id or ""):
        sys.exit("--routine-id must look like trig_…")
    path = path or ROUTINES / name / "SKILL.md"
    text = path.read_text()
    head, fm, body = text.split("---", 2)
    old = re.search(r"^([ \t]*)routine_id:[ \t]*(\S+)[ \t]*$", fm, re.M)
    if not old:
        sys.exit(f"{path}: no routine_id in the front matter")
    indent, old_id = old.group(1), old.group(2)
    if old_id == routine_id:
        return old_id
    fm = re.sub(r"^([ \t]*)runtime:[ \t]*\S+[ \t]*$", r"\1runtime: cloud", fm, count=1, flags=re.M)
    fm = re.sub(r"^[ \t]*replaces:.*\n", "", fm, flags=re.M)
    fm = fm.replace(old.group(0), f"{indent}routine_id: {routine_id}\n{indent}replaces: {old_id}", 1)
    path.write_text(f"{head}---{fm}---{body}")
    return old_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--to-desktop", nargs="*")
    ap.add_argument("--emit-update")
    ap.add_argument("--emit-create", help="print a RemoteTrigger create body for a NEW cloud routine built from the repo file")
    ap.add_argument("--like", help="with --emit-create: copy environment/model/tools/connectors from this routine")
    ap.add_argument("--enabled", action="store_true", help="with --emit-create: create it enabled (default: disabled)")
    ap.add_argument("--name", help="with --emit-create: the new routine's display name (default: the live one)")
    ap.add_argument("--record-create", help="write a created routine's id into routines/<name>/SKILL.md")
    ap.add_argument("--routine-id")
    ap.add_argument("--dump")
    a = ap.parse_args()
    dump = load_dump(a.dump) if a.dump else None
    if a.check:
        sys.exit(1 if check(dump) else 0)
    if a.to_desktop is not None:
        to_desktop(a.to_desktop)
    if a.emit_update:
        if dump is None:
            sys.exit("--emit-update needs --dump")
        emit_update(a.emit_update, dump)
    if a.emit_create:
        if dump is None:
            sys.exit("--emit-create needs --dump")
        print(json.dumps(create_body(a.emit_create, dump, like=a.like, enabled=a.enabled, new_name=a.name), ensure_ascii=False))
    if a.record_create:
        old = record_create(a.record_create, a.routine_id)
        print(f"{a.record_create}: runtime cloud, routine_id {a.routine_id} (replaces {old}). "
              f"Disable {old}; then --check --dump <fresh list> must show live:ok.")


if __name__ == "__main__":
    main()
