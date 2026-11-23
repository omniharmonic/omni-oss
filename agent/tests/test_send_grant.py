"""One door (2026-10-15): every send script refuses without a one-time Prism grant.

Offline: no SMTP, no X API, no Matrix, no Prism. The "verifier" is a fake transport
that answers the way Prism's /send-grant/verify does (and checks the digest the
script computed against the approved draft, exactly as Prism does).
"""
import argparse
import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import send_grant  # noqa: E402

TOKEN = "A" * 43
APR = "apr_0123456789abcdef"

# Shared with apps/server/test/omni-send-grant.test.ts in the Prism repo.
GOLDEN = {
    "tweet": "aebf2adad1845cf81fd71d0f57214fefaa3a88e5c0ccc26751b9c53f0fcaaa15",
    "email": "2b75e74fe15277efa1f3c3cc3f5ad84c5447501fe4e24b24a7967584a54e81a6",
    "reply": "d109e6fda69cc1f0c862d2c752f7f1eb760a1ff87edb0c24f8bd1aeeaf81f62f",
    "message": "3e57c88bfe703235f34dd866219ea7b29c3d983e7f6d4ec68bdb2e934e0c5664",
}


class FakePrism:
    """A verifier that knows ONE live grant for ONE approved digest, single use."""

    def __init__(self, approved_digest, token=TOKEN, approval=APR):
        self.digest, self.token, self.approval = approved_digest, token, approval
        self.calls = []

    def __call__(self, url, data):
        body = json.loads(data.decode("utf-8"))
        self.calls.append((url, body))
        if not url.endswith("/api/omni/approvals/%s/send-grant/verify" % self.approval):
            return 403, b'{"ok":false,"error":"wrong_approval"}'
        if self.token is None or body.get("token") != self.token:
            return 403, b'{"ok":false,"error":"invalid_grant"}'
        self.token = None  # consumed
        if body.get("digest") != self.digest:
            return 403, b'{"ok":false,"error":"digest_mismatch"}'
        return 200, json.dumps({"ok": True, "approvalId": self.approval}).encode()


def grant_fd(token=TOKEN):
    """An inherited-pipe descriptor carrying `token`, the way Prism passes it (fd 3)."""
    r, w = os.pipe()
    os.write(w, token.encode("ascii"))
    os.close(w)
    return str(r)


def grant_env(token=TOKEN, approval=APR):
    return {send_grant.FD_ENV: grant_fd(token), send_grant.APPROVAL_ENV: approval}


class DigestTest(unittest.TestCase):
    def test_golden_vectors_match_prism(self):
        self.assertEqual(send_grant.tweet_digest("hello"), GOLDEN["tweet"])
        self.assertEqual(send_grant.email_digest(["kevin@example.org"], None, "Grüße", None, None, None,
                                                 "Hi\r\nthere ✓\n"), GOLDEN["email"])
        self.assertEqual(send_grant.email_digest(["a@b.co"], ["c@d.co"], None, "vault/messages/email/n1",
                                                 None, None, "ok"), GOLDEN["reply"])
        self.assertEqual(send_grant.message_digest("!r:hs", "ab"), GOLDEN["message"])

    def test_every_field_is_bound(self):
        base = send_grant.email_digest(["a@b.co"], [], "S", None, None, None, "body")
        for other in (
            send_grant.email_digest(["x@b.co"], [], "S", None, None, None, "body"),
            send_grant.email_digest(["a@b.co"], ["c@d.co"], "S", None, None, None, "body"),
            send_grant.email_digest(["a@b.co"], [], "S2", None, None, None, "body"),
            send_grant.email_digest(["a@b.co"], [], "S", None, "<parent@x>", None, "body"),
            send_grant.email_digest(["a@b.co"], [], "S", None, None, None, "body "),
        ):
            self.assertNotEqual(base, other)


