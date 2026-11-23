"""omni-bridge — Hermes' side of the Omni gateway (Prism Server `/api/omni/*`).

What it is for
--------------
The Omni app talks to the Prism Server; the Prism Server's Omni gateway talks to
this Hermes through its API server (`api_server` platform, loopback). This
plugin is what makes an Omni turn a full-power assistant with guarded edges
(docs/omni/tool-policy.md):

``omni_propose`` (a tool, every turn of a configured process)
    Puts a DRAFT in front of the owner: an email, a reply, a chat message, a
    calendar invite, a post, a wallet proposal. It sends nothing and can send
    nothing: only owner decision in the app, bound to the exact draft he
    saw, makes the Prism Server act. On an Omni turn the draft is filed under
    its thread; on any other turn (Telegram, the desktop app, the CLI, cron) it
    is proposed with no thread and lands in "Needs you".

the one door (``pre_tool_call``, EVERY turn) — policy.decide_outward
    Owner requirement (2026-10-15): nothing is sent to another person unless
    The owner approved that exact message in Omni. On every turn that is NOT an
    Omni turn the plugin refuses outward sends (the SENDS rows of
    guard_table.py, Hermes' send_message to anyone but his own chat, outward MCP
    tools, sender code in execute_code / written files), credential dumps (the
    SECRETS rows) and switching the guard off (launchctl setenv, writes to the
    plugin, Hermes' settings, launchd or the send scripts). Nothing else on
    those surfaces changes: no sandbox, no approval cards, no path rules.

the tool policy (``pre_tool_call``, Omni turns only) — policy.py
    Default ALLOW. An Omni turn has the terminal, files, code, the browser,
    cron, delegation, memory, skills and every MCP server. What is guarded is
    the table in guard_table.py: outward sends are refused (use
    ``omni_propose``), secrets are unreadable, and the things only the owner
    should decide — network egress from the shell, installs, restarts,
    deletions, writes outside the workspace, scheduled jobs, other agents —
    PAUSE on an approval card in the Omni app (approvals.py) and run only if he
    approves that exact call.

the sandbox (sandbox.py)
    Every shell command of an Omni turn runs inside a macOS sandbox with a
    clean environment: no secret file, no Keychain, no network, no write
    outside the workspace, no way to make a program outside the sandbox act
    (launchd, AppleEvents, `open`, local sockets) — whatever the command turns
    out to be. The rules above decide what a command is allowed beyond that; the
    sandbox decides what it can do. That is why an obfuscated command needs no
    special rule: it runs contained.

the other tools that touch files (contain.py)
    Hermes' file tools do their work as shell commands on the turn's terminal
    environment; that environment is wrapped so they run in the same sandbox.
    A tool that opens a file in Hermes' own process gets a private copy read
    inside the sandbox; one that writes gets a private destination the sandbox
    delivers from. No file access of an Omni turn rests on a path rule alone.

the full toolset (``omni-full``)
    Hermes' full tools are offered to Omni through a toolset this plugin
    defines (``platform_toolsets.api_server: [omni-full, omni]``). No plugin,
    no toolset, no shell. And a plugin Hermes has stopped consulting withdraws
    it: the self-test asks Hermes' own hook dispatch a question it must refuse.

output scrubbing (redact.py)
    The exact values of known credentials are removed from every tool result
    of an Omni turn.

the post-turn notice (``on_session_end``)
    When a turn finishes on an Omni thread from some OTHER surface (the CLI, a
    cron job, Telegram resuming it), the gateway is told so the app shows the
    thread as unread.

Which turns are Omni turns
--------------------------
* a turn the gateway drives (platform ``api_server``);
* a sub-agent such a turn delegated to (it inherits the turn's context);
* a cron job created from an Omni turn or through the gateway (its stored
  origin says ``api_server``) — it runs later under these same rules;
* if the context is lost entirely, a session id of the gateway's own shape.

Scope — what it must never touch
--------------------------------
Every Hermes process that shares this ``HERMES_HOME`` loads the plugin (the
gateway, the desktop app, the CLI, cron, the Buzz relay agents). It does nothing
unless ``OMNI_SERVICE_TOKEN`` (>= 16 characters) is set, ``OMNI_GATEWAY_URL``
(default ``http://127.0.0.1:8787``) is plain http on loopback, and the process
is not a Buzz relay agent (``OMNI_HERMES_GUARD=buzz``: it registers nothing
there). On every turn that is not an Omni turn — Telegram, the desktop app, the
CLI, another cron job — it offers ``omni_propose`` and applies the one door
above, and nothing else: it changes no command, scrubs nothing and makes no
network call except the proposal itself.

Failing closed
--------------
If the gateway is unreachable, ``omni_propose`` returns an error and an
approval cannot be asked for — so the action that needed one does not run. A
fault in the policy blocks the call. Without a working sandbox every shell
command needs the owner's approval. The token, drafts and commands are never
logged; a failure is logged as a status code or an exception name.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import approvals, contain, guard_table, paths, policy, redact, sandbox

logger = logging.getLogger(__name__)

PLUGIN = "omni-bridge"
TOOLSET = "omni"
TOOL = "omni_propose"
CARD_TOOL = "omni_card"
CARD_PATH = "/api/omni/hooks/cards/thread"
#: Is a thread an item card's thread? (GET CARD_PATH/<thread id> → {"carded": bool})
CARD_THREAD_TTL = 30.0
CARD_EGRESS_BLOCK = ("This conversation is about an item card, which untrusted email or chat text drives, and the browser holds "
                     "sign-ins, so the browser is off here (web search, extract and fetch still work). For a step that needs "
                     "the signed-in browser, propose it as an `action` with omni_propose, or do it in a direct conversation "
                     "with the owner. (omni-bridge: net.card-thread)")
CARD_DRAFT_KINDS = ("email", "email-reply", "message", "calendar-rsvp", "calendar-invite")
#: Hermes' full toolset FOR OMNI. It exists only while this plugin is loaded and its
#: self-test passed — `platform_toolsets.api_server: [omni-full, omni]` therefore means
#: "full tools when the guard is there, none when it is not".
FULL_TOOLSET = "omni-full"
#: Tools that wait for an answer only a terminal or Telegram's buttons can give (guard row tool.clarify).
OMNI_UNANSWERABLE_TOOLS = ("clarify",)
SELFTEST_TASK = "omni-bridge-selftest"
DEFAULT_GATEWAY = "http://127.0.0.1:8787"
PROPOSE_PATH = "/api/omni/hooks/propose"
TURN_PATH = "/api/omni/hooks/turn"
OMNI_PLATFORM = "api_server"

KINDS = ("email", "email-reply", "message", "calendar-rsvp", "calendar-invite", "tweet", "wallet-proposal", "action")
CRON_SESSION = re.compile(r"^cron_([a-f0-9]{12})_")
# The gateway is the validator; these only turn an obviously wrong call into a
# clear message for the model without a round trip.
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
#: An action waits for the owner up to the gateway's limit unless the draft says otherwise.
ACTION_DEFAULT_EXPIRY_S = 7 * 86400
ACTION_GRANTS_PATH = "/api/omni/hooks/threads/%s/action-grants"
#: How long one answer of the grants hook is trusted (an approved action's turn lasts minutes).
ACTION_GRANTS_TTL = 5.0
_MAX_BODY = 256 * 1024
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# A session the gateway created: `omni_` + 24 hex (apps/server/src/omni/store.ts newId).
_OMNI_THREAD = re.compile(r"^omni_[0-9a-f]{8,64}$")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

NOT_SENT = "Nothing was proposed and nothing was sent."


# ── configuration (read at call time: a restart-free change is seen) ─────────

def _in_buzz() -> bool:
    return os.environ.get("OMNI_HERMES_GUARD", "").strip().lower() == "buzz"


def _token() -> str:
    t = os.environ.get("OMNI_SERVICE_TOKEN", "").strip()
    return t if len(t) >= 16 else ""


def _gateway() -> str:
    """The gateway's origin, or "" when it is not a plain-http loopback URL."""
    raw = (os.environ.get("OMNI_GATEWAY_URL", "") or DEFAULT_GATEWAY).strip().rstrip("/")
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


