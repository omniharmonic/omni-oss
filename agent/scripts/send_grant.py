"""send_grant — the "one door" check every send script makes before it sends.

Owner requirement (2026-10-15): NOTHING is sent to another person (email, chat
message, tweet, calendar invite/RSVP) unless the owner approved that EXACT
message from a signed-in device. The only process that can attest to that is
the Prism server: its Omni approval executor claims a pending approval whose
digest matches what the owner saw on his device, then launches the sender with
a ONE-TIME SEND GRANT on an inherited pipe (never the environment or argv —
a same-user `ps eww` shows environments):

    fd 3                       the grant (random, ≤ 60 s, single use)
    PRISM_SEND_GRANT_FD=3      which descriptor carries it (not a secret)
    PRISM_SEND_GRANT_APPROVAL  the approval id it is bound to (not a secret)

Before it opens a socket, the sender computes the digest of exactly what it is
about to send (``send_digest`` — the same length-prefixed encoding as
apps/server/src/omni/send-grant.ts in the Prism repo) and asks Prism, over
loopback, to verify AND consume the grant:

    POST http://127.0.0.1:8787/api/omni/approvals/<id>/send-grant/verify
         {"token": <grant>, "digest": <hex>}

Only a 200 ``{"ok": true}`` for that approval lets the send proceed. Anything
else — no grant, an expired or reused grant, other recipients or other text,
Prism unreachable — raises ``GrantRefused`` and the script exits non-zero with
"Sends must be approved in Omni". There is no flag, environment switch or
argument that skips this.

Where the verifier lives is NOT taken from the environment (anyone can set an
environment variable): it is Prism's fixed loopback port, overridable only by
``PRISM_GRANT_URL`` in THIS repo's ``.env`` file, and only to another plain-http
IPv4-loopback URL (127.0.0.1 only: `localhost` may resolve to ::1 while Prism
binds 127.0.0.1). Port 8787 is held by the running Prism server, so a fake
verifier cannot answer there while Prism is up. Residual: while Prism is
restarting, a same-user process could bind 8787 and answer "ok" to a script it
launched itself — it would then still need the raw credentials the send uses,
which such a process could use directly anyway.

Honest limit: a process running as this user that can rewrite this repo's
files, or that reads the underlying credentials (Keychain, .env) and talks to
SMTP / the X API / Matrix itself, is not stopped by this module. What it stops
is every path that goes through the repo's send scripts — which is every path
an agent surface is given.

Python 3.9 compatible (the agent host's system Python).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple, Union

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FD_ENV = "PRISM_SEND_GRANT_FD"
APPROVAL_ENV = "PRISM_SEND_GRANT_APPROVAL"
DEFAULT_VERIFIER = "http://127.0.0.1:8787"
VERIFY_PATH = "/api/omni/approvals/%s/send-grant/verify"
REFUSAL = "Sends must be approved in Omni"
_LOOPBACK = frozenset({"127.0.0.1"})
_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")

FieldValue = Union[None, str, Sequence[str]]


class GrantRefused(RuntimeError):
    """The send was not authorized by a live Omni approval. Never carries the grant."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(
            "%s (the one-time send grant was refused: %s). Nothing was sent. Propose "
            "the draft for the owner's review instead (omni_propose, or "
            "scripts/omni_propose.py); only his approval of that exact draft in the "
            "Omni app sends it." % (REFUSAL, reason))


# ── the digest (twin of sendDigest in apps/server/src/omni/send-grant.ts) ────

def send_digest(channel: str, fields: Iterable[Tuple[str, FieldValue]]) -> str:
    """SHA-256 hex over a length-prefixed, ordered encoding of the send's fields.

    "prism-send-grant/v1\\n" <channel> "\\n", then per field
    <name> "\\n" <count> "\\n" and per value <utf-8 byte length> ":" <value> "\\n".
    None is an empty list; a str is a one-element list.
    """
    h = hashlib.sha256()
    h.update(("prism-send-grant/v1\n%s\n" % channel).encode("utf-8"))
    for name, value in fields:
        if value is None:
            values = []  # type: Sequence[str]
        elif isinstance(value, str):
            values = [value]
        else:
            values = list(value)
        h.update(("%s\n%d\n" % (name, len(values))).encode("utf-8"))
        for v in values:
            if not isinstance(v, str):
                raise TypeError("send_digest: every value must be a str")
            b = v.encode("utf-8")
            h.update(("%d:" % len(b)).encode("utf-8"))
            h.update(b)
            h.update(b"\n")
    return h.hexdigest()