class RequireGrantTest(unittest.TestCase):
    def test_no_grant_refuses_without_calling_prism(self):
        fake = FakePrism("0" * 64)
        with self.assertRaises(send_grant.GrantRefused) as cm:
            send_grant.require_grant("0" * 64, environ={}, transport=fake)
        self.assertIn("Sends must be approved in Omni", str(cm.exception))
        self.assertEqual(fake.calls, [])

    def test_valid_grant_once_then_refused(self):
        d = send_grant.tweet_digest("hi")
        fake = FakePrism(d)
        env = grant_env()
        self.assertEqual(send_grant.require_grant(d, environ=env, transport=fake), APR)
        self.assertNotIn(send_grant.FD_ENV, env, "the descriptor is consumed")
        with self.assertRaises(send_grant.GrantRefused):
            send_grant.require_grant(d, environ=grant_env(), transport=fake)

    def test_other_content_or_approval_is_refused(self):
        fake = FakePrism(send_grant.tweet_digest("approved"))
        with self.assertRaises(send_grant.GrantRefused) as cm:
            send_grant.require_grant(send_grant.tweet_digest("something else"), environ=grant_env(), transport=fake)
        self.assertIn("digest_mismatch", str(cm.exception))
        env = grant_env(approval="apr_other")
        with self.assertRaises(send_grant.GrantRefused):
            send_grant.require_grant(send_grant.tweet_digest("approved"), environ=env, transport=FakePrism(send_grant.tweet_digest("approved")))

    def test_unreachable_or_odd_answers_refuse(self):
        def down(url, data):
            raise ConnectionRefusedError()
        d = send_grant.tweet_digest("x")
        for transport in (down, lambda u, b: (200, b'{"ok":true}'), lambda u, b: (200, b"garbage"),
                          lambda u, b: (500, b"")):
            with self.assertRaises(send_grant.GrantRefused):
                send_grant.require_grant(d, environ=grant_env(), transport=transport)
        for env in (grant_env(token="short"), {send_grant.FD_ENV: grant_fd()},
                    grant_env(approval="../x"), {send_grant.FD_ENV: "0", send_grant.APPROVAL_ENV: APR},
                    {send_grant.FD_ENV: "not-a-fd", send_grant.APPROVAL_ENV: APR}):
            with self.assertRaises(send_grant.GrantRefused):
                send_grant.require_grant(d, environ=dict(env), transport=FakePrism(d))

    def test_verifier_location_is_not_taken_from_the_environment(self):
        with mock.patch.dict(os.environ, {"PRISM_GRANT_URL": "http://attacker.example:80"}), \
                mock.patch.object(send_grant, "_dotenv_value", return_value=None):
            self.assertEqual(send_grant.verifier_base(), "http://127.0.0.1:8787")
        for bad in ("https://127.0.0.1:8787", "http://10.0.0.5:8787", "http://user:pw@127.0.0.1:1", "http://127.0.0.1:8787/x",
                    "http://localhost:8787", "http://[::1]:8787"):
            with mock.patch.object(send_grant, "_dotenv_value", return_value=bad):
                self.assertEqual(send_grant.verifier_base(), "", bad)
                with self.assertRaises(send_grant.GrantRefused):
                    send_grant.require_grant("0" * 64, environ=grant_env(), transport=FakePrism("0" * 64))


class ProtonSendTest(unittest.TestCase):
    """proton_send.py: every non-dry-run path ends at the grant, before any socket."""

    def setUp(self):
        import proton_send
        self.ps = proton_send
        self.smtp = mock.patch.object(proton_send, "smtp_connect", side_effect=AssertionError("SMTP must not be reached"))
        self.smtp.start()
        self.env = mock.patch.dict(os.environ, {"PROTON_SEND_ALLOW_EXTERNAL": "1"})
        self.env.start()

    def tearDown(self):
        self.smtp.stop()
        self.env.stop()

    def args(self, **kw):
        base = dict(to=["kevin@example.org"], cc=None, subject="Friday", body=None, body_file="-",
                    reply_to_note=None, in_reply_to=None, references=None, approved=True,
                    dry_run=False, allow_external=True, json=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def run_cli(self, args, body, transport=None, env=None):
        stdin = SimpleNamespace(buffer=io.BytesIO(body.encode("utf-8")))
        real = send_grant.require_grant

        def require(digest):
            return real(digest, environ=dict(env or {}), transport=transport)
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(send_grant, "require_grant", side_effect=require), \
                mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO):
            return self.ps.cmd_send(args)

    def test_approved_flag_and_env_switch_are_not_enough(self):
        with self.assertRaises(self.ps.SendError) as cm:
            self.run_cli(self.args(), "See you Friday.\n")
        self.assertTrue(str(cm.exception).startswith("Sends must be approved in Omni"))

    def test_library_call_without_grant_is_refused(self):
        with self.assertRaises(self.ps.ApprovalRequired):
            self.ps.send_email("kevin@example.org", "s", "plain", approved=True, allow_external=True)

    def test_grant_for_the_exact_draft_reaches_smtp_once(self):
        body = "See you Friday.\r\n"
        approved = send_grant.email_digest(["kevin@example.org"], None, "Friday", None, None, None, body)
        fake = FakePrism(approved)
        sent = []

        class SMTP:
            def send_message(self, msg, from_addr, to_addrs):
                sent.append((to_addrs, msg["Subject"], msg.get_content()))

            def quit(self):
                pass
        self.smtp.stop()
        with mock.patch.object(self.ps, "smtp_connect", return_value=(SMTP(), "f" * 64)):
            self.assertEqual(self.run_cli(self.args(), body, fake, grant_env()), 0)
            # The same grant cannot send again, and tampered recipients never verify.
            with self.assertRaises(self.ps.SendError):
                self.run_cli(self.args(), body, fake, grant_env())
            with self.assertRaises(self.ps.SendError):
                self.run_cli(self.args(to=["mallory@example.org"]), body, FakePrism(approved), grant_env())
            with self.assertRaises(self.ps.SendError):
                self.run_cli(self.args(), body + "PS: wire money", FakePrism(approved), grant_env())
            with self.assertRaises(self.ps.SendError):
                self.run_cli(self.args(in_reply_to="<x@y>"), body, FakePrism(approved), grant_env())
        self.smtp.start()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ["kevin@example.org"])
        self.assertEqual(sent[0][2], "See you Friday.\n")

    def test_dry_run_needs_no_grant(self):
        if True:
            self.assertEqual(self.run_cli(self.args(dry_run=True, approved=False), "Plain.\n"), 0)


