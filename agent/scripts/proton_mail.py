#!/usr/bin/env python3
"""
proton_mail — read the owner's Proton mailbox through the local Proton Mail
Bridge and land it in Parachute as `email`-tagged notes.

owner@example.com is a Workspace identity whose mail is delivered by
Proton, so `gog gmail` 400s on it forever (a Workspace domain whose MX points at Proton).
Proton has no mail API — Bridge is the only supported route: it holds the
account, decrypts locally, and re-serves the mailbox as plain IMAP on
127.0.0.1. Nothing here ever touches Proton's servers directly.

Security posture:
  - The Bridge password lives in the macOS Keychain (service
    "omni-proton-bridge"), never in .env, never in a plist, never in
    a log line. Same pattern as the Buzz nsec.
  - The connection is loopback-only and STARTTLS'd against Bridge's
    self-signed cert, which we PIN on first setup. A fingerprint change means
    either a Bridge reinstall (re-run `setup --repin`) or something is
    impersonating Bridge on 1143 — we fail closed either way.
  - Mailboxes are opened READ-ONLY and bodies fetched with BODY.PEEK, so the
    agent can never mark the owner's mail as read or mutate his mailbox.
  - Message text stays local: Bridge → loopback → Parachute's local SQLite.

Commands:
    setup            Store the Bridge password + pin its TLS cert
    health           Bridge alive? login OK? how stale is the last sync?
    sync             Incremental fetch of new mail into Parachute
    search "query"   Ad-hoc IMAP search (the `gog gmail messages search` analogue)
    attachment       Save one exact attachment from a known message UID

Typical use:
    python3 scripts/proton_mail.py setup
    python3 scripts/proton_mail.py sync --since-days 7
    python3 scripts/proton_mail.py health --json
"""

from __future__ import annotations

import argparse
import email
import email.policy
import getpass
import hashlib
import html
import imaplib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "state" / "proton-mail-state.json"

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

KEYCHAIN_SERVICE = os.environ.get("PROTON_KEYCHAIN_SERVICE", "omni-proton-bridge")
#: The Bridge mailbox. Config: PROTON_BRIDGE_USER, else OMNI_OWNER_EMAIL. Empty = no mailbox (fails closed).
ACCOUNT = (os.environ.get("PROTON_BRIDGE_USER") or os.environ.get("OMNI_OWNER_EMAIL") or "").strip()
HOST = os.environ.get("PROTON_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROTON_BRIDGE_PORT", "1143"))

# Bridge exposes Proton labels as IMAP folders. INBOX is what triage reads;
# Sent is what reply-detection needs (inbound_triage.check_for_replies).
DEFAULT_MAILBOXES = ["INBOX"]

NOTE_DIR = "vault/messages/email"
SOURCE = "proton-bridge"

# Bodies are stored whole so the triage agent can actually read them, but a
# 200KB marketing HTML blob helps nobody and bloats the vault.
MAX_BODY_CHARS = 20_000
# How many recent (uid -> note path) pairs we keep per mailbox so a later run
# can refresh read/unread state without re-fetching entire messages.
UID_INDEX_CAP = 500
# Dedupe set of already-synced UIDs. Must comfortably exceed the number of
# messages a date window can hold — this inbox runs ~60 messages/day.
SYNCED_UID_CAP = 20_000


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent),
                               prefix=f".{STATE_FILE.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Credentials (Keychain)
# ---------------------------------------------------------------------------

def _keychain_copy_password(service: str, account: str, keychain=None, *, interactive: bool = False) -> Optional[str]:
    """Normal file-based Keychain API; no subprocess, export or logging.

    Noninteractive by default. Only the explicit local-owner authorization
    command requests normal macOS interaction; no environment flag enables it.

    The optional Keychain reference exists for isolated synthetic fixtures.
    Production uses the existing user search list and exact Bridge service/account.
    """
    import ctypes
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    security.SecKeychainGetUserInteractionAllowed.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
    security.SecKeychainGetUserInteractionAllowed.restype = ctypes.c_int32
    security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
    security.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    previous = ctypes.c_ubyte()
    if security.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)) != 0:
        raise RuntimeError("Keychain interaction status unavailable")
    if security.SecKeychainSetUserInteractionAllowed(interactive) != 0:
        raise RuntimeError("Keychain interaction mode unavailable")
    data = ctypes.c_void_p()
    length = ctypes.c_uint32()
    try:
        encoded_service, encoded_account = service.encode("utf-8"), account.encode("utf-8")
        status = security.SecKeychainFindGenericPassword(keychain, len(encoded_service), encoded_service, len(encoded_account), encoded_account, ctypes.byref(length), ctypes.byref(data), None)
        if status == -25300:  # errSecItemNotFound
            return None
        if status != 0:
            # Preserve permission/session failures; never mistake them for no item.
            raise RuntimeError("Keychain Bridge lookup unavailable (OSStatus %d)" % status)
        try:
            return ctypes.string_at(data, length.value).decode("utf-8") or None
        except UnicodeDecodeError:
            raise RuntimeError("Keychain Bridge encoding unavailable") from None
    finally:
        if data.value:
            security.SecKeychainItemFreeContent(None, data)
        security.SecKeychainSetUserInteractionAllowed(previous.value)


def keychain_read(account: str = ACCOUNT) -> Optional[str]:
    """Read only the existing Bridge item using macOS's normal application API."""
    if os.environ.get("OMNI_SANDBOX") in {"reader", "approved"}:
        return _keychain_copy_password(KEYCHAIN_SERVICE, account)
    # Existing unsandboxed sync/setup jobs retain their established security ACL.
    # This is application authentication, not a credential-printing CLI surface.
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Bridge application authentication unavailable") from None
    return result.stdout.strip() or None if result.returncode == 0 else None


def keychain_write(password: str, account: str = ACCOUNT) -> None:
    """Store (or replace) the Bridge password.

    Delegates to buzz_agents.write_secret, which passes the value on STDIN.
    `-w <value>` would put the password in argv, where any process running as
    this user can read it out of `ps` — that form once exposed every Buzz nsec
    on this machine. `-T /usr/bin/security` is the ACL that lets the launchd
    job read it back without a GUI prompt.
    """
    from buzz_agents import write_secret

    try:
        write_secret(KEYCHAIN_SERVICE, account, password)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"keychain write failed: {(exc.stderr or '').strip()}") from exc


# ---------------------------------------------------------------------------
# Connection (STARTTLS + certificate pinning)
# ---------------------------------------------------------------------------

class BridgeError(RuntimeError):
    pass


