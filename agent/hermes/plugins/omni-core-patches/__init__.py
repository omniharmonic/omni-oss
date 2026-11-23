"""omni-core-patches — Omni's fixes to Hermes core that must survive `hermes update`.

Why a plugin and not a patch file: `hermes update` resets the Hermes checkout, and
upstream moves fast enough that a saved `git diff` stops applying within weeks
(the wake fix below did: upstream reflowed `gateway/wake.py` between 2026-08-27 and
2026-11-12, and `git apply --check` of the saved patch fails on origin/main). A
plugin lives in ~/.hermes/plugins (a symlink into this repo), loads on every start,
and wraps behaviour instead of replacing source lines, so it keeps working across
cosmetic upstream rewrites.

Every patch reports one of these states in agent.log, so `grep omni-core-patches`
after an update tells you where you stand:

    APPLIED        — wrapped and active
    UPSTREAM-FIXED — upstream now does this itself; the patch stepped aside
                     (delete it from this file)
    CANNOT-APPLY   — the code it wraps changed shape; Hermes runs UNPATCHED
                     (rework the patch)
    SKIPPED        — not relevant in this process (e.g. CLI without the gateway)

PATCH 1 — wake idempotency (incident 2026-11-08, deleg_bc642936)
-----------------------------------------------------------------
`gateway.wake._self_post_chat_completion` POSTs a background-delegation result back
into the session as a chat turn, with a 600 s timeout and three retries. A wake turn
that outlasts the timeout is retried as a FRESH turn each time: up to six duplicate
"[ASYNC DELEGATION ... COMPLETE]" rows and "Duplicate notification" replies per
delegation. The API server already de-duplicates on `Idempotency-Key` (joins the
in-flight run, or returns the cached result) — the wake just never sends one.

The fix sends `Idempotency-Key: wake:<session>:<sha256(text)[:32]>` on that POST.
Same session + same text = one turn, however many times it is retried.

How, without copying upstream's function: the wrapper sets a context variable for
the duration of the wake call, and a wrapper on `aiohttp.ClientSession.post` adds
the header only when (a) that variable is set — i.e. we are inside a wake — and
(b) the URL is the chat-completions endpoint. Context variables follow the awaiting
task, so no other request in the process is touched. A header the caller already
set (upstream fixing this, or the old local source patch) wins.

Upstream PR: not yet opened (NousResearch/hermes-agent). When it merges, this patch
reports UPSTREAM-FIXED; then delete it.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

PATCH_ATTR = "__omni_core_patch__"
CHAT_COMPLETIONS_SUFFIX = "/v1/chat/completions"

_WAKE_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omni_wake_idempotency_key", default=None)


def wake_idempotency_key(session_id: str, text: str) -> str:
    """Stable per (session, wake text). Must match the key the old source patch used,
    so a run started before this plugin loaded is still joined, not duplicated."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return f"wake:{session_id}:{digest}"


def _has_idempotency_header(headers: Any) -> bool:
    try:
        return any(str(k).lower() == "idempotency-key" for k in headers)
    except TypeError:
        return False


def with_wake_header(url: Any, headers: Any, key: str | None) -> Any:
    """Headers to send. Unchanged unless we are inside a wake POSTing to chat completions
    and nobody set an Idempotency-Key already."""
    if not key or not str(url).rstrip("/").endswith(CHAT_COMPLETIONS_SUFFIX):
        return headers
    if headers is not None and _has_idempotency_header(headers):
        return headers
    merged = dict(headers or {})
    merged["Idempotency-Key"] = key
    return merged


def wrap_self_post(orig: Callable) -> Callable:
    @functools.wraps(orig)
    async def _self_post_chat_completion(adapter: Any, *args: Any, **kwargs: Any) -> Any:
        text = kwargs.get("text")
        session_id = kwargs.get("session_id")
        if not isinstance(text, str) or not isinstance(session_id, str):
            return await orig(adapter, *args, **kwargs)  # unknown call shape: hands off
        token = _WAKE_KEY.set(wake_idempotency_key(session_id, text))
        try:
            return await orig(adapter, *args, **kwargs)
        finally:
            _WAKE_KEY.reset(token)

    setattr(_self_post_chat_completion, PATCH_ATTR, True)
    return _self_post_chat_completion


def wrap_session_post(orig_post: Callable) -> Callable:
    @functools.wraps(orig_post)
    def post(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        key = _WAKE_KEY.get()
        if key:
            kwargs["headers"] = with_wake_header(url, kwargs.get("headers"), key)
        return orig_post(self, url, *args, **kwargs)

    setattr(post, PATCH_ATTR, True)
    return post


def wake_patch_state(func: Callable) -> str:
    """APPLIED-able, UPSTREAM-FIXED or CANNOT-APPLY, judged from the target's source."""
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return "CANNOT-APPLY"
    if "Idempotency-Key" in src:
        return "UPSTREAM-FIXED"
    # We inject at the aiohttp POST; if the wake stopped using one, the shim would be
    # a silent no-op — say so rather than claim APPLIED.
    if "aiohttp" not in src or ".post(" not in src:
        return "CANNOT-APPLY"
    return "OK"


def apply_wake_patch(wake_module: Any, client_session_cls: Any) -> str:
    func = getattr(wake_module, "_self_post_chat_completion", None)
    if func is None or not inspect.iscoroutinefunction(func):
        return "CANNOT-APPLY"
    if getattr(func, PATCH_ATTR, False):
        return "APPLIED"  # already wrapped (plugin loaded twice in one process)
    state = wake_patch_state(func)
    if state != "OK":
        return state
    post = getattr(client_session_cls, "post", None)
    if post is None:
        return "CANNOT-APPLY"
    if not getattr(post, PATCH_ATTR, False):
        client_session_cls.post = wrap_session_post(post)
    wake_module._self_post_chat_completion = wrap_self_post(func)
    return "APPLIED"


def _patch_wake() -> str:
    try:
        import aiohttp
        from gateway import wake
    except Exception as exc:  # noqa: BLE001 — a CLI-only install may lack the gateway
        logger.info("omni-core-patches: wake-idempotency SKIPPED (%s)", exc)
        return "SKIPPED"
    return apply_wake_patch(wake, aiohttp.ClientSession)


PATCHES: dict[str, Callable[[], str]] = {
    "wake-idempotency": _patch_wake,
}


def register(ctx):  # noqa: ARG001 — patches need no ctx surface
    for name, apply in PATCHES.items():
        try:
            state = apply()
        except Exception:  # noqa: BLE001 — fail open, loudly
            logger.exception("omni-core-patches: %s CANNOT-APPLY (error)", name)
            continue
        log = logger.warning if state in ("CANNOT-APPLY", "UPSTREAM-FIXED") else logger.info
        log("omni-core-patches: %s %s", name, state)
