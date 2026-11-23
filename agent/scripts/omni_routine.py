#!/usr/bin/env python3
"""omni_routine — publish one routine run (morning / evening briefing, tweet draft) into its own Omni chat.

POSTs to the Prism server's loopback hook `POST /api/omni/hooks/routine`, with the same base URL
and service token as omni_propose (OMNI_GATEWAY_URL / OMNI_SERVICE_TOKEN, else ~/.hermes/.env).
The server makes the chat `omni-routine-<routine>-<day>`, stores the message once per post_id
(a replay appends nothing and never pushes) and creates an approval only through its normal
propose path. Nothing is ever sent from here: a tweet goes out only when the owner approves it
on a device.

Three attempts with backoff on a 5xx or a network error; a 4xx is never retried. The token is
never printed or put in an error.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import omni_propose
import os
OWNER_TZ = os.environ.get("OMNI_TIMEZONE", "").strip() or "UTC"  # config: the owner's IANA timezone (identity owner.timezone)

ROUTINE_PATH = "/api/omni/hooks/routine"
ROUTINES = ("morning", "evening", "tweet")
TZ = ZoneInfo(OWNER_TZ)
MAX_BODY = 20000
RETRY_DELAYS = (2.0, 5.0)
_CUT = "\n\n…(truncated — full note in Parachute)"

Transport = Callable[[str, bytes, Dict[str, str]], Tuple[int, bytes]]


class RoutinePublishError(RuntimeError):
    """The routine post did not land. Never carries the token."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def local_day(now: Optional[datetime] = None) -> str:
    """Today in the owner's timezone, YYYY-MM-DD."""
    return (now or datetime.now(TZ)).astimezone(TZ).strftime("%Y-%m-%d")


def routine_title(name: str, day: str) -> str:
    """"Morning briefing · Sat 17 Oct"."""
    return "%s · %s" % (name, datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b"))


def _utf16_len(s: str) -> int:
    """Length as the server counts it (JS `string.length`): an astral character is two units."""
    return len(s.encode("utf-16-le")) // 2


def _bounded(body: str) -> str:
    """At most MAX_BODY UTF-16 units, suffix included. Cuts on whole characters, so a surrogate
    pair is never split."""
    if _utf16_len(body) <= MAX_BODY:
        return body
    room = MAX_BODY - _utf16_len(_CUT)
    used = 0
    end = 0
    for ch in body:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > room:
            break
        used += w
        end += 1
    return body[:end] + _CUT


def publish_routine(routine: str, day: str, title: str, post_id: str, body: str, push: bool,
                    approval: Optional[Dict[str, Any]] = None, *, base: Optional[str] = None,
                    token: Optional[str] = None, transport: Optional[Transport] = None,
                    sleep: Callable[[float], Any] = time.sleep) -> Dict[str, Any]:
    if routine not in ROUTINES:
        raise RoutinePublishError("routine must be one of %s" % ", ".join(ROUTINES))
    base = base or omni_propose.gateway()
    token = token or omni_propose._token()
    if not base or not token:
        raise RoutinePublishError("the Omni gateway is not configured on this machine "
                                  "(OMNI_SERVICE_TOKEN / OMNI_GATEWAY_URL)")
    payload: Dict[str, Any] = {"routine": routine, "day": day, "title": title, "postId": post_id,
                               "body": _bounded(body), "push": bool(push)}
    if approval:
        payload["approval"] = approval
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": "Bearer " + token, "User-Agent": "omni-routine/1.0"}
    send = transport or omni_propose._http_post
    last = "no attempt"
    for attempt in range(3):
        if attempt:
            sleep(RETRY_DELAYS[attempt - 1])
        try:
            status, raw = send(base + ROUTINE_PATH, data, headers)
        except Exception as e:  # network: retried
            last = "gateway unreachable (%s)" % type(e).__name__
            continue
        try:
            res = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            res = {}
        if status in (200, 201):
            if isinstance(res, dict) and isinstance(res.get("threadId"), str):
                return {"ok": True, "thread_id": res["threadId"], "post_id": res.get("postId", post_id),
                        "replayed": status == 200 or bool(res.get("replayed")),
                        "approval_id": res.get("approvalId")}
            raise RoutinePublishError("the gateway answered without a receipt", status)
        if 400 <= status < 500:
            code = res.get("error") if isinstance(res, dict) else None
            raise RoutinePublishError("the gateway refused the routine post (%s%s)"
                                      % (status, " " + code[:80] if isinstance(code, str) else ""), status)
        last = "gateway answered %s" % status
    raise RoutinePublishError("routine post failed after 3 attempts: " + last)
