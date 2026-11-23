#!/usr/bin/env python3
from __future__ import annotations

"""
Omni Agent — Matrix Bridge Reader

Reads and sends messages via a local Matrix homeserver with WhatsApp and
Telegram bridges. Analogous to how `gog gmail` works for email.

Usage:
    python3 scripts/matrix_reader.py rooms
    python3 scripts/matrix_reader.py recent [--hours 24]
    python3 scripts/matrix_reader.py read <room_id_or_name> [--limit 20]
    python3 scripts/matrix_reader.py triage [--hours 24]
    python3 scripts/matrix_reader.py send-to "Name" --platform Telegram -m "message" --approved
        (ONE DOOR: send / send-to also need a one-time Prism send grant —
        scripts/send_grant.py — so they only work when Prism's approval executor
        launches them; propose instead: scripts/omni_propose.py message ...)
    python3 scripts/matrix_reader.py find-room "Name" [--platform Telegram]
    python3 scripts/matrix_reader.py send <room_id> <message> --approved  (legacy)
    python3 scripts/matrix_reader.py search <query>
    venv/bin/python scripts/matrix_reader.py health   (the venv has PyYAML: system
        python3 cannot read the bridge configs, so every bridge reads "unknown")

Environment variables (from .env):
    MATRIX_HOMESERVER, MATRIX_USER, MATRIX_ACCESS_TOKEN, MATRIX_DEVICE_ID
"""

import argparse
import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
SYNC_STATE_FILE = STATE_DIR / "matrix-sync-state.json"

# Simple .env loader (no python-dotenv dependency)
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_dotenv(PROJECT_ROOT / ".env")

HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
USER_ID = os.getenv("MATRIX_USER", "@omni-agent:localhost")
ACCESS_TOKEN = os.getenv("MATRIX_ACCESS_TOKEN", "")
DEVICE_ID = os.getenv("MATRIX_DEVICE_ID", "OMNI_AGENT")

# Bridge ghost user patterns — used to detect which platform a room belongs to
_GHOST_PATTERNS = {
    "WhatsApp": re.compile(r"^@whatsapp_", re.IGNORECASE),
    "Telegram": re.compile(r"^@telegram_", re.IGNORECASE),
    "Discord": re.compile(r"^@discord_", re.IGNORECASE),
    "Messenger": re.compile(r"^@messenger_", re.IGNORECASE),
    "Instagram": re.compile(r"^@instagram_", re.IGNORECASE),
    "Twitter": re.compile(r"^@twitter_", re.IGNORECASE),
    "Signal": re.compile(r"^@signal_", re.IGNORECASE),
    # mautrix-slack ghosts: slack_<team>-<user> (one per workspace).
    "Slack": re.compile(r"^@slack_", re.IGNORECASE),
}
# Keep legacy names for backward compat
_WHATSAPP_RE = _GHOST_PATTERNS["WhatsApp"]
_TELEGRAM_RE = _GHOST_PATTERNS["Telegram"]

# Management bot user IDs — rooms with just agent + one of these are management rooms
_BRIDGE_BOTS = {
    "@whatsappbot:localhost",
    "@telegrambot:localhost",
    "@discordbot:localhost",
    "@messengerbot:localhost",
    "@twitterbot:localhost",
    "@signalbot:localhost",
    "@slackbot:localhost",
}

# Bot MXID → the docker container running that bridge. Health used to be inferred
# from whether the bot appeared in a joined room, but room membership survives a
# logout FOREVER — so a bridge whose session was revoked still read as
# "connected". On 2026-08-04 four bridges died (telegram/instagram logged out,
# linkedin crashed on a DB race, whatsapp went client-outdated) and this check
# reported all seven OK for a week. Container state and the bridge's own
# bridge_state actually move when a bridge dies, so those are what we read now.
_BOT_CONTAINER = {
    "@whatsappbot:localhost": "matrix-whatsapp",
    "@telegrambot:localhost": "matrix-telegram",
    "@discordbot:localhost": "matrix-discord",
    "@messengerbot:localhost": "matrix-meta-messenger",
    "@twitterbot:localhost": "matrix-twitter",
    # Signal + Slack (bridgev2, v26.11) run from the agent host's docker-compose.override.yml
    # since 2026-11-22. Before the owner links an account their provisioning whoami
    # has no logins → "unknown", which check_oauth flags until the login is done.
    "@signalbot:localhost": "matrix-signal",
    "@slackbot:localhost": "matrix-slack",
}

# bridge_state values that mean the bridge cannot carry messages, → why.
_BAD_BRIDGE_STATES = {
    "BAD_CREDENTIALS": "logged out / credentials rejected — needs relogin",
    "UNCONFIGURED": "no account linked",
    "UNKNOWN_ERROR": "bridge error (check docker logs)",
    "BRIDGE_UNREACHABLE": "bridge cannot reach the remote network",
    "BAD_CONFIG": "invalid bridge config",
}

# A bridge silent this long is not verifiably alive. The four that died in August
# went quiet for days while Docker still showed them Up.
_BRIDGE_SILENT_HOURS = 24.0