def _nonempty(values: Optional[Sequence[str]]) -> list:
    return [v for v in (values or []) if isinstance(v, str) and v]


def email_digest(to: Optional[Sequence[str]], cc: Optional[Sequence[str]], subject: Optional[str],
                 reply_to_note: Optional[str], in_reply_to: Optional[str], references: Optional[str],
                 body: str) -> str:
    """Digest of a proton_send.py invocation, from its raw argv values and stdin body."""
    return send_digest("email", [
        ("to", _nonempty(to)),
        ("cc", _nonempty(cc)),
        ("subject", subject or None),
        ("reply_to_note", reply_to_note or None),
        ("in_reply_to", in_reply_to or None),
        ("references", references or None),
        ("body", body),
    ])


def tweet_digest(text: str) -> str:
    return send_digest("tweet", [("text", text)])


def message_digest(room_id: str, body: str) -> str:
    return send_digest("message", [("room_id", room_id), ("body", body)])


# ── where the verifier is ────────────────────────────────────────────────────

def _dotenv_value(name: str) -> Optional[str]:
    try:
        for line in (PROJECT_ROOT / ".env").read_text().splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() == name:
                return val.strip().strip("'\"")
    except OSError:
        return None
    return None


def verifier_base() -> str:
    """Prism's loopback origin. Never from the process environment."""
    raw = (_dotenv_value("PRISM_GRANT_URL") or DEFAULT_VERIFIER).strip().rstrip("/")
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
    return "http://%s%s" % (host, ":%d" % port if port else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never follow: the grant goes to Prism only
        return None


Transport = Callable[[str, bytes], Tuple[int, bytes]]


def _http_post(url: str, data: bytes) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "send-grant/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, res.read(64 * 1024)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(64 * 1024)
        except Exception:
            return e.code, b""


# ── the check ────────────────────────────────────────────────────────────────

def _read_grant_fd(env) -> Optional[str]:
    """Read (once) and close the descriptor Prism passed the grant on."""
    raw = env.get(FD_ENV)
    if raw is None:
        return None
    try:
        fd = int(str(raw))
    except ValueError:
        return None
    if fd < 3 or fd > 255:
        return None
    try:
        chunks = []
        while sum(len(c) for c in chunks) <= 128:
            b = os.read(fd, 128)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("ascii", "replace").strip()
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def require_grant(digest: str, environ: Optional[dict] = None,
                  transport: Optional[Transport] = None) -> str:
    """Verify and consume this process's one-time grant for `digest`.

    Returns the approval id on success; raises GrantRefused otherwise. Call it
    immediately before the network write, after every local validation.
    The grant is read from its inherited descriptor (which is then closed, so a
    child process cannot inherit it).
    """
    env = os.environ if environ is None else environ
    token = _read_grant_fd(env)
    if hasattr(env, "pop"):
        env.pop(FD_ENV, None)
    approval_id = env.get(APPROVAL_ENV)
    if not token:
        raise GrantRefused("no grant: this send was not launched by an Omni approval")
    if not isinstance(token, str) or not _TOKEN.match(token):
        raise GrantRefused("malformed grant")
    if not approval_id or not _APPROVAL_ID.match(approval_id):
        raise GrantRefused("no approval id")
    if not isinstance(digest, str) or not re.match(r"^[a-f0-9]{64}$", digest):
        raise GrantRefused("no digest")
    base = verifier_base()
    if not base:
        raise GrantRefused("PRISM_GRANT_URL in .env is not a plain-http loopback URL")
    url = base + VERIFY_PATH % urllib.parse.quote(approval_id, safe="")
    body = json.dumps({"token": token, "digest": digest}).encode("utf-8")
    try:
        status, raw = (transport or _http_post)(url, body)
    except Exception as e:  # refused, timed out: no verdict = no send
        raise GrantRefused("Prism could not be reached to verify the grant (%s)" % type(e).__name__)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        parsed = {}
    if status == 200 and isinstance(parsed, dict) and parsed.get("ok") is True and parsed.get("approvalId") == approval_id:
        return approval_id
    err = parsed.get("error") if isinstance(parsed, dict) else None
    raise GrantRefused(str(err)[:80] if isinstance(err, str) and err else "verifier answered %s" % status)


def refuse_and_exit(exc: GrantRefused, code: int = 3) -> None:
    """CLI helper: print the refusal (never the grant) and exit non-zero."""
    print("ERROR: %s" % exc, file=sys.stderr)
    sys.exit(code)
