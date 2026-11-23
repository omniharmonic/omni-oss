#!/usr/bin/env python3
"""
proton_send — send mail as the owner through the local Proton Mail Bridge.

The read side lives in scripts/proton_mail.py (IMAP on 127.0.0.1:1143). This is
its outbound twin: Bridge also serves SMTP on 127.0.0.1:1025, authenticates with
the same per-account Bridge password, and hands the message to Proton, which
encrypts it and files a copy in Sent. Nothing here talks to Proton's servers
directly and no message ever leaves the loopback interface in cleartext.

Safety posture — this file exists as much to REFUSE to send as to send:
  - ONE DOOR (2026-10-15): a real send needs a ONE-TIME SEND GRANT from the
    Prism server (scripts/send_grant.py). Prism's Omni executor mints it only
    after the owner approved this exact draft on a signed-in device; it is bound
    to the digest of exactly the recipients, subject and body this process was
    handed, lives ≤ 60 s and is consumed on first use. Without it the script
    exits non-zero with "Sends must be approved in Omni". `--approved` and the
    env switch below are no longer sufficient on their own.
  - `--approved` is mandatory. There is no code path to the SMTP socket that
    does not pass an explicit approved=True; the transport function re-asserts
    it. Outbound comms go through the approval gate (state/pending-approvals),
    exactly like matrix_reader.py's `send --approved`.
  - Recipients are pinned to owner@example.com. Reaching a third party
    needs BOTH `--allow-external` on the command line AND
    PROTON_SEND_ALLOW_EXTERNAL=1 in the environment or .env — the flag alone is
    an unauthenticated argv token and this repo runs `claude -p` sessions on
    attacker-authored email text, so "only the approval gate passes that flag"
    was a convention, not a guarantee.
  - Markdown is rejected outright. Gmail renders `**bold**` literally, and
    CLAUDE.md forbids it in outgoing mail; a body containing markdown is a
    drafting bug, not something to silently pass through.
  - The Bridge password comes from the macOS Keychain only (service
    "omni-proton-bridge"), is never logged, never in argv, never in
    .env. TLS is STARTTLS against Bridge's self-signed cert, pinned in
    state/proton-mail-state.json. Mismatch fails closed.
  - Bridge presents the SAME certificate on 1025 as on 1143, so the existing
    `certFingerprint` pin covers both. If that ever stops being true, pin the
    SMTP cert separately with `pin-smtp`.

Commands:
    send      Compose + send (needs --approved) or --dry-run to inspect
    pin-smtp  Re-pin the SMTP certificate (after a Bridge reinstall)
    --test    Offline self-check of the safety invariants

Typical use:
    python3 scripts/proton_send.py send --to owner@example.com \\
        --subject "Hello" --body-file draft.txt --dry-run
    python3 scripts/proton_send.py send --reply-to-note \\
        vault/messages/email/some-note-abc12345 --body-file reply.txt --approved
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import socket
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import send_grant  # noqa: E402  (the one-door check; stdlib only)

#: Config: PROTON_BRIDGE_USER, else OMNI_OWNER_EMAIL; PROTON_FROM_NAME, else OMNI_OWNER_NAME.
ACCOUNT = (os.environ.get("PROTON_BRIDGE_USER") or os.environ.get("OMNI_OWNER_EMAIL") or "").strip()
FROM_NAME = (os.environ.get("PROTON_FROM_NAME") or os.environ.get("OMNI_OWNER_NAME") or "").strip()
SMTP_HOST = os.environ.get("PROTON_BRIDGE_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("PROTON_BRIDGE_SMTP_PORT", "1025"))

# The only address this script may mail while outbound is still being trusted.
# Self-sends are the test channel; anything else needs --allow-external.
#: Config: OMNI_OWNER_EMAIL. Empty = there is no self-address, so every send needs --allow-external
#: and a grant (fails closed).
SELF_ADDRESS = os.environ.get("OMNI_OWNER_EMAIL", "").strip().lower()

MAX_BODY_CHARS = 100_000


class SendError(RuntimeError):
    """Anything that stops a send. Never carries the password or the body."""


class ApprovalRequired(SendError):
    """Raised when a send is attempted without explicit approval."""


# ---------------------------------------------------------------------------
# Markdown rejection
#
# Gmail (and most clients) render text/plain literally, so markdown that looks
# fine in a terminal arrives as `**Hi**` and reads as sloppy automation.
# CLAUDE.md: "NEVER use markdown in emails." We enforce it here rather than
# hoping every caller remembers.
# ---------------------------------------------------------------------------

_MARKDOWN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("bold (**text**)", re.compile(r"\*\*[^*\n]+\*\*")),
    ("bold/italic underscores (__text__)", re.compile(r"__[^_\n]+__")),
    ("heading (# Heading)", re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+\S")),
    ("link ([text](url))", re.compile(r"\[[^\]\n]{1,120}\]\([^)\n]{1,300}\)")),
    ("code fence (```)", re.compile(r"```")),
    ("inline code (`code`)", re.compile(r"`[^`\n]+`")),
    ("bullet list (- item)", re.compile(r"(?m)^[ \t]{0,3}[-*+][ \t]+\S")),
    ("blockquote (> quote)", re.compile(r"(?m)^[ \t]{0,3}>[ \t]+\S")),
]


def find_markdown(body: str) -> list[tuple[str, str]]:
    """Return [(what, offending snippet), ...]. Empty means the body is plain."""
    found: list[tuple[str, str]] = []
    for label, pattern in _MARKDOWN_PATTERNS:
        match = pattern.search(body)
        if match:
            # Show the offending line, not just the matched token — "- i" is a
            # useless thing to hand back to whoever has to fix the draft.
            start = body.rfind("\n", 0, match.start()) + 1
            end = body.find("\n", match.end())
            snippet = body[start:end if end != -1 else len(body)].strip()
            if len(snippet) > 60:
                snippet = snippet[:60] + "…"
            found.append((label, snippet))
    return found


def assert_plain_text(body: str) -> None:
    found = find_markdown(body)
    if not found:
        return
    lines = "\n".join(f"  - {label}: {snippet}" for label, snippet in found)
    raise SendError(
        "Refusing to send: the body contains markdown, which Gmail renders "
        "literally.\n" + lines + "\n"
        "Rewrite as plain text: no **bold**, no # headers, no [links](url), no "
        "backticks, no `- ` bullets. Use blank lines for sections, numbered "
        "lists (1. 2. 3.), and ALL CAPS for emphasis."
    )


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

def _addresses(value: Optional[str | Iterable[str]]) -> list[str]:
    """Normalize a str / list-of-str / comma list into bare email addresses."""
    if not value:
        return []
    if isinstance(value, str):
        raw = [value]
    else:
        raw = list(value)
    out: list[str] = []
    for _, addr in getaddresses([r for r in raw if r]):
        addr = addr.strip()
        if addr and addr not in out:
            out.append(addr)
    return out


def _valid(addr: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr))


_TRUE = ("1", "true", "yes", "on")


def external_policy_enabled() -> bool:
    """Is third-party outbound mail switched on at all?

    `--allow-external` alone is NOT enough. The flag is an unauthenticated
    argv token, and this repo spawns `claude -p --dangerously-skip-permissions`
    sessions whose prompts embed inbound email bodies — so "the flag is only
    passed by the approval gate" was a convention, not a property. Requiring
    PROTON_SEND_ALLOW_EXTERNAL to be on as well means that as shipped (flag
    absent from .env) no argv this script can be handed reaches a third party.

    Honest limit: an attacker who already has arbitrary code execution as this
    user can set the variable or edit .env. This makes accidental and
    tool-less-injection sends impossible; it does not defeat a live shell.
    """
    raw = os.environ.get("PROTON_SEND_ALLOW_EXTERNAL")
    if raw is None:
        # launchd/cron callers do not load .env; read it directly so the policy
        # means the same thing no matter who invoked the script.
        try:
            for line in (PROJECT_ROOT / ".env").read_text().splitlines():
                key, _, val = line.partition("=")
                if key.strip() == "PROTON_SEND_ALLOW_EXTERNAL":
                    raw = val.strip().strip("'\"")
                    break
        except OSError:
            raw = None
    return (raw or "").strip().lower() in _TRUE


def assert_allowed_recipients(recipients: list[str], allow_external: bool) -> None:
    if not recipients:
        raise SendError("No recipients. Pass --to or --reply-to-note.")
    bad = [a for a in recipients if not _valid(a)]
    if bad:
        raise SendError(f"Malformed recipient address(es): {', '.join(bad)}")
    if allow_external:
        if external_policy_enabled():
            return
        external = [a for a in recipients if a.lower() != SELF_ADDRESS.lower()]
        if external:
            raise SendError(
                "--allow-external was passed but third-party outbound mail is "
                "switched OFF. Set PROTON_SEND_ALLOW_EXTERNAL=1 in .env (and "
                "restart the Telegram bot) to enable it. Refused recipients: "
                + ", ".join(external)
            )
        return
    external = [a for a in recipients if a.lower() != SELF_ADDRESS.lower()]
    if external:
        raise SendError(
            "Refusing to send to an external recipient while outbound mail is "
            f"still being trusted: {', '.join(external)}\n"
            f"Only {SELF_ADDRESS} is permitted. Pass --allow-external to "
            "override — and only after the owner has approved that specific "
            "recipient through the approval gate."
        )


# ---------------------------------------------------------------------------
# Threading
# ---------------------------------------------------------------------------

def _angle(mid: str) -> str:
    mid = (mid or "").strip()
    if not mid:
        return ""
    if mid.startswith("<") and mid.endswith(">"):
        return mid
    return f"<{mid.strip('<>')}>"


def build_references(in_reply_to: Optional[str],
                     references: Optional[str | Iterable[str]]) -> str:
    """RFC 5322 References: the parent's chain, then the parent's Message-ID.

    Notes written by proton_mail.py record two ids: `messageId` (this message)
    and `threadId` (the root of its References chain). That is enough to build a
    chain the recipient's client will thread on — root first, parent last,
    deduped, in order.
    """
    chain: list[str] = []
    if references:
        raw = references.split() if isinstance(references, str) else list(references)
        for ref in raw:
            ref = _angle(ref)
            if ref and ref not in chain:
                chain.append(ref)
    parent = _angle(in_reply_to or "")
    if parent and parent not in chain:
        chain.append(parent)
    return " ".join(chain)


def reply_context_from_note(note_path: str) -> dict:
    """Pull recipient + threading headers off a synced email note.

    This is the ergonomic path the bot uses: point at the Parachute note the
    triage pipeline surfaced and get a correctly-threaded reply for free.
    """
    from parachute_client import ParachuteClient

    client = ParachuteClient(timeout=20)
    note = client.get_note(note_path, include_content=False)
    if not note:
        raise SendError(f"No Parachute note at {note_path}")
    md = note.metadata or {}
    if "email" not in (note.tags or []) and md.get("type") != "email":
        raise SendError(
            f"{note_path} is not an email note (tags: {', '.join(note.tags or []) or 'none'})"
        )

    sender = md.get("from") or ""
    _, addr = parseaddr(sender)
    if not addr or not _valid(addr):
        raise SendError(f"Cannot parse a reply address out of note From: {sender!r}")

    subject = (md.get("subject") or "").strip() or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    message_id = (md.get("messageId") or "").strip()
    thread_id = (md.get("threadId") or "").strip()
    if not message_id:
        raise SendError(
            f"{note_path} has no messageId — cannot thread a reply to it."
        )
    refs = [thread_id] if thread_id and thread_id != message_id else []

    return {
        "to": [addr],
        "subject": subject,
        "in_reply_to": message_id,
        "references": refs,
        "note_path": note.path or note_path,
    }


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

class _MsgIdListHeader:
    """A References / In-Reply-To header that is never encoded-word folded.

    Python's default header policy treats these as unstructured text, so a
    single message-id longer than the 78-column fold width gets RFC 2047
    encoded — References arrives as `=?utf-8?q?=3CMYuE…` and no client can
    match it against the parent's Message-ID, which silently breaks threading.
    Proton generates exactly such ids (a 100-char base64 `@protonmail.internalid`),
    so this is not a theoretical case. Fold on the spaces between ids instead,
    which is what RFC 5322 actually calls for.
    """

    max_count = 1

    @classmethod
    def parse(cls, value, kwds):
        kwds["parse_tree"] = None
        kwds["decoded"] = " ".join(str(value).split())
        kwds["defects"] = []

    def fold(self, *, policy):
        line = f"{self.name}:"
        lines: list[str] = []
        for mid in str(self).split():
            if len(line) + 1 + len(mid) > 78 and not line.endswith(":"):
                lines.append(line)
                line = " " + mid
            else:
                line += " " + mid
        lines.append(line)
        return policy.linesep.join(lines) + policy.linesep


def _smtp_policy():
    from email.headerregistry import HeaderRegistry
    import email.policy

    registry = HeaderRegistry()
    registry.map_to_type("references", _MsgIdListHeader)
    registry.map_to_type("in-reply-to", _MsgIdListHeader)
    # SMTP policy = CRLF line endings, i.e. the actual wire format.
    return email.policy.SMTP.clone(header_factory=registry)


MESSAGE_POLICY = _smtp_policy()


def build_message(to: list[str], subject: str, body: str,
                  cc: Optional[list[str]] = None,
                  in_reply_to: Optional[str] = None,
                  references: Optional[str | Iterable[str]] = None) -> EmailMessage:
    msg = EmailMessage(policy=MESSAGE_POLICY)
    msg["From"] = f"{FROM_NAME} <{ACCOUNT}>"
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=ACCOUNT.split("@")[-1])

    if in_reply_to:
        msg["In-Reply-To"] = _angle(in_reply_to)
    chain = build_references(in_reply_to, references)
    if chain:
        msg["References"] = chain

    # Trailing newline so clients that append a signature/quote start cleanly.
    text = body.rstrip("\n") + "\n"
    # Pure ASCII gets 7bit; anything else would default to base64, which makes
    # the message unreadable on the wire and is treated as marginally spammier
    # for text/plain. Quoted-printable keeps it legible either way.
    kwargs = {} if text.isascii() else {"cte": "quoted-printable"}
    msg.set_content(text, subtype="plain", charset="utf-8", **kwargs)
    return msg


# ---------------------------------------------------------------------------
# Transport (STARTTLS + certificate pinning)
# ---------------------------------------------------------------------------

def _fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _check_smtp_pin(fp: str, repin: bool = False) -> str:
    """Trust-on-first-use, shared with the IMAP pin.

    Bridge serves one self-signed cert for both 1143 and 1025, so the existing
    `certFingerprint` is the pin. `smtpCertFingerprint` exists only for the day
    Bridge starts using a separate cert — set it with `pin-smtp`, never
    automatically, because auto-pinning a *differing* cert on a port we already
    have a pin for is how a fail-closed check turns into a rubber stamp.
    """
    from proton_mail import load_state, save_state

    state = load_state()
    pinned = state.get("smtpCertFingerprint") or state.get("certFingerprint")

    if repin or not pinned:
        state["smtpCertFingerprint"] = fp
        state["smtpCertPinnedAt"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return fp

    if fp != pinned:
        raise SendError(
            "Bridge SMTP TLS fingerprint does not match the pin.\n"
            f"  pinned: {pinned[:24]}…\n"
            f"  seen:   {fp[:24]}…\n"
            "If you reinstalled Proton Mail Bridge, re-pin with:\n"
            "  python3 scripts/proton_send.py pin-smtp\n"
            f"If you did not, something else is listening on {SMTP_HOST}:"
            f"{SMTP_PORT} — do not send, and do not enter the password."
        )
    return fp


def smtp_connect(repin: bool = False, timeout: int = 30) -> tuple[smtplib.SMTP, str]:
    """Open a STARTTLS'd, pinned, authenticated SMTP session to Bridge."""
    from proton_mail import keychain_read

    try:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout)
    except (OSError, socket.timeout) as exc:
        raise SendError(
            f"cannot reach Proton Bridge SMTP at {SMTP_HOST}:{SMTP_PORT} ({exc}). "
            "Is Proton Mail Bridge running?"
        ) from exc

    try:
        smtp.ehlo()
        if not smtp.has_extn("starttls"):
            # Bridge always offers STARTTLS. If this listener does not, it is
            # not Bridge — refuse rather than fall back to cleartext AUTH.
            raise SendError(
                f"{SMTP_HOST}:{SMTP_PORT} does not offer STARTTLS — refusing to "
                "authenticate in cleartext."
            )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # pinned below instead
        smtp.starttls(context=ctx)
        der = smtp.sock.getpeercert(binary_form=True)
        fp = _fingerprint(der) if der else ""
        _check_smtp_pin(fp, repin=repin)
        smtp.ehlo()

        password = keychain_read(ACCOUNT)
        if not password:
            raise SendError(
                f"no Bridge password in the Keychain for {ACCOUNT}. "
                "Run: python3 scripts/proton_mail.py setup"
            )
        try:
            smtp.login(ACCOUNT, password)
        except smtplib.SMTPAuthenticationError as exc:
            # Never echo the password or the raw server line, which can embed it.
            raise SendError(
                f"Bridge rejected SMTP auth for {ACCOUNT} (code {exc.smtp_code}). "
                "The Bridge password changes if the account is re-added in "
                "Bridge — re-run: python3 scripts/proton_mail.py setup"
            ) from exc
        finally:
            del password
        return smtp, fp
    except Exception:
        try:
            smtp.quit()
        except Exception:
            pass
        raise