# Map bot usernames to platform labels (for management room identification)
_BOT_PLATFORM = {
    "@whatsappbot:localhost": "WhatsApp",
    "@telegrambot:localhost": "Telegram",
    "@discordbot:localhost": "Discord",
    "@messengerbot:localhost": "Messenger",
    "@instagrambot:localhost": "Instagram",
    "@twitterbot:localhost": "Twitter",
    "@linkedinbot:localhost": "LinkedIn",
    "@signalbot:localhost": "Signal",
    "@slackbot:localhost": "Slack",
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _api(method: str, path: str, body: dict | None = None,
         timeout: int = 15) -> dict | list | None:
    """Call the Matrix Client-Server API. Returns parsed JSON or None."""
    url = f"{HOMESERVER}{path}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if raw:
                return json.loads(raw)
            return None
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: Matrix API {method} {path} → {e.code}: {err_body}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"ERROR: Cannot reach Matrix homeserver: {e.reason}", file=sys.stderr)
        return None


def _api_get(path: str, params: dict | None = None, timeout: int = 15):
    if params:
        path = f"{path}?{urlencode(params)}"
    return _api("GET", path, timeout=timeout)


def _api_put(path: str, body: dict, timeout: int = 15):
    return _api("PUT", path, body=body, timeout=timeout)


