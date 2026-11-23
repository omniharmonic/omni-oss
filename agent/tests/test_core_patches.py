"""omni-core-patches: the wake Idempotency-Key survives `hermes update` as a plugin.

Runs a real aiohttp server on loopback and a stand-in `gateway.wake` whose
`_self_post_chat_completion` is shaped like upstream's (2026-11-12 origin/main):
local `import aiohttp`, `ClientSession(...)`, `http.post(url, json=, headers=)`.

    python3 -m unittest tests.test_core_patches -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import types
import unittest
from pathlib import Path

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "omni_core_patches", ROOT / "hermes/plugins/omni-core-patches/__init__.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


UPSTREAM_SHAPED = '''
async def _self_post_chat_completion(adapter, *, text, session_id,
                                     notification_category="result", profile=None):
    import aiohttp
    url = f"http://127.0.0.1:{adapter._port}/v1/chat/completions"
    headers = {"Authorization": "Bearer k", "X-Hermes-Session-Id": session_id}
    payload = {"model": "hermes-agent", "messages": [{"role": "user", "content": text}]}
    async with aiohttp.ClientSession() as http:
        async with http.post(url, json=payload, headers=headers) as resp:
            await resp.read()

async def wake(adapter, text, session_id):
    # callers reach the function through the module global, as upstream's do
    await _self_post_chat_completion(adapter, text=text, session_id=session_id)
'''

UPSTREAM_FIXED = UPSTREAM_SHAPED.replace(
    '"X-Hermes-Session-Id": session_id}',
    '"X-Hermes-Session-Id": session_id, "Idempotency-Key": "upstream"}')


def load_fake_wake(src: str, path: Path) -> types.ModuleType:
    """A real module from a real file, so inspect.getsource works as it does on Hermes."""
    path.write_text(src)
    spec = importlib.util.spec_from_file_location(f"fake_wake_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSession:
    """Stands in for aiohttp.ClientSession so patching it never leaks into the real class."""

    def __init__(self):
        self.calls = []

    def post(self, url, *args, **kwargs):
        self.calls.append((url, kwargs.get("headers")))
        return "ctx"


class HeaderRules(unittest.TestCase):
    def test_key_matches_old_source_patch(self):
        # Same derivation as ~/.hermes/LOCAL-PATCH-wake-idempotency.patch, so a run the
        # old patch started is joined, not duplicated, across the switch-over.
        k = cp.wake_idempotency_key("sess", "hello")
        self.assertEqual(k, "wake:sess:2cf24dba5fb0a30e26e83b2ac5b9e29e")

    def test_only_inside_a_wake(self):
        self.assertEqual(cp.with_wake_header("http://h/v1/chat/completions", {"a": "1"}, None),
                         {"a": "1"})

    def test_only_chat_completions(self):
        self.assertEqual(cp.with_wake_header("http://h/v1/responses", {"a": "1"}, "k"), {"a": "1"})

    def test_added_inside_a_wake(self):
        self.assertEqual(cp.with_wake_header("http://h/v1/chat/completions", {"a": "1"}, "k"),
                         {"a": "1", "Idempotency-Key": "k"})

    def test_existing_key_wins_any_case(self):
        h = {"idempotency-key": "theirs"}
        self.assertIs(cp.with_wake_header("http://h/v1/chat/completions", h, "k"), h)

    def test_caller_dict_not_mutated(self):
        h = {"a": "1"}
        cp.with_wake_header("http://h/v1/chat/completions", h, "k")
        self.assertEqual(h, {"a": "1"})


class PatchStates(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_upstream_shape_applies(self):
        mod = load_fake_wake(UPSTREAM_SHAPED, self.tmp / "a.py")
        self.assertEqual(cp.apply_wake_patch(mod, FakeSession), "APPLIED")
        self.assertTrue(getattr(mod._self_post_chat_completion, cp.PATCH_ATTR))

    def test_double_load_is_idempotent(self):
        mod = load_fake_wake(UPSTREAM_SHAPED, self.tmp / "b.py")
        cp.apply_wake_patch(mod, FakeSession)
        once = FakeSession.post
        self.assertEqual(cp.apply_wake_patch(mod, FakeSession), "APPLIED")
        self.assertIs(FakeSession.post, once)

    def test_steps_aside_when_upstream_fixed(self):
        mod = load_fake_wake(UPSTREAM_FIXED, self.tmp / "c.py")
        before = mod._self_post_chat_completion

        class Untouched(FakeSession):
            pass
        post = Untouched.post
        self.assertEqual(cp.apply_wake_patch(mod, Untouched), "UPSTREAM-FIXED")
        self.assertIs(mod._self_post_chat_completion, before)
        self.assertIs(Untouched.post, post)

    def test_refuses_when_function_gone(self):
        self.assertEqual(cp.apply_wake_patch(types.SimpleNamespace(), FakeSession), "CANNOT-APPLY")

    def test_refuses_when_wake_stops_using_aiohttp_post(self):
        mod = load_fake_wake("async def _self_post_chat_completion(a, *, text, session_id):\n"
                             "    await a.run_internal_session_turn(text)\n", self.tmp / "d.py")
        self.assertEqual(cp.apply_wake_patch(mod, FakeSession), "CANNOT-APPLY")


@unittest.skipIf(aiohttp is None, "aiohttp not installed")
class EndToEnd(unittest.TestCase):
    """Real aiohttp, real server: the header reaches the wire on a wake and only then."""

    def test_wake_sends_key_and_other_posts_do_not(self):
        import tempfile
        mod = load_fake_wake(UPSTREAM_SHAPED, Path(tempfile.mkdtemp()) / "wake_e2e.py")
        seen: list[dict] = []

        async def handler(request):
            seen.append({"path": request.path, "key": request.headers.get("Idempotency-Key")})
            return web.json_response({"ok": True})

        orig_post = aiohttp.ClientSession.post

        async def scenario():
            app = web.Application()
            app.router.add_post("/v1/chat/completions", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                self.assertEqual(cp.apply_wake_patch(mod, aiohttp.ClientSession), "APPLIED")
                adapter = types.SimpleNamespace(_port=port)
                await mod.wake(adapter, "[ASYNC DELEGATION deleg_1 COMPLETE]", "sess-1")
                await mod.wake(adapter, "[ASYNC DELEGATION deleg_1 COMPLETE]", "sess-1")
                # an ordinary POST from the same process, outside any wake
                async with aiohttp.ClientSession() as http:
                    async with http.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                                         json={}) as r:
                        await r.read()
            finally:
                await runner.cleanup()

        try:
            asyncio.run(scenario())
        finally:
            aiohttp.ClientSession.post = orig_post

        want = cp.wake_idempotency_key("sess-1", "[ASYNC DELEGATION deleg_1 COMPLETE]")
        self.assertEqual([s["key"] for s in seen], [want, want, None])


if __name__ == "__main__":
    unittest.main()