def _fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def connect(pin: bool = False, timeout: int = 30, *, require_pin: bool = False) -> tuple[imaplib.IMAP4, str]:
    """Open a STARTTLS'd IMAP connection to Bridge. Returns (conn, fingerprint).

    Bridge's certificate is self-signed and regenerated per install, so normal
    CA validation is meaningless here. We instead trust-on-first-use: `setup`
    records the fingerprint, and every run after that refuses to talk to a
    listener presenting anything else.
    """
    try:
        conn = imaplib.IMAP4(HOST, PORT, timeout=timeout)
    except (OSError, socket.timeout) as exc:
        raise BridgeError(
            f"cannot reach Proton Bridge at {HOST}:{PORT} ({exc}). "
            "Is Proton Mail Bridge running?"
        ) from exc

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # pinned below instead
    try:
        conn.starttls(ssl_context=ctx)
    except Exception as exc:
        raise BridgeError(f"STARTTLS to Bridge failed: {exc}") from exc

    der = conn.sock.getpeercert(binary_form=True)
    fp = _fingerprint(der) if der else ""

    state = load_state()
    pinned = state.get("certFingerprint")
    if require_pin and not pinned:
        try:
            conn.logout()
        except Exception:
            pass
        raise BridgeError("Bridge TLS pin missing; run setup before read-only use")
    if pin or not pinned:
        state["certFingerprint"] = fp
        state["certPinnedAt"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    elif fp != pinned:
        try:
            conn.logout()
        except Exception:
            pass
        raise BridgeError(
            "Bridge TLS fingerprint changed.\n"
            f"  pinned: {pinned[:24]}…\n"
            f"  seen:   {fp[:24]}…\n"
            "If you reinstalled Bridge, re-pin with:\n"
            "  python3 scripts/proton_mail.py setup --repin\n"
            "If you did not, something else is listening on "
            f"{HOST}:{PORT} — do not enter the password."
        )
    return conn, fp


def login(conn: imaplib.IMAP4, account: str = ACCOUNT,
          password: Optional[str] = None) -> None:
    if password is None:
        try:
            password = keychain_read(account)
        except RuntimeError as exc:
            raise BridgeError(str(exc)) from None
    if not password:
        raise BridgeError(
            f"no Bridge password in the Keychain for {account}. "
            "Run: python3 scripts/proton_mail.py setup"
        )
    try:
        conn.login(account, password)
    except imaplib.IMAP4.error as exc:
        # Never echo the password or the raw server line, which can embed it.
        raise BridgeError(
            f"Bridge rejected the login for {account}. The Bridge password is "
            "per-account and changes if you re-add the account in Bridge — "
            "copy the current one from Bridge → account → Mailbox details, "
            "then re-run: python3 scripts/proton_mail.py setup"
        ) from exc


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


_HTML_BLOCK = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_HTML_BR = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</tr>")
_HTML_TAG = re.compile(r"(?s)<[^>]+>")
_WS_RUNS = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    text = _HTML_BLOCK.sub(" ", markup)
    text = _HTML_BR.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Table-based marketing HTML leaves hundreds of whitespace-only lines that
    # survive a naive blank-run collapse. Strip each line first.
    text = "\n".join(line.strip() for line in text.splitlines())
    return _WS_RUNS.sub("\n\n", text).strip()


def extract_body(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to a stripped text/html part."""
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain if ctype == "text/plain" else html).append(text)

    body = "\n\n".join(plain).strip() or html_to_text("\n\n".join(html))
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n[… truncated …]"
    return body


def attachment_names(msg: email.message.Message) -> list[str]:
    names = []
    for part in msg.walk():
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition or part.get_filename():
            name = _decode(part.get_filename())
            if name:
                names.append(name)
    return names


def extract_attachment(raw: bytes, name: str) -> bytes:
    """Return the decoded payload of the one attachment named exactly `name`."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    matches = []
    for part in msg.walk():
        if _decode(part.get_filename()) != name:
            continue
        payload = part.get_payload(decode=True)
        if payload is not None:
            matches.append(payload)
    if not matches:
        raise ValueError(f"attachment not found: {name}")
    if len(matches) > 1:
        raise ValueError(f"attachment name is ambiguous: {name}")
    return matches[0]


_INTERNAL_ID = "@protonmail.internalid"


def thread_root(msg: email.message.Message) -> str:
    """Best-effort thread key: the root of the References chain, else the
    In-Reply-To parent, else this message's own id. Proton has no Gmail-style
    threadId over IMAP, so this stands in for it.

    Proton's own @protonmail.internalid references are skipped, because they
    are per-message, not per-thread. Proton INJECTS one onto messages that
    arrived with no References header at all, so refs[0] on a thread-starter
    was its internalid — a second, uglier messageId. That made threadId unique
    per message (302 distinct ids across 310 notes), so replies never grouped
    with their starter, and worse: Proton STRIPS supplied internalid refs from
    outbound mail, so sent_thread_ids() never contains one and
    inbound_triage.check_for_replies could never match a reply back to the
    item it answered — every thread-starter stayed 'surfaced' forever.
    """
    refs = [r for r in (msg.get("References") or "").split()
            if _INTERNAL_ID not in r.lower()]
    if refs:
        return refs[0].strip("<>")
    irt = (msg.get("In-Reply-To") or "").strip()
    if irt and _INTERNAL_ID not in irt.lower():
        return irt.strip("<>")
    return (msg.get("Message-ID") or "").strip().strip("<>")


# ---------------------------------------------------------------------------
# Header-derived labels
# ---------------------------------------------------------------------------
#
# Why headers instead of sender substrings
# ----------------------------------------
# The downstream classifier (Prism's `message-classify` structured skill) reads
# `metadata.labels` twice: once as a deterministic shortcut that assigns an
# importance with NO model call, and once as a hint inside the model prompt.
# Gmail supplied CATEGORY_PROMOTIONS / CATEGORY_SOCIAL for that; Proton over
# IMAP supplies nothing, so we derive the equivalent here.
#
# Everything below keys off headers the *sender's own infrastructure* stamps —
# List-Unsubscribe, List-Id, Precedence, Auto-Submitted, and the ESP campaign
# category (Mailgun's X-Mailgun-Tag, Amazon SES's Feedback-ID `cat-…` field).
# That beats matching on sender strings for three reasons:
#   1. It is declarative. A sender that sets List-Unsubscribe is *telling* us
#      the message is bulk; `@substack.com` only tells us who sent it, and
#      Substack sends both "X liked your post" and real newsletter posts.
#   2. It survives rebranding, new subdomains, and senders we have never seen —
#      a denylist of marketing domains is stale the day it is written.
#   3. It fails in the safe direction: no header, no label, model decides.
# Sender/subject shape is used only for SOCIAL and PROMOTIONS, which have no
# standard header — and even then only *in conjunction with* a bulk header.
#
# The bar for a deterministic label is high: a wrong one skips the model
# entirely, so a message that looks operational (money, travel, an order, an
# account or security notice) is never labelled SOCIAL or PROMOTIONS even when
# it arrives over marketing plumbing. Airlines send flight-delay notices with a
# List-Unsubscribe header; that message is not promotional noise.

# --- bulk / automation (declared by the sender's mail infrastructure) -------
_BULK_HEADERS = ("List-Unsubscribe", "List-Id", "List-Post", "List-Help")
_BULK_PRECEDENCE = re.compile(r"(?i)^\s*(bulk|list|junk)\s*$")
_AUTO_HEADERS = ("X-Auto-Response-Suppress", "X-Autoreply", "X-Autorespond")
_AUTO_SUBMITTED = re.compile(r"(?i)^\s*auto-(generated|replied|notified)")
_AUTO_PRECEDENCE = re.compile(r"(?i)^\s*auto[_-]?reply\s*$")

# ESP headers that mark a message as sent by a campaign/notification platform
# rather than typed by a person. Used to veto DIRECT, never to label on its own.
_ESP_HEADERS = ("Feedback-ID", "Feedback-Id", "X-Mailgun-Tag", "X-SES-Outgoing",
                "X-Ses-Outgoing", "X-Campaign", "X-CSA-Complaints",
                "X-Complaints-To", "X-Entity-Ref-ID")

# Localparts no human types from. Anything here is machine-sent even when the
# display name is a person's ("Sam Rivera (via Google Docs)").
_NOREPLY_LOCALPART = re.compile(
    r"(?i)^(no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|notifications?|alerts?|"
    r"mailer|mail|bounce\w*|postmaster|automated|auto|system|admin|webmaster|"
    r"billing|invoice|invoices|receipts?|statements?|payments?|failed-payments|"
    r"support|service|help|updates?|news|newsletters?|marketing|deals|offers|"
    r"promo\w*|sales|info|hello|team|welcome|drive-shares[-\w]*|reaction|forum|"
    r"reply-[0-9a-f]{6,})\b")
_VIA_SENDER = re.compile(r"(?i)\(via [^)]+\)\s*$")

# A VERP / bounce-tracking Return-Path is the tell of a sending platform even
# when From looks like a colleague ("Fred from Fireflies.ai" <fred@fireflies.ai>
# returning to bounces+1696684-a48a-owner=example.com@send.example.net).
# Mail a person actually sent returns to that person's own address.
_BOUNCE_LOCALPART = re.compile(r"(?i)^(bounce\w*|msprvs\d*|prvs|return|reject)\b")

# --- ESP campaign categories -----------------------------------------------
# Mailgun's X-Mailgun-Tag and SES's Feedback-ID both carry the sender's own name
# for the campaign that produced the message. Substack — roughly half this
# inbox — labels every notification this way (`post-reaction`, `comment`,
# `follow-notification`, `subscription-notification`), which is a far cleaner
# signal than parsing "Someone liked your post" out of a subject line.
_SOCIAL_CATEGORY = re.compile(
    r"(?i)(reaction|comment|follow|mention|subscription-notification|"
    r"free-welcome|chat-thread|messages?-request|live-stream|recommendation)")
_PROMO_CATEGORY = re.compile(
    r"(?i)(expiry|unfinished-subscription|upsell|promo|offer|winback)")

# --- social platforms -------------------------------------------------------
# Only consulted for senders that already declared themselves bulk, and only
# together with a social subject shape.
_SOCIAL_DOMAIN = re.compile(
    r"(?i)(^|\.)(substack\.com|linkedin\.com|facebookmail\.com|x\.com|twitter\.com|"
    r"instagram\.com|redditmail\.com|medium\.com|mastodon\.\w+|meetup\.com|"
    r"pinterest\.com|quora\.com|nextdoor\.com|tiktok\.com|youtube\.com|"
    r"discourse\.\w+|hylo\.com)$")
_SOCIAL_SUBJECT = re.compile(
    r"(?i)(\bliked\b|\breacted to\b|\bfollowed you\b|new follower|"
    r"mentioned you|new comment on|replied to your|new (free|paid) subscriber|"
    r"started following|viewed your profile|endorsed you|invited you to connect|"
    r"new message request|new thread from|is now following)")

# --- chat / collaboration platforms -----------------------------------------
# A team-chat notification ("Casey sent you messages", "You've got 2 unread
# messages") is a POINTER to a conversation living somewhere else — the ask, if
# there is one, is answered in Slack, not by replying to the email. The owner's
# rubric has always called these informational, but the model kept promoting
# them to action-required whenever the digest quoted a genuine request in its
# body ("could you find me tomorrow…"). It was reading the quoted conversation
# as if it had been addressed to it.
#
# The platform is knowable from the envelope, so this doesn't need a model at
# all. Matched against the From domain AND the Return-Path domain, because
# Slack sends From notification@slack.com but bounces to mail.slack.com, and
# Discord/Teams do the equivalent. Kept to platforms whose email is *only* ever
# a notification about messages elsewhere — deliberately NOT Substack (whose
# "new comment" mail is social noise, already handled) and NOT the generic
# Google notification domains (accounts.google.com sends security alerts from
# notifications.google.com, which must stay with the model).
_CHAT_DOMAIN = re.compile(
    r"(?i)(^|\.)(slack\.com|slack-mail\.com|discord\.com|discordapp\.com|"
    r"teams\.microsoft\.com|chat\.google\.com|zulipchat\.com|mattermost\.com|"
    r"twist\.com|flock\.com|rocket\.chat)$")

# --- promotional shape ------------------------------------------------------
# Marketing mail is sent from infrastructure named for marketing. These match
# the From localpart and the *first* domain label, both of which the sender
# chose to segregate campaign traffic — e.g. deals@mkt.airline.example.
# Deliberately excludes generic mail subdomains (email., mail., e., em1.):
# newsletters, receipts, and flight alerts all ship from those too.
_PROMO_LOCALPART = re.compile(
    r"(?i)^(deals?|promo\w*|offers?|marketing|sales|store|shop|campaigns?)\b")
_PROMO_SUBDOMAIN = re.compile(
    r"(?i)^(mkt|marketing|promo\w*|deals?|campaigns?|offers?)$")

# --- operational content (never noise) --------------------------------------
# TRANSACTIONAL is emitted as a label; OPERATIONAL only vetoes the SOCIAL and
# PROMOTIONS shortcuts. Both are matched on the subject, never the body: bodies
# are full of marketing footers that mention "order" and "account".
_TRANSACTIONAL_SUBJECT = re.compile(
    r"(?i)(\breceipt\b|\binvoice\b|\bpayment\b|\bpayout\b|\brefund\b|\bbilling\b|"
    r"\bcharged\b|\bunsuccessful\b|\bpast due\b|\boverdue\b|"
    r"\border\s*(#|confirm|shipp|status)|\bshipped\b|\bdelivery\b|\btracking number\b|"
    r"\bitinerary\b|\bbooking\b|\breservation\b|\bconfirmation (code|number)\b|"
    r"\bboarding pass\b|\bcheck[- ]?in\b|\bflight\b)")
# A bare "$50" means nothing — "Win $5,000 for your local school" is marketing.
# Money only reads as transactional next to a transaction word, and those words
# are already in the pattern above.
#
# "your trip" was here and has been removed: it is travel-marketing VOICE, not a
# travel record. It fired only on "Are you ready for your trip to Lisbon?" from
# deals@mkt.airline.example — a seat-upgrade pitch — and the resulting
# TRANSACTIONAL label vetoed the PROMOTIONS shortcut that the sender's own
# campaign infrastructure (deals@ + mkt.) had already earned, sending a pure
# marketing mail to the model instead. Real trip mail identifies itself by
# record: itinerary, booking, reservation, boarding pass, check-in, flight —
# all still matched above, including "Your Upcoming Trip to Lisbon", which
# arrives as a Booking Confirmation.
_OPERATIONAL_SUBJECT = re.compile(
    r"(?i)(\bpassword\b|\bsign[- ]?in\b|\bsigned in\b|\blogin\b|\blog in\b|"
    r"\bsecurity\b|\bverif\w+\b|\bauthentication\b|\bone[- ]time code\b|"
    r"\b2fa\b|\bpasskey\b|\baccount (is|was|has|access|suspend|clos|paus)|"
    r"\bsuspend\w*\b|\bpaused\b|\bexpir\w+\b|\bdeadline\b|\bmaintenance\b|"
    r"\boutage\b|\bdelayed\b|\bcancel\w*\b|\bdelet\w+\b|\bmigrat\w+\b)")


def _header(msg: email.message.Message, name: str) -> str:
    try:
        return (msg.get(name) or "").strip()
    except Exception:
        return ""


def _sender_address(msg: email.message.Message) -> tuple[str, str]:
    """(localpart, domain) of the From address, lowercased. ('','') if absent."""
    addrs = getaddresses([msg.get("From") or ""])
    addr = next((a for _, a in addrs if "@" in a), "")
    local, _, domain = addr.lower().partition("@")
    return local, domain


def _esp_category(msg: email.message.Message) -> str:
    """The sender's own name for the campaign that produced this message.

    Mailgun puts it in X-Mailgun-Tag verbatim; SES puts it in Feedback-ID as
    the `cat-<name>` field of a colon-delimited tuple.
    """
    tag = _header(msg, "X-Mailgun-Tag")
    if tag:
        return tag
    fb = _header(msg, "Feedback-ID") or _header(msg, "Feedback-Id")
    match = re.search(r"cat-([\w.-]+)", fb)
    return match.group(1) if match else ""


def derive_labels(msg: email.message.Message, mailbox: str,
                  is_unread: bool) -> list[str]:
    """Classify one message into coarse source labels from its headers.

    Returns the Proton folder it came from (the analogue of a Gmail label),
    UNREAD when applicable, and zero or more of:

      BULK           sender declared list/bulk mail (List-*, Precedence)
      AUTOMATED      machine-generated (Auto-Submitted, auto-response headers)
      SOCIAL         reaction / follow / comment / subscriber notification
      CHAT           chat-platform notification about messages living elsewhere
      PROMOTIONS     marketing campaign
      TRANSACTIONAL  receipt, invoice, payment, order, booking, travel
      DIRECT         a person typed this to the owner (no bulk/ESP plumbing)

    SOCIAL, CHAT and PROMOTIONS are the ones wired to a shortcut downstream, so
    they are deliberately the hardest to earn: bulk plumbing AND a social or
    promotional shape AND nothing operational in the subject (CHAT instead
    needs a known platform in the envelope, which no other sender can forge
    into being).
    """
    labels: list[str] = [mailbox.upper()]
    if is_unread:
        labels.append("UNREAD")

    subject = _decode(msg.get("Subject"))
    local, domain = _sender_address(msg)
    category = _esp_category(msg)

    return_path = _header(msg, "Return-Path").strip("<>").lower()
    rp_local, _, rp_domain = return_path.partition("@")

    bulk = (any(_header(msg, h) for h in _BULK_HEADERS)
            or bool(_BULK_PRECEDENCE.match(_header(msg, "Precedence"))))
    automated = (bool(_AUTO_SUBMITTED.match(_header(msg, "Auto-Submitted")))
                 or bool(_AUTO_PRECEDENCE.match(_header(msg, "Precedence")))
                 or any(_header(msg, h) for h in _AUTO_HEADERS))
    esp = any(_header(msg, h) for h in _ESP_HEADERS)
    verp = bool(rp_local) and (
        "=" in rp_local
        or bool(_BOUNCE_LOCALPART.match(rp_local))
        or "bounce" in rp_domain.split("."))
    machine_sender = (bool(_NOREPLY_LOCALPART.match(local))
                      or bool(_VIA_SENDER.search(_decode(msg.get("From"))))
                      or verp)

    if bulk:
        labels.append("BULK")
    if automated:
        labels.append("AUTOMATED")

    transactional = bool(_TRANSACTIONAL_SUBJECT.search(subject))
    if transactional:
        labels.append("TRANSACTIONAL")

    # Operational subjects are exempt from the noise shortcuts even when they
    # arrive over bulk plumbing — "Your flight to LIS is delayed" is not junk.
    operational = transactional or bool(_OPERATIONAL_SUBJECT.search(subject))

    # A chat platform in the envelope identifies a digest of messages that
    # live elsewhere — but it must still yield to an operational subject.
    # Slack, Discord and Teams send one-time sign-in codes, password resets
    # and new-device alerts from the SAME domains as their digests, and CHAT
    # is wired to a `informational` shortcut that returns before the model is
    # ever called. Without this veto a time-boxed login code could never be
    # classified action-required no matter what the prompt said.
    if not operational and (_CHAT_DOMAIN.search(domain)
                            or _CHAT_DOMAIN.search(rp_domain)):
        labels.append("CHAT")

    if bulk and not operational:
        social = (bool(_SOCIAL_CATEGORY.search(category))
                  or (bool(_SOCIAL_DOMAIN.search(domain))
                      and bool(_SOCIAL_SUBJECT.search(subject))))
        if social:
            labels.append("SOCIAL")
        elif (_PROMO_CATEGORY.search(category)
              or _PROMO_LOCALPART.match(local)
              or _PROMO_SUBDOMAIN.match(domain.split(".")[0])):
            labels.append("PROMOTIONS")

    # No list plumbing, no ESP, no robot localpart: a human wrote this. The
    # model needs to know, because the owner's genuinely actionable mail —
    # invoices from collaborators, logistics questions — looks unremarkable
    # next to the shouting of the marketing that surrounds it.
    if not (bulk or automated or esp or machine_sender):
        labels.append("DIRECT")

    return labels


# ONE DOOR (review R2-1): a Prism sign-in email carries a credential (a magic-link or
# device-approval token). It must never reach an agent through `search` output or the
# vault through `sync` — whoever reads it could sign in as the owner.
_SIGNIN_LINK = re.compile(r"/auth/(callback|device)[^\s\"'<>]*[?&](token|code)=", re.IGNORECASE)
_SIGNIN_SUBJECT = re.compile(r"sign[- ]?in|log[- ]?in|new device", re.IGNORECASE)


def _magic_from_address() -> str:
    """Prism's MAGIC_FROM sender, if this repo's .env names it (PRISM_MAGIC_FROM). The
    link pattern below catches every sign-in mail on its own; this only adds the sender."""
    try:
        for line in (PROJECT_ROOT / ".env").read_text().splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() == "PRISM_MAGIC_FROM":
                return (parseaddr(val.strip().strip("'\""))[1] or "").lower()
    except OSError:
        pass
    return ""


def is_prism_signin_mail(m: dict) -> bool:
    sender = (parseaddr(m.get("from") or "")[1] or "").lower()
    ours = _magic_from_address()
    if ours and sender == ours and _SIGNIN_SUBJECT.search(m.get("subject") or ""):
        return True
    return bool(_SIGNIN_LINK.search(m.get("body") or ""))


def parse_message(raw: bytes, flags: str, mailbox: str, uid: int) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    subject = _decode(msg.get("Subject")) or "(no subject)"
    sender = _decode(msg.get("From"))
    to = ", ".join(a for _, a in getaddresses([msg.get("To") or ""]) if a)
    cc = ", ".join(a for _, a in getaddresses([msg.get("Cc") or ""]) if a)
    message_id = (msg.get("Message-ID") or "").strip().strip("<>")

    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)

    is_unread = "\\Seen" not in flags

    return {
        "uid": uid,
        "mailbox": mailbox,
        "labels": derive_labels(msg, mailbox, is_unread),
        "subject": subject,
        "from": sender,
        "to": to,
        "cc": cc,
        "messageId": message_id or f"{mailbox}-uid-{uid}",
        "threadId": thread_root(msg) or f"{mailbox}-uid-{uid}",
        "date": dt,
        "isUnread": is_unread,
        "isFlagged": "\\Flagged" in flags,
        "body": extract_body(msg),
        "attachments": attachment_names(msg),
    }


_HREF_RE = re.compile(r'''(?is)href\s*=\s*["'](https?://[^"']+)["']''')
_TEXT_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def extract_links(raw: bytes) -> list[str]:
    """Return deduplicated HTTP(S) links without exposing message source or credentials."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    found: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        try:
            text = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            if isinstance(payload, bytes):
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            else:
                text = str(payload)
        candidates = _HREF_RE.findall(text) + _TEXT_URL_RE.findall(text)
        for candidate in candidates:
            url = html.unescape(candidate).rstrip(".,);]")
            if url not in found:
                found.append(url)
    return found


def message_payload(raw: bytes, flags: str, mailbox: str, uid: int) -> dict:
    """JSON-safe exact-message view for a known UID."""
    message = parse_message(raw, flags, mailbox, uid)
    return {
        "uid": uid,
        "mailbox": mailbox,
        "date": message["date"].astimezone().strftime("%Y-%m-%d %H:%M"),
        "from": message["from"],
        "to": message["to"],
        "cc": message["cc"],
        "subject": message["subject"],
        "unread": message["isUnread"],
        "body": message["body"],
        "links": extract_links(raw),
        "attachments": message["attachments"],
    }


# ---------------------------------------------------------------------------
# Parachute write
# ---------------------------------------------------------------------------

def note_content(m: dict) -> str:
    header = [f"# {m['subject']}", ""]
    header.append(f"**From:** {m['from']}  ")
    if m["to"]:
        header.append(f"**To:** {m['to']}  ")
    if m["cc"]:
        header.append(f"**Cc:** {m['cc']}  ")
    header.append(f"**Date:** {m['date'].astimezone().strftime('%Y-%m-%d %H:%M')}  ")
    if m["attachments"]:
        header.append(f"**Attachments:** {', '.join(m['attachments'])}  ")
    header += ["", "---", "", m["body"]]
    return "\n".join(header)


def note_path(m: dict) -> str:
    """Deterministic path. Subject slugs collide constantly ("Re: ..." chains,
    recurring invites), so a short message-id hash disambiguates."""
    from parachute_writer import slugify

    slug = slugify(m["subject"])[:80] or "no-subject"
    digest = hashlib.sha256(m["messageId"].encode()).hexdigest()[:8]
    return f"{NOTE_DIR}/{slug}-{digest}"


def upsert_email_note(client, m: dict) -> tuple[str, bool]:
    """Write one message as an `email`-tagged note, matching the schema the
    old Prism Gmail sync used so /email, triage, and the missions pipeline
    keep working unchanged. Returns (path, created)."""
    path = note_path(m)
    metadata = {
        "type": "email",
        "subject": m["subject"],
        "from": m["from"],
        "to": m["to"],
        # Cc from the headers ("" when none): Omni's reply check allows from + to + cc.
        "cc": m["cc"],
        "date": m["date"].astimezone().strftime("%Y-%m-%d %H:%M"),
        "isUnread": m["isUnread"],
        # Folder + read state + the header-derived source labels the triage
        # classifier keys its shortcuts off (see derive_labels).
        "labels": m.get("labels") or (
            [m["mailbox"].upper()] + (["UNREAD"] if m["isUnread"] else [])),
        "messageCount": 1,
        "threadId": m["threadId"],
        "messageId": m["messageId"],
        "lastMessageAt": int(m["date"].timestamp() * 1000),
        "source": SOURCE,
        "account": ACCOUNT,
        "mailbox": m["mailbox"],
        "uid": m["uid"],
    }

    existing = client.get_note(path, include_content=False)
    if existing:
        # Content too, not just metadata: a re-sync after a parser fix should
        # heal notes written by the older parser.
        client.update_note(existing.id, content=note_content(m), metadata=metadata)
        return path, False

    client.create_note(
        content=note_content(m),
        path=path,
        tags=["email"],
        metadata=metadata,
    )
    return path, True


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def _uidvalidity(conn: imaplib.IMAP4, mailbox: str) -> int:
    typ, data = conn.status(f'"{mailbox}"', "(UIDVALIDITY)")
    if typ != "OK" or not data:
        return 0
    match = re.search(rb"UIDVALIDITY (\d+)", data[0])
    return int(match.group(1)) if match else 0


def _search_uids(conn: imaplib.IMAP4, criteria: list[str]) -> list[int]:
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return sorted(int(x) for x in data[0].split())


_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")



#: Mailbox names reach IMAP as a quoted string built here. Anything outside this set (a quote, a backslash,
#: CR/LF) could close the quote and append a command of its own (e.g. STORE \\Deleted on a read-write
#: SELECT) — imaplib on the agent host's Python 3.9 does not refuse CR/LF itself. Bridge's folders fit this set.
_MAILBOX_RE = re.compile(r"^[A-Za-z0-9 _./&+-]{1,100}$")


def _quoted_mailbox(name: str) -> str:
    if not isinstance(name, str) or not _MAILBOX_RE.match(name):
        raise BridgeError("refusing an unusual mailbox name")
    return '"%s"' % name


def _fetch_message(conn: imaplib.IMAP4, uid: int) -> Optional[tuple[bytes, str]]:
    """BODY.PEEK — fetching must never set \\Seen on the owner's mail."""
    typ, data = conn.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
    if typ != "OK" or not data:
        return None
    raw, flags = None, ""
    for item in data:
        if isinstance(item, tuple):
            meta, payload = item
            raw = payload
            match = _FLAGS_RE.search(meta)
            if match:
                flags = match.group(1).decode(errors="replace")
        elif isinstance(item, bytes):
            match = _FLAGS_RE.search(item)
            if match and not flags:
                flags = match.group(1).decode(errors="replace")
    return (raw, flags) if raw else None


def _fetch_flags(conn: imaplib.IMAP4, uids: Iterable[int]) -> dict[int, str]:
    uids = list(uids)
    if not uids:
        return {}
    out: dict[int, str] = {}
    typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), "(FLAGS)")
    if typ != "OK":
        return out
    for item in data or []:
        blob = item[0] if isinstance(item, tuple) else item
        if not isinstance(blob, bytes):
            continue
        uid_match = re.search(rb"UID (\d+)", blob)
        flag_match = _FLAGS_RE.search(blob)
        if uid_match and flag_match:
            out[int(uid_match.group(1))] = flag_match.group(1).decode(errors="replace")
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    print(f"Proton Bridge IMAP: {HOST}:{PORT}   account: {ACCOUNT}")
    conn, fp = connect(pin=True)
    print(f"Connected. TLS fingerprint pinned: {fp[:32]}…")

    existing = keychain_read()
    if existing and not args.repin and not args.force:
        print("A Bridge password is already in the Keychain — verifying it.")
        password = existing
    else:
        if args.repin and existing:
            password = existing
            print("Re-pinned the certificate; reusing the stored password.")
        else:
            print("\nPaste the Bridge password (Bridge → your account → "
                  "Mailbox details → Password).")
            print("It is not echoed, not logged, and goes straight to the Keychain.")
            password = getpass.getpass("Bridge password: ").strip()
            if not password:
                print("No password entered — aborting.", file=sys.stderr)
                return 1

    try:
        login(conn, password=password)
    except BridgeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    typ, boxes = conn.list()
    names = []
    for b in boxes or []:
        match = re.search(rb'"?([^"]+)"?$', b)
        if match:
            names.append(match.group(1).decode(errors="replace"))
    conn.logout()

    if password != existing:
        keychain_write(password)
        if keychain_read() != password:
            print("Keychain readback mismatch — refusing to continue.", file=sys.stderr)
            return 1
        print(f'Password stored in Keychain (service "{KEYCHAIN_SERVICE}").')

    print("\nLogin OK. Mailboxes visible to the agent:")
    for n in names:
        print(f"  {n}")
    print("\nNext: python3 scripts/proton_mail.py sync --since-days 7")
    return 0