def _transmit(msg: EmailMessage, recipients: list[str], approved: bool,
              granted: bool = False) -> dict:
    """The ONLY function in this module that opens a socket to the MTA.

    It re-checks `approved` AND that a Prism send grant was verified, so that no
    future refactor can reach the wire by skipping the caller-side gates.
    """
    if approved is not True:
        raise ApprovalRequired(
            "Internal guard: _transmit called without approved=True. "
            "Refusing to send."
        )
    if granted is not True:
        raise ApprovalRequired(
            send_grant.REFUSAL + " (internal guard: _transmit called without a "
            "verified send grant). Nothing was sent."
        )

    smtp, fp = smtp_connect()
    try:
        smtp.send_message(msg, from_addr=ACCOUNT, to_addrs=recipients)
    except smtplib.SMTPException as exc:
        raise SendError(f"Bridge refused the message: {exc}") from exc
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return {"certFingerprint": fp[:16]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_email(to, subject: str, body: str, cc=None,
               in_reply_to: Optional[str] = None,
               references=None,
               approved: bool = False,
               dry_run: bool = False,
               allow_external: bool = False,
               grant_check: Optional[Callable[[], str]] = None) -> dict:
    """Compose and (only with approved=True) send mail as the owner.

    Returns a dict describing what was composed/sent. Raises SendError for any
    refusal — markdown in the body, a disallowed recipient, a missing approval,
    a fingerprint mismatch, or an SMTP failure.

    Order matters: content and recipient checks run BEFORE the approval check,
    so a --dry-run draft still fails fast on a bad body, and the approval check
    runs before anything touches the network.

    A real send also needs `grant_check`: a callable that verifies (and
    consumes) the Prism one-time send grant for the digest of exactly what the
    CLI was handed (cmd_send builds it). No callable, or a refused grant, and
    nothing is sent — there is no in-process caller that can skip it.
    """
    to_list = _addresses(to)
    cc_list = _addresses(cc)
    recipients = to_list + [a for a in cc_list if a not in to_list]

    subject = (subject or "").strip()
    if not subject:
        raise SendError("Refusing to send a message with an empty subject.")
    if not (body or "").strip():
        raise SendError("Refusing to send an empty body.")
    if len(body) > MAX_BODY_CHARS:
        raise SendError(f"Body is {len(body)} chars; limit is {MAX_BODY_CHARS}.")

    assert_allowed_recipients(recipients, allow_external)
    assert_plain_text(body)

    msg = build_message(to_list, subject, body, cc=cc_list,
                        in_reply_to=in_reply_to, references=references)
    raw = msg.as_string()

    result: dict[str, Any] = {
        "ok": False,
        "sent": False,
        "dryRun": bool(dry_run),
        "from": f"{FROM_NAME} <{ACCOUNT}>",
        "to": to_list,
        "cc": cc_list,
        "subject": subject,
        "messageId": msg["Message-ID"],
        "inReplyTo": msg.get("In-Reply-To"),
        "references": msg.get("References"),
        "bytes": len(raw.encode("utf-8")),
    }

    if dry_run:
        result["ok"] = True
        result["raw"] = raw
        return result

    if approved is not True:
        raise ApprovalRequired(
            "Refusing to send: --approved (approved=True) is required.\n"
            "All outbound mail MUST clear the approval gate. Draft it, route it "
            "through request_approval(), and only then send with --approved.\n"
            "Use --dry-run to inspect the composed message."
        )

    if grant_check is None:
        raise ApprovalRequired(
            send_grant.REFUSAL + ": a send needs the one-time grant Prism issues "
            "when the owner approves this exact draft in the Omni app. Propose it "
            "instead (scripts/omni_propose.py email ...). Nothing was sent."
        )
    try:
        result["approvalId"] = grant_check()
    except send_grant.GrantRefused as exc:
        raise SendError(str(exc)) from None

    result.update(_transmit(msg, recipients, approved=approved, granted=True))
    result["ok"] = True
    result["sent"] = True
    result["sentAt"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_body(args) -> str:
    """The body EXACTLY as handed over (no newline translation): the send-grant
    digest is computed over these bytes, the same ones Prism hashed."""
    if args.body_file:
        if args.body_file == "-":
            return sys.stdin.buffer.read().decode("utf-8")
        path = Path(args.body_file)
        if not path.exists():
            raise SendError(f"body file not found: {path}")
        return path.read_bytes().decode("utf-8")
    if args.body is not None:
        return args.body
    raise SendError("Pass --body-file (or - for stdin) or --body.")


def cmd_send(args) -> int:
    raw_body = _read_body(args)
    # Digest of exactly what this invocation was handed (argv + stdin), before any
    # note lookup or normalisation. Prism computed the same from the approved draft.
    digest = send_grant.email_digest(
        args.to, args.cc, args.subject, args.reply_to_note,
        args.in_reply_to, args.references, raw_body)
    # Same line endings the old text-mode read produced (the wire is CRLF anyway).
    body = raw_body.replace("\r\n", "\n").replace("\r", "\n")

    to = list(args.to or [])
    cc = list(args.cc or [])
    subject = args.subject
    in_reply_to = args.in_reply_to
    references = args.references

    if args.reply_to_note:
        ctx = reply_context_from_note(args.reply_to_note)
        to = to or ctx["to"]
        subject = subject or ctx["subject"]
        # Explicit threading (what Prism's executor passes, bound by the send digest) is
        # final: nothing is taken from the note at send time — not even References when
        # the approved payload had none (review round 2, S6 minor).
        if not args.in_reply_to:
            references = references or ctx["references"]
        in_reply_to = in_reply_to or ctx["in_reply_to"]
        print(f"Reply context from {ctx['note_path']}:", file=sys.stderr)
        print(f"  to:          {', '.join(ctx['to'])}", file=sys.stderr)
        print(f"  subject:     {ctx['subject']}", file=sys.stderr)
        print(f"  in-reply-to: <{ctx['in_reply_to']}>", file=sys.stderr)

    result = send_email(
        to=to, subject=subject, body=body, cc=cc,
        in_reply_to=in_reply_to, references=references,
        approved=args.approved, dry_run=args.dry_run,
        allow_external=args.allow_external,
        grant_check=lambda: send_grant.require_grant(digest),
    )

    if args.dry_run:
        print("--- DRY RUN: composed message, nothing sent ---")
        print(result.pop("raw"))
        print("--- end ---")
        if not args.approved:
            print("(no --approved given; this would have been refused anyway)",
                  file=sys.stderr)
    else:
        print(f"Sent to {', '.join(result['to'])}"
              + (f" (cc {', '.join(result['cc'])})" if result["cc"] else ""))
        print(f"  subject:    {result['subject']}")
        print(f"  message-id: {result['messageId']}")
        if result.get("inReplyTo"):
            print(f"  in-reply-to: {result['inReplyTo']}")
            print(f"  references:  {result['references']}")

    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "raw"}, indent=2))
    return 0