class TweetSendersTest(unittest.TestCase):
    def test_executor_refuses_without_grant_and_never_calls_the_provider(self):
        import omni_tweet_send as ots

        def no_call(_):
            raise AssertionError("must not post")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(send_grant.FD_ENV, None)
            out = ots.send({"text": "hello"}, no_call, ("k",) * 4)
        self.assertEqual(out, {"status": "failed", "error": "send_grant_refused"})

    def test_executor_posts_exactly_the_granted_text(self):
        import omni_tweet_send as ots
        seen = []

        class R:
            status_code = 201

            def json(self):
                return {"data": {"id": "7"}}
        out = ots.send({"text": "  exact  "}, lambda b: (seen.append(b) or R()), ("k",) * 4,
                       grant=lambda d: send_grant.require_grant(d, environ=grant_env(), transport=FakePrism(send_grant.tweet_digest("  exact  "))))
        self.assertEqual(out, {"status": "sent", "postId": "7"})
        self.assertEqual(seen, [{"text": "  exact  "}])
        bad = ots.send({"text": "other"}, lambda b: self.fail("must not post"), ("k",) * 4,
                       grant=lambda d: send_grant.require_grant(d, environ=grant_env(), transport=FakePrism(send_grant.tweet_digest("approved"))))
        self.assertEqual(bad["error"], "send_grant_refused")

    def test_legacy_twitter_post_cannot_post(self):
        import twitter_post
        with mock.patch.object(twitter_post, "TWITTER_API_KEY", "k"), mock.patch.object(twitter_post, "TWITTER_API_SECRET", "k"), \
                mock.patch.object(twitter_post, "TWITTER_ACCESS_TOKEN", "k"), mock.patch.object(twitter_post, "TWITTER_ACCESS_TOKEN_SECRET", "k"), \
                mock.patch.object(twitter_post, "POST_LOG", Path(os.devnull + ".nonexistent")), \
                mock.patch.object(twitter_post.requests, "post", side_effect=AssertionError("must not post")), \
                mock.patch.dict(os.environ, {}, clear=False), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            os.environ.pop(send_grant.FD_ENV, None)
            with self.assertRaises(SystemExit) as cm:
                twitter_post.post_tweet("A perfectly ordinary tweet about bioregions.")
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("Sends must be approved in Omni", err.getvalue())


class MatrixSendTest(unittest.TestCase):
    def test_send_and_send_to_refuse_without_grant(self):
        import matrix_reader
        with mock.patch.object(matrix_reader, "_resolve_room", return_value="!room:hs"), \
                mock.patch.object(matrix_reader, "_resolve_room_strict", return_value=("!room:hs", None)), \
                mock.patch.object(matrix_reader, "_get_room_info", return_value={"platform": "Telegram", "name": "X", "member_count": 2}), \
                mock.patch.object(matrix_reader, "_api_put", side_effect=AssertionError("must not send")), \
                mock.patch.dict(os.environ, {}, clear=False), mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            os.environ.pop(send_grant.FD_ENV, None)
            for fn, ns in ((matrix_reader.cmd_send, argparse.Namespace(room="!room:hs", message="hi", approved=True)),
                           (matrix_reader.cmd_send_to, argparse.Namespace(name="X", platform="Telegram", message="hi", approved=True))):
                with self.assertRaises(SystemExit) as cm:
                    fn(ns)
                self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("Sends must be approved in Omni", err.getvalue())

    def test_granted_message_sends_exactly_once(self):
        import matrix_reader
        puts = []
        approved = send_grant.message_digest("!room:hs", "on my way")
        fake = FakePrism(approved)
        real = send_grant.require_grant
        with mock.patch.object(matrix_reader, "_resolve_room", return_value="!room:hs"), \
                mock.patch.object(matrix_reader, "_get_room_info", return_value={"platform": "Telegram", "name": "X", "member_count": 2}), \
                mock.patch.object(matrix_reader, "_api_put", side_effect=lambda p, b: (puts.append(b) or {"event_id": "$e"})), \
                mock.patch.object(send_grant, "require_grant", side_effect=lambda d: real(d, environ=grant_env(), transport=fake)), \
                mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO):
            matrix_reader.cmd_send(argparse.Namespace(room="!room:hs", message="on my way", approved=True))
            with self.assertRaises(SystemExit):
                matrix_reader.cmd_send(argparse.Namespace(room="!room:hs", message="on my way", approved=True))
        self.assertEqual(puts, [{"msgtype": "m.text", "body": "on my way"}])