def _active() -> bool:
    """The bridge is configured in this process (never in a Buzz relay agent)."""
    return (not _in_buzz()) and bool(_token()) and bool(_gateway())


def _session(name: str) -> str:
    """A session context value (`HERMES_SESSION_*`), "" when there is none."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:
        return os.environ.get(name, "") or ""


# Sub-agents of Omni turns, by their own session id → the Omni thread they work for.
_children = {}
_children_lock = threading.Lock()


def _cron_origin(session_id: str):
    """The stored origin of the cron job a `cron_<job>_…` session runs, or None."""
    m = CRON_SESSION.match(session_id or "")
    if not m:
        return None
    try:
        from cron.jobs import get_job
        job = get_job(m.group(1)) or {}
    except Exception:
        return None
    origin = job.get("origin")
    return origin if isinstance(origin, dict) else None


def _scope(session_id: str = ""):
    """(is an Omni turn?, kind, thread id) for the call being made right now.

    kind: "turn" (the gateway drives it), "subagent", "cron", or "" when it is
    not an Omni turn. Any other platform (telegram, cli, …) is never an Omni
    turn, whatever the session id.
    """
    platform = _session("HERMES_SESSION_PLATFORM").strip().lower()
    sid = session_id or _session("HERMES_SESSION_ID")
    if platform == OMNI_PLATFORM:
        return True, "turn", _thread_id()
    with _children_lock:
        parent = _children.get(sid)
    if parent is not None:
        return True, "subagent", parent
    if platform:
        return False, "", ""
    # No platform at all: a cron job (Hermes clears the session context for those), or a
    # context that did not reach this thread.
    origin = _cron_origin(sid)
    if origin is not None:
        if str(origin.get("platform", "")).lower() == OMNI_PLATFORM:
            chat = str(origin.get("chat_id", "") or "")
            return True, "cron", chat if _OMNI_THREAD.match(chat) else ""
        return False, "", ""
    if _OMNI_THREAD.match(sid or ""):
        return True, "turn", sid
    return False, "", ""


def _is_omni_turn(session_id: str = "") -> bool:
    return _scope(session_id)[0]


def _thread_id() -> str:
    """The Omni thread of this turn = the Hermes session the gateway is driving.

    The API server binds the id the gateway asked for as the chat id; the
    session id can change underneath it when Hermes compacts a long session.
    It comes from the session context, never from the model's arguments, so a
    draft cannot be filed under another thread.
    """
    for name in ("HERMES_SESSION_CHAT_ID", "HERMES_SESSION_ID"):
        v = _session(name).strip()
        if v and _SESSION_ID.match(v):
            return v
    return ""


def _on_subagent_start(parent_session_id=None, child_session_id=None, **_kw):
    """A turn delegated work: the child is an Omni turn if its parent is."""
    try:
        if not _active() or not child_session_id:
            return None
        ok, _kind, thread = _scope(parent_session_id or "")
        if ok:
            with _children_lock:
                if len(_children) > 2000:
                    _children.clear()
                _children[child_session_id] = thread
    except Exception:
        pass
    return None


# ── the gateway call ─────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect would carry the token elsewhere
        return None


def _post(path: str, body, timeout: float, method: str = "POST"):
    """Call a gateway hook with JSON. Returns (status, parsed body | None).

    status 0 = not reachable / not configured. No proxy, no redirect; the token
    goes only in the Authorization header to the loopback gateway.
    """
    base, token = _gateway(), _token()
    if not base or not token:
        return 0, None
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + token, "User-Agent": "omni-bridge/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as res:
            status, raw = res.status, res.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(64 * 1024)
        except Exception:
            raw = b""
    except Exception as e:  # refused, timed out, reset: all "not reachable"
        logger.warning("omni-bridge: %s not reachable (%s)", path, type(e).__name__)
        return 0, None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        parsed = None
    return status, parsed if isinstance(parsed, dict) else None


# ── omni_propose ─────────────────────────────────────────────────────────────

PROPOSE_SCHEMA = {
    "name": TOOL,
    "description": (
        "Put a draft in front of the owner for his approval in the Omni app. This is the ONLY "
        "way anything leaves on his behalf from this conversation: you cannot send email, "
        "messages, invites, posts or funds yourself, and you must not try another way. "
        "The tool stores the draft and returns an approval id; NOTHING is sent until he "
        "approves that exact draft on his device, and he may edit, cancel or ask you to "
        "revise it. After calling it, tell him the draft is waiting for his review — never "
        "say it was sent. Write the full final text: he approves exactly what you pass. "
        "Email bodies are plain text (no markdown).\n\n"
        "payload by kind:\n"
        "- email: {to: [addresses], cc?: [addresses], subject, body}\n"
        "- email-reply: {noteId: the email note being answered, expectTo: [the addresses "
        "the reply goes to], cc?, body}\n"
        "- message: {roomId, body}\n"
        "- calendar-rsvp: {eventId: exact Google event/occurrence ID, response: accepted|declined|tentative}. "
        "Read the event first; propose the requested response, never claim it was sent.\n"
        "- calendar-invite: {title, start, end (RFC 3339 with offset), attendees?: [addresses], "
        "location?, description?}\n"
        "- tweet: {text}\n"
        "- wallet-proposal: {to, amount, token?, chain, purpose}\n"
        "- action: anything else outward he asked for that no kind above covers (a booking page's Confirm, "
        "a web form, a sign-up, an RSVP on a website). {title: one line naming what and when, reaches: who or "
        "what it affects, url?: where, steps: the exact steps with every value you will enter, grants?: guard "
        "rule ids the work needs without pausing for him again (e.g. tool.browser-act to type into the page, "
        "sys.browser to start agent-browser from the terminal)}. He may approve it hours later: Prism then starts "
        "a new turn here with exactly the approved steps, and only during that turn are the grants honoured. Do "
        "the preparation first (find the slot, check the form), so the steps are exact. Never answer a request "
        "like this by stopping, and never substitute a different action (an email instead of the booking)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS),
                     "description": "What kind of outward action the draft is."},
            "payload": {"type": "object",
                        "description": "The complete draft, in the shape listed for its kind."},
            "summary": {"type": "string",
                        "description": "One short line for the approval card, e.g. 'Email Dana about Friday'."},
            "expires_in_hours": {"type": "number",
                                 "description": "Optional: how long the draft stays open (default 24, at most 168)."},
        },
        "required": ["kind", "payload"],
    },
}


def _action_payload(payload: dict):
    """(payload with `permissions` filled from the guard table, problem | None). The card shows the
    permissions in words; the grants the agent named must be approval-tier rows it may name."""
    grants = payload.get("grants")
    if grants in (None, "", []):
        grants = []
    if not isinstance(grants, list) or not all(isinstance(g, str) for g in grants):
        return payload, "grants must be a list of guard rule ids."
    table = guard_table.grantable()
    unknown = sorted({g for g in grants if g not in table})
    if unknown:
        return payload, ("these cannot be granted: %s. Grantable: %s." % (", ".join(unknown), ", ".join(sorted(table))))
    out = {k: v for k, v in payload.items() if k not in ("grants", "permissions")}
    if grants:
        ordered = list(dict.fromkeys(grants))
        out["grants"] = ordered
        out["permissions"] = [table[g] for g in ordered]
    return out, None


_grants_cache = {}       # thread id → (checked at, {rule id: approval id})
_grants_lock = threading.Lock()


def _action_grant(thread: str, rule: str) -> str:
    """The approval id of the approved action whose turn is running in this thread and names
    `rule`, or "". Asked of the gateway, cached briefly. Fails closed: no answer, no grant."""
    if not thread or not _SESSION_ID.match(thread) or rule not in guard_table.grantable():
        return ""
    now = time.monotonic()
    with _grants_lock:
        hit = _grants_cache.get(thread)
    if hit is None or now - hit[0] > ACTION_GRANTS_TTL:
        granted = {}
        try:
            status, res = _post(ACTION_GRANTS_PATH % thread, None, timeout=5, method="GET")
            if status == 200 and res and isinstance(res.get("actions"), list):
                for a in res["actions"]:
                    if isinstance(a, dict) and isinstance(a.get("approvalId"), str) and isinstance(a.get("grants"), list):
                        for g in a["grants"]:
                            if isinstance(g, str):
                                granted.setdefault(g, a["approvalId"])
        except Exception:
            granted = {}
        with _grants_lock:
            if len(_grants_cache) > 200:
                _grants_cache.clear()
            _grants_cache[thread] = (now, granted)
        hit = (now, granted)
    return hit[1].get(rule, "")


def _err(msg: str) -> str:
    return json.dumps({"error": "%s: %s %s" % (TOOL, msg, NOT_SENT)}, ensure_ascii=False)


def _propose_available() -> bool:
    """Schema gate: the tool exists on every turn of a configured (non-Buzz) process —
    proposing is the one way ANY surface may act on a send (one door, 2026-10-15)."""
    try:
        return _active()
    except Exception:
        return False


def _card_available() -> bool:
    """Schema gate for omni_card: an Omni turn of a configured process only. One door opened
    omni_propose to every surface; a card belongs to an Omni thread, so omni_card stays there."""
    try:
        return _active() and _is_omni_turn()
    except Exception:
        return False


def _handle_propose(args: dict, **_kw) -> str:
    """Propose a draft. Returns JSON for the model; never raises, never sends."""
    try:
        if not _active():
            return _err("the Omni gateway is not configured in this Hermes.")
        ok, _kind, scoped_thread = _scope()
        args = args if isinstance(args, dict) else {}
        kind = args.get("kind")
        payload = args.get("payload")
        if isinstance(payload, str):  # some models pass the object as a JSON string
            try:
                payload = json.loads(payload)
            except Exception:
                payload = None
        if kind not in KINDS:
            return _err("kind must be one of %s." % ", ".join(KINDS))
        if not isinstance(payload, dict):
            return _err("payload must be an object (see the tool description for its shape).")
        missing = [k for k in _REQUIRED[kind] if payload.get(k) in (None, "", [])]
        if missing:
            return _err("the %s draft is missing %s." % (kind, ", ".join(missing)))
        if kind == "action":
            payload, problem = _action_payload(payload)
            if problem:
                return _err(problem)
        # Only an Omni turn files the draft under its thread. Any other surface's chat id
        # (a Telegram chat, a CLI session) is not an Omni thread: no threadId, Needs you.
        thread = (scoped_thread or _thread_id()) if ok else ""
        if ok and not thread and _kind == "turn":
            return _err("this turn has no session id to file the draft under.")
        body = {"kind": kind, "payload": payload}
        if thread:
            body["threadId"] = thread
        summary = args.get("summary")
        if isinstance(summary, str) and summary.strip():
            body["summary"] = summary.strip()[:300]
        hours = args.get("expires_in_hours")
        if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
            body["expiresInSec"] = int(max(60, min(7 * 86400, hours * 3600)))
        elif kind == "action":
            body["expiresInSec"] = ACTION_DEFAULT_EXPIRY_S
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _MAX_BODY:
            return _err("the draft is too large.")

        status, res = _post(PROPOSE_PATH, body, timeout=15)
        if status == 201 and res and isinstance(res.get("id"), str):
            logger.info("omni-bridge: proposed %s (%s)", res["id"], kind)
            return json.dumps({
                "ok": True,
                "approval_id": res["id"],
                "status": res.get("status", "pending"),
                "expires_at": res.get("expiresAt"),
                "sent": False,
                "message": "The draft is waiting for the owner's review in the Omni app. "
                           "Nothing has been sent; only his approval of this exact draft sends it.",
            }, ensure_ascii=False)
        if status == 0:
            return _err("the Omni gateway is not reachable.")
        if status == 400:
            detail = res.get("detail") if res else None
            hint = (" (%s)" % detail[:200]) if isinstance(detail, str) and detail else ""
            logger.warning("omni-bridge: the gateway refused a %s draft (400)", kind)
            return _err("the gateway did not accept the draft%s." % hint)
        logger.warning("omni-bridge: propose answered %s", status)
        if status in (401, 403):
            return _err("the Omni gateway refused this Hermes (its hook is not open to it).")
        if status == 404:
            return _err("the Omni gateway is switched off.")
        return _err("the Omni gateway answered %s." % status)
    except Exception as e:  # fail closed: an internal bug proposes nothing
        logger.warning("omni-bridge: propose failed (%s)", type(e).__name__)
        return _err("an internal error.")


# ── omni_card ────────────────────────────────────────────────────────────────

CARD_SCHEMA = {
    "name": CARD_TOOL,
    "description": (
        "Show the owner new options on the card of the item this conversation is about, when he needs to choose "
        "again (the first plan did not work, the other person answered, you found a better way). Give 2 to 4 "
        "concrete, distinct options. The one you recommend, if it would send anything, carries its complete "
        "draft in an omni_propose payload shape. This sends NOTHING: a draft becomes a pending approval only if "
        "he picks it, and only his approval of that exact draft sends it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "short lowercase id, e.g. 'monday'"},
                "label": {"type": "string", "description": "at most 80 characters, starts with a verb"},
                "plan": {"type": "string", "description": "what you will do if he picks it"},
                "produces": {"type": "array", "items": {"type": "string", "enum": ["email", "email-reply", "message", "calendar-rsvp", "calendar-invite", "task", "note", "nothing"]}},
                "draft": {"type": "object", "properties": {"kind": {"type": "string", "enum": list(CARD_DRAFT_KINDS)}, "payload": {"type": "object"}}, "required": ["kind", "payload"]},
            }, "required": ["id", "label", "plan", "produces"]}},
            "recommended": {"type": "string", "description": "the id of the option you recommend"},
            "headline": {"type": "string", "description": "optional new headline: one plain sentence, no ids or markdown"},
            "ask": {"type": "string", "description": "optional: what is being asked of him now"},
            "urgency": {"type": "string", "enum": ["today", "this-week", "whenever"]},
            "context": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "source": {"type": "string"}}, "required": ["text"]}},
        },
        "required": ["options"],
    },
}


def _card_err(msg: str) -> str:
    return json.dumps({"error": "%s: %s Nothing was sent." % (CARD_TOOL, msg)}, ensure_ascii=False)


def _card_rules():
    """The agent's own card rules (scripts/proactive_intelligence/cards.py), the ones the server's are mirrored in.
    Loaded by path: the plugin sits in the repo, and the module is standard-library only."""
    import importlib.util
    path = os.path.join(paths.repo_root(), "scripts", "proactive_intelligence", "cards.py")
    spec = importlib.util.spec_from_file_location("omni_bridge_card_rules", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_card(cards, args: dict, options: list) -> None:
    """Raise cards.CardInvalid for anything the server's cards/thread route would answer 400 to.
    Only fields the model passed are checked (the server keeps the rest of the card)."""
    seen = set()
    for i, raw in enumerate(options):
        draft = raw.get("draft") if isinstance(raw, dict) else None
        payload = draft.get("payload") if isinstance(draft, dict) else None
        # The reply-to-this-item check needs the card's source note, which only the server knows.
        event = {"id": payload.get("noteId")} if isinstance(payload, dict) else {"id": ""}
        cards._option(raw, i, seen, event)
    rec = args.get("recommended")
    if rec is not None:
        if not isinstance(rec, str) or rec not in seen:
            raise cards.CardInvalid("recommended: one of the option ids")
        chosen = next(o for o in options if o["id"] == rec)
        if set(cards.DRAFT_KINDS).intersection(chosen["produces"]) and not chosen.get("draft"):
            raise cards.CardInvalid("recommended: an option that sends needs its draft")
    if args.get("headline") is not None:
        if not cards.readable_headline(cards._text(args["headline"], "headline", 160)):
            raise cards.CardInvalid("headline: plain words, not a slug, path or markdown")
    if args.get("ask") is not None:
        cards._text(args["ask"], "ask", 300, required=False)
    if args.get("urgency") is not None and args["urgency"] not in cards.URGENCIES:
        raise cards.CardInvalid("urgency: one of " + "|".join(cards.URGENCIES))
    facts = args.get("context")
    if facts is not None:
        if not isinstance(facts, list) or len(facts) > 4:
            raise cards.CardInvalid("context: up to 4 facts")
        for i, f in enumerate(facts):
            if not isinstance(f, dict):
                raise cards.CardInvalid("context[%d]: an object" % i)
            cards._text(f.get("text"), "context[%d].text" % i, 200)
            cards._text(f.get("source"), "context[%d].source" % i, 200, required=False)


_CARD_409 = {
    "draft_waiting": "a draft is waiting for owner decision; let him decide, or propose a new draft with omni_propose.",
    "card_not_open": "this card is already handled.",
    "card_resolved": "the item was answered elsewhere, so this card is closed.",
}


def _handle_card(args: dict, **_kw) -> str:
    """New options for this thread's card. Returns JSON for the model; never raises, never sends."""
    try:
        if not _active():
            return _card_err("the Omni gateway is not configured in this Hermes.")
        ok, _kind, scoped_thread = _scope()
        if not ok:
            return _card_err("this tool works only in a conversation started from the Omni app.")
        thread = scoped_thread or _thread_id()
        if not thread:
            return _card_err("this turn has no conversation to update.")
        args = args if isinstance(args, dict) else {}
        options = args.get("options")
        if isinstance(options, str):  # some models pass the list as a JSON string
            try:
                options = json.loads(options)
            except Exception:
                options = None
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            return _card_err("give 2 to 4 options.")
        try:
            cards = _card_rules()
        except Exception:
            return _card_err("the card rules could not be loaded.")
        try:
            _check_card(cards, args, options)
        except cards.CardInvalid as e:
            return _card_err("the card would not be accepted (%s)." % str(e)[:200])
        body = {"threadId": thread, "options": options}
        for key in ("recommended", "headline", "ask", "urgency", "context"):
            if args.get(key) not in (None, "", []):
                body[key] = args[key]
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _MAX_BODY:
            return _card_err("the card is too large.")
        status, res = _post(CARD_PATH, body, timeout=15)
        if status == 200 and res and isinstance(res.get("card"), dict):
            return json.dumps({"ok": True, "card_id": res["card"].get("id"), "state": res["card"].get("state"), "sent": False,
                               "message": "The owner sees the new options on the card. Nothing has been sent."}, ensure_ascii=False)
        if status == 0:
            return _card_err("the Omni gateway is not reachable.")
        error = res.get("error") if res else None
        detail = res.get("detail") if res else None
        if status == 404 and error == "no_card_for_thread":
            return _card_err("this conversation has no card to update.")
        if status == 409:
            return _card_err(_CARD_409.get(error, "the card cannot take new options right now."))
        if status == 400:
            return _card_err("the gateway did not accept the card%s." % ((" (%s)" % detail[:200]) if isinstance(detail, str) and detail else ""))
        if status in (401, 403):
            return _card_err("the Omni gateway refused this Hermes (its hook is not open to it).")
        if status == 404:
            return _card_err("the Omni gateway is switched off.")
        return _card_err("the Omni gateway answered %s." % status)
    except Exception as e:  # fail closed
        logger.warning("omni-bridge: card update failed (%s)", type(e).__name__)
        return _card_err("an internal error.")