def cmd_pin_smtp(args) -> int:
    try:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
    except OSError as exc:
        print(f"cannot reach Bridge SMTP: {exc}", file=sys.stderr)
        return 1
    try:
        smtp.ehlo()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        smtp.starttls(context=ctx)
        der = smtp.sock.getpeercert(binary_form=True)
        fp = _fingerprint(der) if der else ""
    finally:
        try:
            smtp.quit()
        except Exception:
            pass

    from proton_mail import load_state

    imap_fp = load_state().get("certFingerprint")
    _check_smtp_pin(fp, repin=True)
    print(f"SMTP certificate pinned: {fp[:32]}…")
    print("  same certificate as IMAP" if fp == imap_fp
          else "  NOTE: differs from the pinned IMAP certificate")
    return 0


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """Offline verification of every safety invariant. Sends nothing."""
    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")

    def raises(exc_type, fn) -> Exception:
        try:
            fn()
        except exc_type as exc:
            return exc
        raise AssertionError(f"expected {exc_type.__name__}, nothing raised")

    print("proton_send self-check (no mail is sent)\n")

    print("approval gate:")
    check("send without approved= is refused", lambda: raises(
        ApprovalRequired,
        lambda: send_email(SELF_ADDRESS, "s", "plain body")))
    check("approved='yes' (truthy, not True) is refused", lambda: raises(
        ApprovalRequired,
        lambda: send_email(SELF_ADDRESS, "s", "plain body", approved="yes")))
    check("_transmit re-asserts approval", lambda: raises(
        ApprovalRequired,
        lambda: _transmit(build_message([SELF_ADDRESS], "s", "b"),
                          [SELF_ADDRESS], approved=False)))
    check("approved=True without a send grant is refused", lambda: raises(
        ApprovalRequired,
        lambda: send_email(SELF_ADDRESS, "s", "plain body", approved=True)))
    check("_transmit re-asserts the send grant", lambda: raises(
        ApprovalRequired,
        lambda: _transmit(build_message([SELF_ADDRESS], "s", "b"),
                          [SELF_ADDRESS], approved=True)))
    def _refused_grant():
        raise send_grant.GrantRefused("test")
    check("a refused grant stops the send", lambda: raises(
        SendError,
        lambda: send_email(SELF_ADDRESS, "s", "plain body", approved=True,
                           grant_check=_refused_grant)))
    check("dry-run never sets sent", lambda: (lambda r: (
        _assert(r["ok"] and not r["sent"] and r["dryRun"], "dry-run flags wrong"),
        _assert("raw" in r, "dry-run did not return the composed message"),
    ))(send_email(SELF_ADDRESS, "s", "plain body", dry_run=True)))

    print("\nrecipient guard:")
    check("external recipient refused by default", lambda: raises(
        SendError,
        lambda: send_email("someone@example.com", "s", "b", dry_run=True)))
    check("external cc refused by default", lambda: raises(
        SendError,
        lambda: send_email(SELF_ADDRESS, "s", "b", cc="someone@example.com",
                           dry_run=True)))
    def _with_policy(value: Optional[str], fn):
        prev = os.environ.get("PROTON_SEND_ALLOW_EXTERNAL")
        if value is None:
            os.environ.pop("PROTON_SEND_ALLOW_EXTERNAL", None)
        else:
            os.environ["PROTON_SEND_ALLOW_EXTERNAL"] = value
        try:
            return fn()
        finally:
            if prev is None:
                os.environ.pop("PROTON_SEND_ALLOW_EXTERNAL", None)
            else:
                os.environ["PROTON_SEND_ALLOW_EXTERNAL"] = prev

    check("--allow-external alone is refused when policy is off",
          lambda: _with_policy("0", lambda: raises(
              SendError,
              lambda: send_email("someone@example.com", "s", "b",
                                 dry_run=True, allow_external=True))))
    check("--allow-external + policy on permits it",
          lambda: _with_policy("1", lambda: _assert(
              send_email("someone@example.com", "s", "b", dry_run=True,
                         allow_external=True)["ok"],
              "allow_external did not pass with policy on")))
    check("empty recipients refused", lambda: raises(
        SendError, lambda: send_email("", "s", "b", dry_run=True)))
    check("malformed recipient refused", lambda: raises(
        SendError, lambda: send_email("not-an-address", "s", "b",
                                      dry_run=True, allow_external=True)))

    print("\nmarkdown rejection:")
    for label, sample in [
        ("bold", "Hi,\n\nThis is **important**.\n"),
        ("heading", "# Update\n\nHello.\n"),
        ("link", "See [the doc](https://example.com) please.\n"),
        ("inline code", "Run `make test` first.\n"),
        ("code fence", "```\ncode\n```\n"),
        ("bullets", "Agenda:\n- one\n- two\n"),
        ("blockquote", "> quoted\n\nreply\n"),
    ]:
        check(f"rejects {label}", lambda s=sample: raises(
            SendError, lambda: send_email(SELF_ADDRESS, "s", s, dry_run=True)))

    check("plain text with a signature dash passes", lambda: _assert(
        send_email(SELF_ADDRESS, "s",
                   "Hi,\n\n1. one\n2. two\n\n-- The owner\n",
                   dry_run=True)["ok"], "plain body was rejected"))
    check("markdown is caught even in dry-run", lambda: raises(
        SendError,
        lambda: send_email(SELF_ADDRESS, "s", "**x**", dry_run=True)))

    print("\ncompose + threading:")

    def _headers():
        r = send_email(SELF_ADDRESS, "Re: Test", "body", dry_run=True,
                       in_reply_to="parent@mail.example",
                       references=["root@mail.example"])
        _assert(r["inReplyTo"] == "<parent@mail.example>", r["inReplyTo"])
        _assert(r["references"] == "<root@mail.example> <parent@mail.example>",
                r["references"])
        _assert(f"From: {FROM_NAME} <{ACCOUNT}>" in r["raw"], "From header wrong")
        _assert("Date: " in r["raw"], "no Date header")
        _assert("charset=\"utf-8\"" in r["raw"].lower(), "not utf-8")
    check("in-reply-to/references built correctly", _headers)

    def _long_id():
        # Proton mints ~110-char message-ids. If the folder encoded-words them,
        # threading is silently dead — this is the regression guard for that.
        long_id = "MYuE" + "x" * 96 + "==@protonmail.internalid"
        r = send_email(SELF_ADDRESS, "Re: t", "body", dry_run=True,
                       in_reply_to="parent@mail.example", references=[long_id])
        raw = r["raw"].replace("\r\n", "\n")
        block = raw.split("References:")[1].split("\n\n")[0]
        _assert("=?" not in block, f"References was encoded-word folded: {block[:60]}")
        _assert(f"<{long_id}>" in block.replace("\n ", ""),
                "long message-id was mangled by folding")
        import email as _email
        import email.policy as _pol
        parsed = _email.message_from_string(raw, policy=_pol.default)
        _assert(parsed["In-Reply-To"] == "<parent@mail.example>", parsed["In-Reply-To"])
        _assert(parsed["References"].split() ==
                [f"<{long_id}>", "<parent@mail.example>"], parsed["References"])
    check("overlong message-ids survive folding unencoded", _long_id)

    check("references dedupes a self-rooted thread", lambda: _assert(
        build_references("a@b", ["a@b"]) == "<a@b>",
        build_references("a@b", ["a@b"])))
    check("angle brackets are not doubled", lambda: _assert(
        build_references("<a@b>", None) == "<a@b>",
        build_references("<a@b>", None)))
    def _utf8():
        r = send_email(SELF_ADDRESS, "Grüße", "Grüße — naïve\n", dry_run=True)
        _assert("utf-8" in r["raw"].lower(), "body not declared utf-8")
        _assert("Subject: " in r["raw"], "no Subject header")
    check("utf-8 subject and body survive compose", _utf8)
    check("empty subject refused", lambda: raises(
        SendError, lambda: send_email(SELF_ADDRESS, "  ", "b", dry_run=True)))
    check("empty body refused", lambda: raises(
        SendError, lambda: send_email(SELF_ADDRESS, "s", "   \n", dry_run=True)))

    print("\nbridge SMTP (no auth, no send):")
    try:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        try:
            smtp.ehlo()
            has_tls = smtp.has_extn("starttls")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            smtp.starttls(context=ctx)
            fp = _fingerprint(smtp.sock.getpeercert(binary_form=True))
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        check("STARTTLS offered", lambda: _assert(has_tls, "no STARTTLS"))
        check("certificate matches the pin", lambda: _check_smtp_pin(fp))
    except OSError as exc:
        print(f"  skip  Bridge SMTP unreachable ({exc}) — offline checks only")

    print()
    if failures:
        print(f"✗ proton_send self-check FAILED ({len(failures)})")
        return 1
    print("✓ proton_send self-check passed")
    return 0


