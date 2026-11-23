"""Asking the owner: an approval card in the Omni app for one exact tool call.

`request()` files the call with the gateway (`POST /api/omni/hooks/propose`,
kind `command`), then WAITS — the tool call is paused inside Hermes — until
he decides on his device, the wait runs out, or the turn ends. Only `approved`
lets the call run, and it is the call that was shown: the gateway binds the
approval to the digest of the payload, and this function is holding that very
call. Deny, expiry, a cancelled turn, an unreachable gateway: all mean "do not
run".
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional, Tuple

PROPOSE_PATH = "/api/omni/hooks/propose"
STATUS_PATH = "/api/omni/hooks/approvals/%s"
RESULT_PATH = "/api/omni/hooks/approvals/%s/result"
WITHDRAW_PATH = "/api/omni/hooks/approvals/%s/withdraw"
_MAX_INPUT = 64 * 1024


def wait_seconds() -> int:
    """How long a call waits for the owner (OMNI_BRIDGE_APPROVAL_WAIT_S, 60–3600, default 600)."""
    try:
        v = int(os.environ.get("OMNI_BRIDGE_APPROVAL_WAIT_S", "") or 600)
    except ValueError:
        v = 600
    return max(60, min(3600, v))


def request(post: Callable, payload: dict, thread: str, summary: str = "",
            poll: float = 1.5) -> Tuple[str, Optional[str], str]:
    """Ask and wait. Returns (outcome, approval id, detail).

    outcome: approved | denied | expired | cancelled | unreachable | refused | too-large
    """
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > _MAX_INPUT:
        return "too-large", None, ""
    sleep, clock = time.sleep, time.monotonic
    wait = wait_seconds()
    body = {"kind": "command", "payload": payload, "expiresInSec": wait}
    if thread:
        body["threadId"] = thread
    if summary:
        body["summary"] = summary[:300]
    status, res = post(PROPOSE_PATH, body, 15)
    if status == 0:
        return "unreachable", None, ""
    if status != 201 or not res or not isinstance(res.get("id"), str):
        detail = res.get("detail") if isinstance(res, dict) and isinstance(res.get("detail"), str) else ""
        return "refused", None, "%s %s" % (status, detail[:160])
    approval_id = res["id"]
    deadline = clock() + wait + 5
    misses = 0
    while clock() < deadline:
        sleep(poll)
        status, res = post(STATUS_PATH % approval_id, None, 10, "GET")
        if status != 200 or not res:
            misses += 1
            if misses >= 20:         # the gateway went away: stop waiting, do not run
                return "unreachable", approval_id, ""
            continue
        misses = 0
        state = res.get("status")
        if state == "pending":
            continue
        if state == "approved":
            return "approved", approval_id, ""
        if state in ("cancelled", "revised"):
            # Cancelled by the owner = denied; cancelled by the gateway = the turn ended first.
            by_gateway = res.get("decidedVia") in ("turn-ended", "withdrawn")
            return ("cancelled" if by_gateway else "denied"), approval_id, ""
        return ("expired" if state == "expired" else "denied"), approval_id, ""
    # Nobody answered: take the question back so the card does not linger.
    post(WITHDRAW_PATH % approval_id, {}, 5)
    return "expired", approval_id, ""


def report(post: Callable, approval_id: str, ok: bool) -> None:
    """Tell the gateway how the approved call ended (best effort)."""
    try:
        post(RESULT_PATH % approval_id, {"ok": bool(ok)}, 5)
    except Exception:
        pass


def refusal(outcome: str, decision, detail: str = "") -> str:
    """What the model is told when an action that needed approval does not run."""
    what = decision.title or "this action"
    tail = " Do not try to do it another way; tell the owner what you needed and why. (omni-bridge: %s)" % decision.rule
    if outcome == "denied":
        return "The owner denied this in the Omni app (%s). It was not run." % what + tail
    if outcome == "expired":
        return "The owner did not answer the approval request in time (%s). It was not run." % what + tail
    if outcome == "cancelled":
        return "The approval request was withdrawn because the turn ended (%s). It was not run." % what + tail
    if outcome == "too-large":
        return ("This call is too large to show the owner for approval (%s). It was not run. Put the content in a file in "
                "the workspace and act on the file instead." % what + tail)
    if outcome == "unreachable":
        return ("This needs the owner's approval (%s) and the Omni gateway that would ask him is not reachable. "
                "It was not run." % what + tail)
    return ("This needs the owner's approval (%s) and the Omni gateway would not take the request%s. It was not run."
            % (what, (" (%s)" % detail.strip()) if detail.strip() else "") + tail)