# ── the post-turn notice ─────────────────────────────────────────────────────

def _announce(session_id: str) -> None:
    status, _ = _post(TURN_PATH, {"sessionId": session_id}, timeout=3)
    if status != 202:
        logger.info("omni-bridge: turn notice answered %s", status or "unreachable")


def _on_session_end(session_id: str = "", platform: str = "", completed=None,
                    interrupted=None, **_kw):
    """A turn ended. Tell the gateway — only for an Omni thread run from elsewhere."""
    try:
        if not _active():
            return None
        sid = session_id or ""
        if not _OMNI_THREAD.match(sid):
            return None  # not a thread the gateway created: none of Omni's business
        surface = (platform or "").strip().lower()
        # The gateway's own turns (it is streaming them) and anything with no
        # surface at all (Hermes' internal runs: titles, reviews) are not news.
        if not surface or surface == OMNI_PLATFORM:
            return None
        if interrupted or completed is False:
            return None
        # Off the turn's thread: a slow gateway must not delay the reply. Not a
        # daemon, so a CLI process that exits right after the turn still tells.
        threading.Thread(target=_announce, args=(sid,), name="omni-bridge-turn").start()
    except Exception as e:
        logger.debug("omni-bridge: turn notice skipped (%s)", type(e).__name__)
    return None