def cmd_sync(args) -> int:
    from parachute_client import ParachuteClient

    state = load_state()
    mailboxes = args.mailbox or DEFAULT_MAILBOXES
    client = ParachuteClient(timeout=30)

    conn, _ = connect()
    try:
        login(conn)
        created_total = updated_total = 0

        for mailbox in mailboxes:
            box_state = state.setdefault("mailboxes", {}).setdefault(mailbox, {})
            uidvalidity = _uidvalidity(conn, mailbox)
            typ, _ = conn.select(_quoted_mailbox(mailbox), readonly=True)
            if typ != "OK":
                print(f"  {mailbox}: cannot select — skipped", file=sys.stderr)
                continue

            if box_state.get("uidValidity") not in (None, uidvalidity):
                # Bridge rebuilt its local store; old UIDs are meaningless.
                print(f"  {mailbox}: UIDVALIDITY changed — forgetting synced UIDs")
                box_state["syncedUids"] = []
                box_state["uidIndex"] = {}
            box_state["uidValidity"] = uidvalidity

            # Window-based, NOT a `lastUid+1:*` cursor. Bridge assigns UIDs as
            # it backfills, which runs newest-to-oldest — so a high-water mark
            # walks *backwards* through history instead of tracking new mail,
            # and would drag all ~10k archived messages into the vault. Asking
            # for a date window every run is correct under any UID ordering.
            since = (datetime.now(timezone.utc)
                     - timedelta(days=args.since_days)).strftime("%d-%b-%Y")
            window = _search_uids(conn, ["SINCE", since])
            box_state.pop("lastUid", None)  # retired high-water mark
            synced = set(box_state.get("syncedUids", []))
            synced |= {int(u) for u in box_state.get("uidIndex", {})}
            uids = [u for u in window if u not in synced]

            if args.max and len(uids) > args.max:
                print(f"  {mailbox}: {len(uids)} unsynced in window, taking the "
                      f"{args.max} newest (next run picks up the rest)")
                # Highest UIDs first. Backfill order is unreliable, but a
                # genuinely *arriving* message always gets the highest UID
                # yet issued — so this keeps new mail from starving behind a
                # backlog. The rest drains over the following runs.
                uids = sorted(uids, reverse=True)[:args.max]

            created = updated = 0
            uid_index = box_state.setdefault("uidIndex", {})
            for uid in uids:
                fetched = _fetch_message(conn, uid)
                if not fetched:
                    continue
                raw, flags = fetched
                m = parse_message(raw, flags, mailbox, uid)
                if is_prism_signin_mail(m):
                    synced.add(uid)  # never stored; never retried
                    continue
                try:
                    path, was_created = upsert_email_note(client, m)
                except Exception as exc:
                    print(f"  {mailbox}/{uid}: write failed — {exc}", file=sys.stderr)
                    continue
                uid_index[str(uid)] = path
                synced.add(uid)
                created += was_created
                updated += (not was_created)

            # Refresh read/unread on recently-synced messages so /email's
            # unread count doesn't drift after the owner reads things in Proton.
            refreshed = 0
            if args.refresh_flags and uid_index:
                recent = sorted((int(u) for u in uid_index), reverse=True)[:UID_INDEX_CAP]
                for uid, flags in _fetch_flags(conn, recent).items():
                    path = uid_index.get(str(uid))
                    if not path:
                        continue
                    unread = "\\Seen" not in flags
                    note = client.get_note(path, include_content=False)
                    if not note:
                        continue
                    if bool(note.metadata.get("isUnread")) != unread:
                        # UNREAD is also carried in labels (derive_labels), so
                        # both have to move together or the two disagree.
                        labels = [l for l in (note.metadata.get("labels") or [])
                                  if l != "UNREAD"]
                        if unread:
                            labels.insert(min(1, len(labels)), "UNREAD")
                        client.update_note(note.id, metadata={"isUnread": unread,
                                                              "labels": labels})
                        refreshed += 1

            # Keep both indexes bounded. syncedUids is the dedupe set, so it
            # must stay larger than any plausible date window.
            if len(uid_index) > UID_INDEX_CAP:
                keep = sorted((int(u) for u in uid_index), reverse=True)[:UID_INDEX_CAP]
                box_state["uidIndex"] = {str(u): uid_index[str(u)] for u in keep}
            box_state["syncedUids"] = sorted(synced)[-SYNCED_UID_CAP:]

            conn.close()
            created_total += created
            updated_total += updated
            print(f"  {mailbox}: {created} new, {updated} updated, "
                  f"{refreshed} flag changes ({len(synced)} known UIDs)")

        state["lastSync"] = datetime.now(timezone.utc).isoformat()
        state["lastSyncCreated"] = created_total
        save_state(state)
        print(f"\nSync complete: {created_total} new, {updated_total} updated.")
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def sent_thread_ids(days: int = 14, mailbox: str = "Sent", *, strict: bool = False, with_dates: bool = False):
    """Thread keys the owner has replied into recently.

    inbound_triage uses this to close the loop on surfaced items. It used to
    ask Gmail for `in:sent` on the owner's Proton address — a call that cannot succeed against a
    Proton mailbox, so reply-detection had been silently returning zero.

    Returns every message-id in each sent message's identity + References
    chain, so it matches whichever id an inbound note recorded as threadId.
    with_dates=True returns key -> latest UTC epoch-ms, from INTERNALDATE
    or an aware Date header. strict=True rejects partial or missing evidence.
    """
    conn, _ = connect(timeout=20, require_pin=True)
    try:
        login(conn)
        typ, _ = conn.select(_quoted_mailbox(mailbox), readonly=True)
        if typ != "OK":
            if strict:
                raise BridgeError("Sent mailbox unavailable")
            return set()
        since = (datetime.now(timezone.utc)
                 - timedelta(days=days)).strftime("%d-%b-%Y")
        if strict:
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            if typ != "OK" or not data or data[0] is None:
                raise BridgeError("Sent search unavailable")
            uids = sorted(int(x) for x in data[0].split())
        else:
            uids = _search_uids(conn, ["SINCE", since])
        if not uids:
            return {} if with_dates else set()
        if strict and len(uids) > 10000:
            raise BridgeError("Sent search exceeds safe bound")

        threads: set[str] = set()
        dates: dict[str, int] = {}
        typ, data = conn.uid(
            "FETCH", ",".join(str(u) for u in uids),
            "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES DATE)])",
        )
        if typ != "OK":
            if strict:
                raise BridgeError("Sent headers unavailable")
            return set()
        if strict and sum(isinstance(item, tuple) and isinstance(item[1], bytes) for item in data or []) != len(uids):
            raise BridgeError("Sent headers incomplete")
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            headers = email.message_from_bytes(item[1], policy=email.policy.default)
            sent_at = None
            if with_dates:
                # INTERNALDATE is server evidence; Date is the fallback for older Bridge responses.
                match = re.search(rb'INTERNALDATE "([^"]+)"', item[0])
                raw_date = match.group(1).decode("ascii") if match else headers.get("Date")
                try:
                    parsed = email.utils.parsedate_to_datetime(raw_date)
                    if parsed.tzinfo is None:
                        raise ValueError("timezone missing")
                    sent_at = int(parsed.timestamp() * 1000)
                except (TypeError, ValueError, OverflowError):
                    raise BridgeError("Sent message date unavailable")
            message_threads = []
            for field in ("Message-ID", "In-Reply-To"):
                value = (headers.get(field) or "").strip().strip("<>")
                if value:
                    threads.add(value)
                    message_threads.append(value)
            for ref in (headers.get("References") or "").split():
                key = ref.strip("<>")
                threads.add(key)
                message_threads.append(key)
            if with_dates:
                for key in message_threads:
                    dates[key] = max(dates.get(key, 0), sent_at)
        return dates if with_dates else threads
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def bridge_process_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Proton Mail Bridge.app/Contents/MacOS/bridge"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def health() -> tuple[bool, str, dict]:
    """(healthy, message, detail). Used by check_oauth.py."""
    detail: dict[str, Any] = {"host": HOST, "port": PORT, "account": ACCOUNT}

    if not bridge_process_running():
        return False, ("Proton Mail Bridge is not running — open "
                       "/Applications/Proton Mail Bridge.app"), detail

    try:
        conn, fp = connect(timeout=15, require_pin=True)
    except BridgeError as exc:
        return False, str(exc), detail
    detail["certFingerprint"] = fp[:16]

    try:
        login(conn)
    except BridgeError as exc:
        try:
            conn.logout()
        except Exception:
            pass
        return False, str(exc), detail

    try:
        typ, data = conn.status('"INBOX"', "(MESSAGES UNSEEN)")
        if typ == "OK" and data:
            match = re.search(rb"MESSAGES (\d+).*UNSEEN (\d+)", data[0])
            if match:
                detail["messages"] = int(match.group(1))
                detail["unseen"] = int(match.group(2))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    state = load_state()
    last_sync = state.get("lastSync")
    detail["lastSync"] = last_sync
    if last_sync:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(last_sync)).total_seconds() / 3600
        detail["lastSyncAgeHours"] = round(age_h, 1)
        # The launchd job runs every 5 min; 6h of silence means it is wedged.
        if age_h > 6:
            return False, (f"Bridge reachable but no Parachute sync in "
                           f"{age_h:.0f}h — check "
                           "logs/proton-mail-stderr.log"), detail
    else:
        return False, "Bridge reachable but no sync has ever run", detail

    unseen = detail.get("unseen")
    return True, (f"Bridge OK ({unseen} unread in INBOX, synced "
                  f"{detail['lastSyncAgeHours']}h ago)"), detail