def _assert(condition, message: str = "") -> None:
    if not condition:
        raise AssertionError(message or "assertion failed")


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser (also used by tests to replay the exact argv Prism builds)."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true",
                        help="run the offline safety self-check and exit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", help="compose and send (requires --approved)")
    p_send.add_argument("--to", action="append",
                        help="recipient (repeatable; comma lists ok)")
    p_send.add_argument("--cc", action="append", help="cc recipient (repeatable)")
    p_send.add_argument("--subject")
    p_send.add_argument("--body-file", help="path to the plain-text body, or - for stdin")
    p_send.add_argument("--body", help="inline body (prefer --body-file)")
    p_send.add_argument("--reply-to-note",
                        help="Parachute email note path; fills in recipient, "
                             "Re: subject, and threading headers")
    p_send.add_argument("--in-reply-to", help="override the parent Message-ID")
    p_send.add_argument("--references", help="override the References chain")
    p_send.add_argument("--approved", action="store_true",
                        help="Confirm the draft cleared the approval gate. REQUIRED to send.")
    p_send.add_argument("--dry-run", action="store_true",
                        help="print the composed MIME message and exit without connecting")
    p_send.add_argument("--allow-external", action="store_true",
                        help=f"permit recipients other than {SELF_ADDRESS}")
    p_send.add_argument("--json", action="store_true")
    p_send.set_defaults(func=cmd_send)

    p_pin = sub.add_parser("pin-smtp", help="re-pin the Bridge SMTP certificate")
    p_pin.set_defaults(func=cmd_pin_smtp)
    return parser


def main() -> int:
    if "--test" in sys.argv[1:]:
        return _selftest()

    args = build_parser().parse_args()
    try:
        return args.func(args)
    except SendError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