# ── the Omni tool policy ─────────────────────────────────────────────────────

_TOOLS_SENTENCE = (
    "You have your full tools here: terminal, files, code, web, browser, memory, skills, cron, "
    "delegation and the vault."
)


def _capability_block(manifest=None) -> str:
    """What Omni can do on the Omni app, rendered from config/omni-capabilities.yaml (scripts/gen_capabilities.py).

    Falls back to the old hand-written sentence, logged at WARNING, when the file, PyYAML or the
    renderer is unavailable: a prompt without the manifest is worse than no prompt at all.
    """
    try:
        import importlib.util
        root = paths.repo_root()
        spec = importlib.util.spec_from_file_location("omni_gen_capabilities", os.path.join(root, "scripts", "gen_capabilities.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data = mod.load(manifest) if manifest else mod.load()
        errs = mod.check(data)
        if errs:
            raise ValueError("; ".join(errs))
        return mod.render("omni-app", data)
    except Exception as e:
        logger.warning("omni-bridge: capability manifest unavailable (%s: %s); using the built-in tool list", type(e).__name__, e)
        return _TOOLS_SENTENCE


_CAPABILITIES = _capability_block()

GUIDE = (
    "[Omni surface — how this conversation works]\n"
    "You have your full tools here: terminal, files, code, web, browser, memory, skills, cron, "
    "delegation and the vault. Three things are different from other surfaces:\n"
    "1. Anything that leaves on the owner's behalf (email, a message to someone, a calendar invite, "
    "a post, a wallet proposal) is DRAFTED with the omni_propose tool. He approves the exact draft "
    "in the app. Never try to send another way; say the draft is waiting, never that it was sent. "
    "Anything else outward he asked for that has no kind of its own (a booking page, a web form, a "
    "sign-up) is proposed as kind `action`: the exact steps, plus the permissions it needs. Never stop "
    "at 'there is no way to approve this', and never do a different action than he asked for.\n"
    "2. Shell commands run in a sandbox: they can read almost everything but no credentials, can "
    "write only in the workspace (%(ws)s) and temporary directories, and have no network "
    "— except the agent's own read commands (gog calendar/drive/gmail reads, "
    "scripts/omni_cli.py, proton_mail.py search, wallet.py balance, gh and git reads), which work "
    "normally. Use Hermes' web tools for the public web.\n"
    "3. Some actions pause for the owner's approval in the app (reaching the network from the shell, "
    "installs, restarts, deleting or writing outside the workspace, scheduling jobs, memory and "
    "skill changes). The call simply waits; if he approves, it runs exactly as you wrote it. If a "
    "command fails with 'Operation not permitted' and truly needs more, run it again prefixed with "
    "`OMNI_NEEDS=network ` (or `write`) to ask him. Do not retry a refused action another way.\n"
    "For his calendar, tasks, commitments, mail triage and briefings, follow the omni-briefing, "
    "omni-tasks and omni-commitments skills (skill_view): they name the exact read commands. "
    "`ps`, `sudo`, the clipboard and other apps are not available in the sandbox; use `pgrep -l NAME`, and "
    "`netstat -anv -p tcp | grep LISTEN` for listening ports (lsof sees only this command's own processes)."
)

INTERNAL_GUIDE = (
    "[Omni surface — owner-authorized internal autonomy]\n"
    "Complete the owner's internal work without asking for routine approval. What you can do, and what "
    "needs him, is this (one manifest, config/omni-capabilities.yaml):\n"
    + _CAPABILITIES + "\n"
    "Run code with terminal, not execute_code. "
    "Protect credentials and the approval boundary; use existing authenticated readers and executors, never print secrets. "
    "Outbound communications still require his approval of the exact content and recipients: email, Matrix/chat messages, "
    "tweets/posts, calendar invitations and RSVP. Draft them with omni_propose; never send through a browser, shell, "
    "scheduled job, delegated agent or another tool to evade that approval. A job may prepare drafts, not pre-authorize future sends. "
    "When he asks for an outward step that none of those kinds covers (a booking page's Confirm, a web form, a sign-up), "
    "prepare it, then propose kind `action` with the exact steps and the guard permissions they need; once he approves "
    "(whenever he does) a new turn does exactly those steps. Never stop at 'there is no gate for this', and never substitute "
    "a different action (an email instead of the booking he asked for). "
    "Say a draft is waiting only when it actually needs approval. Do not invent permission requirements for internal work. "
    "You improve yourself: when your own code, routine, prompt or repo skill caused a failure, fix it and ship the fix "
    "(skill omni-self-improve) instead of only reporting it or pausing the job. "
    "Do not retry an explicitly refused action through another path."
)

_pending = {}            # tool_call_id → approval id, for reporting how an approved call ended
_pending_lock = threading.Lock()


def _block(msg: str) -> dict:
    return {"action": "block", "message": msg}


_card_threads = {}       # thread id → (checked at, carded?)
_card_threads_lock = threading.Lock()


def _card_thread(thread: str) -> bool:
    """Is this Omni thread an item card's thread? Asked of the gateway, cached briefly. Fails closed: an
    unreachable gateway or an answer that is not a plain yes/no counts as a card thread."""
    if not thread:
        return False
    now = time.monotonic()
    with _card_threads_lock:
        hit = _card_threads.get(thread)
        if hit is not None and now - hit[0] < CARD_THREAD_TTL:
            return hit[1]
    if not _SESSION_ID.match(thread):
        return True
    status, body = _post(CARD_PATH + "/" + urllib.parse.quote(thread, safe=""), None, timeout=3, method="GET")
    carded = not (status == 200 and isinstance(body, dict) and body.get("carded") is False)
    if status == 200 and isinstance(body, dict) and isinstance(body.get("carded"), bool):
        with _card_threads_lock:
            if len(_card_threads) > 1000:
                _card_threads.clear()
            _card_threads[thread] = (now, carded)
    return carded


def _task_cwd(task_id: str):
    """Where the turn's terminal is, if Hermes will say."""
    try:
        try:  # Hermes after ~2026-11 moved it; the old home is kept for the installed version
            from tools.terminal_tool_lifecycle import get_active_env
        except ImportError:
            from tools.terminal_tool import get_active_env
        env = get_active_env(task_id or "")
        cwd = getattr(env, "cwd", None)
        return cwd if isinstance(cwd, str) and cwd else None
    except Exception:
        return None


def _wrap_terminal(args: dict, d) -> dict:
    """The terminal call, rewritten to run inside the sandbox at the decided tier."""
    prof = sandbox.profile(d.tier, network=d.network, keychain=d.keychain, read_secrets=d.grants(),
                           write_anywhere=d.write_anywhere, automation=getattr(d, "automation", False),
                           browser=getattr(d, "browser", False))
    for root in paths.workspace_roots()[:1]:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
    command = d.command if d.command is not None else str(args.get("command") or "")
    return {"action": "modify", "args": {"command": sandbox.wrap(command, prof, tier=d.tier, cwd=d.cwd, trusted_env=d.trusted_env(),
                                                              env_extra=dict(getattr(d, "env", None) or {}),
                                                              path=paths.trusted_path() if (d.secrets or d.keychain or d.trusted_env()) else None)}}


def _card(tool_name: str, args: dict, d, cwd, kind: str) -> dict:
    """What the owner is shown: the exact call, where, and why it was flagged."""
    payload = {"tool": tool_name, "rule": d.rule, "title": d.title, "reason": d.message}
    if tool_name == "terminal":
        payload["command"] = d.command if d.command is not None else str(args.get("command") or "")
        payload["cwd"] = str(d.cwd or d.detail.get("cwd") or cwd or "")
    else:
        payload["input"] = args
    if kind and kind != "turn":
        payload["origin"] = kind
    return payload


SKILL_PROPOSALS = ".skill-proposals"


def _stash_skill_change(args, message: str) -> str:
    """A skill_manage call refused because the skill lives in the repo (self.skill-repo). Hermes' own
    post-turn review makes these after scheduled runs and has no terminal to ship with, so the refusal
    alone dead-ends and the lesson is lost (103 times between 2026-10-28 and 2026-11-05). Keep the change
    in the workspace — one file per distinct change, counted — so the next self-improvement pass
    (skill omni-self-improve) ships it. Returns the refusal, naming where it was kept."""
    try:
        a = args if isinstance(args, dict) else {}
        name = str(a.get("name") or a.get("skill") or "unknown")[:120]
        keep = {k: (v[:65536] if isinstance(v, str) else v) for k, v in a.items() if k in ("name", "skill", "action", "content", "patch", "old_string", "new_string", "file_path", "file_content", "description", "category")}
        digest = __import__("hashlib").sha256(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        folder = os.path.join(paths.default_workspace(), SKILL_PROPOSALS)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, "%s-%s.json" % (re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:60] or "skill", digest))
        rec = {"skill": name, "change": keep, "count": 0, "first": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                rec.update({k: old[k] for k in ("count", "first") if k in old})
        except (OSError, ValueError):
            pass
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        logger.warning("omni-bridge: kept a refused change to repo skill %s for shipping (%s, seen %d×)", name, os.path.basename(path), rec["count"])
        return message + " The change you wanted was kept at %s; the next self-improvement pass ships it, so you need not retry it." % path
    except Exception as e:
        logger.warning("omni-bridge: could not keep a refused skill change (%s)", type(e).__name__)
        return message


def _outward_only(tool_name: str, args, task_id: str):
    """A turn that is not an Omni turn: only the one door (policy.decide_outward)."""
    d = policy.decide_outward(tool_name or "", args if isinstance(args, dict) else {}, None if task_id == SELFTEST_TASK else _task_cwd(task_id))
    if d is None:
        return None
    logger.warning("omni-bridge: blocked %s on a non-Omni turn (%s)", tool_name, d.rule)
    if d.rule == "self.skill-repo" and task_id != SELFTEST_TASK:
        return _block(_stash_skill_change(args, d.message))
    return _block(d.message)


def _pre_tool_call(tool_name: str = "", args: dict = None, session_id: str = "", task_id: str = "",
                   tool_call_id: str = "", **_kw):
    """Apply the Omni tool policy to one call of an Omni turn; on every other turn,
    only the one door (no outward send, no credential dump, no switching the guard off)."""
    if _in_buzz():
        return None  # a Buzz relay agent: omni-buzz-guard governs it
    try:
        # The one door does not depend on the bridge being configured (S1): a Hermes
        # without OMNI_SERVICE_TOKEN / a loopback gateway still refuses sends.
        ok, kind, thread = _scope(session_id or "") if _active() else (False, "", "")
    except Exception:
        ok = False  # cannot tell whose turn it is: at least the one door applies
    if not ok:
        try:
            return _outward_only(tool_name, args, task_id)
        except Exception:
            logger.exception("omni-bridge: outward check failed for %s", tool_name)
            # Not Omni: a bug here must not brick Telegram — it blocks only a tool that sends.
            return _block("omni-bridge internal error: the call was not run.") if policy._OUTWARD_TOOL.search(tool_name or "") else None
    try:
        args = args if isinstance(args, dict) else {}
        tool_name = tool_name or ""
        testing = task_id == SELFTEST_TASK
        cwd = None if testing else _task_cwd(task_id)
        d = policy.decide(tool_name, args, cwd)
        # A card's thread (the item's text drove the turn): web, browser and fetch run in the signed-out browser
        # (owner decision 2026-10-22), each host logged for the weekly audit. While the browser uses the owner's
        # signed-in profile they are off again. Only calls that reach the web ask the gateway.
        if d.action != policy.BLOCK and policy.reaches_web(tool_name, args, d) and _card_thread(thread):
            if policy.card_egress(tool_name, args, d) and not (kind in ("turn", "subagent") and _action_grant(thread, "browser.signed-in")):
                logger.warning("omni-bridge: blocked %s on an item card's thread (net.card-thread)", tool_name)
                return _block(CARD_EGRESS_BLOCK)
            for host in policy.egress_hosts(tool_name, args):
                logger.info("omni-bridge: card-thread egress %s %s", tool_name, host)
        is_shell = tool_name == "terminal"
        is_file = tool_name in contain.FILE_TOOLS
        # Everything that reaches this turn's terminal environment runs in the sandbox:
        # the file tools' own commands as much as the terminal's.
        env_state = "ok"
        if (is_shell or is_file) and not testing:
            env_state = contain.guard_env(task_id, lambda: _scope()[0]) if sandbox.available() else "failed"
        uncontained = (is_shell and not sandbox.available()) or (is_file and env_state != "ok")
        if d.action == policy.ALLOW and uncontained:
            # Nothing would contain it on this machine. Ask instead.
            d = policy.Decision(policy.APPROVE, "sandbox.unavailable",
                                "Run a shell command (no sandbox on this machine)" if is_shell else "Use a file tool (it cannot be sandboxed here)",
                                "The sandbox that normally contains this is not available here.",
                                tier="approved", network=True, write_anywhere=True, command=d.command)
        if d.action == policy.BLOCK:
            if not testing:
                logger.warning("omni-bridge: blocked %s on an Omni turn (%s)", tool_name, d.rule)
            if d.rule == "self.skill-repo" and not testing:
                return _block(_stash_skill_change(args, d.message))
            return _block(d.message)
        if testing and d.action == policy.APPROVE:
            return _block("omni-bridge: self-test")       # the self-test never asks anyone anything
        approved = False
        granted_by = _action_grant(thread, d.rule) if d.action == policy.APPROVE and kind in ("turn", "subagent") else ""
        if granted_by:
            # The owner approved an action that named this permission, and that action's turn is the
            # one running here (Prism's grants hook): it runs as if he had approved this call.
            approved = True
            logger.warning("omni-bridge: %s granted by approved action %s (%s)", tool_name, granted_by, d.rule)
        elif d.action == policy.APPROVE:
            payload = _card(tool_name, args, d, cwd, kind)
            logger.warning("omni-bridge: %s on an Omni turn needs approval (%s)", tool_name, d.rule)
            outcome, approval_id, detail = approvals.request(_post, payload, thread, summary=d.title)
            if outcome != "approved":
                logger.warning("omni-bridge: %s not approved (%s)", tool_name, outcome)
                return _block(approvals.refusal(outcome, d, detail))
            approved = True
            if tool_call_id and approval_id:
                with _pending_lock:
                    if len(_pending) > 500:
                        _pending.clear()
                    _pending[tool_call_id] = approval_id
            logger.warning("omni-bridge: %s approved by the owner (%s)", tool_name, approval_id)
        if testing:
            return _wrap_terminal(args, d) if is_shell and sandbox.available() else None
        # An approved write outside the workspace: the one thing an approval adds for a tool
        # that is not the terminal. Still no secret, still not the guard's own files.
        outside = sandbox.profile("approved", write_anywhere=True) if (approved or d.write_anywhere) and d.rule == "write.outside" and sandbox.available() else None
        contain.enter(task_id, tool_call_id)
        if is_shell:
            if not sandbox.available():
                return None          # approved, and there is no sandbox to run it in
            return _wrap_terminal(args, d)
        if is_file:
            if outside and env_state == "ok":
                contain.grant(task_id, tool_call_id, outside, "approved")
            return None
        if tool_name in ("tool_search", "tool_describe", TOOL):
            return None
        # An MCP server opens a local file itself, outside the sandbox: like an in-process tool it is
        # handed a private copy (work unblock 9), so the file cannot change under it.
        # A tool that opens a local file in Hermes' own process gets a private copy of it
        # (read inside the sandbox), or a private place to write (delivered through the sandbox).
        try:
            changed = contain.private_paths(args, cwd, tool_call_id, write_profile=outside)
        except contain.Refused as e:
            contain.leave(tool_call_id)
            logger.warning("omni-bridge: blocked %s on an Omni turn (file.handover)", tool_name)
            return _block("%s (omni-bridge: file.handover)" % e)
        return {"action": "modify", "args": changed} if changed else None
    except Exception:  # fail closed: a policy bug must not open the gate
        logger.exception("omni-bridge: tool policy failed for %s", tool_name)
        return _block("omni-bridge internal error: the call was not run.")


def _post_tool_call(tool_name: str = "", tool_call_id: str = "", status: str = "", result=None, **_kw):
    """A call ended: deliver what it wrote privately, forget its copies, and tell the
    gateway how an approved call ended (the card then says ran / failed)."""
    try:
        if tool_call_id:
            contain.deliver(tool_call_id)
            contain.leave(tool_call_id)
    except Exception:
        logger.exception("omni-bridge: clean-up after %s failed", tool_name)
    try:
        with _pending_lock:
            approval_id = _pending.pop(tool_call_id, None) if tool_call_id else None
        if not approval_id:
            return None
        failed = str(status or "").lower() in ("error", "failed", "blocked", "timeout")
        if not failed and isinstance(result, str):
            try:
                data = json.loads(result)
            except ValueError:
                data = None
            if isinstance(data, dict):
                code = data.get("exit_code")
                failed = bool(data.get("error")) or (code is not None and code != 0) or data.get("success") is False
        threading.Thread(target=approvals.report, args=(_post, approval_id, not failed), name="omni-bridge-result").start()
    except Exception:
        pass
    return None


def _transform_tool_result(tool_name: str = "", result=None, session_id: str = "", tool_call_id: str = "", **_kw):
    """Remove known credential values from what a tool returned (Omni turns only)."""
    try:
        if not isinstance(result, str) or not _active() or not _scope(session_id or "")[0]:
            return None
        clean = redact.scrub(contain.show_named_paths(tool_call_id or "", result))
        if tool_name in ("terminal",) + tuple(contain.FILE_TOOLS) and any(error in clean for error in ("Operation not permitted", "Permission denied")) and "omni-bridge:" not in clean:
            clean = _hint(clean)
        return clean if clean != result else None
    except Exception:
        return None


TRUSTED_READER_GUIDE = (
    "Authenticated readers must each be one standalone terminal command, optionally after cd to the agent repository. "
    "Use venv/bin/python scripts/omni_cli.py calendar today --json, "
    "venv/bin/python scripts/omni_cli.py tasks list active --json, "
    "venv/bin/python scripts/proton_mail.py health --json, bounded matrix_reader.py read commands, "
    "or gog calendar get primary EVENT_ID --account ACCOUNT --json. "
    "A reader may be piped into filters that open no file (jq, head, tail, grep, sort, uniq, wc, cut, sed -n/s///, awk without getline) "
    "or saved with one trailing > into the workspace or /tmp. "
    "Do not aggregate readers in python -c, subprocess wrappers, tee, xargs, compound scripts or code execution: "
    "those do not inherit reader credentials. Summarize each tool result afterward. "
    "If .env or proton-mail-state.json is denied, retry the exact standalone reader; OMNI_NEEDS=network/write cannot grant credentials. "
    "Do not read, export or print credentials, and do not ask the owner to repair authentication until the actual standalone reader has been tested. "
)


def _repo_write_denied(text: str) -> bool:
    """The refusal names a file of the production checkout that is not a credential: a write the ship makes instead."""
    repo = paths.repo_root()
    for m in re.finditer(re.escape(repo) + r"/[^\s'\":,)]+", text):
        if not paths.is_secret(paths.real(m.group(0))):
            return True
    return False


def _hint(text: str) -> str:
    if _repo_write_denied(text):
        note = " (omni-bridge: the production checkout is read-only to every turn. " + policy.SHIP_HINT + ")"
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("output"), str):
                data["output"] += note
                return json.dumps(data, ensure_ascii=False)
        except ValueError:
            pass
        return text + note
    note = " (omni-bridge: " + TRUSTED_READER_GUIDE + "Sandbox restrictions still apply; credential access is limited to exact trusted readers. Workspace: " + paths.default_workspace() + ".)"
    if not policy.internal_autonomy():
        note += " For non-reader network or write access, OMNI_NEEDS=network or OMNI_NEEDS=write requests the owner's approval; neither grants reader credentials."
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("output"), str):
            data["output"] += note
            return json.dumps(data, ensure_ascii=False)
    except ValueError:
        pass
    return text + note


