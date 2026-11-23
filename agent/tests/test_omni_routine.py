"""omni_routine: the one client the routines use to publish into Omni. Offline: the transport is fake."""
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import omni_routine  # noqa: E402
from omni_routine import RoutinePublishError, local_day, publish_routine, routine_title  # noqa: E402

TOKEN = "test-service-secret-0123456789"
ENV = {"OMNI_SERVICE_TOKEN": TOKEN, "OMNI_GATEWAY_URL": "http://127.0.0.1:8787"}
RECEIPT = json.dumps({"threadId": "omni-routine-morning-2026-10-17", "postId": "morning-2026-10-17-1",
                      "replayed": False, "approvalId": None}).encode()


class Transport:
    """Answers in order (the last one repeats); an exception instance is raised."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, data, headers):
        self.calls.append((url, json.loads(data.decode("utf-8")), headers))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class PublishRoutineTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.sleeps = []

    def tearDown(self):
        self.env.stop()

    def publish(self, transport, **kw):
        args = dict(routine="morning", day="2026-10-17", title="Morning briefing · Sat 17 Oct",
                    post_id="morning-2026-10-17-1", body="Good morning!", push=True)
        args.update(kw)
        return publish_routine(transport=transport, sleep=self.sleeps.append, **args)

    def test_request_shape_and_receipt(self):
        t = Transport((201, RECEIPT))
        res = self.publish(t, approval={"kind": "tweet", "payload": {"text": "Hi"}, "summary": "Hi"})
        url, body, headers = t.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8787/api/omni/hooks/routine")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, {"routine": "morning", "day": "2026-10-17", "title": "Morning briefing · Sat 17 Oct",
                                "postId": "morning-2026-10-17-1", "body": "Good morning!", "push": True,
                                "approval": {"kind": "tweet", "payload": {"text": "Hi"}, "summary": "Hi"}})
        self.assertEqual(res, {"ok": True, "thread_id": "omni-routine-morning-2026-10-17",
                               "post_id": "morning-2026-10-17-1", "replayed": False, "approval_id": None})

    def test_no_approval_key_without_an_approval(self):
        t = Transport((201, RECEIPT))
        self.publish(t)
        self.assertNotIn("approval", t.calls[0][1])

    def test_a_replay_is_a_success(self):
        t = Transport((200, json.dumps({"threadId": "x", "postId": "p", "replayed": True, "approvalId": None}).encode()))
        self.assertTrue(self.publish(t)["replayed"])

    def test_retries_5xx_and_network_errors_with_backoff(self):
        t = Transport((503, b"{}"), ConnectionRefusedError(), (201, RECEIPT))
        self.assertTrue(self.publish(t)["ok"])
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(self.sleeps, [2.0, 5.0])

    def test_gives_up_after_three_attempts(self):
        t = Transport((500, b"{}"))
        with self.assertRaises(RoutinePublishError):
            self.publish(t)
        self.assertEqual(len(t.calls), 3)

    def test_never_retries_a_4xx(self):
        t = Transport((400, json.dumps({"error": "invalid_body", "detail": "body: 1–20000 characters"}).encode()))
        with self.assertRaises(RoutinePublishError) as caught:
            self.publish(t)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.sleeps, [])

    def test_a_long_body_is_cut_not_refused(self):
        t = Transport((201, RECEIPT))
        self.publish(t, body="x" * 25000)
        sent = t.calls[0][1]["body"]
        self.assertLessEqual(len(sent), 20000)
        self.assertTrue(sent.endswith("…(truncated — full note in Parachute)"))

    def test_the_bound_is_in_utf16_units_like_the_server(self):
        """The server checks JS `body.length` (UTF-16 units); astral emoji count twice."""
        utf16 = lambda s: len(s.encode("utf-16-le")) // 2  # noqa: E731
        for body in ("🔭" * 15000, "🔭 Morning intelligence ↓\n" + "a🔁" * 9000, "x" * 19999 + "🔭"):
            t = Transport((201, RECEIPT))
            self.publish(t, body=body)
            sent = t.calls[0][1]["body"]
            self.assertLessEqual(utf16(sent), 20000)
            self.assertTrue(sent.endswith("…(truncated — full note in Parachute)"))
            self.assertTrue(body.startswith(sent[:-len("\n\n…(truncated — full note in Parachute)")]))
        t = Transport((201, RECEIPT))
        self.publish(t, body="🔭" * 10000)
        self.assertEqual(t.calls[0][1]["body"], "🔭" * 10000, "exactly at the bound is kept whole")

    def test_unconfigured_gateway_is_an_error(self):
        with patch.dict(os.environ, {"OMNI_SERVICE_TOKEN": ""}), \
             patch.object(omni_routine.omni_propose, "_hermes_env_value", return_value=None):
            with self.assertRaises(RoutinePublishError):
                self.publish(Transport((201, RECEIPT)))

    def test_the_token_never_reaches_an_error(self):
        t = Transport(OSError("boom " + TOKEN))
        with self.assertRaises(RoutinePublishError) as caught:
            self.publish(t)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_unknown_routine_is_refused_before_any_request(self):
        t = Transport((201, RECEIPT))
        with self.assertRaises(RoutinePublishError):
            self.publish(t, routine="nightly")
        self.assertEqual(t.calls, [])


class TitleTests(unittest.TestCase):
    def test_title_and_local_day(self):
        self.assertEqual(routine_title("Morning briefing", "2026-10-17"), "Morning briefing · Sat 17 Oct")
        self.assertEqual(routine_title("Tweet draft", "2026-10-18"), "Tweet draft · Sun 18 Oct")
        self.assertEqual(local_day(datetime(2026, 10, 18, 5, 0, tzinfo=timezone.utc)), "2026-10-17")


if __name__ == "__main__":
    unittest.main()