def _api_post(path: str, body: dict, timeout: int = 15):
    return _api("POST", path, body=body, timeout=timeout)

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        try:
            return json.loads(SYNC_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sync_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")

# ---------------------------------------------------------------------------
# Room helpers
# ---------------------------------------------------------------------------

def _get_joined_rooms() -> list[str]:
    resp = _api_get("/_matrix/client/v3/joined_rooms")
    if resp and "joined_rooms" in resp:
        return resp["joined_rooms"]
    return []


def _get_room_state(room_id: str) -> list[dict]:
    resp = _api_get(f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state")
    if isinstance(resp, list):
        return resp
    return []


def _get_room_name(state_events: list[dict]) -> str:
    for ev in state_events:
        if ev.get("type") == "m.room.name":
            return ev.get("content", {}).get("name", "")
    return ""


def _get_room_members(state_events: list[dict]) -> list[str]:
    members = []
    for ev in state_events:
        if ev.get("type") == "m.room.member":
            if ev.get("content", {}).get("membership") == "join":
                members.append(ev.get("state_key", ""))
    return members


def _detect_platform(members: list[str]) -> str:
    """Detect platform from ghost user patterns in room membership."""
    for platform, pattern in _GHOST_PATTERNS.items():
        if any(pattern.match(m) for m in members):
            return platform
    # Fallback: check if a bridge bot is present (management rooms)
    for m in members:
        if m in _BOT_PLATFORM:
            return _BOT_PLATFORM[m]
    return "Matrix"


def _is_management_room(members: list[str]) -> bool:
    """True if room is just agent + a bridge bot (management/status room)."""
    non_bot = [m for m in members if m not in _BRIDGE_BOTS]
    if len(non_bot) <= 1 and any(m in _BRIDGE_BOTS for m in members):
        return True
    return False


def _display_name_from_id(user_id: str) -> str:
    """Best-effort display name from user ID."""
    # Try to get profile display name
    resp = _api_get(f"/_matrix/client/v3/profile/{quote(user_id, safe='')}/displayname")
    if resp and resp.get("displayname"):
        return resp["displayname"]
    # Fallback: extract from user ID
    local = user_id.split(":")[0].lstrip("@")
    # Clean up bridge prefixes
    for prefix in ("whatsapp_", "telegram_", "discord_", "messenger_", "instagram_", "twitter_", "linkedin_"):
        if local.startswith(prefix):
            local = local[len(prefix):]
    return local.replace("_", " ").title()


# Cache display names within a session
_display_name_cache: dict[str, str] = {}

def _get_display_name(user_id: str) -> str:
    if user_id not in _display_name_cache:
        _display_name_cache[user_id] = _display_name_from_id(user_id)
    return _display_name_cache[user_id]


def _platform_tag(user_id: str) -> str:
    if _WHATSAPP_RE.match(user_id):
        return " (WA)"
    if _TELEGRAM_RE.match(user_id):
        return " (TG)"
    return ""


def _ts_to_str(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _resolve_room(id_or_name: str, rooms: list[str] | None = None) -> str | None:
    """Resolve a room ID or partial name to a room ID.

    Returns the first match. For safety-critical sends, use
    _resolve_room_strict() which requires platform and rejects ambiguity.
    """
    # If it looks like a room ID, use directly
    if id_or_name.startswith("!"):
        return id_or_name
    # Search by name
    if rooms is None:
        rooms = _get_joined_rooms()
    id_or_name_lower = id_or_name.lower()
    for rid in rooms:
        state = _get_room_state(rid)
        name = _get_room_name(state)
        if name and id_or_name_lower in name.lower():
            return rid
    return None


def _resolve_room_strict(
    name_query: str,
    platform: str | None = None,
    rooms: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a contact name to exactly one room ID, with platform filter.

    Returns (room_id, error_message). Exactly one must be non-None.
    If multiple rooms match, returns an error listing all candidates so the
    caller can disambiguate — NEVER silently picks one.
    """
    if name_query.startswith("!"):
        return name_query, None

    if rooms is None:
        rooms = _get_joined_rooms()

    query_lower = name_query.lower()
    platform_lower = platform.lower() if platform else None
    matches: list[tuple[str, str, str]] = []  # (room_id, name, platform)

    for rid in rooms:
        info = _get_room_info(rid)
        if info["is_management"]:
            continue
        room_name = info["name"]
        if not room_name or query_lower not in room_name.lower():
            continue
        room_platform = info["platform"]
        if platform_lower and room_platform.lower() != platform_lower:
            continue
        matches.append((rid, room_name, room_platform))

    if len(matches) == 0:
        hint = f" on {platform}" if platform else ""
        return None, f"No room found matching '{name_query}'{hint}."

    if len(matches) == 1:
        return matches[0][0], None

    # Ambiguous — list all candidates so the agent can pick
    lines = [f"Ambiguous: {len(matches)} rooms match '{name_query}':"]
    for rid, rname, rplat in matches:
        lines.append(f"  [{rplat}] {rname}  →  {rid}")
    lines.append("Use --platform to narrow, or pass the exact room ID.")
    return None, "\n".join(lines)

# ---------------------------------------------------------------------------
# Room info (cached per invocation)
# ---------------------------------------------------------------------------

_room_info_cache: dict[str, dict] = {}

def _get_room_info(room_id: str) -> dict:
    if room_id not in _room_info_cache:
        state = _get_room_state(room_id)
        members = _get_room_members(state)
        _room_info_cache[room_id] = {
            "name": _get_room_name(state) or room_id,
            "members": members,
            "platform": _detect_platform(members),
            "is_management": _is_management_room(members),
            "member_count": len(members),
        }
    return _room_info_cache[room_id]

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_rooms(args: argparse.Namespace) -> None:
    """List all joined rooms with names, platform labels, and member count."""
    rooms = _get_joined_rooms()
    if not rooms:
        print("No joined rooms found.")
        return

    print(f"JOINED ROOMS ({len(rooms)} total)\n")
    for rid in sorted(rooms):
        info = _get_room_info(rid)
        if info["is_management"]:
            continue
        tag = f"[{info['platform']}]"
        print(f"  {tag:<12} {info['name']}")
        print(f"             {rid}  ({info['member_count']} members)")
    print()


def cmd_recent(args: argparse.Namespace) -> None:
    """Show rooms with messages in the last N hours."""
    hours = args.hours
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    rooms = _get_joined_rooms()

    print(f"ROOMS WITH ACTIVITY (last {hours}h)\n")
    found = False

    for rid in rooms:
        info = _get_room_info(rid)
        if info["is_management"]:
            continue

        # Fetch recent messages
        resp = _api_get(
            f"/_matrix/client/v3/rooms/{quote(rid, safe='')}/messages",
            params={"limit": "50", "dir": "b"},
        )
        if not resp or "chunk" not in resp:
            continue

        recent = [
            ev for ev in resp["chunk"]
            if ev.get("type") == "m.room.message"
            and ev.get("origin_server_ts", 0) >= cutoff_ms
        ]
        if not recent:
            continue

        found = True
        latest = recent[0]
        latest_body = latest.get("content", {}).get("body", "")
        latest_sender = _get_display_name(latest.get("sender", ""))
        latest_ts = _ts_to_str(latest.get("origin_server_ts", 0))
        tag = f"[{info['platform']}]"

        preview = latest_body[:80] + ("..." if len(latest_body) > 80 else "")
        print(f"  {tag:<12} {info['name']} | {len(recent)} messages")
        print(f"             Latest: [{latest_ts}] {latest_sender}: {preview}")
        print()

    if not found:
        print(f"  No activity in the last {hours} hours.")
    print()


def cmd_read(args: argparse.Namespace) -> None:
    """Read recent messages from a specific room."""
    room_id = _resolve_room(args.room)
    if not room_id:
        print(f"ERROR: Room not found: {args.room}", file=sys.stderr)
        sys.exit(1)

    info = _get_room_info(room_id)
    limit = args.limit

    resp = _api_get(
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages",
        params={"limit": str(limit), "dir": "b"},
    )
    if not resp or "chunk" not in resp:
        print("No messages found.")
        return

    messages = [
        ev for ev in resp["chunk"]
        if ev.get("type") == "m.room.message"
    ]
    # Reverse to chronological order
    messages.reverse()

    tag = f"[{info['platform']}]"
    print(f"{tag} {info['name']} — last {len(messages)} messages\n")

    for ev in messages:
        ts = _ts_to_str(ev.get("origin_server_ts", 0))
        sender = ev.get("sender", "")
        name = _get_display_name(sender)
        ptag = _platform_tag(sender)
        body = ev.get("content", {}).get("body", "")
        msgtype = ev.get("content", {}).get("msgtype", "m.text")

        if msgtype == "m.image":
            body = "[image]"
        elif msgtype == "m.file":
            body = f"[file: {body}]"
        elif msgtype == "m.audio":
            body = "[audio message]"
        elif msgtype == "m.video":
            body = "[video]"

        print(f"[{ts}] {name}{ptag}: {body}")

    # Trusted Omni readers must not mutate the legacy read cursor. Operator CLI
    # reads retain their existing local bookkeeping outside the reader sandbox.
    if messages and os.environ.get("OMNI_SANDBOX") != "reader":
        last_event_id = resp["chunk"][0].get("event_id", "")
        state = _load_sync_state()
        if "per_room" not in state:
            state["per_room"] = {}
        state["per_room"][room_id] = {
            "last_event_id": last_event_id,
            "last_read_ts": int(time.time() * 1000),
        }
        _save_sync_state(state)

    print()


def cmd_triage(args: argparse.Namespace) -> None:
    """Summarize all new messages since last triage across all bridged rooms."""
    hours = args.hours
    state = _load_sync_state()
    last_triage_ts = state.get("last_triage_ts", 0)

    # Use either last triage or --hours cutoff, whichever is more recent
    hours_cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    cutoff_ms = max(last_triage_ts, hours_cutoff_ms) if last_triage_ts else hours_cutoff_ms

    cutoff_dt = datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).astimezone()
    print(f"=== NEW MESSAGES (since {cutoff_dt.strftime('%Y-%m-%d %H:%M')}) ===\n")

    rooms = _get_joined_rooms()
    any_messages = False

    for rid in rooms:
        info = _get_room_info(rid)
        if info["is_management"]:
            continue

        resp = _api_get(
            f"/_matrix/client/v3/rooms/{quote(rid, safe='')}/messages",
            params={"limit": "100", "dir": "b"},
        )
        if not resp or "chunk" not in resp:
            continue

        new_msgs = [
            ev for ev in resp["chunk"]
            if ev.get("type") == "m.room.message"
            and ev.get("origin_server_ts", 0) > cutoff_ms
        ]
        if not new_msgs:
            continue

        # Reverse to chronological
        new_msgs.reverse()
        any_messages = True

        tag = f"[{info['platform']}]"
        print(f"{tag} {info['name']} | {len(new_msgs)} messages")

        # Show up to 10 messages, summarize rest
        display = new_msgs[:10]
        for ev in display:
            ts = _ts_to_str(ev.get("origin_server_ts", 0))
            sender = _get_display_name(ev.get("sender", ""))
            body = ev.get("content", {}).get("body", "")
            msgtype = ev.get("content", {}).get("msgtype", "m.text")
            if msgtype != "m.text":
                body = f"[{msgtype.split('.')[-1]}]"
            preview = body[:100] + ("..." if len(body) > 100 else "")
            print(f"  [{ts}] {sender}: {preview}")

        if len(new_msgs) > 10:
            print(f"  ... and {len(new_msgs) - 10} more messages")
        print()

    if not any_messages:
        print("  No new messages.\n")

    # Update triage timestamp
    state["last_triage_ts"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    _save_sync_state(state)


def cmd_analyze(args: argparse.Namespace) -> None:
    """Produce dashboard-ready JSON analysis of message activity.

    Outputs structured data for the dashboard's Messages panel:
    - Per-platform status and unread counts
    - Priority-ranked conversations (DMs > groups, known contacts > spam)
    - Pending replies (messages from others that the owner hasn't responded to)
    - Cross-platform contact map
    """
    hours = args.hours
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    agent_user = USER_ID

    rooms = _get_joined_rooms()
    platforms: dict[str, dict] = {}
    conversations: list[dict] = []
    pending_replies: list[dict] = []

    for rid in rooms:
        info = _get_room_info(rid)
        if info["is_management"]:
            continue

        platform = info["platform"]
        if platform == "Matrix":
            continue  # Skip raw Matrix rooms (usually stale management)

        if platform not in platforms:
            platforms[platform] = {"rooms": 0, "new_messages": 0, "unread_dms": 0}
        platforms[platform]["rooms"] += 1

        resp = _api_get(
            f"/_matrix/client/v3/rooms/{quote(rid, safe='')}/messages",
            params={"limit": "50", "dir": "b"},
        )
        if not resp or "chunk" not in resp:
            continue

        all_msgs = [ev for ev in resp["chunk"] if ev.get("type") == "m.room.message"]
        new_msgs = [ev for ev in all_msgs if ev.get("origin_server_ts", 0) > cutoff_ms]

        # Filter out agent's own messages for "inbound" count
        inbound = [ev for ev in new_msgs if ev.get("sender") != agent_user]

        if not inbound:
            continue

        platforms[platform]["new_messages"] += len(inbound)

        # Determine if this is a DM (<=4 members typically) vs group
        is_dm = info["member_count"] <= 4
        if is_dm:
            platforms[platform]["unread_dms"] += 1

        # Get the latest inbound message
        latest = inbound[0]  # Already in reverse-chronological
        latest_sender = _get_display_name(latest.get("sender", ""))
        latest_body = latest.get("content", {}).get("body", "")
        latest_ts = latest.get("origin_server_ts", 0)

        # Check if the owner's last message is BEFORE the latest inbound
        # (meaning someone is waiting for a reply)
        agent_msgs = [ev for ev in all_msgs if ev.get("sender") == agent_user]
        last_agent_ts = agent_msgs[0].get("origin_server_ts", 0) if agent_msgs else 0
        needs_reply = latest_ts > last_agent_ts and is_dm

        conv = {
            "room_id": rid,
            "name": info["name"],
            "platform": platform,
            "is_dm": is_dm,
            "inbound_count": len(inbound),
            "latest_sender": latest_sender,
            "latest_preview": latest_body[:120],
            "latest_ts": latest_ts,
            "needs_reply": needs_reply,
        }
        conversations.append(conv)

        if needs_reply:
            hours_waiting = (datetime.now(timezone.utc).timestamp() * 1000 - latest_ts) / 3600000
            pending_replies.append({
                "name": info["name"],
                "platform": platform,
                "sender": latest_sender,
                "preview": latest_body[:80],
                "hours_waiting": round(hours_waiting, 1),
            })

    # Sort: DMs first, then by recency
    conversations.sort(key=lambda c: (not c["is_dm"], -c["latest_ts"]))
    pending_replies.sort(key=lambda r: -r["hours_waiting"])

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hours_window": hours,
        "platforms": platforms,
        "total_inbound": sum(p["new_messages"] for p in platforms.values()),
        "total_unread_dms": sum(p["unread_dms"] for p in platforms.values()),
        "conversations": conversations[:20],  # Top 20
        "pending_replies": pending_replies,
    }

    # Write to state file for dashboard consumption
    state_file = Path(__file__).resolve().parent.parent / "state" / "message-analysis.json"
    with open(state_file, "w") as f:
        json.dump(result, f, indent=2)

    # Also print human-readable summary
    print(f"MESSAGE ANALYSIS ({hours}h window)\n")
    print("Platform Status:")
    for p, data in sorted(platforms.items()):
        print(f"  [{p}] {data['new_messages']} inbound, {data['unread_dms']} unread DMs, {data['rooms']} rooms")

    if pending_replies:
        print(f"\nPending Replies ({len(pending_replies)}):")
        for pr in pending_replies[:10]:
            print(f"  [{pr['platform']}] {pr['sender']} — {pr['hours_waiting']}h waiting — {pr['preview']}")

    if conversations:
        print(f"\nActive Conversations ({len(conversations)}):")
        for c in conversations[:10]:
            tag = "DM" if c["is_dm"] else "Group"
            reply_flag = " NEEDS REPLY" if c["needs_reply"] else ""
            print(f"  [{c['platform']}] {c['name']} ({tag}, {c['inbound_count']} msgs){reply_flag}")

    print(f"\nAnalysis written to state/message-analysis.json")


def _require_send_grant(room_id: str, message: str) -> None:
    """ONE DOOR: `--approved` is an argv token anyone can type. A message leaves
    only with Prism's one-time grant for exactly this room and text."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import send_grant
    try:
        send_grant.require_grant(send_grant.message_digest(room_id, message))
    except send_grant.GrantRefused as exc:
        send_grant.refuse_and_exit(exc)


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to a room by ID. Requires --approved flag for safety.

    DEPRECATED for agent use — prefer `send-to` which resolves by name+platform
    and rejects ambiguous matches. This command is retained for direct room ID use.
    """
    if not args.approved:
        print(
            "ERROR: Sending messages requires the --approved flag.\n"
            "All outbound messages MUST go through the approval gate.\n"
            "The agent should draft the message, route through request_approval(),\n"
            "and only call this script with --approved after the owner approves.",
            file=sys.stderr,
        )
        sys.exit(1)

    room_id = _resolve_room(args.room)
    if not room_id:
        print(f"ERROR: Room not found: {args.room}", file=sys.stderr)
        sys.exit(1)

    _require_send_grant(room_id, args.message)

    txn_id = str(uuid.uuid4())
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
        f"/send/m.room.message/{quote(txn_id, safe='')}"
    )
    body = {
        "msgtype": "m.text",
        "body": args.message,
    }

    resp = _api_put(path, body)
    if resp and "event_id" in resp:
        info = _get_room_info(room_id)
        print(f"Sent to [{info['platform']}] {info['name']}")
        print(f"Event ID: {resp['event_id']}")
    else:
        print("ERROR: Failed to send message.", file=sys.stderr)
        sys.exit(1)


def cmd_send_to(args: argparse.Namespace) -> None:
    """Send a message to a contact by name + platform. Safe against misdirection.

    Uses strict resolution: rejects ambiguous matches, requires --platform when
    multiple rooms match, and prints the resolved target for confirmation.
    """
    if not args.approved:
        print(
            "ERROR: Sending messages requires the --approved flag.\n"
            "All outbound messages MUST go through the approval gate.",
            file=sys.stderr,
        )
        sys.exit(1)

    room_id, error = _resolve_room_strict(args.name, platform=args.platform)
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    info = _get_room_info(room_id)
    # Print resolved target so it's visible in logs for audit
    print(f"Resolved: [{info['platform']}] {info['name']}  ({room_id})")

    _require_send_grant(room_id, args.message)

    txn_id = str(uuid.uuid4())
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
        f"/send/m.room.message/{quote(txn_id, safe='')}"
    )
    body = {
        "msgtype": "m.text",
        "body": args.message,
    }

    resp = _api_put(path, body)
    if resp and "event_id" in resp:
        print(f"Sent to [{info['platform']}] {info['name']}")
        print(f"Event ID: {resp['event_id']}")
    else:
        print("ERROR: Failed to send message.", file=sys.stderr)
        sys.exit(1)


def cmd_find_room(args: argparse.Namespace) -> None:
    """Look up a room ID by contact name and optional platform. No message sent.

    Outputs the resolved room ID, name, and platform — useful for verification
    before sending. Rejects ambiguous matches.
    """
    room_id, error = _resolve_room_strict(args.name, platform=args.platform)
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    info = _get_room_info(room_id)
    print(f"[{info['platform']}] {info['name']}")
    print(f"Room ID: {room_id}")
    print(f"Members: {info['member_count']}")


def cmd_auto_join(args: argparse.Namespace) -> None:
    """Join all rooms on the server that the agent isn't already in.

    Uses the Synapse admin API to list rooms, then the client API to join.
    Should be run periodically to pick up new bridge portal rooms.
    """
    # Get rooms we're already in
    joined = set(_api_get("/_matrix/client/v3/joined_rooms").get("joined_rooms", []))

    # Get all rooms on the server (admin API)
    all_rooms_resp = _api_get("/_synapse/admin/v1/rooms?limit=100")
    if not all_rooms_resp:
        print("ERROR: Could not list rooms (admin API). Is the agent user an admin?", file=sys.stderr)
        sys.exit(1)

    all_rooms = all_rooms_resp.get("rooms", [])
    newly_joined = 0

    for room in all_rooms:
        room_id = room["room_id"]
        name = room.get("name", "(unnamed)")
        if room_id not in joined:
            resp = _api_post(f"/_matrix/client/v3/join/{quote(room_id, safe='')}", {})
            if resp and "room_id" in resp:
                print(f"  Joined: {name}")
                newly_joined += 1
            else:
                print(f"  Failed: {name}")

    if newly_joined == 0:
        print("All rooms already joined.")
    else:
        print(f"\nJoined {newly_joined} new room(s).")


def cmd_search(args: argparse.Namespace) -> None:
    """Search for a user across bridges via the user directory."""
    query = args.query
    resp = _api_post(
        "/_matrix/client/v3/user_directory/search",
        body={"search_term": query, "limit": 20},
    )
    if not resp or "results" not in resp:
        print("No results or search failed.")
        return

    results = resp["results"]
    if not results:
        print(f"No users found matching '{query}'.")
        return

    print(f"USER SEARCH: '{query}' ({len(results)} results)\n")
    for user in results:
        uid = user.get("user_id", "")
        display = user.get("display_name", "")
        if _WHATSAPP_RE.match(uid):
            platform = "[WhatsApp]"
        elif _TELEGRAM_RE.match(uid):
            platform = "[Telegram]"
        else:
            platform = "[Matrix]"
        print(f"  {platform:<12} {display or '(no name)'}")
        print(f"             {uid}")
    print()


def _docker(*args: str, timeout: int = 10) -> tuple[int, str]:
    """Run a docker command, returning (returncode, combined output)."""
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "docker not installed"
    except subprocess.TimeoutExpired:
        return 124, "docker timed out"
    except Exception as e:
        return 1, str(e)


def _last_log_age_hours(logs: str) -> float | None:
    """Hours since the most recent timestamped log line, or None if unparseable."""
    for line in reversed(logs.strip().splitlines()):
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)", line)
        if not m:
            continue
        try:
            # docker emits RFC3339Nano (9 fractional digits); fromisoformat wants ≤6.
            dt = datetime.fromisoformat(m.group(1)[:26]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return None


# The probe runs inside the existing Synapse network namespace. Its command is
# static; the shared secret and owner ID travel only through stdin, and its
# stdout contains allowlisted state codes, never the provisioning response.
_BRIDGE_PROVISION_PROBE = r"""
import json, re, sys, urllib.request
p = json.load(sys.stdin)
try:
    request = urllib.request.Request(p["url"], headers={"Authorization": "Bearer " + p["token"]})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("response too large")
        data = json.loads(raw)
    if p.get("kind") == "discord":
        state = data.get("Discord") if isinstance(data, dict) else None
        if isinstance(state, dict) and isinstance(state.get("logged_in"), bool) and isinstance(state.get("connected"), bool):
            print(json.dumps(["CONNECTED" if state["logged_in"] and state["connected"] else "UNCONFIGURED" if not state["logged_in"] else "TRANSIENT_DISCONNECT"]))
        else:
            print("null")
        sys.exit(0)
    logins = data.get("logins") if isinstance(data, dict) else None
    if not isinstance(logins, list):
        print("null")
    else:
        states = []
        for login in logins:
            state = login.get("state_event") if isinstance(login, dict) else None
            if state is None and isinstance(login, dict) and isinstance(login.get("state"), dict):
                state = login["state"].get("state_event")
            states.append(state if isinstance(state, str) and re.fullmatch("[A-Z_]+", state) else "UNKNOWN")
        output = json.dumps(states)
        print(output if len(output) <= 8192 else "null")
except Exception:
    print("null")
"""


def _bridge_provision_states(container: str) -> list[str] | None:
    """Current bridgev2 login states; None means unavailable/unsupported.

    An empty list is authoritative no-login evidence and must not fall back to
    an old CONNECTED log. Older bridges without the v3 schema use log fallback.
    """
    if container not in _BOT_CONTAINER.values():
        return None
    name = container.removeprefix("matrix-")
    try:
        import yaml
        config = yaml.safe_load((PROJECT_ROOT / "matrix" / f"mautrix-{name}" / "config.yaml").read_text())
        port = int(config.get("appservice", {}).get("port", 0))
        provisioning = config.get("bridge", {}).get("provisioning", {}) if name == "discord" else config.get("provisioning", {})
        token = provisioning.get("shared_secret")
        if not 1 <= port <= 65535 or not isinstance(token, str) or not token or token == "disable":
            return None
        endpoint = "/_matrix/provision/v3/whoami"
        if name == "discord":
            prefix = provisioning.get("prefix", "/_matrix/provision")
            if not isinstance(prefix, str) or not re.fullmatch(r"/[A-Za-z0-9_/-]+", prefix):
                return None
            endpoint = prefix.rstrip("/") + "/v1/ping"
        payload = {"url": f"http://{container}:{port}{endpoint}?" + urlencode({"user_id": USER_ID}), "token": token, "kind": name}
        result = subprocess.run(
            ["docker", "exec", "-i", "matrix-synapse", "python", "-c", _BRIDGE_PROVISION_PROBE],
            input=json.dumps(payload), capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0 or len(result.stdout) > 8192:
            return None
        states = json.loads(result.stdout)
        if isinstance(states, list) and all(isinstance(state, str) and re.fullmatch(r"[A-Z_]+", state) for state in states):
            return states
    except Exception:
        pass  # Configs, credentials and raw provisioning errors stay private.
    return None


def _bridge_connection_state(states: list[str]) -> tuple[str, str]:
    for state in states:
        if state in _BAD_BRIDGE_STATES:
            return "failed", f"{state}: {_BAD_BRIDGE_STATES[state]}"
    if any(state in {"CONNECTING", "TRANSIENT_DISCONNECT"} for state in states):
        return "degraded", "remote connection is transitional or disconnected"
    if states and all(state == "CONNECTED" for state in states):
        return "connected", "CONNECTED"
    return "unknown", "no affirmative connection evidence"


def _bridge_runtime_state(container: str) -> tuple[str, str]:
    """Real health for one bridge → (status, detail).

    Statuses: connected | degraded | failed | down | stale | not deployed | unknown.
    Never reports healthy on uncertainty — an unreadable bridge is "unknown" so
    the caller escalates instead of assuming OK, which is the exact mistake the
    membership-based check made.
    """
    rc, out = _docker("inspect", "-f", "{{.State.Status}}", container, timeout=2)
    if rc != 0:
        if "no such object" in out.lower() or "no such container" in out.lower():
            return "not deployed", f"no container named {container}"
        return "unknown", "docker inspect unavailable"

    state = out.strip()
    if state != "running":
        return "down", f"container is {state}"

    states = _bridge_provision_states(container)
    if states is not None:
        status, detail = _bridge_connection_state(states)
        return status, f"provisioning: {detail}"

    rc, logs = _docker("logs", "--tail", "400", "--timestamps", container, timeout=2)
    if rc != 0:
        return "unknown", "docker logs unavailable"

    events = re.findall(r'"state_event"\s*:\s*"([A-Z_]+)"', logs)
    last_event = events[-1] if events else ""
    if last_event in _BAD_BRIDGE_STATES:
        return "failed", f"{last_event}: {_BAD_BRIDGE_STATES[last_event]}"

    age = _last_log_age_hours(logs)
    if age is not None and age > _BRIDGE_SILENT_HOURS:
        return "stale", f"silent for {age:.0f}h (last state: {last_event or 'none seen'})"
    if last_event == "CONNECTED":
        return "connected", last_event
    if last_event in {"CONNECTING", "TRANSIENT_DISCONNECT"}:
        return "degraded", f"{last_event}: remote connection is not established"
    hint = "" if importlib.util.find_spec("yaml") else "; PyYAML missing — run with venv/bin/python"
    return "unknown", f"no affirmative connection evidence (last state: {last_event or 'none seen'}){hint}"


def cmd_health(args: argparse.Namespace) -> None:
    """Check Synapse health and bridge status."""
    status = {
        "homeserver": HOMESERVER,
        "user": USER_ID,
        "synapse": "unknown",
        "bridges": {},
    }

    def synapse_health() -> str:
        try:
            with urlopen(Request(f"{HOMESERVER}/health"), timeout=5) as resp:
                return "healthy" if resp.status == 200 else f"status {resp.status}"
        except Exception:
            return "unreachable"

    def health_api(path):
        try:
            return _api_get(path, timeout=5)
        except Exception:
            return None  # A timed-out or malformed probe is unavailable, not a CLI crash.

    # Runtime probes and bounded single-request stats run independently. Never
    # walk room membership: it adds hundreds of requests without proving health.
    # Each bridge is bounded at 2s inspect + 8s provisioning + 2s fallback logs,
    # keeping this command below check_oauth's 15s subprocess budget.
    status["bridge_detail"] = {}
    status["bridge_in_rooms"] = {bot.split(":")[0].lstrip("@"): None for bot in _BRIDGE_BOTS}
    with ThreadPoolExecutor(max_workers=len(_BOT_CONTAINER) + 3) as pool:
        health = pool.submit(synapse_health)
        auth = pool.submit(health_api, "/_matrix/client/v3/account/whoami")
        rooms = pool.submit(health_api, "/_matrix/client/v3/joined_rooms")
        bridges = {bot: pool.submit(_bridge_runtime_state, container) for bot, container in _BOT_CONTAINER.items()}
        status["synapse"] = health.result()
        whoami = auth.result()
        joined = rooms.result()
        status["auth"] = "valid" if isinstance(whoami, dict) and whoami.get("user_id") else "failed"
        if status["auth"] == "valid":
            status["authenticated_as"] = whoami["user_id"]
        status["joined_rooms"] = len(joined["joined_rooms"]) if isinstance(joined, dict) and isinstance(joined.get("joined_rooms"), list) else None
        status["active_rooms"] = None  # Requires a room walk; not a health signal.
        for bot in _BRIDGE_BOTS:
            name = bot.split(":")[0].lstrip("@")
            state, detail = bridges[bot].result() if bot in bridges else ("unknown", "no container mapping")
            status["bridges"][name] = state
            status["bridge_detail"][name] = detail

    # Output
    print("MATRIX HEALTH CHECK\n")
    print(f"  Homeserver:  {status['homeserver']}")
    print(f"  Synapse:     {status['synapse']}")
    print(f"  Auth:        {status['auth']}")
    if "authenticated_as" in status:
        print(f"  User:        {status['authenticated_as']}")
    print(f"  Rooms:       {status['joined_rooms'] if status['joined_rooms'] is not None else 'unknown'} joined")
    print()
    print("  Bridges:")
    for name, st in sorted(status["bridges"].items()):
        detail = status.get("bridge_detail", {}).get(name, "")
        print(f"    {name:<20} {st:<14} {detail}")
    print()

    # Also dump JSON for programmatic use
    print("--- JSON ---")
    print(json.dumps(status, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="matrix_reader.py",
        description="Omni Agent — Matrix bridge reader/writer",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # rooms
    sub.add_parser("rooms", help="List all joined rooms")

    # recent
    p_recent = sub.add_parser("recent", help="Rooms with recent activity")
    p_recent.add_argument("--hours", type=int, default=24, help="Lookback hours (default 24)")

    # read
    p_read = sub.add_parser("read", help="Read messages from a room")
    p_read.add_argument("room", help="Room ID (!abc:localhost) or partial name")
    p_read.add_argument("--limit", type=int, default=20, help="Number of messages (default 20)")

    # triage
    p_triage = sub.add_parser("triage", help="Triage new messages since last check")
    p_triage.add_argument("--hours", type=int, default=24, help="Max lookback hours (default 24)")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Dashboard-ready analysis of message activity")
    p_analyze.add_argument("--hours", type=int, default=24, help="Analysis window (default 24)")

    # send (legacy — prefer send-to)
    p_send = sub.add_parser("send", help="Send by room ID (legacy — prefer send-to)")
    p_send.add_argument("room", help="Room ID or partial name")
    p_send.add_argument("message", help="Message text to send")
    p_send.add_argument("--approved", action="store_true",
                        help="Confirm message was approved via approval gate. REQUIRED.")

    # send-to (safe — resolves by name+platform, rejects ambiguity)
    p_send_to = sub.add_parser("send-to", help="Send to a contact by name+platform (SAFE)")
    p_send_to.add_argument("name", help="Contact name (e.g. 'Sam Rivera')")
    p_send_to.add_argument("--platform", "-p", help="Platform filter: Telegram, WhatsApp, Messenger, etc.")
    p_send_to.add_argument("--message", "-m", required=True, help="Message text to send")
    p_send_to.add_argument("--approved", action="store_true",
                           help="Confirm message was approved via approval gate. REQUIRED.")

    # find-room (lookup only, no message sent)
    p_find = sub.add_parser("find-room", help="Look up room ID by contact name+platform")
    p_find.add_argument("name", help="Contact name (e.g. 'Sam Rivera')")
    p_find.add_argument("--platform", "-p", help="Platform filter: Telegram, WhatsApp, Messenger, etc.")

    # search
    p_search = sub.add_parser("search", help="Search for a user across bridges")
    p_search.add_argument("query", help="Search query (name or phone)")

    # health
    sub.add_parser("health", help="Check Synapse and bridge health")

    # auto-join
    sub.add_parser("auto-join", help="Join all rooms the agent isn't in yet")

    args = parser.parse_args()

    if not ACCESS_TOKEN:
        print("ERROR: MATRIX_ACCESS_TOKEN not set. Check .env file.", file=sys.stderr)
        sys.exit(1)

    dispatch = {
        "rooms": cmd_rooms,
        "recent": cmd_recent,
        "read": cmd_read,
        "triage": cmd_triage,
        "analyze": cmd_analyze,
        "send": cmd_send,
        "send-to": cmd_send_to,
        "find-room": cmd_find_room,
        "search": cmd_search,
        "health": cmd_health,
        "auto-join": cmd_auto_join,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