def _transform_terminal_output(output=None, **_kw):
    try:
        if not isinstance(output, str) or not _active() or not _scope()[0]:
            return None
        clean = redact.scrub(output)
        return clean if clean != output else None
    except Exception:
        return None


def _pre_llm_call(session_id: str = "", platform: str = "", is_first_turn=None, **_kw):
    """Tell the model how the Omni surface works — on Omni turns only, as turn context."""
    try:
        if not _active() or (platform or "").strip().lower() != OMNI_PLATFORM:
            return None
        if not _scope(session_id or "")[0]:
            return None
        if _guard_ok is None:
            # Before this turn's first model call, so before any tool call. In a thread of its
            # own: the test sets (and clears) a session context, and this turn's must not move.
            t = threading.Thread(target=_self_test, name="omni-bridge-selftest", daemon=True)
            t.start()
            t.join(30)
        return {"context": (INTERNAL_GUIDE if policy.internal_autonomy() else GUIDE % {"ws": paths.default_workspace()}) + "\n" + TRUSTED_READER_GUIDE}
    except Exception:
        return None


# ── Omni's full toolset exists only while the guard does ─────────────────────

_guard_ok = None      # None: not tested yet; True / False: the self-test's verdict
_settle_thread = None


def _define_full_toolset(mirror: bool = False) -> int:
    """`omni-full` = the tools the Telegram agent has. Returns how many."""
    import toolsets
    names = set()
    if mirror:
        try:
            from hermes_cli.config import load_config
            from hermes_cli.tools_config import _get_platform_tools
            for key in _get_platform_tools(load_config(), "telegram", include_default_mcp_servers=False):
                names.update(toolsets.resolve_toolset(key))
        except Exception as e:
            logger.warning("omni-bridge: could not read Telegram's tool list (%s); using Hermes' core tools", type(e).__name__)
            names = set()
    if not names:
        names = set(toolsets.resolve_toolset("hermes-telegram"))
    names.discard(TOOL)
    # Hermes' clarify parks the turn on a prompt the Omni apps cannot show or answer (Prism ignores
    # clarify.request); the turn hung until clarify_timeout (2026-11-17). Omni asks in its reply instead.
    names.difference_update(OMNI_UNANSWERABLE_TOOLS)
    toolsets.create_custom_toolset(FULL_TOOLSET, "Omni: the Telegram agent's tools, under omni-bridge's tool policy "
                                   "(docs/omni/tool-policy.md). Exists only while that plugin is loaded.", tools=sorted(names))
    return len(names)


