"""One-door review round 1 (S7, S6, N1, N3, B4, S5), agent side. Offline.

* The argv Prism builds (tests/fixtures/send-grant-vectors.json — an identical copy of
  the Prism repo's apps/server/test/fixtures/send-grant-vectors.json) parsed by the REAL
  proton_send.py parser gives the same digest Prism bound the grant to.
* The REAL proton_send.py, run as a subprocess against a fake loopback verifier: refuses
  with no grant (verifier never called), passes the gate with the right grant on fd 3
  (stops at the unreachable SMTP port — nothing sent), refuses tampered recipients.
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import send_grant  # noqa: E402

VECTORS = json.loads((ROOT / "tests/fixtures/send-grant-vectors.json").read_text())
TOKEN = "Q" * 43
APR = "apr_review0001"


class ArgvVectorsTest(unittest.TestCase):
    def test_prism_argv_parsed_by_the_real_parser_gives_prisms_digest(self):
        import proton_send
        parser = proton_send.build_parser()
        self.assertGreaterEqual(len(VECTORS["email"]), 5)
        for v in VECTORS["email"]:
            args = parser.parse_args(v["argv"])
            self.assertTrue(args.approved and args.body_file == "-", v["argv"])
            d = send_grant.email_digest(args.to, args.cc, args.subject, args.reply_to_note,
                                        args.in_reply_to, args.references, v["body"])
            self.assertEqual(d, v["digest"], v["argv"])
            if v["kind"] == "email-reply":
                # S6: everything the reply puts on the wire arrives on argv, not from the note.
                self.assertTrue(args.subject and args.in_reply_to and args.reply_to_note)
        for t in VECTORS["tweet"]:
            self.assertEqual(send_grant.tweet_digest(t["text"]), t["digest"])
        for m in VECTORS["message"]:
            self.assertEqual(send_grant.message_digest(m["room"], m["body"]), m["digest"])


class _Verifier:
    """A loopback Prism stand-in: one live grant, one approved digest, single use."""

    def __init__(self, digest):
        self.digest, self.token, self.calls = digest, TOKEN, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.calls.append((self.path, body))
                ok = (self.path == "/api/omni/approvals/%s/send-grant/verify" % APR and outer.token
                      and body.get("token") == outer.token and body.get("digest") == outer.digest)
                if body.get("token") == outer.token:
                    outer.token = None
                data = json.dumps({"ok": True, "approvalId": APR} if ok else {"ok": False, "error": "refused"}).encode()
                self.send_response(200 if ok else 403)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RealProtonSendTest(unittest.TestCase):
    """Runs scripts/proton_send.py itself, from a copy whose .env points at the fake verifier."""

    def setUp(self):
        self.vec = VECTORS["email"][0]  # to kevin@example.org, subject Friday
        self.verifier = _Verifier(self.vec["digest"])
        self.tmp = tempfile.mkdtemp(prefix="onedoor-")
        os.makedirs(os.path.join(self.tmp, "scripts"))
        for f in ("proton_send.py", "send_grant.py"):
            shutil.copy(ROOT / "scripts" / f, os.path.join(self.tmp, "scripts", f))
        Path(self.tmp, ".env").write_text("PRISM_GRANT_URL=http://127.0.0.1:%d\n" % self.verifier.port)
        # The SMTP step imports proton_mail for the Keychain; reaching the Keychain would
        # mean a socket was open — the stub makes that a loud failure instead.
        Path(self.tmp, "scripts/proton_mail.py").write_text(
            "def keychain_read(*a):\n    raise SystemExit('KEYCHAIN REACHED')\n"
            "def load_state():\n    return {}\ndef save_state(s):\n    pass\n")

    def tearDown(self):
        self.verifier.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_send(self, argv, grant=None):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "PROTON_SEND_ALLOW_EXTERNAL": "1",
               "PROTON_BRIDGE_SMTP_PORT": str(_closed_port())}
        fds = ()
        r = w = None
        if grant is not None:
            r, w = os.pipe()
            os.write(w, grant.encode())
            os.close(w)
            env["PRISM_SEND_GRANT_FD"] = str(r)
            env["PRISM_SEND_GRANT_APPROVAL"] = APR
            fds = (r,)
        try:
            return subprocess.run([sys.executable, os.path.join(self.tmp, "scripts/proton_send.py")] + argv,
                                  input=self.vec["body"].encode(), capture_output=True, env=env, pass_fds=fds, timeout=60)
        finally:
            if r is not None:
                os.close(r)

    def test_no_grant_refuses_and_never_asks_prism(self):
        p = self.run_send(self.vec["argv"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"ERROR: Sends must be approved in Omni", p.stderr)
        self.assertEqual(self.verifier.calls, [])

    def test_valid_grant_passes_the_gate_once(self):
        p = self.run_send(self.vec["argv"], TOKEN)
        # The gate passed (one verify with Prism's exact digest); SMTP is unreachable, so nothing left.
        self.assertEqual(len(self.verifier.calls), 1)
        self.assertEqual(self.verifier.calls[0][1]["digest"], self.vec["digest"])
        self.assertIn(b"cannot reach Proton Bridge SMTP", p.stderr)
        again = self.run_send(self.vec["argv"], TOKEN)
        self.assertIn(b"Sends must be approved in Omni", again.stderr, "single use")

    def test_tampered_recipient_is_refused(self):
        argv = list(self.vec["argv"])
        argv[argv.index("kevin@example.org")] = "mallory@example.org"
        p = self.run_send(argv, TOKEN)
        self.assertIn(b"Sends must be approved in Omni", p.stderr)
        self.assertNotIn(b"cannot reach Proton Bridge SMTP", p.stderr)


class GrantTransportTest(unittest.TestCase):
    def test_grant_is_never_read_from_the_environment(self):
        # N1: a token in the environment is ignored; only the inherited descriptor counts.
        env = {"PRISM_SEND_GRANT": TOKEN, send_grant.APPROVAL_ENV: APR}
        with self.assertRaises(send_grant.GrantRefused):
            send_grant.require_grant("0" * 64, environ=env, transport=lambda u, b: (200, json.dumps({"ok": True, "approvalId": APR}).encode()))

    def test_localhost_is_not_a_verifier(self):
        # S5: 127.0.0.1 only.
        for bad in ("http://localhost:8787", "http://[::1]:8787"):
            with mock.patch.object(send_grant, "_dotenv_value", return_value=bad):
                self.assertEqual(send_grant.verifier_base(), "")


class ProposeBodyFileTest(unittest.TestCase):
    def test_body_file_refuses_credential_paths(self):
        import omni_propose
        home = Path.home()
        for p in (home / ".hermes/.env", ROOT / ".env", home / ".ssh/id_ed25519", home / "Library/Messages/chat.db",
                  Path("/x/apps/server/prism-server.db"), ROOT / "state/proton-mail-state.json"):
            with self.assertRaises(omni_propose.ProposeError, msg=str(p)):
                omni_propose._read(str(p))
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("plain draft")
        try:
            self.assertEqual(omni_propose._read(f.name), "plain draft")
        finally:
            os.unlink(f.name)


class BuzzPermissionModeTest(unittest.TestCase):
    def test_omni_buzz_agent_runs_dont_ask(self):
        import buzz_agents
        env = buzz_agents.AGENTS["omni"]["extra_env"]
        self.assertEqual(env.get("BUZZ_ACP_PERMISSION_MODE"), "dont-ask")
        self.assertTrue(env.get("CLAUDE_CODE_EXECUTABLE", "").endswith("buzz_omni_claude"))




class Round2MailTest(unittest.TestCase):
    """Review round 2: sign-in mail never reaches search output or the vault; explicit
    reply threading takes nothing from the note."""

    def test_signin_mail_is_recognised(self):
        import proton_mail
        self.assertTrue(proton_mail.is_prism_signin_mail({"from": "x@y.z", "subject": "Fwd", "body": "go https://p.example/auth/callback?token=abc"}))
        self.assertTrue(proton_mail.is_prism_signin_mail({"from": "x@y.z", "subject": "", "body": "https://p/auth/device/approve?code=1"}))
        self.assertFalse(proton_mail.is_prism_signin_mail({"from": "dana@example.org", "subject": "Sign in sheet for Friday", "body": "lunch"}))

    def test_search_hides_signin_mail(self):
        import argparse as ap
        import io
        import proton_mail
        msgs = {1: {"from": "Prism <login@p.example>", "subject": "Your Prism sign-in link", "body": "https://p/auth/callback?token=SECRET"},
                2: {"from": "dana@example.org", "subject": "Lunch", "body": "Friday?"}}
        from datetime import datetime, timezone

        class Conn:
            def select(self, *a, **k):
                return "OK", None

            def logout(self):
                pass
        with mock.patch.object(proton_mail, "connect", return_value=(Conn(), None)), mock.patch.object(proton_mail, "login"), \
                mock.patch.object(proton_mail, "_search_uids", return_value=[1, 2]), \
                mock.patch.object(proton_mail, "_fetch_message", side_effect=lambda c, uid: (b"", "")), \
                mock.patch.object(proton_mail, "parse_message", side_effect=lambda raw, flags, mb, uid: {**msgs[uid], "date": datetime.now(timezone.utc), "isUnread": False}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            proton_mail.cmd_search(ap.Namespace(mailbox="INBOX", since_days=None, unread=False, query=None, max=10, json=True))
        rows = json.loads(out.getvalue())
        self.assertEqual([r["subject"] for r in rows], ["Lunch"])
        self.assertNotIn("SECRET", out.getvalue())

    def test_explicit_reply_threading_takes_no_references_from_the_note(self):
        import argparse as ap
        import io
        import proton_send
        seen = {}
        ctx = {"to": ["dana@example.org"], "subject": "Re: Note subject", "in_reply_to": "<note-mid@x>", "references": ["<note-root@x>"], "note_path": "n1"}
        args = ap.Namespace(to=["dana@example.org"], cc=None, subject="Re: Plans", body="ok", body_file=None, reply_to_note="n1",
                            in_reply_to="<m1@x>", references=None, approved=False, dry_run=True, allow_external=True, json=False)
        with mock.patch.object(proton_send, "reply_context_from_note", return_value=ctx), \
                mock.patch.object(proton_send, "send_email", side_effect=lambda **kw: (seen.update(kw) or {"raw": "", "to": [], "cc": [], "subject": ""})), \
                mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch("sys.stderr", new_callable=io.StringIO):
            proton_send.cmd_send(args)
        self.assertEqual(seen["subject"], "Re: Plans")
        self.assertEqual(seen["in_reply_to"], "<m1@x>")
        self.assertIsNone(seen["references"], "nothing from the note at send time")


if __name__ == "__main__":
    unittest.main()