def cmd_authorize_keychain(args) -> int:
    """Explicit local owner application-auth flow; never an agent/SSH capability."""
    def result(healthy: bool, status: str, osstatus=None) -> int:
        body = {"healthy": healthy, "status": status}
        if osstatus is not None:
            body["keychainOSStatus"] = osstatus
        print(json.dumps(body))
        return 0 if healthy else 1
    if any(k in os.environ for k in ("OMNI_SANDBOX", "SSH_CONNECTION", "SSH_TTY")):
        return result(False, "local_owner_terminal_required")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return result(False, "local_owner_terminal_required")
    conn = None
    password = None
    try:
        # Never prompt before validating the existing TLS pin, and never create one.
        conn, _ = connect(timeout=15, require_pin=True)
        try:
            password = _keychain_copy_password(KEYCHAIN_SERVICE, ACCOUNT, interactive=True)
        except RuntimeError as exc:
            match = re.search(r"OSStatus (-?\d+)", str(exc))
            return result(False, "keychain_authorization_failed", int(match.group(1)) if match else None)
        if not password:
            return result(False, "existing_keychain_item_missing")
        login(conn, password=password)
        return result(True, "authenticated")
    except (BridgeError, OSError):
        return result(False, "bridge_authentication_unavailable")
    finally:
        password = None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def cmd_health(args) -> int:
    healthy, message, detail = health()
    if args.json:
        print(json.dumps({"healthy": healthy, "message": message, **detail}, indent=2))
    else:
        print(("OK: " if healthy else "FAILED: ") + message)
        for k, v in detail.items():
            print(f"  {k}: {v}")
    return 0 if healthy else 1