def _withdraw_full_toolset(why: str) -> None:
    global _guard_ok
    _guard_ok = False
    try:
        import toolsets
        toolsets.TOOLSETS.pop(FULL_TOOLSET, None)
    except Exception:
        pass
    logger.error("omni-bridge: SELF-TEST FAILED — %s. Omni's full tools are WITHDRAWN until Hermes is restarted "
                 "with a working guard (platform_toolsets.api_server: [%s, …] now offers none of them).", why, FULL_TOOLSET)


def _self_test() -> bool:
    """Ask Hermes itself — through its own hook dispatch — whether this plugin's policy is
    applied to an Omni turn. If it is not, Omni's full tools are withdrawn: a guard that is
    loaded but not consulted must not leave a shell open."""
    global _guard_ok
    try:
        from gateway.session_context import clear_session_vars, set_session_vars
        from hermes_cli import plugins as hp
        sid = "omni_" + "0" * 24
        tokens = set_session_vars(platform=OMNI_PLATFORM, chat_id=sid, session_key="omni-bridge-selftest", session_id=sid)
        try:
            action, message = hp.get_pre_tool_call_directive(
                "terminal", {"command": "cat %s/.env" % paths.hermes_home()}, task_id=SELFTEST_TASK, session_id=sid, tool_call_id="")
            if action != "block" or "omni-bridge: secret.path" not in (message or ""):
                _withdraw_full_toolset("Hermes did not refuse a secret read on an Omni turn")
                return False
            if sandbox.available():
                details = hp._get_pre_tool_call_directive_details(
                    "terminal", {"command": "true"}, task_id=SELFTEST_TASK, session_id=sid, tool_call_id="")
                wrapped = (getattr(details, "modified_args", None) or {}).get("command", "")
                if not isinstance(wrapped, str) or not wrapped.startswith(sandbox.WRAP_PREFIX):
                    _withdraw_full_toolset("Hermes did not take the sandboxed form of a shell command")
                    return False
        finally:
            clear_session_vars(tokens)
    except Exception as e:
        _withdraw_full_toolset("the check could not be made (%s)" % type(e).__name__)
        return False
    _guard_ok = True
    return True


