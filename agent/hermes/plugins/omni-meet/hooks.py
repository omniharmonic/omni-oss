"""Prism's speaker-review hooks, over loopback (speaker-review contract).

The same approach as omni-bridge's ``_post``: ``OMNI_GATEWAY_URL`` (default ``http://127.0.0.1:8787``) must
be a plain-http loopback origin and ``OMNI_SERVICE_TOKEN`` (>= 16 characters) is sent only in the
Authorization header. No proxy, no redirects, short timeouts. Every call returns ``(status, body)``;
status 0 = not configured or not reachable. Failures are logged without the token or the body.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY = "http://127.0.0.1:8787"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
BASE_PATH = "/api/omni/hooks/speaker-reviews"
POST_TIMEOUT_S = 20.0
GET_TIMEOUT_S = 5.0
_REVIEW_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def token() -> str:
    t = os.environ.get("OMNI_SERVICE_TOKEN", "").strip()
    return t if len(t) >= 16 else ""


def gateway() -> str:
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect would carry the token elsewhere
        return None


def call(method: str, path: str, body: Any = None, timeout: float = GET_TIMEOUT_S) -> Tuple[int, Optional[Dict[str, Any]]]:
    base, tok = gateway(), token()
    if not base or not tok:
        logger.warning("omni-meet: speaker reviews need OMNI_SERVICE_TOKEN and a loopback OMNI_GATEWAY_URL")
        return 0, None
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + tok, "User-Agent": "omni-meet/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as res:
            status, raw = res.status, res.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(64 * 1024)
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001 — refused, timed out, reset: all "not reachable"
        logger.warning("omni-meet: %s %s not reachable (%s)", method, BASE_PATH, type(e).__name__)
        return 0, None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:  # noqa: BLE001
        parsed = None
    if status >= 400:
        logger.warning("omni-meet: %s %s answered %d", method, BASE_PATH, status)
    return status, parsed if isinstance(parsed, dict) else None


def review_id_ok(review_id: Any) -> bool:
    return isinstance(review_id, str) and bool(_REVIEW_ID.match(review_id))


def post_review(body: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
    return call("POST", BASE_PATH, body, POST_TIMEOUT_S)


def get_review(review_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
    if not review_id_ok(review_id):
        return 404, None
    return call("GET", "%s/%s" % (BASE_PATH, urllib.parse.quote(review_id)), None, GET_TIMEOUT_S)


def close_review(review_id: str, status: str) -> Tuple[int, Optional[Dict[str, Any]]]:
    if not review_id_ok(review_id) or status not in ("done", "expired"):
        return 400, None
    return call("POST", "%s/%s/close" % (BASE_PATH, urllib.parse.quote(review_id)), {"status": status}, GET_TIMEOUT_S)