class SelfOnlyTelegramTest(unittest.TestCase):
    def test_send_telegram_is_pinned_to_the_owner_chat(self):
        import importlib
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "42"}), mock.patch("sys.stderr", new_callable=io.StringIO):
            sys.modules.pop("send_telegram", None)
            st = importlib.import_module("send_telegram")
        self.assertEqual(st.CHAT_ID, os.environ["OMNI_OWNER_TELEGRAM_CHAT"])


class ProposeClientTest(unittest.TestCase):
    def test_propose_posts_exact_payload_and_sends_nothing(self):
        import omni_propose
        calls = []

        def transport(url, data, headers):
            calls.append((url, json.loads(data.decode("utf-8")), headers))
            return 201, b'{"id":"apr_1","digest":"x","status":"pending","expiresAt":"2026-10-16T00:00:00Z"}'
        payload = {"to": ["dana@example.org"], "subject": "Friday", "body": "Plain\r\ntext"}
        with mock.patch.dict(os.environ, {"OMNI_SERVICE_TOKEN": "t" * 32, "OMNI_GATEWAY_URL": "http://127.0.0.1:8787"}):
            res = omni_propose.propose("email", payload, summary="Email\nDana", transport=transport)
        self.assertEqual(res["approval_id"], "apr_1")
        self.assertIs(res["sent"], False)
        url, body, headers = calls[0]
        self.assertEqual(url, "http://127.0.0.1:8787/api/omni/hooks/propose")
        self.assertEqual(body, {"kind": "email", "payload": payload, "summary": "Email Dana"})
        self.assertEqual(headers["Authorization"], "Bearer " + "t" * 32)

    def test_unconfigured_or_remote_gateway_refuses(self):
        import omni_propose
        with mock.patch.object(omni_propose, "_hermes_env_value", return_value=None):
            with mock.patch.dict(os.environ, {"OMNI_SERVICE_TOKEN": "", "OMNI_GATEWAY_URL": ""}):
                with self.assertRaises(omni_propose.ProposeError):
                    omni_propose.propose("tweet", {"text": "x"}, transport=lambda *a: self.fail("no call"))
            with mock.patch.dict(os.environ, {"OMNI_SERVICE_TOKEN": "t" * 32, "OMNI_GATEWAY_URL": "https://example.com"}):
                with self.assertRaises(omni_propose.ProposeError):
                    omni_propose.propose("tweet", {"text": "x"}, transport=lambda *a: self.fail("no call"))
        with mock.patch.dict(os.environ, {"OMNI_SERVICE_TOKEN": "t" * 32, "OMNI_GATEWAY_URL": "http://127.0.0.1:8787"}):
            with self.assertRaises(omni_propose.ProposeError):
                omni_propose.propose("email", {"to": ["a@b.co"]}, transport=lambda *a: self.fail("no call"))


class HardenedClaudeArgvTest(unittest.TestCase):
    def test_no_permission_bypass_and_no_general_bash(self):
        import agent_claude
        argv = agent_claude.claude_argv("claude", "-hi", resume="s1")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertNotIn("Bash", allowed)
        self.assertFalse([t for t in allowed if "proton_send" in t or "twitter_post" in t or "gmail send" in t])
        denied = argv[argv.index("--disallowedTools") + 1]
        self.assertIn("Bash(python3 scripts/proton_send.py:*)", denied)
        self.assertIn("Read(./.env)", denied)
        self.assertEqual(argv[-2:], ["--", "-hi"])
        with self.assertRaises(ValueError):
            agent_claude.allowed_tools(["Bash"])


if __name__ == "__main__":
    unittest.main()