def _settle() -> None:
    """A few seconds after Hermes loaded its plugins: mirror Telegram's real tool list,
    then test the guard."""
    try:
        time.sleep(float(os.environ.get("OMNI_BRIDGE_SETTLE_S", "8") or 8))
        n = _define_full_toolset(mirror=True)
        if _self_test():
            logger.warning("omni-bridge: self-test passed; %s = %d tools", FULL_TOOLSET, n)
    except Exception:
        logger.exception("omni-bridge: settle failed")
        _withdraw_full_toolset("the check could not be made")


# ── registration ─────────────────────────────────────────────────────────────

def _management_owner_intent(args, context):
    """The gateway verifies current owner-turn scope, never a model authorization flag."""
    ok, kind, thread = _scope(context.get("session_id", ""))
    if not ok or kind != "turn" or not thread:
        return None
    status, result = _post("/api/omni/hooks/proactive/owner-intent", {
        "threadId": thread, "sessionId": _session("HERMES_SESSION_ID"),
        "toolCallId": context.get("tool_call_id", ""), "change": args,
    }, timeout=5)
    if status != 200 or not isinstance(result, dict) or result.get("authorized") is not True:
        return None
    if not isinstance(result.get("turn_id"), str) or not isinstance(result.get("owner_id"), str):
        return None
    return result


