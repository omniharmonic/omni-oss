#!/usr/bin/env python3
"""omni_propose — put a draft in the owner's "Needs you" queue in the Omni app.

The ONE way any surface of this repo (the Telegram bot, missions, the dashboard,
swarm workers, Claude/Buzz agents, scripts) may act on an outward send: it
PROPOSES the exact payload to the Prism server's loopback hook
(`POST /api/omni/hooks/propose`, the same hook Hermes' omni_propose tool uses).
Nothing is sent. The owner sees the draft untruncated in the Omni app, may edit,
cancel or approve it, and only his approval of that exact draft — on a signed-in
device — makes Prism's executor send it (with a one-time grant the send script
verifies; see scripts/send_grant.py).

    from omni_propose import propose
    res = propose("email", {"to": ["dana@example.org"], "subject": "Friday",
                            "body": "Plain text."}, summary="Email Dana about Friday")
    # -> {"ok": True, "approval_id": "apr_…", "sent": False, ...}

CLI (body / text on stdin with `-`):
    python3 scripts/omni_propose.py email --to a@b.co [--cc c@d.co] --subject S --body-file -
    python3 scripts/omni_propose.py email-reply --note NOTE_ID --expect-to a@b.co --body-file -
    python3 scripts/omni_propose.py message --room '!room:server' --body-file -
    python3 scripts/omni_propose.py tweet --text-file -
    python3 scripts/omni_propose.py calendar-invite --title T --start ISO --end ISO [--attendee a@b.co ...] [--with-meet]
    python3 scripts/omni_propose.py calendar-rsvp --event-id ID --response accepted|declined|tentative
    python3 scripts/omni_propose.py action --title T --reaches WHO --steps-file - [--url U] [--grant tool.browser-act ...]

kind `action` is for an outward step no other kind covers (a booking page's Confirm, a web form,
a sign-up). The owner may approve it whenever he likes; Prism then starts a turn that does exactly
the approved steps, and only during that turn does the guard honour the named grants.
Prints one JSON object. Exit 0 = proposed (still NOT sent), 1 = not proposed.

Configuration (never printed): OMNI_SERVICE_TOKEN and OMNI_GATEWAY_URL from the
environment, else from Hermes' env file (~/.hermes/.env, or $HERMES_HOME/.env).
The gateway must be plain http on loopback; no proxy, no redirect.

Python 3.9 compatible.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

KINDS = ("email", "email-reply", "message", "calendar-rsvp", "calendar-invite", "tweet", "wallet-proposal", "action")
PROPOSE_PATH = "/api/omni/hooks/propose"
DEFAULT_GATEWAY = "http://127.0.0.1:8787"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
NOT_SENT = "Nothing was sent."
_REQUIRED = {
    "email": ("to", "subject", "body"),
    "email-reply": ("noteId", "expectTo", "body"),
    "message": ("roomId", "body"),
    "calendar-rsvp": ("eventId", "response"),
    "calendar-invite": ("title", "start", "end"),
    "tweet": ("text",),
    "wallet-proposal": ("to", "amount", "chain", "purpose"),
    "action": ("title", "reaches", "steps"),
}
ACTION_DEFAULT_EXPIRY_S = 7 * 86400


def _grantable() -> Dict[str, str]:
    """{rule id: title} an action may name, from the guard's own table (one source of truth)."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "hermes" / "plugins" / "omni-bridge" / "guard_table.py"
    spec = importlib.util.spec_from_file_location("omni_propose_guard_table", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod.grantable()


def action_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Check an action's grants against the guard table and add `permissions` (their titles)
    for the card. Raises ProposeError naming what cannot be granted."""
    grants = payload.get("grants") or []
    if not isinstance(grants, list) or not all(isinstance(g, str) for g in grants):
        raise ProposeError("grants must be a list of guard rule ids. %s" % NOT_SENT)
    out = {k: v for k, v in payload.items() if k not in ("grants", "permissions")}
    if grants:
        table = _grantable()
        unknown = sorted({g for g in grants if g not in table})
        if unknown:
            raise ProposeError("these cannot be granted: %s. Grantable: %s. %s" % (", ".join(unknown), ", ".join(sorted(table)), NOT_SENT))
        ordered = list(dict.fromkeys(grants))
        out["grants"] = ordered
        out["permissions"] = [table[g] for g in ordered]
    return out


class ProposeError(RuntimeError):
    """The draft could not be put in the queue. Never carries the token."""


def _hermes_env_value(name: str) -> Optional[str]:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    try:
        for line in (Path(home) / ".env").read_text().splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() == name:
                return val.strip().strip("'\"")
    except OSError:
        return None
    return None


def _setting(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    return v or (_hermes_env_value(name) or "").strip()


def _token() -> str:
    t = _setting("OMNI_SERVICE_TOKEN")
    return t if len(t) >= 16 else ""


def gateway() -> str:
    raw = (_setting("OMNI_GATEWAY_URL") or DEFAULT_GATEWAY).rstrip("/")
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return ""
    if u.scheme != "http" or host not in _LOOPBACK or u.username or u.password:
        return ""
    if u.path not in ("", "/") or u.query or u.fragment:
        return ""
    netloc = "[::1]" if host == "::1" else host
    return "http://%s%s" % (netloc, ":%d" % port if port else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


Transport = Callable[[str, bytes, Dict[str, str]], Tuple[int, bytes]]


def _http_post(url: str, data: bytes, headers: Dict[str, str]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=15) as res:
            return res.status, res.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(64 * 1024)
        except Exception:
            return e.code, b""


def propose(kind: str, payload: Dict[str, Any], summary: Optional[str] = None,
            expires_in_sec: Optional[int] = None, thread_id: Optional[str] = None,
            transport: Optional[Transport] = None) -> Dict[str, Any]:
    """Create a PENDING approval for exactly `payload`. Sends nothing. Raises ProposeError."""
    if kind not in KINDS:
        raise ProposeError("kind must be one of %s. %s" % (", ".join(KINDS), NOT_SENT))
    if not isinstance(payload, dict):
        raise ProposeError("payload must be an object. %s" % NOT_SENT)
    missing = [k for k in _REQUIRED[kind] if payload.get(k) in (None, "", [])]
    if missing:
        raise ProposeError("the %s draft is missing %s. %s" % (kind, ", ".join(missing), NOT_SENT))
    if kind == "action":
        payload = action_payload(payload)
        if expires_in_sec is None:
            expires_in_sec = ACTION_DEFAULT_EXPIRY_S
    base, token = gateway(), _token()
    if not base or not token:
        # Inside an Omni / Hermes turn the sandbox hides the gateway token on purpose: the
        # omni_propose TOOL is the way there (2026-11-05: an agent tried this script first).
        raise ProposeError("the Omni gateway is not configured for this process "
                           "(OMNI_SERVICE_TOKEN / OMNI_GATEWAY_URL). In an Omni or Hermes conversation, "
                           "call the omni_propose tool instead: its shell cannot see the token. %s" % NOT_SENT)
    body: Dict[str, Any] = {"kind": kind, "payload": payload}
    if thread_id:
        body["threadId"] = thread_id
    if isinstance(summary, str) and summary.strip():
        body["summary"] = " ".join(summary.split())[:300]
    if isinstance(expires_in_sec, int) and not isinstance(expires_in_sec, bool):
        body["expiresInSec"] = max(60, min(7 * 86400, expires_in_sec))
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": "Bearer " + token, "User-Agent": "omni-propose/1.0"}
    try:
        status, raw = (transport or _http_post)(base + PROPOSE_PATH, data, headers)
    except Exception as e:
        raise ProposeError("the Omni gateway is not reachable (%s). %s" % (type(e).__name__, NOT_SENT))
    try:
        res = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        res = {}
    if status == 201 and isinstance(res, dict) and isinstance(res.get("id"), str):
        return {
            "ok": True, "approval_id": res["id"], "status": res.get("status", "pending"),
            "expires_at": res.get("expiresAt"), "sent": False,
            "message": "The draft is waiting for the owner's review in the Omni app (Needs you). "
                       "Nothing has been sent; only his approval of this exact draft sends it.",
        }
    detail = res.get("detail") if isinstance(res, dict) else None
    hint = (" (%s)" % detail[:200]) if isinstance(detail, str) and detail else ""
    raise ProposeError("the Omni gateway answered %s%s. %s" % (status, hint, NOT_SENT))


# ── CLI ──────────────────────────────────────────────────────────────────────

_SECRET_DIRS = (".hermes", ".ssh", ".gnupg", ".aws", ".config", ".prism", ".pm2", ".claude",
                "Library/Keychains", "Library/Messages", "Library/Mail")


def _secret_path(p: Path) -> bool:
    """A credential store or private history: never read as a draft body (review N3 —
    `--body-file ~/.hermes/.env` would otherwise put a secret on an approval card)."""
    home = Path.home().resolve()
    name = p.name.lower()
    if name.startswith(".env") or name.endswith((".pem", ".p8", ".key")) or name == ".claude.json":
        return True
    if "prism-server" in name and ".db" in name or name == "proton-mail-state.json":
        return True
    try:
        rel = p.relative_to(home).as_posix()
    except ValueError:
        return False
    return any(rel == d or rel.startswith(d + "/") for d in _SECRET_DIRS)


def _read(arg: Optional[str]) -> Optional[str]:
    if arg is None:
        return None
    if arg == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    p = Path(arg).expanduser().resolve()
    if _secret_path(p):
        raise ProposeError("refusing to read a credential file as a draft body. %s" % NOT_SENT)
    return p.read_text(encoding="utf-8")


def _payload(args) -> Tuple[str, Dict[str, Any]]:
    k = args.kind
    if k == "email":
        return k, {k2: v for k2, v in {"to": args.to, "cc": args.cc, "subject": args.subject,
                                       "body": _read(args.body_file) if args.body_file else args.body}.items() if v}
    if k == "email-reply":
        return k, {k2: v for k2, v in {"noteId": args.note, "expectTo": args.expect_to, "cc": args.cc,
                                       "body": _read(args.body_file) if args.body_file else args.body}.items() if v}
    if k == "message":
        return k, {"roomId": args.room, "body": _read(args.body_file) if args.body_file else args.body}
    if k == "tweet":
        return k, {"text": _read(args.text_file) if args.text_file else args.text}
    if k == "calendar-invite":
        p = {"title": args.title, "start": args.start, "end": args.end, "attendees": args.attendee,
             "location": args.location, "description": args.description, "withMeet": True if args.with_meet else None}
        return k, {k2: v for k2, v in p.items() if v}
    if k == "calendar-rsvp":
        return k, {"eventId": args.event_id, "response": args.response}
    if k == "action":
        p = {"title": args.title, "reaches": args.reaches, "url": args.url,
             "steps": _read(args.steps_file) if args.steps_file else args.steps, "grants": args.grant}
        return k, {k2: v for k2, v in p.items() if v}
    raise ProposeError("unsupported kind")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Propose a draft for the owner's approval in Omni. Sends nothing.")
    ap.add_argument("--summary", help="one line for the approval card")
    ap.add_argument("--expires-hours", type=float)
    sub = ap.add_subparsers(dest="kind", required=True)
    e = sub.add_parser("email")
    e.add_argument("--to", action="append", required=True)
    e.add_argument("--cc", action="append")
    e.add_argument("--subject", required=True)
    e.add_argument("--body")
    e.add_argument("--body-file")
    r = sub.add_parser("email-reply")
    r.add_argument("--note", required=True)
    r.add_argument("--expect-to", action="append", required=True)
    r.add_argument("--cc", action="append")
    r.add_argument("--body")
    r.add_argument("--body-file")
    m = sub.add_parser("message")
    m.add_argument("--room", required=True)
    m.add_argument("--body")
    m.add_argument("--body-file")
    t = sub.add_parser("tweet")
    t.add_argument("--text")
    t.add_argument("--text-file")
    c = sub.add_parser("calendar-invite")
    c.add_argument("--title", required=True)
    c.add_argument("--start", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--attendee", action="append")
    c.add_argument("--location")
    c.add_argument("--description")
    c.add_argument("--with-meet", action="store_true", help="add a Google Meet link (every online meeting)")
    v = sub.add_parser("calendar-rsvp")
    v.add_argument("--event-id", required=True)
    v.add_argument("--response", required=True, choices=["accepted", "declined", "tentative"])
    a = sub.add_parser("action", help="an outward step no other kind covers (booking page, web form, sign-up)")
    a.add_argument("--title", required=True, help="one line: what, and when")
    a.add_argument("--reaches", required=True, help="who or what it affects")
    a.add_argument("--url")
    a.add_argument("--steps", help="the exact steps, with every value that will be entered")
    a.add_argument("--steps-file")
    a.add_argument("--grant", action="append", help="a guard rule id the steps need (repeatable)")
    args = ap.parse_args(argv)
    try:
        kind, payload = _payload(args)
        hours = args.expires_hours
        res = propose(kind, payload, summary=args.summary,
                      expires_in_sec=int(hours * 3600) if hours and hours > 0 else None)
    except (ProposeError, OSError, UnicodeDecodeError) as exc:
        print(json.dumps({"ok": False, "sent": False, "error": str(exc)}))
        return 1
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