def cmd_search(args) -> int:
    """Ad-hoc read — the `gog gmail messages search` analogue for Proton."""
    conn, _ = connect(require_pin=True)
    try:
        login(conn)
        typ, _ = conn.select(_quoted_mailbox(args.mailbox), readonly=True)
        if typ != "OK":
            print(f"cannot select {args.mailbox}", file=sys.stderr)
            return 1

        criteria: list[str] = []
        if args.since_days:
            since = (datetime.now(timezone.utc)
                     - timedelta(days=args.since_days)).strftime("%d-%b-%Y")
            criteria += ["SINCE", since]
        if args.unread:
            criteria.append("UNSEEN")
        if args.query:
            # imaplib joins criteria with spaces, so an unquoted multi-word
            # query became separate tokens and the server rejected the second
            # word as an unknown search key ("proton_send test 1" -> BAD
            # unknown search key 'test'). IMAP quoting: wrap in double quotes,
            # backslash-escape any quote or backslash inside.
            escaped = args.query.replace("\\", "\\\\").replace('"', '\\"')
            criteria += ["TEXT", f'"{escaped}"']
        if not criteria:
            criteria = ["ALL"]

        uids = _search_uids(conn, criteria)[-args.max:]
        rows = []
        for uid in reversed(uids):
            fetched = _fetch_message(conn, uid)
            if not fetched:
                continue
            raw, flags = fetched
            m = parse_message(raw, flags, args.mailbox, uid)
            if is_prism_signin_mail(m):
                continue  # a sign-in credential: not shown to whoever runs search
            rows.append({
                "uid": uid,
                "date": m["date"].astimezone().strftime("%Y-%m-%d %H:%M"),
                "from": m["from"],
                "subject": m["subject"],
                "unread": m["isUnread"],
                "snippet": m["body"][:200].replace("\n", " "),
            })

        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                mark = "•" if r["unread"] else " "
                print(f"{mark} {r['date']}  {r['from'][:40]:40}  {r['subject'][:60]}")
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def cmd_message(args) -> int:
    """Read one exact message by UID without changing mailbox state."""
    conn, _ = connect(require_pin=True)
    try:
        login(conn)
        typ, _ = conn.select(_quoted_mailbox(args.mailbox), readonly=True)
        if typ != "OK":
            raise BridgeError(f"cannot select {args.mailbox}")
        fetched = _fetch_message(conn, args.uid)
        if not fetched:
            raise BridgeError(f"message UID not found: {args.uid}")
        raw, flags = fetched
        message = parse_message(raw, flags, args.mailbox, args.uid)
        raw_text = raw.decode("utf-8", errors="replace")
        if is_prism_signin_mail(message) or _SIGNIN_LINK.search(raw_text):
            raise BridgeError("refusing exact read for sign-in mail")
        print(json.dumps(message_payload(raw, flags, args.mailbox, args.uid), indent=2))
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def cmd_attachment(args) -> int:
    """Save one exact attachment without exposing credentials or changing mail state."""
    output = Path(args.output).expanduser()
    if output.exists() and not args.force:
        raise BridgeError(f"output already exists: {output} (pass --force to replace)")

    conn, _ = connect(require_pin=True)
    try:
        login(conn)
        typ, _ = conn.select(_quoted_mailbox(args.mailbox), readonly=True)
        if typ != "OK":
            raise BridgeError(f"cannot select {args.mailbox}")
        fetched = _fetch_message(conn, args.uid)
        if not fetched:
            raise BridgeError(f"message UID not found: {args.uid}")
        raw, flags = fetched
        message = parse_message(raw, flags, args.mailbox, args.uid)
        if is_prism_signin_mail(message):
            raise BridgeError("refusing attachment access for sign-in mail")
        try:
            payload = extract_attachment(raw, args.name)
        except ValueError as exc:
            raise BridgeError(str(exc)) from exc
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        print(json.dumps({"uid": args.uid, "name": args.name,
                          "output": str(output), "bytes": len(payload)}))
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="store Bridge password + pin TLS cert")
    p_setup.add_argument("--repin", action="store_true",
                         help="re-pin the certificate (after a Bridge reinstall)")
    p_setup.add_argument("--force", action="store_true",
                         help="replace the stored password")
    p_setup.set_defaults(func=cmd_setup)

    p_sync = sub.add_parser("sync", help="incremental fetch into Parachute")
    p_sync.add_argument("--mailbox", action="append",
                        help=f"mailbox to sync (repeatable; default {DEFAULT_MAILBOXES})")
    p_sync.add_argument("--since-days", type=int, default=7,
                        help="first-run lookback window (default 7)")
    p_sync.add_argument("--max", type=int, default=200,
                        help="max messages per mailbox per run (default 200)")
    p_sync.add_argument("--no-refresh-flags", dest="refresh_flags",
                        action="store_false",
                        help="skip refreshing read/unread on recent messages")
    p_sync.set_defaults(func=cmd_sync, refresh_flags=True)

    p_authorize = sub.add_parser("authorize-keychain", help="owner local Terminal: authorize existing Bridge item through macOS; never stores credentials")
    p_authorize.set_defaults(func=cmd_authorize_keychain)

    p_health = sub.add_parser("health", help="bridge + login + sync-recency probe")
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=cmd_health)

    p_search = sub.add_parser("search", help="ad-hoc IMAP search")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--mailbox", default="INBOX")
    p_search.add_argument("--max", type=int, default=20)
    p_search.add_argument("--since-days", type=int, default=14)
    p_search.add_argument("--unread", action="store_true")
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_message = sub.add_parser("message", help="read one exact message by UID")
    p_message.add_argument("--uid", type=int, required=True)
    p_message.add_argument("--mailbox", default="INBOX")
    p_message.set_defaults(func=cmd_message)

    p_attachment = sub.add_parser("attachment", help="save one attachment by message UID")
    p_attachment.add_argument("--uid", type=int, required=True)
    p_attachment.add_argument("--name", required=True)
    p_attachment.add_argument("--output", required=True)
    p_attachment.add_argument("--mailbox", default="INBOX")
    p_attachment.add_argument("--force", action="store_true")
    p_attachment.set_defaults(func=cmd_attachment)

    args = parser.parse_args()
    try:
        return args.func(args)
    except BridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