def register(ctx):
    if _in_buzz():
        return  # a Buzz relay agent: nothing at all (omni-buzz-guard governs it)
    try:
        # The check must be asked on every agent build: it depends on the turn.
        from tools.registry import no_cache_check_fn
        no_cache_check_fn(_propose_available)
        no_cache_check_fn(_card_available)
    except Exception:
        pass
    if _active():
        try:
            from . import jobs_api
            jobs_api.install()
        except Exception as exc:
            logger.warning("Omni jobs adapter unavailable: %s", type(exc).__name__)
    ctx.register_tool(
        name=TOOL,
        toolset=TOOLSET,
        schema=PROPOSE_SCHEMA,
        handler=_handle_propose,
        check_fn=_propose_available,
        emoji="📝",
    )
    ctx.register_tool(
        name=CARD_TOOL,
        toolset=TOOLSET,
        schema=CARD_SCHEMA,
        handler=_handle_card,
        check_fn=_card_available,
        emoji="🃏",
    )
    from . import management_tools
    management_tools.register(ctx, _scope, _active, paths.repo_root, _management_owner_intent,
                              lambda snapshot: _post("/api/omni/hooks/proactive/preferences", snapshot, timeout=5)[0] == 200)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("transform_terminal_output", _transform_terminal_output)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("on_session_end", _on_session_end)
    if _active():
        try:
            _define_full_toolset()
            global _settle_thread
            _settle_thread = threading.Thread(target=_settle, name="omni-bridge-settle", daemon=True)
            _settle_thread.start()
        except Exception:
            logger.exception("omni-bridge: could not define the %s toolset", FULL_TOOLSET)
        logger.warning("omni-bridge: ACTIVE (full tools, guarded edges; sandbox %s)",
                       "on" if sandbox.available() else "UNAVAILABLE — every shell command will need approval")
    elif _token() and not _gateway():
        logger.warning("omni-bridge: OFF — OMNI_GATEWAY_URL is not a plain-http loopback URL")
