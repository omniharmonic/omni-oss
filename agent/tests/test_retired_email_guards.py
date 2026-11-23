import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "meeting_followup.py"


def load_meeting_followup():
    spec = importlib.util.spec_from_file_location("meeting_followup_under_test", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class RetiredEmailOutboundGuardsTest(unittest.TestCase):
    def test_followup_is_proposed_never_sent_and_never_to_retired_address(self):
        # One door (2026-10-15): the follow-up is PROPOSED in Omni (Alex sends it
        # as himself on approval), so there is no cc and no gog call at all.
        module = load_meeting_followup()
        captured = {}
        sys.path.insert(0, str(ROOT / "scripts"))
        import omni_propose

        def fake_propose(kind, payload, **kw):
            captured["kind"], captured["payload"] = kind, payload
            return {"ok": True, "approval_id": "apr_x", "sent": False}

        def no_run(*a, **k):
            raise AssertionError("must not run gog")

        with patch.object(omni_propose, "propose", side_effect=fake_propose), \
                patch.object(module.subprocess, "run", side_effect=no_run):
            ok = module.send_email(
                recipients=["recipient@example.com"],
                subject="Follow-up",
                body="Hello",
                csv_content=None,
                csv_filename=None,
                dry_run=False,
            )

        self.assertTrue(ok)
        self.assertEqual(captured["kind"], "email")
        self.assertEqual(captured["payload"]["to"], ["recipient@example.com"])
        self.assertNotIn("owner@work.example.com", repr(captured["payload"]))

if __name__ == "__main__":
    unittest.main()
